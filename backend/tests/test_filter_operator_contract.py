from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.core.filter_operators import canonicalize_filter_config
from app.schemas.table_view_preset import (
    TableViewPresetCreate,
    TableViewPresetResponse,
)
from app.services.first_pyramid_flatten import parse_fp_filter


def test_legacy_operator_aliases_are_input_only() -> None:
    config = {
        "filters": [
            {"key": "a", "op": "ne", "value": 1},
            {"key": "b", "op": "is_empty", "value": ""},
            {"key": "c", "op": "is_not_empty", "value": ""},
            {"key": "d", "op": "contains_any", "value": "x,y"},
            {"key": "e", "op": "contains_all", "value": "x,y"},
        ]
    }
    normalized = canonicalize_filter_config(config)
    assert [item["op"] for item in normalized["filters"]] == [
        "neq",
        "empty",
        "not_empty",
        "has_any",
        "has_all",
    ]
    assert config["filters"][0]["op"] == "ne"


def test_preset_requests_and_responses_emit_canonical_operators() -> None:
    created = TableViewPresetCreate(
        table_id="market",
        name="legacy",
        config={"filters": [{"key": "fp_summary", "op": "ne", "value": "上行"}]},
    )
    assert created.config["filters"][0]["op"] == "neq"

    now = datetime.now(UTC)
    response = TableViewPresetResponse(
        id=uuid4(),
        user_id=uuid4(),
        table_id="market",
        strategy_key="dsa_selector",
        name="historical",
        config={"filters": [{"key": "fp_summary", "op": "is_empty", "value": ""}]},
        is_default=False,
        created_at=now,
        updated_at=now,
    )
    assert response.config["filters"][0]["op"] == "empty"


def test_first_pyramid_query_accepts_old_alias_and_returns_canonical() -> None:
    specs = parse_fp_filter("fp_trend_bars:ne:5;fp_summary:is_empty:")
    assert [spec.operator for spec in specs] == ["neq", "empty"]
