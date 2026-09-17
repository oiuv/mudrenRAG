import pytest
from pydantic import ValidationError

from app.metadata import matches_metadata
from app.models import Condition, MetadataCondition, Record, RetrievalRequest


@pytest.mark.parametrize(("actual", "operator", "expected", "result"), [
    ("hello world", "contains", "world", True),
    ("hello world", "not contains", "other", True),
    ("hello world", "start with", "hello", True),
    ("hello world", "end with", "world", True),
    ("alice", "is", "alice", True),
    ("alice", "is not", "bob", True),
    ("alice", "in", ["alice", "bob"], True),
    ("alice", "not in", ["bob"], True),
    (None, "empty", None, True),
    ("", "empty", None, True),
    ([], "empty", None, True),
    ("alice", "not empty", None, True),
    (3, "=", 3, True),
    (3, "≠", 4, True),
    (3, ">", 2, True),
    (3, "<", 4, True),
    (3, "≥", 3, True),
    (3, "≤", 3, True),
    ("2026-01-01T00:00:00Z", "before", "2026-01-02", True),
    ("2026-01-02T08:00:00+08:00", "after", "2026-01-01T23:00:00Z", True),
    ("2026-01-02T08:00:00+08:00", "before", "2026-01-02T00:00:00Z", False),
    ("not a date", "after", "2026-01-01", False),
    (None, "is not", "alice", False),
    ("3", ">", 2, False),
    (True, "=", 1, False),
    (12, "contains", "1", False),
    ("ALICE", "is", "alice", False),
    (float("nan"), ">", 2, False),
])
def test_operators(actual, operator, expected, result):
    condition = MetadataCondition(conditions=[Condition(name="field", comparison_operator=operator, value=expected)])
    assert matches_metadata({"field": actual}, condition) is result


def test_logical_operators_and_empty_conditions():
    filters = [
        Condition(name="author", comparison_operator="is", value="alice"),
        Condition(name="thread_id", comparison_operator=">", value=100),
    ]
    metadata = {"author": "alice", "thread_id": 1}
    assert not matches_metadata(metadata, MetadataCondition(conditions=filters))
    assert matches_metadata(metadata, MetadataCondition(logical_operator="or", conditions=filters))
    assert matches_metadata(metadata, MetadataCondition(logical_operator="or", conditions=[]))


@pytest.mark.parametrize("fields", [
    {"name": ["author"], "comparison_operator": "is", "value": "alice"},
    {"name": "author", "comparison_operator": "unknown", "value": "alice"},
    {"name": "id", "comparison_operator": ">", "value": "3"},
    {"name": "id", "comparison_operator": ">", "value": True},
    {"name": "id", "comparison_operator": ">", "value": float("nan")},
    {"name": "id", "comparison_operator": ">", "value": float("inf")},
    {"name": "author", "comparison_operator": "in", "value": "alice"},
    {"name": "author", "comparison_operator": "in", "value": [1]},
    {"name": "author", "comparison_operator": "is"},
    {"name": "published_at", "comparison_operator": "before", "value": "yesterday"},
])
def test_invalid_conditions_are_rejected(fields):
    with pytest.raises(ValidationError):
        Condition(**fields)


@pytest.mark.parametrize(("key", "value"), [
    ("top_k", 0), ("top_k", -1), ("top_k", 101), ("top_k", True),
    ("top_k", 1.5), ("score_threshold", -0.1), ("score_threshold", 1.1),
    ("score_threshold", float("nan")),
])
def test_invalid_retrieval_settings(key, value):
    settings = {"top_k": 3, "score_threshold": 0.0, key: value}
    with pytest.raises(ValidationError):
        RetrievalRequest(knowledge_id="forum", query="question", retrieval_setting=settings)


@pytest.mark.parametrize("query", ["", "   ", "x" * 8193])
def test_invalid_query(query):
    with pytest.raises(ValidationError):
        RetrievalRequest(knowledge_id="forum", query=query, retrieval_setting={"top_k": 1, "score_threshold": 0})


def test_record_metadata_is_never_null():
    assert Record(content="text", title="title", score=0.5).metadata == {}
    with pytest.raises(ValidationError):
        Record(content="text", title="title", score=0.5, metadata=None)
