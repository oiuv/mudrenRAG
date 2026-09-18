from types import SimpleNamespace as NS
from unittest.mock import Mock

import numpy as np
import pytest
from filelock import FileLock, Timeout

import scripts.sync_data as sync
from app.config import Settings
from app.index_store import SNAPSHOT_NAME, load_snapshot, save_snapshot
from tests.helpers import snapshot, vector


def embedding_client(fail_on=None):
    calls = []

    def create(**kwargs):
        calls.append(kwargs["input"])
        if len(calls) == fail_on:
            raise RuntimeError("simulated failed embedding batch")
        # Reverse response order to exercise response.index binding.
        return NS(data=[
            NS(index=i, embedding=vector(float(text.split("标题：")[1].split("\n")[0]), kwargs["dimensions"]))
            for i, text in reversed(list(enumerate(kwargs["input"])))
        ])

    client = Mock()
    client.embeddings.create.side_effect = create
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    return client, calls


def test_changed_added_deleted_and_restored_threads():
    client, calls = embedding_client()
    old = sync.build_snapshot([(1, "1", "original"), (2, "2", "removed")], client)
    new = sync.build_snapshot([(1, "1", "edited"), (3, "3", "new")], client, old)
    assert [entry["thread_id"] for entry in new.entries] == [1, 3]
    assert len(calls) == 2
    np.testing.assert_array_equal(new.index.reconstruct(0), vector(1))
    np.testing.assert_array_equal(new.index.reconstruct(1), vector(3))
    reused = sync.build_snapshot([(1, "1", "edited"), (3, "3", "new")], client, new)
    assert reused.entries == new.entries
    assert len(calls) == 2
    restored = sync.build_snapshot([(1, "1", "edited"), (2, "2", "restored"), (3, "3", "new")], client, new)
    assert len(calls[-1]) == 1
    assert [entry["thread_id"] for entry in restored.entries] == [1, 2, 3]


def test_legacy_vectors_are_regenerated():
    client, calls = embedding_client()
    result = sync.build_snapshot([(1, "1", "text")], client, snapshot((1,)))
    assert len(calls) == 1
    assert result.entries[0]["source_hash"]


def test_empty_database_clears_index_without_embedding_calls():
    client, calls = embedding_client()
    result = sync.build_snapshot([], client, snapshot((1,)))
    assert result.index.ntotal == 0
    assert result.entries == []
    assert calls == []


def test_partial_batch_failure_does_not_publish_or_skip_threads(tmp_path, monkeypatch):
    settings = Settings("test-dify", "test-dashscope", tmp_path)
    save_snapshot(tmp_path, snapshot((99,)))
    before = (tmp_path / SNAPSHOT_NAME).read_bytes()
    rows = [(i, str(i), "content") for i in range(1, 7)]
    connection = Mock()
    client, calls = embedding_client(fail_on=2)
    monkeypatch.setattr(sync, "BATCH_SIZE", 2)
    monkeypatch.setattr(sync, "get_db_connection", lambda: connection)
    monkeypatch.setattr(sync, "iter_threads", lambda connection: (row for row in rows))
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: client)
    with pytest.raises(RuntimeError, match="failed embedding"):
        sync.synchronize(settings)
    assert len(calls) == 2  # Stop at the failed batch; never advance beyond it.
    assert (tmp_path / SNAPSHOT_NAME).read_bytes() == before
    connection.close.assert_called_once()

    retry_client, retry_calls = embedding_client()
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: retry_client)
    sync.synchronize(settings)
    result = load_snapshot(tmp_path)
    assert [entry["thread_id"] for entry in result.entries] == list(range(1, 7))
    for i in range(6):
        np.testing.assert_array_equal(result.index.reconstruct(i), vector(i + 1))
    assert len(retry_calls) == 3


@pytest.mark.parametrize("items", [
    [],
    [NS(index=1, embedding=vector())],
    [NS(index=0, embedding=[1.0])],
    [NS(index=0, embedding=[float("nan")] * 1024)],
])
def test_invalid_embedding_response_is_rejected(items):
    client = Mock()
    client.embeddings.create.return_value = NS(data=items)
    with pytest.raises(ValueError):
        sync.build_snapshot([(1, "1", "text")], client)


