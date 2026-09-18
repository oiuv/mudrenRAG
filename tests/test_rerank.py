import json

import httpx
import pytest

from app.config import DEFAULT_EMBEDDING_MODEL, DEFAULT_RERANK_MODEL
from app.models import Record
from app.rerank import REQUEST_INPUT_BUDGET, RerankInputError, prepare_documents
from tests.test_api import body, post, serving, thread_response


def rerank_handler(calls, results=None):
    def handler(request):
        if request.method == "GET":
            assert "authorization" not in request.headers
            return thread_response(request)
        calls.append(request)
        payload = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test-dashscope"
        if results is not None:
            return httpx.Response(200, json={"output": {"results": results}})
        count = len(payload["input"]["documents"])
        return httpx.Response(200, json={"output": {"results": [
            {"index": i, "relevance_score": 0.99 - rank * 0.1}
            for rank, i in enumerate(reversed(range(count)))
        ][:payload["parameters"]["top_n"]]}})
    return handler


def test_default_models_rerank_candidates_outside_vector_top_k(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True) as (client, embeddings, _):
        response = post(client, body(retrieval_setting={"top_k": 1, "score_threshold": 0.95}))
        assert response.status_code == 200
        assert embeddings.embeddings.create.call_args.kwargs["model"] == DEFAULT_EMBEDDING_MODEL
    records = response.json()["records"]
    assert [record["metadata"]["thread_id"] for record in records] == [3]
    assert records[0]["score"] == 0.99
    payload = json.loads(calls[0].content)
    assert payload["model"] == DEFAULT_RERANK_MODEL
    assert len(payload["input"]["documents"]) == 3
    assert payload["parameters"] == {"top_n": 1, "return_documents": False}
    assert calls[0].url.path == "/api/v1/services/rerank/text-rerank/text-rerank"


def test_custom_models_dimension_endpoint_and_candidate_limit(serving):
    calls = []
    with serving(
        handler=rerank_handler(calls), rerank_enabled=True,
        embedding_model="custom-embedding", embedding_dimension=256,
        rerank_model="custom-rerank", rerank_api_url="https://custom.example/rerank",
        rerank_candidates=2,
    ) as (client, embeddings, _):
        response = post(client, body(retrieval_setting={"top_k": 1, "score_threshold": 0}))
        assert response.status_code == 200
        kwargs = embeddings.embeddings.create.call_args.kwargs
        assert kwargs["model"] == "custom-embedding" and kwargs["dimensions"] == 256
    assert str(calls[0].url) == "https://custom.example/rerank"
    assert json.loads(calls[0].content)["model"] == "custom-rerank"
    assert len(json.loads(calls[0].content)["input"]["documents"]) == 2
    assert response.json()["records"][0]["metadata"]["thread_id"] == 2


def test_candidate_limit_is_at_least_top_k(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True, rerank_candidates=1) as (client, _, _):
        response = post(client, body(retrieval_setting={"top_k": 3, "score_threshold": 0}))
    assert len(response.json()["records"]) == 3
    assert len(json.loads(calls[0].content)["input"]["documents"]) == 3


def test_metadata_filter_precedes_rerank(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True) as (client, _, _):
        response = post(client, body(metadata_condition={
            "conditions": [{"name": "author", "comparison_operator": "is", "value": "alice"}],
        }))
    assert response.status_code == 200
    assert response.json()["records"][0]["metadata"]["thread_id"] == 3
    assert json.loads(calls[0].content)["input"]["documents"] == ["Thread 3\nContent 3"]


def test_threshold_applies_to_rerank_scores(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True) as (client, _, _):
        response = post(client, body(retrieval_setting={"top_k": 3, "score_threshold": 0.9}))
    assert [record["score"] for record in response.json()["records"]] == [0.99]


def test_empty_after_rerank_threshold_is_normal_success(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True) as (client, _, _):
        response = post(client, body(retrieval_setting={"top_k": 1, "score_threshold": 1}))
    assert response.status_code == 200
    assert response.json() == {"records": []}


@pytest.mark.parametrize("failure", ["http", "timeout", "provider", "invalid-json"])
def test_rerank_failures_are_reported_without_vector_fallback(serving, failure):
    def handler(request):
        if request.method == "GET":
            return thread_response(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("rerank timeout", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "provider":
            return httpx.Response(200, json={"code": "InvalidApiKey", "message": "invalid"})
        return httpx.Response(200, content=b"not json")
    with serving(handler=handler, rerank_enabled=True) as (client, _, _):
        response = post(client)
    assert response.status_code == 502
    assert response.json()["error_code"] == 5005


@pytest.mark.parametrize("results", [
    [],
    [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}],
    [{"index": -1, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.8}],
    [{"index": 99, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.8}],
    [{"index": True, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.8}],
    [{"index": 0, "relevance_score": "0.9"}, {"index": 1, "relevance_score": 0.8}],
    [{"index": 0, "relevance_score": 1.1}, {"index": 1, "relevance_score": 0.8}],
])
def test_invalid_rerank_response_is_rejected(serving, results):
    with serving(handler=rerank_handler([], results), rerank_enabled=True) as (client, _, _):
        response = post(client)
    assert response.status_code == 502
    assert response.json()["error_code"] == 5005


def test_rerank_results_are_sorted_and_attached_to_correct_documents(serving):
    results = [{"index": 2, "relevance_score": 0.7}, {"index": 0, "relevance_score": 0.9}]
    with serving(handler=rerank_handler([], results), rerank_enabled=True) as (client, _, _):
        response = post(client)
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [1, 3]
    assert [record["score"] for record in response.json()["records"]] == [0.9, 0.7]


def test_no_rerank_request_for_no_matching_documents(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=True) as (client, _, _):
        response = post(client, body(metadata_condition={
            "conditions": [{"name": "author", "comparison_operator": "is", "value": "nobody"}],
        }))
    assert response.json() == {"records": []}
    assert calls == []


def test_rerank_disabled_skips_endpoint(serving):
    calls = []
    with serving(handler=rerank_handler(calls), rerank_enabled=False) as (client, _, _):
        response = post(client)
    assert response.status_code == 200
    assert response.json()["records"][0]["metadata"]["thread_id"] == 1
    assert calls == []


def test_long_documents_fit_one_request_budget_without_mutating_records():
    records = [Record(content="长文" * 10000, title="标题", score=0.5) for _ in range(100)]
    query = "问题" * 50
    documents = prepare_documents(query, records)
    total = sum(len(document.encode("utf-8")) + len(query.encode("utf-8")) for document in documents)
    assert total <= REQUEST_INPUT_BUDGET
    assert all(documents)
    assert all(record.content == "长文" * 10000 for record in records)


def test_long_query_reports_input_limit_before_calling_rerank(serving):
    calls = []
    with serving(handler=rerank_handler(calls), ids=tuple(range(1, 101)), rerank_enabled=True) as (client, _, _):
        response = post(client, body(query="问题" * 4000, retrieval_setting={"top_k": 100, "score_threshold": 0}))
    assert response.status_code == 422
    assert response.json()["error_code"] == 1003
    assert calls == []


def test_single_item_input_limit():
    with pytest.raises(RerankInputError):
        prepare_documents("x" * 30001, [Record(content="text", title="title", score=0.5)])
