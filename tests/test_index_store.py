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
    assert load_snapshot(tmp_path, embedding_model="text-embedding-v4").entries == legacy.entries
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


def test_index_model_and_dimension_must_match_query_configuration(tmp_path):
    save_snapshot(tmp_path, snapshot((1,), model="old-embedding", dimension=256))
    with pytest.raises(ValueError, match="configured model"):
        load_snapshot(tmp_path)
    with pytest.raises(ValueError, match="EMBEDDING_DIMENSION"):
        load_snapshot(tmp_path, embedding_model="old-embedding")
    loaded = load_snapshot(tmp_path, embedding_model="old-embedding", embedding_dimension=256)
    assert loaded.embedding_model == "old-embedding"
    assert loaded.index.d == 256


def test_version_one_keeps_original_model_identity(tmp_path):
    import numpy as np
    old = snapshot((1,), model="text-embedding-v4")
    manifest = json.dumps({"version": 1, "model": "text-embedding-v4", "entries": old.entries}).encode()
    np.savez(tmp_path / SNAPSHOT_NAME, index=faiss.serialize_index(old.index), manifest=np.frombuffer(manifest, dtype=np.uint8))
    assert load_snapshot(tmp_path, embedding_model="text-embedding-v4").embedding_model == "text-embedding-v4"
    with pytest.raises(ValueError, match="configured model"):
        load_snapshot(tmp_path)


def test_legacy_files_cannot_be_used_with_new_default_model(tmp_path):
    old = snapshot((1,))
    faiss.write_index(old.index, str(tmp_path / "threads.index"))
    (tmp_path / "id_mapping.json").write_text('{"0": 1}', encoding="utf-8")
    with pytest.raises(ValueError, match="configured model"):
        load_snapshot(tmp_path)


def test_hot_reload_rejects_incompatible_model_and_preserves_working_index(tmp_path):
    save_snapshot(tmp_path, snapshot((1,)))
    store = IndexStore(tmp_path, reload_interval=0)
    first = store.get_snapshot()
    save_snapshot(tmp_path, snapshot((2,), model="another-model"))
    assert store.get_snapshot() is first
    assert "configured model" in store.last_error


def test_hybrid_snapshot_roundtrip_and_atomic_hot_reload(tmp_path):
    old = snapshot((1,), keyword_texts=["oldkeyword"])
    save_snapshot(tmp_path, old)
    store = IndexStore(tmp_path, reload_interval=0, bm25_enabled=True)
    first = store.get_snapshot()
    assert first.keyword_index.search("oldkeyword", 1)[0][0] == 0
    save_snapshot(tmp_path, snapshot((2,), keyword_texts=["newkeyword"]))
    second = store.get_snapshot()
    assert second.entries == [{"thread_id": 2}]
    assert second.keyword_index.search("oldkeyword", 1) == []
    assert second.keyword_index.search("newkeyword", 1)[0][0] == 0
    assert first.entries == [{"thread_id": 1}]
    assert first.keyword_index.search("oldkeyword", 1)[0][0] == 0


def test_missing_bm25_requires_sync_only_when_enabled(tmp_path):
    save_snapshot(tmp_path, snapshot((1,)))
    assert IndexStore(tmp_path, bm25_enabled=False).get_snapshot().index.ntotal == 1
    with pytest.raises(RuntimeError, match="BM25 index is missing"):
        IndexStore(tmp_path, bm25_enabled=True).get_snapshot()


def test_bm25_count_mismatch_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="BM25 documents"):
        save_snapshot(tmp_path, snapshot((1, 2), keyword_texts=["only one"]))


def test_corrupt_bm25_tokens_are_rejected(tmp_path):
    value = snapshot((1,), keyword_texts=["valid"])
    value.keyword_tokens[0].append(None)
    with pytest.raises(ValueError, match="Invalid tokens"):
        save_snapshot(tmp_path, value)


def test_failed_hybrid_publication_keeps_both_old_indexes(tmp_path, monkeypatch):
    save_snapshot(tmp_path, snapshot((1,), keyword_texts=["original"]))
    def interrupted(*args):
        raise OSError("interrupted publication")
    monkeypatch.setattr(store_module.os, "replace", interrupted)
    with pytest.raises(OSError):
        save_snapshot(tmp_path, snapshot((2,), keyword_texts=["replacement"]))
    unchanged = load_snapshot(tmp_path)
    assert unchanged.entries == [{"thread_id": 1}]
    assert unchanged.keyword_index.search("original", 1)
    assert unchanged.keyword_index.search("replacement", 1) == []


def test_hybrid_reload_rejects_snapshot_without_keywords(tmp_path):
    save_snapshot(tmp_path, snapshot((1,), keyword_texts=["original"]))
    store = IndexStore(tmp_path, reload_interval=0, bm25_enabled=True)
    first = store.get_snapshot()
    save_snapshot(tmp_path, snapshot((2,)))
    assert store.get_snapshot() is first
    assert "BM25 index is missing" in store.last_error
