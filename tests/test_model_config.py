import pytest

from app.config import (
    DEFAULT_EMBEDDING_MODEL, DEFAULT_RERANK_MODEL, Settings,
)

MODEL_ENV = (
    "EMBEDDING_MODEL", "EMBEDDING_DIMENSION", "EMBEDDING_BASE_URL",
    "RERANK_MODEL", "RERANK_API_URL", "RERANK_ENABLED", "RERANK_CANDIDATES", "RERANK_TIMEOUT",
    "BM25_ENABLED", "BM25_CANDIDATES", "VECTOR_CANDIDATES", "RRF_K",
)


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for key in MODEL_ENV:
        monkeypatch.delenv(key, raising=False)


def test_default_models_and_rerank_enabled():
    settings = Settings.from_env()
    assert settings.embedding_model == "qwen3.7-text-embedding-flash" == DEFAULT_EMBEDDING_MODEL
    assert settings.embedding_dimension == 1024
    assert settings.rerank_model == "qwen3.7-text-rerank" == DEFAULT_RERANK_MODEL
    assert settings.rerank_enabled
    assert settings.rerank_candidates == 20


def test_model_configuration_is_read_from_environment(monkeypatch):
    overrides = {
        "EMBEDDING_MODEL": "custom-embedding",
        "EMBEDDING_DIMENSION": "512",
        "EMBEDDING_BASE_URL": "https://embedding.example/v1/",
        "RERANK_MODEL": "custom-rerank",
        "RERANK_API_URL": "https://rerank.example/api",
        "RERANK_ENABLED": "false",
        "RERANK_CANDIDATES": "40",
        "RERANK_TIMEOUT": "45",
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env()
    assert settings.embedding_model == "custom-embedding"
    assert settings.embedding_dimension == 512
    assert settings.embedding_base_url == "https://embedding.example/v1"
    assert settings.rerank_model == "custom-rerank"
    assert settings.rerank_api_url == "https://rerank.example/api"
    assert not settings.rerank_enabled
    assert settings.rerank_candidates == 40
    assert settings.rerank_timeout == 45


@pytest.mark.parametrize(("name", "value"), [
    ("EMBEDDING_MODEL", ""), ("RERANK_MODEL", " "),
    ("EMBEDDING_DIMENSION", "0"), ("EMBEDDING_DIMENSION", "2048"),
    ("RERANK_ENABLED", "maybe"), ("RERANK_CANDIDATES", "0"),
    ("RERANK_CANDIDATES", "501"), ("RERANK_TIMEOUT", "nan"),
    ("EMBEDDING_BASE_URL", "file:///model"), ("RERANK_API_URL", "not-a-url"),
])
def test_invalid_model_settings(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_hybrid_retrieval_defaults():
    settings = Settings.from_env()
    assert settings.bm25_enabled
    assert settings.bm25_candidates == 20
    assert settings.vector_candidates == 20
    assert settings.rrf_k == 60


@pytest.mark.parametrize(("name", "value"), [
    ("BM25_ENABLED", "maybe"), ("BM25_CANDIDATES", "0"),
    ("BM25_CANDIDATES", "501"), ("VECTOR_CANDIDATES", "0"),
    ("VECTOR_CANDIDATES", "501"), ("RRF_K", "0"), ("RRF_K", "1001"),
])
def test_invalid_hybrid_settings(name, value, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Settings.from_env()