def test_second_synchronization_is_rejected_by_lock(tmp_path):
    settings = Settings("key", "key", tmp_path)
    with FileLock(str(tmp_path / ".sync.lock"), timeout=0):
        with pytest.raises(Timeout):
            sync.synchronize(settings)


def test_streaming_query_uses_parameters_and_closes_cursor():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchmany.side_effect = [[(1, "1", "text")], []]
    assert list(sync.iter_threads(connection)) == [(1, "1", "text")]
    sql, params = cursor.execute.call_args.args
    assert "ORDER BY t.id" in sql
    assert "deleted_at IS NULL" in sql and "banned_at IS NULL" in sql
    assert params == ("App\\Thread",)
    cursor.close.assert_called_once()


def test_model_or_dimension_change_reembeds_unchanged_content():
    client, calls = embedding_client()
    rows = [(1, "1", "unchanged")]
    old = sync.build_snapshot(rows, client, embedding_model="text-embedding-v4")
    changed = sync.build_snapshot(rows, client, old, embedding_model="custom-embedding", embedding_dimension=256)
    assert len(calls) == 2
    assert changed.embedding_model == "custom-embedding"
    assert changed.index.d == 256
    assert changed.entries == old.entries
    kwargs = client.embeddings.create.call_args.kwargs
    assert kwargs["model"] == "custom-embedding" and kwargs["dimensions"] == 256


def test_model_migration_is_published_even_when_content_hashes_match(tmp_path, monkeypatch):
    from app.config import DEFAULT_EMBEDDING_MODEL
    client, calls = embedding_client()
    rows = [(1, "1", "unchanged")]
    old = sync.build_snapshot(rows, client, embedding_model="text-embedding-v4")
    save_snapshot(tmp_path, old)
    connection = Mock()
    monkeypatch.setattr(sync, "get_db_connection", lambda: connection)
    monkeypatch.setattr(sync, "iter_threads", lambda connection: (row for row in rows))
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: client)
    sync.synchronize(Settings("test", "test", tmp_path))
    migrated = load_snapshot(tmp_path)
    assert migrated.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert migrated.entries == old.entries
    assert len(calls) == 2


def test_failed_model_migration_keeps_original_snapshot(tmp_path, monkeypatch):
    client, _ = embedding_client()
    rows = [(1, "1", "unchanged")]
    old = sync.build_snapshot(rows, client, embedding_model="text-embedding-v4")
    save_snapshot(tmp_path, old)
    before = (tmp_path / SNAPSHOT_NAME).read_bytes()
    failing_client, _ = embedding_client(fail_on=1)
    monkeypatch.setattr(sync, "get_db_connection", Mock())
    monkeypatch.setattr(sync, "iter_threads", lambda connection: (row for row in rows))
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: failing_client)
    with pytest.raises(RuntimeError, match="failed embedding"):
        sync.synchronize(Settings("test", "test", tmp_path))
    assert (tmp_path / SNAPSHOT_NAME).read_bytes() == before
    assert load_snapshot(tmp_path, embedding_model="text-embedding-v4").embedding_model == "text-embedding-v4"


def test_bm25_migration_reuses_embeddings_and_publishes_new_snapshot(tmp_path, monkeypatch):
    from app.index_store import Snapshot
    rows = [(1, "1", "query_temp")]
    client, calls = embedding_client()
    previous = sync.build_snapshot(rows, client)
    # Simulate a version-2 snapshot with fingerprints but no lexical corpus.
    save_snapshot(tmp_path, Snapshot(previous.index, previous.entries, previous.embedding_model))
    monkeypatch.setattr(sync, "get_db_connection", Mock())
    monkeypatch.setattr(sync, "iter_threads", lambda connection: (row for row in rows))
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: client)
    sync.synchronize(Settings("test", "test", tmp_path))
    migrated = load_snapshot(tmp_path)
    assert len(calls) == 1  # No additional embedding request during BM25 migration.
    assert migrated.keyword_tokens is not None
    assert migrated.keyword_index.search("query_temp", 1)[0][0] == 0
    assert migrated.entries == previous.entries


