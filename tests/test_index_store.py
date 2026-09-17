import json

import faiss
import pytest

import app.index_store as store_module
from app.index_store import IndexStore, SNAPSHOT_NAME, load_snapshot, save_snapshot
from tests.helpers import snapshot


def test_snapshot_roundtrip_and_hot_reload(tmp_path):
    save_snapshot(tmp_path, snapshot((1, 2)))
    store = IndexStore(tmp_path, reload_interval=0)
    first = store.get_snapshot()
    save_snapshot(tmp_path, snapshot((3,)))
    second = store.get_snapshot()
    assert [entry["thread_id"] for entry in first.entries] == [1, 2]
    assert [entry["thread_id"] for entry in second.entries] == [3]
    assert first.index.ntotal == 2  # In-flight requests retain a consistent old snapshot.


def test_empty_index_is_valid(tmp_path):
    save_snapshot(tmp_path, snapshot(()))
    assert load_snapshot(tmp_path).index.ntotal == 0


def test_failed_atomic_replace_preserves_previous_version(tmp_path, monkeypatch):
    save_snapshot(tmp_path, snapshot((1,)))
    previous_bytes = (tmp_path / SNAPSHOT_NAME).read_bytes()

    def failed_replace(*args):
        raise OSError("simulated interrupted publication")

    monkeypatch.setattr(store_module.os, "replace", failed_replace)
    with pytest.raises(OSError):
        save_snapshot(tmp_path, snapshot((2, 3)))
    assert (tmp_path / SNAPSHOT_NAME).read_bytes() == previous_bytes
    assert list(tmp_path.glob(".knowledge-*.npz")) == []


def test_reload_retains_old_snapshot_when_new_file_is_corrupt(tmp_path):
    save_snapshot(tmp_path, snapshot((1,)))
    store = IndexStore(tmp_path, reload_interval=0)
    first = store.get_snapshot()
    (tmp_path / SNAPSHOT_NAME).write_bytes(b"broken")
    assert store.get_snapshot() is first
    assert store.last_error
    save_snapshot(tmp_path, snapshot((2,)))
    assert store.get_snapshot().entries == [{"thread_id": 2}]
    assert store.last_error is None


def test_legacy_import_and_mapping_validation(tmp_path):
    legacy = snapshot((10, 20))
    faiss.write_index(legacy.index, str(tmp_path / "threads.index"))
    (tmp_path / "id_mapping.json").write_text(json.dumps({"0": 10, "1": 20}), encoding="utf-8")
    assert load_snapshot(tmp_path).entries == legacy.entries
    (tmp_path / "id_mapping.json").write_text(json.dumps({"0": 10, "2": 20}), encoding="utf-8")
    with pytest.raises(ValueError, match="Legacy mapping"):
        load_snapshot(tmp_path)


def test_snapshot_rejects_mismatched_counts(tmp_path):
    value = snapshot((1, 2))
    value.entries.pop()
    with pytest.raises(ValueError, match="counts"):
        save_snapshot(tmp_path, value)


def test_snapshot_rejects_duplicate_ids(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        save_snapshot(tmp_path, snapshot((1, 1)))


def test_missing_index_reports_startup_failure(tmp_path):
    with pytest.raises(RuntimeError, match="sync_data.py"):
        IndexStore(tmp_path).get_snapshot()
