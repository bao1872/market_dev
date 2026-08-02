"""Canonical table/filter operators and backward-compatible input aliases."""

from __future__ import annotations

from typing import Any

FILTER_OPERATOR_ALIASES: dict[str, str] = {
    "ne": "neq",
    "is_empty": "empty",
    "is_not_empty": "not_empty",
    "is_null": "empty",
    "is_not_null": "not_empty",
    "contains_any": "has_any",
    "contains_all": "has_all",
    "not_contains_any": "not_has_any",
}

CANONICAL_FILTER_OPERATORS: frozenset[str] = frozenset(
    {
        "contains",
        "not_contains",
        "eq",
        "neq",
        "gt",
        "gte",
        "lt",
        "lte",
        "between",
        "in",
        "not_in",
        "date_eq",
        "before",
        "after",
        "empty",
        "not_empty",
        "has_any",
        "has_all",
        "not_has_any",
    }
)


def canonicalize_filter_operator(operator: Any) -> Any:
    """Return the canonical name while leaving non-string values untouched."""
    if not isinstance(operator, str):
        return operator
    normalized = operator.strip()
    return FILTER_OPERATOR_ALIASES.get(normalized, normalized)


def canonicalize_filter_config(config: dict[str, Any]) -> dict[str, Any]:
    """Copy a preset config and canonicalize every filter operator."""
    normalized = dict(config)
    filters = config.get("filters")
    if not isinstance(filters, list):
        return normalized
    normalized["filters"] = [
        {**item, "op": canonicalize_filter_operator(item.get("op"))}
        if isinstance(item, dict)
        else item
        for item in filters
    ]
    return normalized