def test_keyword_index_tracks_edits_deletions_and_restoration():
    client, _ = embedding_client()
    old = sync.build_snapshot([(1, "1", "oldkeyword"), (2, "2", "deletedword")], client)
    updated = sync.build_snapshot([(1, "1", "newkeyword")], client, old)
    assert updated.keyword_index.search("oldkeyword", 10) == []
    assert updated.keyword_index.search("deletedword", 10) == []
    assert updated.keyword_index.search("newkeyword", 10)[0][0] == 0
    restored = sync.build_snapshot([(1, "1", "newkeyword"), (2, "2", "deletedword")], client, updated)
    position = restored.keyword_index.search("deletedword", 1)[0][0]
    assert restored.entries[position]["thread_id"] == 2


def test_keyword_index_covers_text_beyond_embedding_truncation():
    client, calls = embedding_client()
    result = sync.build_snapshot([(1, "1", "padding " * 2000 + "rare_tail_identifier")], client)
    assert "rare_tail_identifier" not in calls[0][0]
    assert result.keyword_index.search("rare_tail_identifier", 1)[0][0] == 0


def test_empty_database_publishes_empty_lexical_corpus():
    client, _ = embedding_client()
    result = sync.build_snapshot([], client)
    assert result.keyword_tokens == []
    assert result.keyword_index.search("anything", 10) == []


def test_duplicate_zero_indices_retry_individually_without_assuming_order(monkeypatch):
    monkeypatch.setattr(sync, "BATCH_SIZE", 2)
    client, calls = embedding_client()
    original_create = client.embeddings.create.side_effect
    def create(**kwargs):
        result = original_create(**kwargs)
        if len(kwargs["input"]) > 1:
            for item in result.data:
                item.index = 0
        return result
    client.embeddings.create.side_effect = create
    result = sync.build_snapshot([(i, str(i), "content") for i in range(1, 5)], client)
    # The ambiguous batch was reversed; none of its positions may be trusted.
    assert [len(texts) for texts in calls] == [2, 1, 1, 1, 1]
    for position in range(4):
        np.testing.assert_array_equal(result.index.reconstruct(position), vector(position + 1))


def test_invalid_single_retry_keeps_published_snapshot(tmp_path, monkeypatch):
    settings = Settings("dify", "model", tmp_path)
    save_snapshot(tmp_path, snapshot((99,)))
    before = (tmp_path / SNAPSHOT_NAME).read_bytes()
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.embeddings.create.side_effect = [
        NS(data=[NS(index=0, embedding=vector(2)), NS(index=0, embedding=vector(1))]),
        NS(data=[NS(index=1, embedding=vector(1))]),
    ]
    monkeypatch.setattr(sync, "get_db_connection", Mock())
    monkeypatch.setattr(sync, "iter_threads", lambda connection: iter_rows())
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: client)
    def iter_rows():
        yield (1, "1", "text")
        yield (2, "2", "text")
    with pytest.raises(ValueError, match="Single-text"):
        sync.synchronize(settings)
    assert (tmp_path / SNAPSHOT_NAME).read_bytes() == before


def test_interrupted_stream_can_close_with_unread_mysql_rows():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchmany.return_value = [(1, "1", "text"), (2, "2", "text")]
    cursor.close.side_effect = sync.mysql.connector.InternalError("Unread result found")
    rows = sync.iter_threads(connection)
    assert next(rows) == (1, "1", "text")
    rows.close()
    cursor.close.assert_called_once()


def test_cleanup_preserves_original_embedding_error(tmp_path, monkeypatch):
    settings = Settings("dify", "model", tmp_path)
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchmany.return_value = [(i, str(i), "text") for i in range(1, 20)]
    cursor.close.side_effect = sync.mysql.connector.InternalError("Unread result found")
    client, _ = embedding_client(fail_on=1)
    monkeypatch.setattr(sync, "get_db_connection", lambda: connection)
    monkeypatch.setattr(sync, "OpenAI", lambda **kwargs: client)
    with pytest.raises(RuntimeError, match="failed embedding"):
        sync.synchronize(settings)
    connection.close.assert_called_once()
    assert not (tmp_path / SNAPSHOT_NAME).exists()


def test_normal_stream_close_error_is_not_hidden():
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.fetchmany.return_value = []
    cursor.close.side_effect = sync.mysql.connector.InternalError("unexpected close failure")
    with pytest.raises(sync.mysql.connector.InternalError, match="unexpected close failure"):
        list(sync.iter_threads(connection))
