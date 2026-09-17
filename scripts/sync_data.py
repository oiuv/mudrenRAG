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

from app.config import EMBEDDING_DIMENSION, EMBEDDING_MODEL, MAX_TEXT_LENGTH, Settings
from app.index_store import SNAPSHOT_NAME, Snapshot, load_snapshot, save_snapshot

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
        cursor.close()


def build_snapshot(rows, client, previous: Snapshot | None = None) -> Snapshot:
    """Reuse unchanged vectors while reflecting edits, deletions and unbans."""
    old_positions = {
        entry["thread_id"]: (position, entry.get("source_hash"))
        for position, entry in enumerate(previous.entries)
    } if previous is not None else {}
    index = faiss.IndexFlatL2(EMBEDDING_DIMENSION)
    entries = []
    seen = set()
    iterator = iter(rows)
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
            prepared.append((thread_id, digest, f"标题：{title}\n内容：{markdown}"[:MAX_TEXT_LENGTH]))
        if not prepared:
            continue
        vectors = [None] * len(prepared)
        changed = []
        for position, (thread_id, digest, text) in enumerate(prepared):
            old = old_positions.get(thread_id)
            if old is not None and old[1] == digest:
                vectors[position] = previous.index.reconstruct(old[0])
            else:
                changed.append((position, text))
        if changed:
            response = client.embeddings.create(
                model=EMBEDDING_MODEL, input=[text for _, text in changed],
                dimensions=EMBEDDING_DIMENSION,
            )
            # The provider may return items out of order. Bind by response.index.
            items = sorted(response.data, key=lambda item: item.index)
            if [item.index for item in items] != list(range(len(changed))):
                raise ValueError("Embedding response count or indices do not match the requested batch.")
            for (position, _), item in zip(changed, items):
                vectors[position] = item.embedding
        array = np.asarray(vectors, dtype=np.float32)
        if array.shape != (len(prepared), EMBEDDING_DIMENSION) or not np.isfinite(array).all():
            raise ValueError("Embedding response contains invalid vectors.")
        index.add(array)
        entries.extend({"thread_id": thread_id, "source_hash": digest} for thread_id, digest, _ in prepared)
        logger.info("Processed %s threads (%s embeddings generated in this batch).", len(entries), len(changed))
    return Snapshot(index, entries)


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
            previous = load_snapshot(settings.data_dir)
        connection = get_db_connection()
        rows = iter_threads(connection)
        try:
            with OpenAI(
                api_key=settings.dashscope_api_key,
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                timeout=30.0, max_retries=2,
            ) as client:
                snapshot = build_snapshot(rows, client, previous)
        finally:
            try:
                rows.close()
            finally:
                connection.close()
        if previous is not None and snapshot.entries == previous.entries and has_snapshot:
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
