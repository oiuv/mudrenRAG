import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from .config import EMBEDDING_DIMENSION, EMBEDDING_MODEL, Settings
from .index_store import IndexStore
from .metadata import matches_metadata
from .models import ErrorResponse, Record, RetrievalRequest, RetrievalResponse

logger = logging.getLogger(__name__)


class APIError(Exception):
    def __init__(self, status_code: int, error_code: int, message: str):
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    if not settings.dify_api_key or not settings.dashscope_api_key:
        raise RuntimeError("DIFY_API_KEY and DASHSCOPE_API_KEY must both be configured.")
    store = IndexStore(settings.data_dir, settings.index_reload_interval)
    await asyncio.to_thread(store.get_snapshot)
    if not settings.knowledge_id:
        logger.warning("KNOWLEDGE_ID is unset; accepting any knowledge_id for legacy compatibility.")
    async with AsyncOpenAI(
        api_key=settings.dashscope_api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
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
        yield


app = FastAPI(
    title="mudrenRAG Retrieval Service for Dify",
    description="External knowledge retrieval for forum posts.",
    version="1.1.0",
    lifespan=lifespan,
)


@app.exception_handler(APIError)
async def api_error_handler(request: Request, exc: APIError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": exc.error_code, "error_msg": exc.message},
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


async def fetch_record(state, thread_id: int, score: float):
    """Return (record, failed). Deleted posts are skipped without upstream errors."""
    url = f"{state.settings.forum_api_base_url}/threads/{thread_id}"
    try:
        async with state.fetch_slots:
            response = await state.http.get(url)
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

    try:
        response = await state.embeddings.embeddings.create(
            model=EMBEDDING_MODEL, input=request.query, dimensions=EMBEDDING_DIMENSION,
        )
        query = np.asarray([response.data[0].embedding], dtype=np.float32)
        if query.shape != (1, EMBEDDING_DIMENSION) or not np.isfinite(query).all():
            raise ValueError("Invalid query embedding.")
    except Exception as exc:
        logger.exception("Query embedding failed.")
        raise APIError(502, 5002, "Failed to vectorize query; check the embedding service.") from exc

    # Filtering must happen before limiting the final records to top_k.
    filtering = bool(request.metadata_condition and request.metadata_condition.conditions)
    count = snapshot.index.ntotal if filtering else min(request.retrieval_setting.top_k, snapshot.index.ntotal)
    try:
        distances, indices = await asyncio.to_thread(snapshot.index.search, query, count)
    except Exception as exc:
        logger.exception("Vector search failed.")
        raise APIError(503, 5004, "Knowledge index search failed.") from exc
    candidates = []
    for position, distance in zip(indices[0], distances[0]):
        if position < 0 or not np.isfinite(distance):
            continue
        score = 1.0 / (1.0 + max(0.0, float(distance)))
        if score < request.retrieval_setting.score_threshold:
            break
        candidates.append((snapshot.entries[int(position)]["thread_id"], score))

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
        if len(records) >= request.retrieval_setting.top_k:
            break
    if failures and not records:
        raise APIError(
            502, 5003,
            "Forum content could not be retrieved. Check the forum API, HTTPS certificate and server logs.",
        )
    return RetrievalResponse(records=records[:request.retrieval_setting.top_k])
