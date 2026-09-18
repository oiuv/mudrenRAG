import json

import httpx

from tests.test_api import body, post, serving, thread_response


def test_keyword_only_candidate_reaches_model_reranking(serving):
    corpus = ["weather report", "bank accounts", "query_temp retrieves temporary attributes"]
    fetched, rerank_payloads = [], []

    def handler(request):
        if request.method == "GET":
            thread_id = int(request.url.path.rsplit("/", 1)[1])
            fetched.append(thread_id)
            assert "authorization" not in request.headers
            return httpx.Response(200, json={
                "title": f"Thread {thread_id}", "content": {"markdown": corpus[thread_id - 1]},
                "user": {"name": "author"},
            })
        payload = json.loads(request.content)
        rerank_payloads.append(payload)
        position = next(i for i, text in enumerate(payload["input"]["documents"]) if "query_temp" in text)
        return httpx.Response(200, json={"output": {"results": [{"index": position, "relevance_score": 0.98}]}})

    with serving(
        handler=handler, keyword_texts=corpus, vector_values=(0, 1, 5),
        bm25_enabled=True, vector_candidates=1, bm25_candidates=1,
        rerank_enabled=True, rerank_candidates=2,
    ) as (client, _, _):
        response = post(client, body(query="query_temp", retrieval_setting={"top_k": 1, "score_threshold": 0.9}))
    assert response.status_code == 200
    assert response.json()["records"][0]["metadata"]["thread_id"] == 3
    assert response.json()["records"][0]["score"] == 0.98
    assert len(rerank_payloads[0]["input"]["documents"]) == 2
    assert 3 in fetched and len(fetched) == len(set(fetched))


def test_hybrid_without_rerank_uses_normalized_rrf_scores(serving):
    with serving(
        keyword_texts=["query_temp", "weather", "bank"],
        bm25_enabled=True, rerank_enabled=False,
    ) as (client, _, _):
        response = post(client, body(query="query_temp", retrieval_setting={"top_k": 3, "score_threshold": 0.6}))
    assert response.status_code == 200
    records = response.json()["records"]
    assert [record["metadata"]["thread_id"] for record in records] == [1]
    assert records[0]["score"] == 1.0


def test_strict_metadata_filter_can_reach_beyond_each_initial_window(serving):
    with serving(
        keyword_texts=["query_temp", "weather", "query_temp"],
        bm25_enabled=True, vector_candidates=1, bm25_candidates=1, rerank_enabled=False,
    ) as (client, _, _):
        response = post(client, body(
            query="query_temp", retrieval_setting={"top_k": 1, "score_threshold": 0},
            metadata_condition={"conditions": [{"name": "thread_id", "comparison_operator": ">", "value": 2}]},
        ))
    assert response.status_code == 200
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [3]


def test_empty_keyword_query_keeps_vector_candidates(serving):
    with serving(keyword_texts=["alpha", "beta", "gamma"], bm25_enabled=True) as (client, _, _):
        response = post(client, body(query="！？()"))
    assert response.status_code == 200
    assert [record["metadata"]["thread_id"] for record in response.json()["records"]] == [1, 2]


def test_shared_keyword_and_vector_hit_fetches_content_once(serving):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return thread_response(request)
    with serving(handler=handler, keyword_texts=["query_temp", "query_temp", "other"], bm25_enabled=True) as (client, _, _):
        response = post(client, body(query="query_temp"))
    assert response.status_code == 200
    assert len(calls) == len(set(calls))


def test_empty_hybrid_index_does_not_call_embedding_service(serving):
    with serving(ids=(), keyword_texts=[], bm25_enabled=True) as (client, embeddings, _):
        response = post(client)
        assert response.json() == {"records": []}
        embeddings.embeddings.create.assert_not_called()
