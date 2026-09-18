"""DashScope native text reranking; scores belong to one shared request."""
import math

import httpx

from .config import MAX_TEXT_LENGTH, Settings
from .models import Record

# Conservative UTF-8 byte budgets keep input bounded without a tokenizer dependency.
# Each UTF-8 byte can require at most one byte-level token.
REQUEST_INPUT_BUDGET = 120_000
ITEM_INPUT_BUDGET = 30_000


class RerankInputError(ValueError):
    pass


def prepare_documents(query: str, records: list[Record]) -> list[str]:
    query_size = len(query.encode("utf-8"))
    if query_size > ITEM_INPUT_BUDGET:
        raise RerankInputError("Query is too long for reranking.")
    per_document = min(ITEM_INPUT_BUDGET, REQUEST_INPUT_BUDGET // len(records) - query_size)
    if per_document < 1:
        raise RerankInputError("Query is too long for this many candidates; shorten it or reduce RERANK_CANDIDATES/top_k.")
    documents = []
    for record in records:
        text = f"{record.title}\n{record.content}"[:MAX_TEXT_LENGTH]
        text = text.encode("utf-8")[:per_document].decode("utf-8", errors="ignore")
        if not text:
            raise RerankInputError("Insufficient rerank input budget; shorten the query or reduce candidate count.")
        documents.append(text)
    return documents


async def rerank_records(
    client: httpx.AsyncClient, settings: Settings, query: str,
    records: list[Record], top_k: int,
) -> list[Record]:
    if not records:
        return []
    top_n = min(top_k, len(records))
    response = await client.post(
        settings.rerank_api_url,
        headers={"Authorization": f"Bearer {settings.dashscope_api_key}"},
        json={
            "model": settings.rerank_model,
            "input": {"query": query, "documents": prepare_documents(query, records)},
            "parameters": {"top_n": top_n, "return_documents": False},
        },
        timeout=settings.rerank_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code"):
        raise ValueError("Rerank service returned an error response.")
    output = payload.get("output")
    results = output.get("results") if isinstance(output, dict) else None
    if not isinstance(results, list) or len(results) != top_n:
        raise ValueError("Rerank response has an invalid result count.")
    ranked, seen = [], set()
    for result in results:
        if not isinstance(result, dict):
            raise ValueError("Invalid rerank result.")
        index, score = result.get("index"), result.get("relevance_score")
        if type(index) is not int or index < 0 or index >= len(records) or index in seen:
            raise ValueError("Rerank response has an invalid or duplicate document index.")
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError("Rerank response has an invalid score.")
        seen.add(index)
        ranked.append(records[index].model_copy(update={"score": float(score)}))
    return sorted(ranked, key=lambda record: record.score, reverse=True)
