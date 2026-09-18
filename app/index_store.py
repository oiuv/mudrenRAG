"""Atomic index snapshots, legacy import and automatic in-process reload."""
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from .config import DEFAULT_EMBEDDING_DIMENSION, DEFAULT_EMBEDDING_MODEL
from .lexical import BM25Index, TOKENIZER_VERSION

logger = logging.getLogger(__name__)
SNAPSHOT_NAME = "knowledge.npz"


@dataclass(frozen=True)
class Snapshot:
    index: Any
    entries: list[dict]
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    keyword_tokens: list[list[str]] | None = None

    @cached_property
    def keyword_index(self) -> BM25Index | None:
        return BM25Index(self.keyword_tokens) if self.keyword_tokens is not None else None


def validate_snapshot(
    snapshot: Snapshot, embedding_model: str | None = None, embedding_dimension: int | None = None,
):
    index, entries = snapshot.index, snapshot.entries
    if index.d <= 0 or index.metric_type != faiss.METRIC_L2:
        raise ValueError("Index dimension or distance metric is incompatible; run sync_data.py --full.")
    if not isinstance(snapshot.embedding_model, str) or not snapshot.embedding_model:
        raise ValueError("Index is missing its embedding model.")
    if embedding_model is not None and snapshot.embedding_model != embedding_model:
        raise ValueError(f"Index uses {snapshot.embedding_model}, configured model is {embedding_model}; run sync_data.py.")
    if embedding_dimension is not None and index.d != embedding_dimension:
        raise ValueError("Index dimension differs from EMBEDDING_DIMENSION; run sync_data.py.")
    if not isinstance(index, faiss.IndexFlatL2):
        raise ValueError("Expected an IndexFlatL2 index.")
    if not isinstance(entries, list) or index.ntotal != len(entries):
        raise ValueError("Index and mapping counts differ; run sync_data.py --full.")
    if snapshot.keyword_tokens is not None:
        if not isinstance(snapshot.keyword_tokens, list) or len(snapshot.keyword_tokens) != len(entries):
            raise ValueError("BM25 documents and vector mapping counts differ.")
        if any(
            not isinstance(document, list) or any(not isinstance(token, str) or not token for token in document)
            for document in snapshot.keyword_tokens
        ):
            raise ValueError("Invalid tokens in BM25 index.")
    ids = []
    for entry in entries:
        if not isinstance(entry, dict) or type(entry.get("thread_id")) is not int or entry["thread_id"] <= 0:
            raise ValueError("Invalid thread ID in index mapping.")
        digest = entry.get("source_hash")
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64):
            raise ValueError("Invalid content fingerprint in index mapping.")
        ids.append(entry["thread_id"])
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate thread IDs in index mapping.")


def load_snapshot(
    data_dir: Path, embedding_model: str | None = DEFAULT_EMBEDDING_MODEL,
    embedding_dimension: int | None = DEFAULT_EMBEDDING_DIMENSION,
) -> Snapshot:
    path = data_dir / SNAPSHOT_NAME
    if path.exists():
        with np.load(path, allow_pickle=False) as archive:
            manifest = json.loads(archive["manifest"].tobytes().decode("utf-8"))
            if manifest.get("version") not in (1, 2, 3):
                raise ValueError("Unsupported snapshot version.")
            keyword_tokens = None
            if manifest.get("version") == 3:
                lexical = manifest.get("bm25")
                if not isinstance(lexical, dict) or lexical.get("tokenizer") != TOKENIZER_VERSION:
                    raise ValueError("Invalid BM25 metadata or incompatible tokenizer.")
                keyword_tokens = lexical.get("documents")
                if not isinstance(keyword_tokens, list):
                    raise ValueError("BM25 documents are missing.")
            snapshot = Snapshot(
                faiss.deserialize_index(archive["index"]), manifest["entries"], manifest.get("model"),
                keyword_tokens,
            )
            if manifest.get("version") in (2, 3) and manifest.get("dimension") != snapshot.index.d:
                raise ValueError("Snapshot manifest dimension does not match its index.")
    else:
        index = faiss.read_index(str(data_dir / "threads.index"))
        with (data_dir / "id_mapping.json").open(encoding="utf-8") as handle:
            mapping = json.load(handle)
        if not isinstance(mapping, dict) or set(mapping) != {str(i) for i in range(index.ntotal)}:
            raise ValueError("Legacy mapping is incomplete; run sync_data.py --full.")
        # The original dual-file format was generated exclusively by text-embedding-v4.
        snapshot = Snapshot(index, [{"thread_id": mapping[str(i)]} for i in range(index.ntotal)], "text-embedding-v4")
    validate_snapshot(snapshot, embedding_model, embedding_dimension)
    return snapshot


def save_snapshot(data_dir: Path, snapshot: Snapshot):
    """Publish the vector index and its mapping with one atomic replacement."""
    validate_snapshot(snapshot)
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2 if snapshot.keyword_tokens is None else 3,
        "model": snapshot.embedding_model, "dimension": snapshot.index.d, "entries": snapshot.entries,
    }
    if snapshot.keyword_tokens is not None:
        payload["bm25"] = {"tokenizer": TOKENIZER_VERSION, "documents": snapshot.keyword_tokens}
    manifest = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=data_dir, prefix=".knowledge-", suffix=".npz", delete=False) as handle:
            temporary = Path(handle.name)
            np.savez(
                handle, index=faiss.serialize_index(snapshot.index),
                manifest=np.frombuffer(manifest, dtype=np.uint8),
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, data_dir / SNAPSHOT_NAME)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class IndexStore:
    def __init__(
        self, data_dir: Path, reload_interval: float = 5.0,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
        embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION,
        bm25_enabled: bool = False,
    ):
        self.data_dir = data_dir
        self.reload_interval = reload_interval
        self.embedding_model = embedding_model
        self.embedding_dimension = embedding_dimension
        self.bm25_enabled = bm25_enabled
        self.snapshot = None
        self.last_error = None
        self._signature = None
        self._last_check = 0.0
        self._lock = threading.Lock()

    def _file_signature(self):
        snapshot_path = self.data_dir / SNAPSHOT_NAME
        paths = [snapshot_path] if snapshot_path.exists() else [
            self.data_dir / "threads.index", self.data_dir / "id_mapping.json",
        ]
        return tuple((path.name, path.stat().st_mtime_ns, path.stat().st_size) for path in paths)

    def get_snapshot(self) -> Snapshot:
        with self._lock:
            now = time.monotonic()
            if self.snapshot is not None and now - self._last_check < self.reload_interval:
                return self.snapshot
            self._last_check = now
            try:
                signature = self._file_signature()
                if self.snapshot is None or signature != self._signature:
                    candidate = load_snapshot(self.data_dir, self.embedding_model, self.embedding_dimension)
                    if self.bm25_enabled:
                        if candidate.keyword_index is None:
                            raise ValueError("BM25 index is missing; run python scripts/sync_data.py.")
                    if signature != self._file_signature():
                        raise RuntimeError("Index changed while loading; retrying on the next check.")
                    self.snapshot, self._signature = candidate, signature
                    logger.info("Loaded index containing %s threads.", candidate.index.ntotal)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                if self.snapshot is None:
                    raise RuntimeError(f"Cannot load knowledge index: {exc}. Run python scripts/sync_data.py first.") from exc
                logger.exception("Index reload failed; retaining the last valid snapshot.")
            return self.snapshot
