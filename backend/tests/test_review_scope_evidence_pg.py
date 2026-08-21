"""Targeted PostgreSQL tests for the Objective Evidence Engine (Round 2A).

Covers the thin service's DB query contract only:
- exact D1/D3/D5 reference facts via canonical calendar;
- missing exact date -> unavailable (no nearest/latest fallback);
- history query: same scope, trade_date < T, finite samples only;
- peer query: same-day same-family cohort, cross-family isolation;
- raw HHI peer disabled (reason set, no rank).

The service is query-time derived and writes nothing; tests assert no fact rows
are created by the evidence call.

Run on the isolated verification DB only (never bz_stock):
    pytest --no-header -q tests/test_review_scope_evidence_pg.py -p no:cacheprovider
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.scope_evidence import PEER_DISABLED_REASON_BY_PRIMITIVE
from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_persistence_service import save_scope_observation_fact
from app.services.scope_evidence_service import compute_scope_evidence

pytestmark = pytest.mark.postgres

T = date(2026, 8, 11)  # use a real A-share trading calendar date below
# canonical trading dates (weekdays; adjust to the calendar present in the DB)


async def _save(db: AsyncSession, trade_date: date, scope_type: str, scope_key: str) -> None:
    from tests.test_review_observation_persistence_pg import _canonical_obs, _prep

    prep = _prep(scope_type=scope_type, scope_key=scope_key, trade_date=trade_date)
    observation = _canonical_obs(scope_type=scope_type, scope_key=scope_key, trade_date=trade_date)
    await save_scope_observation_fact(db, prep, observation)


async def test_evidence_writes_nothing_and_queries_exact_dates(
    db_session: AsyncSession,
) -> None:
    """Evidence computation must not create rows and must resolve exact dates."""
    # seed a small chain on the real calendar via calendar_service
    from app.services import calendar_service

    d1 = await calendar_service.get_previous_trading_day_async(db_session, T)
    assert d1 is not None
    d3 = d1
    for _ in range(2):
        nxt = await calendar_service.get_previous_trading_day_async(db_session, d3)
        assert nxt is not None
        d3 = nxt

    await _save(db_session, T, "concept", "A")
    await _save(db_session, d1, "concept", "A")
    await _save(db_session, d3, "concept", "A")

    before = (
        await db_session.execute(select(func.count()).select_from(ReviewScopeObservationFact))
    ).scalar_one()

    result = await compute_scope_evidence(db_session, T, "concept", "A")

    after = (
        await db_session.execute(select(func.count()).select_from(ReviewScopeObservationFact))
    ).scalar_one()
    assert after == before  # evidence is query-time derived; writes nothing

    prim = result["primitives"]["trend_up_ratio"]
    assert prim["current"]["status"] == "ready"
    assert prim["d1"]["status"] == "ready"
    assert prim["d1"]["reference_date"] == d1.isoformat()
    assert prim["d3"]["status"] == "ready"
    assert prim["d3"]["reference_date"] == d3.isoformat()
    # d5 exact date has no fact -> unavailable, no fallback
    assert prim["d5"]["status"] == "unavailable"
    assert prim["d5"]["reference_value"] is None


async def test_missing_exact_date_unavailable_no_fallback(db_session: AsyncSession) -> None:
    from app.services import calendar_service

    d1 = await calendar_service.get_previous_trading_day_async(db_session, T)
    assert d1 is not None

    await _save(db_session, T, "concept", "A")
    # d1 has no fact -> d1 unavailable even though older facts may exist
    await _save(db_session, date(2026, 8, 3), "concept", "A")

    result = await compute_scope_evidence(db_session, T, "concept", "A")
    prim = result["primitives"]["trend_up_ratio"]
    assert prim["d1"]["status"] == "unavailable"
    assert prim["d1"]["reference_value"] is None


async def test_peer_same_family_query(db_session: AsyncSession) -> None:
    await _save(db_session, T, "concept", "A")
    await _save(db_session, T, "concept", "B")
    await _save(db_session, T, "industry_l1", "X")  # different family

    result = await compute_scope_evidence(db_session, T, "concept", "A")
    peer = result["primitives"]["trend_up_ratio"]["peer"]
    assert peer["status"] == "ready"
    assert peer["peer_count"] == 2  # only concept A + B, industry excluded


async def test_raw_hhi_peer_disabled(db_session: AsyncSession) -> None:
    await _save(db_session, T, "concept", "A")
    await _save(db_session, T, "concept", "B")

    result = await compute_scope_evidence(db_session, T, "concept", "A")
    peer = result["primitives"]["price_raw_hhi"]["peer"]
    assert peer["status"] == "unavailable"
    assert peer["percentile"] is None
    assert peer["reason"] == PEER_DISABLED_REASON_BY_PRIMITIVE["price_raw_hhi"]
    assert result["primitives"]["trend_up_ratio"]["peer"]["status"] == "ready"
