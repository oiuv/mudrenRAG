import asyncio
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as api
from app.config import Settings
from app.index_store import save_snapshot
from tests.helpers import snapshot, vector

REAL_ASYNC_CLIENT = httpx.AsyncClient


def body(**overrides):
    return {
        "knowledge_id": "forum", "query": "question",
        "retrieval_setting": {"top_k": 2, "score_threshold": 0.0},
        **overrides,
    }


def thread_response(request):
    thread_id = int(request.url.path.rsplit("/", 1)[1])
    return httpx.Response(200, json={
        "title": f"Thread {thread_id}",
        "content": {"markdown": f"Content {thread_id}"},
        "user": {"name": "alice" if thread_id == 3 else "bob"},
        "published_at": "2026-01-01T00:00:00Z",
    })


class FakeEmbeddingClient:
    def __init__(self):
        self.embeddings = NS(create=AsyncMock(return_value=NS(data=[NS(embedding=vector().tolist())])))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def serving(tmp_path, monkeypatch):
    @contextmanager
    def run(handler=thread_response, ids=(1, 2, 3), keyword_texts=None, vector_values=None, **overrides):
        settings = replace(
            Settings("test-key", "test-dashscope", tmp_path, knowledge_id="forum", index_reload_interval=0, rerank_enabled=False, bm25_enabled=False),
            **overrides,
        )
        save_snapshot(tmp_path, snapshot(
            ids, values=vector_values, model=settings.embedding_model,
            dimension=settings.embedding_dimension, keyword_texts=keyword_texts,
        ))
        embeddings = FakeEmbeddingClient()
        embeddings.embeddings.create.return_value = NS(data=[NS(embedding=vector(dimension=settings.embedding_dimension).tolist())])
        monkeypatch.setattr(api.Settings, "from_env", classmethod(lambda cls: settings))
        monkeypatch.setattr(api, "AsyncOpenAI", lambda **kwargs: embeddings)
        monkeypatch.setattr(
            api.httpx, "AsyncClient",
            lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
        )
        with TestClient(api.app) as client:
            yield client, embeddings, settings
    return run


def post(client, payload=None, key="test-key"):
    return client.post("/retrieval", json=payload or body(), headers={"Authorization": f"Bearer {key}"})


def test_dify_response_shape_and_ranking(serving):
    with serving() as (client, _, _):
        response = post(client)
    assert response.status_code == 200
    records = response.json()["records"]
    assert [record["metadata"]["thread_id"] for record in records] == [1, 2]
    assert all(record["content"] and isinstance(record["metadata"], dict) for record in records)
    assert 1 >= records[0]["score"] >= records[1]["score"] >= 0


@pytest.mark.parametrize(("authorization", "error_code"), [
    (None, 1001), ("Basic test-key", 1001), ("Bearer", 1001), ("Bearer wrong", 1002),
])
def test_auth_errors_have_top_level_fields(serving, authorization, error_code):
    with serving() as (client, embeddings, _):
        headers = {"Authorization": authorization} if authorization else {}
        response = client.post("/retrieval", json=body(), headers=headers)
        assert response.status_code == 403
        assert response.json()["error_code"] == error_code
        assert "detail" not in response.json()
        embeddings.embeddings.create.assert_not_called()


def test_wrong_knowledge_id_is_rejected_before_embedding(serving):
    with serving() as (client, embeddings, _):
        response = post(client, body(knowledge_id="unknown"))
        assert response.status_code == 404
        assert response.json()["error_code"] == 2001
        embeddings.embeddings.create.assert_not_called()


def test_legacy_id_compatibility(serving):
    with serving(knowledge_id="") as (client, _, _):
        assert post(client, body(knowledge_id="existing-dify-id")).status_code == 200


def test_valid_metadata_filter_can_reach_beyond_original_top_k(serving):
    payload = body(
        retrieval_setting={"top_k": 1, "score_threshold": 0},
        metadata_condition={"conditions": [{"name": "author", "comparison_operator": "in", "value": ["alice"]}]},
    )
    with serving(fetch_concurrency=2) as (client, _, _):
        response = post(client, payload)
    assert response.status_code == 200
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [3]


