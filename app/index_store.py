"""Atomic index snapshots, legacy import and automatic in-process reload."""
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from .config import EMBEDDING_DIMENSION, EMBEDDING_MODEL

logger = logging.getLogger(__name__)
SNAPSHOT_NAME = "knowledge.npz"


@dataclass(frozen=True)
class Snapshot:
    index: Any
    entries: list[dict]


def validate_snapshot(snapshot: Snapshot):
    index, entries = snapshot.index, snapshot.entries
    if index.d != EMBEDDING_DIMENSION or index.metric_type != faiss.METRIC_L2:
        raise ValueError("Index dimension or distance metric is incompatible; run sync_data.py --full.")
    if not isinstance(index, faiss.IndexFlatL2):
        raise ValueError("Expected an IndexFlatL2 index.")
    if not isinstance(entries, list) or index.ntotal != len(entries):
        raise ValueError("Index and mapping counts differ; run sync_data.py --full.")
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


def load_snapshot(data_dir: Path) -> Snapshot:
    path = data_dir / SNAPSHOT_NAME
    if path.exists():
        with np.load(path, allow_pickle=False) as archive:
            manifest = json.loads(archive["manifest"].tobytes().decode("utf-8"))
            if manifest.get("version") != 1 or manifest.get("model") != EMBEDDING_MODEL:
                raise ValueError("Unsupported snapshot version or embedding model.")
            snapshot = Snapshot(faiss.deserialize_index(archive["index"]), manifest["entries"])
    else:
        index = faiss.read_index(str(data_dir / "threads.index"))
        with (data_dir / "id_mapping.json").open(encoding="utf-8") as handle:
            mapping = json.load(handle)
        if not isinstance(mapping, dict) or set(mapping) != {str(i) for i in range(index.ntotal)}:
            raise ValueError("Legacy mapping is incomplete; run sync_data.py --full.")
        snapshot = Snapshot(index, [{"thread_id": mapping[str(i)]} for i in range(index.ntotal)])
    validate_snapshot(snapshot)
    return snapshot


def save_snapshot(data_dir: Path, snapshot: Snapshot):
    """Publish the vector index and its mapping with one atomic replacement."""
    validate_snapshot(snapshot)
    data_dir.mkdir(parents=True, exist_ok=True)
    manifest = json.dumps(
        {"version": 1, "model": EMBEDDING_MODEL, "entries": snapshot.entries},
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
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
    def __init__(self, data_dir: Path, reload_interval: float = 5.0):
        self.data_dir = data_dir
        self.reload_interval = reload_interval
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
                    candidate = load_snapshot(self.data_dir)
                    if signature != self._file_signature():
                        raise RuntimeError("Index changed while loading; retrying on the next check.")
                    self.snapshot, self._signature = candidate, signature
                    logger.info("Loaded index containing %s threads.", candidate.index.ntotal)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)
                if self.snapshot is None:
                    raise RuntimeError("Cannot load knowledge index. Run python scripts/sync_data.py first.") from exc
                logger.exception("Index reload failed; retaining the last valid snapshot.")
            return self.snapshot
