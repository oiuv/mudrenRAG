"""Shared configuration for the API and synchronization script."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_EMBEDDING_MODEL = "qwen3.7-text-embedding-flash"
DEFAULT_EMBEDDING_DIMENSION = 1024
DEFAULT_RERANK_MODEL = "qwen3.7-text-rerank"
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_RERANK_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
MAX_TEXT_LENGTH = 8192


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError(f"{name} must be true/false or 1/0.")
    return value in ("true", "1")


@dataclass(frozen=True)
class Settings:
    dify_api_key: str
    dashscope_api_key: str
    data_dir: Path
    knowledge_id: str = ""
    forum_api_base_url: str = "https://api.mud.ren"
    forum_base_url: str = "https://bbs.mud.ren"
    http_timeout: float = 15.0
    fetch_concurrency: int = 5
    index_reload_interval: float = 5.0
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_dimension: int = DEFAULT_EMBEDDING_DIMENSION
    embedding_base_url: str = DEFAULT_EMBEDDING_BASE_URL
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_api_url: str = DEFAULT_RERANK_API_URL
    rerank_enabled: bool = True
    rerank_candidates: int = 20
    rerank_timeout: float = 30.0
    bm25_enabled: bool = True
    bm25_candidates: int = 20
    vector_candidates: int = 20
    rrf_k: int = 60

    @classmethod
    def from_env(cls):
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        settings = cls(
            dify_api_key=os.getenv("DIFY_API_KEY", "").strip(),
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", "").strip(),
            data_dir=(PROJECT_ROOT / data_dir).resolve(),
            knowledge_id=os.getenv("KNOWLEDGE_ID", "").strip(),
            forum_api_base_url=os.getenv("FORUM_API_BASE_URL", "https://api.mud.ren").rstrip("/"),
            forum_base_url=os.getenv("FORUM_BASE_URL", "https://bbs.mud.ren").rstrip("/"),
            http_timeout=float(os.getenv("HTTP_TIMEOUT", "15")),
            fetch_concurrency=int(os.getenv("FETCH_CONCURRENCY", "5")),
            index_reload_interval=float(os.getenv("INDEX_RELOAD_INTERVAL", "5")),
            embedding_model=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip(),
            embedding_dimension=int(os.getenv("EMBEDDING_DIMENSION", str(DEFAULT_EMBEDDING_DIMENSION))),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL).rstrip("/"),
            rerank_model=os.getenv("RERANK_MODEL", DEFAULT_RERANK_MODEL).strip(),
            rerank_api_url=os.getenv("RERANK_API_URL", DEFAULT_RERANK_API_URL).rstrip("/"),
            rerank_enabled=env_bool("RERANK_ENABLED", True),
            rerank_candidates=int(os.getenv("RERANK_CANDIDATES", "20")),
            rerank_timeout=float(os.getenv("RERANK_TIMEOUT", "30")),
            bm25_enabled=env_bool("BM25_ENABLED", True),
            bm25_candidates=int(os.getenv("BM25_CANDIDATES", "20")),
            vector_candidates=int(os.getenv("VECTOR_CANDIDATES", "20")),
            rrf_k=int(os.getenv("RRF_K", "60")),
        )
        if not 0 < settings.http_timeout <= 120:
            raise ValueError("HTTP_TIMEOUT must be in (0, 120].")
        if not 1 <= settings.fetch_concurrency <= 32:
            raise ValueError("FETCH_CONCURRENCY must be between 1 and 32.")
        if not 0 <= settings.index_reload_interval <= 3600:
            raise ValueError("INDEX_RELOAD_INTERVAL must be between 0 and 3600.")
        if not settings.embedding_model or not settings.rerank_model:
            raise ValueError("EMBEDDING_MODEL and RERANK_MODEL must not be empty.")
        if not 1 <= settings.embedding_dimension <= 8192:
            raise ValueError("EMBEDDING_DIMENSION must be between 1 and 8192.")
        if settings.embedding_model == DEFAULT_EMBEDDING_MODEL and settings.embedding_dimension not in (256, 512, 768, 1024):
            raise ValueError("qwen3.7-text-embedding-flash supports dimensions 256, 512, 768 or 1024.")
        if not 1 <= settings.rerank_candidates <= 500:
            raise ValueError("RERANK_CANDIDATES must be between 1 and 500.")
        if not 0 < settings.rerank_timeout <= 120:
            raise ValueError("RERANK_TIMEOUT must be in (0, 120].")
        if not 1 <= settings.bm25_candidates <= 500:
            raise ValueError("BM25_CANDIDATES must be between 1 and 500.")
        if not 1 <= settings.vector_candidates <= 500:
            raise ValueError("VECTOR_CANDIDATES must be between 1 and 500.")
        if not 1 <= settings.rrf_k <= 1000:
            raise ValueError("RRF_K must be between 1 and 1000.")
        for url in (
            settings.forum_api_base_url, settings.forum_base_url,
            settings.embedding_base_url, settings.rerank_api_url,
        ):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("API URLs must use HTTP(S) without query strings or fragments.")
        return settings
