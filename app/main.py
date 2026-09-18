import asyncio
import hmac
import logging
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from .config import Settings
from .index_store import IndexStore
from .metadata import matches_metadata
from .models import ErrorResponse, Record, RetrievalRequest, RetrievalResponse
from .rerank import RerankInputError, rerank_records
from .retrieval import retrieve_candidates

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, error_code: int, message: str, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.headers = headers


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    if not settings.dify_api_key or not settings.dashscope_api_key:
        raise RuntimeError("DIFY_API_KEY and DASHSCOPE_API_KEY must both be configured.")
    store = IndexStore(
        settings.data_dir, settings.index_reload_interval,
        settings.embedding_model, settings.embedding_dimension, bm25_enabled=settings.bm25_enabled,
    )
    await asyncio.to_thread(store.get_snapshot)
    if not settings.knowledge_id:
        logger.warning("KNOWLEDGE_ID is unset; accepting any knowledge_id for legacy compatibility.")
    async with AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url=settings.embedding_base_url,
        timeout=30.0, max_retries=2,
    ) as embeddings, httpx.AsyncClient(
        timeout=httpx.Timeout(settings.http_timeout),
        limits=httpx.Limits(max_connections=settings.fetch_concurrency),
    ) as http:
        app.state.settings = settings
        app.state.store = store
        app.state.embeddings = embeddings
        app.state.http = http
        app.state.fetch_slots = asyncio.Semaphore(settings.fetch_concurrency)
        app.state.forum_retry_at = 0.0
        yield


app = FastAPI(
    title="mudrenRAG Retrieval Service for Dify",
    description="External knowledge retrieval for forum posts.",
    version="1.3.0",
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "error_msg": exc.message},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    messages = [
        f"{'.'.join(map(str, error['loc']))}: {error['msg']}"
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error_code": 1003, "error_msg": "; ".join(messages)},
    )


def retry_after_seconds(value: str | None) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError, AttributeError):
            seconds = 60
    return max(1, math.ceil(seconds)) if math.isfinite(seconds) else 60


def check_forum_cooldown(state):
    remaining = math.ceil(state.forum_retry_at - time.monotonic())
    if remaining > 0:
        raise APIError(
            503, 5006, f"论坛正文接口请求过于频繁，请在 {remaining} 秒后重试。",
            headers={"Retry-After": str(remaining)},
        )


async def fetch_record(state, thread_id: int, score: float):
    """Return (record, failed). Deleted posts are skipped without upstream errors."""
    url = f"{state.settings.forum_api_base_url}/threads/{thread_id}"
    try:
        async with state.fetch_slots:
            check_forum_cooldown(state)
            response = await state.http.get(url)
            if response.status_code == 429:
                delay = retry_after_seconds(response.headers.get("Retry-After"))
                state.forum_retry_at = max(state.forum_retry_at, time.monotonic() + delay)
                logger.warning("Forum API rate limited; pausing content requests for %s seconds.", delay)
                check_forum_cooldown(state)
        if response.status_code in (404, 410):
            return None, False
        response.raise_for_status()
        thread = response.json()
        if not isinstance(thread, dict):
            raise ValueError("Forum API must return an object.")
        content = thread.get("content")
        markdown = content.get("markdown") if isinstance(content, dict) else None
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Forum API returned no Markdown content.")
        user = thread.get("user") or {}
        record = Record(
            content=markdown,
            score=score,
            title=thread.get("title") or "Untitled",
            metadata={
                "thread_id": thread_id,
                "url": f"{state.settings.forum_base_url}/threads/{thread_id}",
                "author": user.get("name") if isinstance(user, dict) else None,
                "published_at": thread.get("published_at"),
            },
        )
        return record, False
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Failed to fetch thread %s: %s", thread_id, exc)
        return None, True


@app.get("/health")
async def health(request: Request):
    snapshot = await asyncio.to_thread(request.app.state.store.get_snapshot)
    return {
        "status": "degraded" if request.app.state.store.last_error else "ok",
        "indexed_threads": snapshot.index.ntotal,
    }


@app.post(
    "/retrieval", response_model=RetrievalResponse,
    responses={code: {"model": ErrorResponse} for code in (403, 404, 422, 502, 503)},
)
async def retrieval(request: RetrievalRequest, http_request: Request, authorization: str | None = Header(None)):
    state = http_request.app.state
    settings = state.settings
    parts = authorization.split() if authorization else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise APIError(403, 1001, "Expected Authorization: Bearer <API_KEY>.")
    if not hmac.compare_digest(parts[1].encode("utf-8"), settings.dify_api_key.encode("utf-8")):
        raise APIError(403, 1002, "Invalid API key.")
    if settings.knowledge_id and request.knowledge_id != settings.knowledge_id:
        raise APIError(404, 2001, "Knowledge base not found.")
    try:
        snapshot = await asyncio.to_thread(state.store.get_snapshot)
    except Exception as exc:
        logger.exception("Knowledge index is unavailable.")
        raise APIError(503, 5001, "Knowledge index is unavailable.") from exc
    if snapshot.index.ntotal == 0:
        return RetrievalResponse(records=[])
    check_forum_cooldown(state)

    try:
        response = await state.embeddings.embeddings.create(
            model=settings.embedding_model, input=request.query, dimensions=settings.embedding_dimension,
        )
        query = np.asarray([response.data[0].embedding], dtype=np.float32)
        if query.shape != (1, settings.embedding_dimension) or not np.isfinite(query).all():
            raise ValueError("Invalid query embedding.")
    except Exception as exc:
        logger.exception("Query embedding failed.")
        raise APIError(502, 5002, "Failed to vectorize query; check the embedding service.") from exc

    # Collect a larger candidate set before reranking; apply final score thresholds afterward.
    top_k = request.retrieval_setting.top_k
    candidate_limit = max(top_k, settings.rerank_candidates) if settings.rerank_enabled else top_k
    filtering = bool(request.metadata_condition and request.metadata_condition.conditions)
    try:
        candidates = await asyncio.to_thread(
            retrieve_candidates, snapshot, query, request.query, settings, candidate_limit, filtering,
        )
    except Exception as exc:
        logger.exception("Knowledge search failed.")
        raise APIError(503, 5004, "Knowledge index search failed.") from exc
    if not settings.rerank_enabled:
        candidates = [
            (thread_id, score) for thread_id, score in candidates
            if score >= request.retrieval_setting.score_threshold
        ]

    records, failures = [], 0
    batch_size = settings.fetch_concurrency
    for start in range(0, len(candidates), batch_size):
        results = await asyncio.gather(*(
            fetch_record(state, thread_id, score)
            for thread_id, score in candidates[start:start + batch_size]
        ))
        for record, failed in results:
            failures += int(failed)
            if record is not None and matches_metadata(record.metadata, request.metadata_condition):
                records.append(record)
        if len(records) >= candidate_limit:
            break
    if failures and not records:
        raise APIError(
            502, 5003,
            "Forum content could not be retrieved. Check the forum API, HTTPS certificate and server logs.",
        )
    records = records[:candidate_limit]
    if settings.rerank_enabled and records:
        try:
            records = await rerank_records(state.http, settings, request.query, records, top_k)
        except RerankInputError as exc:
            raise APIError(422, 1003, str(exc)) from exc
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            logger.warning("Reranking failed with model %s: %s", settings.rerank_model, exc)
            raise APIError(502, 5005, "Reranking failed; check the rerank model, endpoint and server logs.") from exc
        records = [record for record in records if record.score >= request.retrieval_setting.score_threshold]
    return RetrievalResponse(records=records[:top_k])
