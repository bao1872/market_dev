"""Pure-unit tests for the History Series Read Contract service.

These tests mock the persistence read-back (``list_scope_observation_facts``)
and the DB session, so they run under ``PURE_UNIT_TEST=1`` with no database.

Scope of this contract: read-only series assembly + availability metadata.
It must NOT compute percentile / velocity / acceleration / persistence / regime
/ signal — those are Analysis B/C responsibilities.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from app.models.market_review import ReviewScopeObservationFact
from app.services import review_observation_history_service as hs
from app.services.review_observation_history_service import (
    ScopeHistoryDateRangeError,
    ScopeHistoryNotActivatedError,
    get_observation_series,
)

pytestmark = pytest.mark.pure_unit


def _make_row(
    trade_date: date,
    readiness: str,
    payload: dict | None = None,
) -> ReviewScopeObservationFact:
    """Build a lightweight fake fact row (only fields the service reads)."""
    row = ReviewScopeObservationFact()
    row.trade_date = trade_date
    row.readiness = readiness
    row.observation_payload = payload if payload is not None else {"scope": {}}
    return row


async def _run_service(rows: list[ReviewScopeObservationFact], **kwargs):
    """Patch the persistence read-back and invoke the service."""
    db = AsyncMock()
    with patch.object(
        hs, "list_scope_observation_facts", AsyncMock(return_value=rows)
    ) as mock_list:
        result = await get_observation_series(db, **kwargs)
        # The service must delegate the actual query to the persistence layer
        # and must NOT compute anything beyond read-back + metadata.
        mock_list.assert_awaited_once()
    return result


# ---------------------------------------------------------------------------
# Activation + range guards
# ---------------------------------------------------------------------------


async def test_rejects_non_activated_scope_type():
    with pytest.raises(ScopeHistoryNotActivatedError):
        await _run_service(
            [],
            scope_type="market",
            scope_key="all_a_share",
            from_date=date(2026, 1, 1),
            to_date=date(2026, 3, 1),
        )


async def test_rejects_inverted_date_range():
    with pytest.raises(ScopeHistoryDateRangeError):
        await _run_service(
            [],
            scope_type="industry_l1",
            scope_key="sw01",
            from_date=date(2026, 3, 1),
            to_date=date(2026, 1, 1),
        )


# ---------------------------------------------------------------------------
# Series assembly + ordering
# ---------------------------------------------------------------------------


async def test_returns_ordered_series_and_metadata():
    # Persistence returns rows already ordered by trade_date ascending; the
    # service trusts that ordering and does NOT re-sort (no analytics here).
    rows = [
        _make_row(date(2026, 1, 1), "ready", {"scope": {"k": "a"}}),
        _make_row(date(2026, 1, 2), "ready", {"scope": {"k": "c"}}),
        _make_row(date(2026, 1, 3), "ready", {"scope": {"k": "b"}}),
    ]
    result = await _run_service(
        rows,
        scope_type="industry_l1",
        scope_key="sw01",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
    )
    series = result["series"]
    # Ordering is guaranteed ascending by trade_date.
    assert [s["trade_date"] for s in series] == [
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    ]
    # Payload is passed through untouched (no recompute / extract).
    assert series[0]["payload"] == {"scope": {"k": "a"}}
    assert all(s["readiness"] == "ready" for s in series)

    avail = result["availability"]
    assert avail["scope_type"] == "industry_l1"
    assert avail["scope_key"] == "sw01"
    assert avail["requested_from_date"] == "2026-01-01"
    assert avail["requested_to_date"] == "2026-01-31"
    assert avail["series_from_date"] == "2026-01-01"
    assert avail["series_to_date"] == "2026-01-03"
    assert avail["total_snapshots"] == 3
    assert avail["ready_snapshots"] == 3
    assert avail["partial_or_unavailable"] == 0
    assert avail["status"] == "ready"


async def test_empty_series_status():
    result = await _run_service(
        [],
        scope_type="industry_l1",
        scope_key="sw01",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
    )
    assert result["series"] == []
    avail = result["availability"]
    assert avail["total_snapshots"] == 0
    assert avail["status"] == "empty"
    assert avail["series_from_date"] is None
    assert avail["series_to_date"] is None


async def test_partial_status_counts_non_ready():
    rows = [
        _make_row(date(2026, 1, 1), "ready"),
        _make_row(date(2026, 1, 2), "no_members"),
        _make_row(date(2026, 1, 3), "unavailable"),
    ]
    result = await _run_service(
        rows,
        scope_type="industry_l2",
        scope_key="sw02",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
    )
    avail = result["availability"]
    assert avail["total_snapshots"] == 3
    assert avail["ready_snapshots"] == 1
    assert avail["partial_or_unavailable"] == 2
    assert avail["status"] == "partial"


# ---------------------------------------------------------------------------
# Boundary: no analytics computed here
# ---------------------------------------------------------------------------


async def test_service_does_not_recompute_payload():
    """The payload is stored as-is; the service must not mutate or extract."""
    original = {"price": {"return": {"mean": 0.012}}, "scope": {"x": 1}}
    rows = [_make_row(date(2026, 1, 1), "ready", original)]
    result = await _run_service(
        rows,
        scope_type="concept",
        scope_key="c01",
        from_date=date(2026, 1, 1),
        to_date=date(2026, 1, 31),
    )
    # Identity preserved (same object content, no field surgery).
    assert result["series"][0]["payload"] is original
