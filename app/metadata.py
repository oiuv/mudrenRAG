"""Evaluate Dify metadata conditions against returned record metadata."""
import math
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Condition, MetadataCondition


def parse_date(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def matches_condition(metadata: dict, condition: "Condition") -> bool:
    actual = metadata.get(condition.name)
    expected = condition.value
    operator = condition.comparison_operator
    empty = actual is None or actual == "" or actual == [] or actual == {}
    if operator == "empty":
        return empty
    if operator == "not empty":
        return not empty
    # Missing values never match negative comparisons; use "empty" explicitly.
    if actual is None:
        return False
    if operator in ("contains", "not contains"):
        if not isinstance(actual, (str, list)):
            return False
        result = expected in actual
        return result if operator == "contains" else not result
    if operator in ("start with", "end with"):
        if not isinstance(actual, str):
            return False
        return actual.startswith(expected) if operator == "start with" else actual.endswith(expected)
    if operator in ("is", "is not"):
        if isinstance(actual, bool) or not isinstance(actual, (str, int, float)):
            return False
        result = actual == expected
        return result if operator == "is" else not result
    if operator in ("in", "not in"):
        if not isinstance(actual, str):
            return False
        result = actual in expected
        return result if operator == "in" else not result
    if operator in ("before", "after"):
        left, right = parse_date(actual), parse_date(expected)
        if left is None or right is None:
            return False
        return left < right if operator == "before" else left > right
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    if not math.isfinite(actual):
        return False
    return {
        "=": lambda: actual == expected,
        "≠": lambda: actual != expected,
        ">": lambda: actual > expected,
        "<": lambda: actual < expected,
        "≥": lambda: actual >= expected,
        "≤": lambda: actual <= expected,
    }[operator]()


def matches_metadata(metadata: dict, condition: "MetadataCondition | None") -> bool:
    if condition is None or not condition.conditions:
        return True
    results = (matches_condition(metadata, item) for item in condition.conditions)
    return all(results) if condition.logical_operator == "and" else any(results)
