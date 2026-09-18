"""Synchronize visible forum threads; publish only after every batch succeeds."""
import argparse
import hashlib
import json
import logging
import os
import sys
from itertools import islice
from pathlib import Path

# Preserve the documented "python scripts/sync_data.py" entry point.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import faiss
import mysql.connector
import numpy as np
from filelock import FileLock, Timeout
from openai import OpenAI

from app.config import DEFAULT_EMBEDDING_DIMENSION, DEFAULT_EMBEDDING_MODEL, MAX_TEXT_LENGTH, Settings
from app.index_store import SNAPSHOT_NAME, Snapshot, load_snapshot, save_snapshot
from app.lexical import tokenize

logger = logging.getLogger(__name__)
BATCH_SIZE = 10


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
        connection_timeout=15,
    )


def iter_threads(connection):
    cursor = connection.cursor()
    try:
        cursor.execute(
            "SELECT t.id, t.title, c.markdown "
            "FROM threads AS t JOIN contents AS c ON t.id = c.contentable_id "
            "WHERE t.deleted_at IS NULL AND t.banned_at IS NULL "
            "AND c.contentable_type = %s ORDER BY t.id",
            ("App\\Thread",),
        )
        while rows := cursor.fetchmany(100):
            yield from rows
    finally:
        interrupted = sys.exc_info()[0] is not None
        try:
            cursor.close()
        except mysql.connector.Error:
            if not interrupted:
                raise
            # An unbuffered result may be unread after an embedding failure.
            # The caller closes the connection; preserve the original failure.
            logger.debug("Interrupted cursor discarded; its connection will be closed.")


def build_snapshot(
    rows, client, previous: Snapshot | None = None, *,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION,
) -> Snapshot:
    """Reuse unchanged vectors only when model and dimensions match."""
    if previous is not None and (
        previous.embedding_model != embedding_model or previous.index.d != embedding_dimension
    ):
        logger.info("Embedding configuration changed; regenerating all vectors.")
        previous = None
    old_positions = {
        entry["thread_id"]: (position, entry.get("source_hash"))
        for position, entry in enumerate(previous.entries)
    } if previous is not None else {}
    index = faiss.IndexFlatL2(embedding_dimension)
    entries = []
    keyword_tokens = []
    seen = set()
    iterator = iter(rows)
    batch_embeddings = True
    while batch := list(islice(iterator, BATCH_SIZE)):
        prepared = []
        for thread_id, title, markdown in batch:
            if type(thread_id) is not int or thread_id <= 0 or thread_id in seen:
                raise ValueError(f"Invalid or duplicate thread ID: {thread_id}")
            seen.add(thread_id)
            title, markdown = title or "", markdown or ""
            if not title.strip() and not markdown.strip():
                continue
            digest = hashlib.sha256(
                json.dumps([title, markdown], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            old = old_positions.get(thread_id)
            if old is not None and old[1] == digest and previous.keyword_tokens is not None:
                tokens = previous.keyword_tokens[old[0]]
            else:
                # Lexical retrieval covers the complete text, including beyond the embedding prefix.
                tokens = tokenize(f"{title}\n{markdown}")
            prepared.append((thread_id, digest, f"标题：{title}\n内容：{markdown}"[:MAX_TEXT_LENGTH], tokens))
        if not prepared:
            continue
        vectors = [None] * len(prepared)
        changed = []
        for position, (thread_id, digest, text, _) in enumerate(prepared):
            old = old_positions.get(thread_id)
            if old is not None and old[1] == digest:
                vectors[position] = previous.index.reconstruct(old[0])
            else:
                changed.append((position, text))
        if changed:
            if batch_embeddings:
                response = client.embeddings.create(
                    model=embedding_model, input=[text for _, text in changed],
                    dimensions=embedding_dimension,
                )
                items = response.data
                if len(changed) > 1 and len(items) == len(changed) and all(item.index == 0 for item in items):
                    # Some compatible endpoints label every batch item as index 0.
                    # Never assume response order: request each text separately.
                    logger.warning(
                        "Embedding endpoint returned duplicate zero indices; retrying individually "
                        "and using single-text requests for the rest of this synchronization."
                    )
                    batch_embeddings = False
                else:
                    items = sorted(items, key=lambda item: item.index)
                    if [item.index for item in items] != list(range(len(changed))):
                        raise ValueError("Embedding response count or indices do not match the requested batch.")
                    for (position, _), item in zip(changed, items):
                        vectors[position] = item.embedding
            if not batch_embeddings:
                for position, text in changed:
                    response = client.embeddings.create(
                        model=embedding_model, input=[text], dimensions=embedding_dimension,
                    )
                    if len(response.data) != 1 or response.data[0].index != 0:
                        raise ValueError("Single-text embedding response count or index is invalid.")
                    vectors[position] = response.data[0].embedding
        array = np.asarray(vectors, dtype=np.float32)
        if array.shape != (len(prepared), embedding_dimension) or not np.isfinite(array).all():
            raise ValueError("Embedding response contains invalid vectors.")
        index.add(array)
        entries.extend({"thread_id": thread_id, "source_hash": digest} for thread_id, digest, _, _ in prepared)
        keyword_tokens.extend(tokens for _, _, _, tokens in prepared)
        logger.info("Processed %s threads (%s embeddings generated in this batch).", len(entries), len(changed))
    return Snapshot(index, entries, embedding_model, keyword_tokens)


def synchronize(settings: Settings, full: bool = False):
    if not settings.dashscope_api_key:
        raise ValueError("DASHSCOPE_API_KEY must be configured.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(settings.data_dir / ".sync.lock"), timeout=0):
        previous = None
        has_snapshot = (settings.data_dir / SNAPSHOT_NAME).exists()
        has_legacy = all((settings.data_dir / name).exists() for name in ("threads.index", "id_mapping.json"))
        if not full and (has_snapshot or has_legacy):
            # Corrupt existing data must be explicitly rebuilt, never silently replaced.
            previous = load_snapshot(settings.data_dir, embedding_model=None, embedding_dimension=None)
        connection = get_db_connection()
        rows = iter_threads(connection)
        try:
            with OpenAI(
                api_key=settings.dashscope_api_key,
                base_url=settings.embedding_base_url,
                timeout=30.0, max_retries=2,
            ) as client:
                snapshot = build_snapshot(
                    rows, client, previous,
                    embedding_model=settings.embedding_model,
                    embedding_dimension=settings.embedding_dimension,
                )
        finally:
            try:
                rows.close()
            finally:
                connection.close()
        if (
            previous is not None and has_snapshot and snapshot.entries == previous.entries
            and snapshot.embedding_model == previous.embedding_model
            and snapshot.index.d == previous.index.d
            and snapshot.keyword_tokens == previous.keyword_tokens
        ):
            logger.info("No content changes; existing snapshot retained.")
            return
        save_snapshot(settings.data_dir, snapshot)
        logger.info("Published %s threads to %s.", snapshot.index.ntotal, settings.data_dir / SNAPSHOT_NAME)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="Rebuild all vectors without reusing existing data.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        synchronize(Settings.from_env(), full=args.full)
    except Timeout:
        logger.error("Another synchronization is running; no index was changed.")
        return 1
    except Exception:
        logger.exception("Synchronization failed; the previously published snapshot was not replaced.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