def test_numeric_metadata_filter(serving):
    payload = body(metadata_condition={"conditions": [{"name": "thread_id", "comparison_operator": ">", "value": 1}]})
    with serving() as (client, _, _):
        assert [record["metadata"]["thread_id"] for record in post(client, payload).json()["records"]] == [2, 3]


def test_invalid_request_returns_structured_validation_error(serving):
    with serving() as (client, embeddings, _):
        response = post(client, body(retrieval_setting={"top_k": 0, "score_threshold": 0}))
        assert response.status_code == 422
        assert response.json()["error_code"] == 1003
        embeddings.embeddings.create.assert_not_called()


def test_empty_index_is_a_success_without_paid_api_call(serving):
    with serving(ids=()) as (client, embeddings, _):
        response = post(client)
        assert response.status_code == 200
        assert response.json() == {"records": []}
        embeddings.embeddings.create.assert_not_called()


def test_no_matching_metadata_returns_empty_success(serving):
    payload = body(metadata_condition={"conditions": [{"name": "author", "comparison_operator": "is", "value": "nobody"}]})
    with serving() as (client, _, _):
        response = post(client, payload)
        assert response.status_code == 200
        assert response.json() == {"records": []}


def test_score_threshold_does_not_round_below_cutoff(serving):
    threshold = 1 / (1 + 0.25 ** 2)
    with serving() as (client, _, _):
        response = post(client, body(retrieval_setting={"top_k": 3, "score_threshold": threshold}))
    assert len(response.json()["records"]) == 2
    assert all(record["score"] >= threshold for record in response.json()["records"])


def test_certificate_errors_are_visible_to_dify(serving):
    def failed(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired", request=request)
    with serving(handler=failed) as (client, _, _):
        response = post(client)
    assert response.status_code == 502
    assert response.json()["error_code"] == 5003
    assert "certificate" in response.json()["error_msg"]


@pytest.mark.parametrize("handler", [
    lambda request: httpx.Response(503),
    lambda request: httpx.Response(200, json={"content": None}),
    lambda request: httpx.Response(200, content=b"not json"),
    lambda request: httpx.Response(200, json=[]),
])
def test_broken_upstream_is_not_silent_empty_success(serving, handler):
    with serving(handler=handler) as (client, _, _):
        assert post(client).status_code == 502


def test_partial_success_preserves_available_results(serving):
    def handler(request):
        if request.url.path.endswith("/1"):
            raise httpx.ReadTimeout("timeout", request=request)
        return thread_response(request)
    with serving(handler=handler) as (client, _, _):
        response = post(client)
    assert response.status_code == 200
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [2]


def test_deleted_posts_are_not_upstream_failures(serving):
    with serving(handler=lambda request: httpx.Response(404)) as (client, _, _):
        response = post(client)
        assert response.status_code == 200
        assert response.json() == {"records": []}


def test_embedding_failure_is_structured(serving):
    with serving() as (client, embeddings, _):
        embeddings.embeddings.create.side_effect = RuntimeError("provider unavailable")
        response = post(client)
        assert response.status_code == 502
        assert response.json()["error_code"] == 5002


def test_hot_reload_is_visible_without_restarting_api(serving):
    with serving() as (client, _, settings):
        assert client.get("/health").json()["indexed_threads"] == 3
        save_snapshot(settings.data_dir, snapshot((99,)))
        response = post(client)
        assert response.json()["records"][0]["metadata"]["thread_id"] == 99
        assert client.get("/health").json() == {"status": "ok", "indexed_threads": 1}


def test_fetches_are_concurrent_bounded_and_return_in_rank_order(serving):
    in_flight = 0
    maximum = 0

    async def handler(request):
        nonlocal in_flight, maximum
        in_flight += 1
        maximum = max(maximum, in_flight)
        thread_id = int(request.url.path.rsplit("/", 1)[1])
        await asyncio.sleep(0.01 * (4 - thread_id))
        in_flight -= 1
        return thread_response(request)

    with serving(handler=handler, fetch_concurrency=2) as (client, _, _):
        response = post(client, body(retrieval_setting={"top_k": 3, "score_threshold": 0}))
    assert maximum == 2
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [1, 2, 3]


def test_missing_credentials_prevent_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(
        api.Settings, "from_env",
        classmethod(lambda cls: Settings("", "", tmp_path)),
    )
    with pytest.raises(RuntimeError, match="must both be configured"):
        with TestClient(api.app):
            pass
