"""Shared configuration for the API and synchronization script."""
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_DIMENSION = 1024
MAX_TEXT_LENGTH = 8192


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
        )
        if not 0 < settings.http_timeout <= 120:
            raise ValueError("HTTP_TIMEOUT must be in (0, 120].")
        if not 1 <= settings.fetch_concurrency <= 32:
            raise ValueError("FETCH_CONCURRENCY must be between 1 and 32.")
        if not 0 <= settings.index_reload_interval <= 3600:
            raise ValueError("INDEX_RELOAD_INTERVAL must be between 0 and 3600.")
        for url in (settings.forum_api_base_url, settings.forum_base_url):
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.query or parsed.fragment:
                raise ValueError("Forum URLs must be HTTP(S) base URLs without query strings or fragments.")
        return settings
