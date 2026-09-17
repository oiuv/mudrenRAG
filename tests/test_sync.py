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
            NS(index=i, embedding=vector(float(text.split("标题：")[1].split("\n")[0])))
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
