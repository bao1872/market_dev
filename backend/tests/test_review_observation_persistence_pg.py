"""Targeted PostgreSQL tests for Canonical Observation Fact Persistence (Round 1C).

Covers: insert, idempotent update (row_count stays 1, payload replaced), date /
scope / family isolation, diagnostics+readiness round-trip, and legacy isolation
(no write to market_review_scope_snapshots).

Run on the isolated verification DB only (never bz_stock):
    pytest --no-header -q tests/test_review_observation_persistence_pg.py \
        -p no:cacheprovider
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import (
    MarketReviewScopeSnapshot,
    ReviewScopeObservationFact,
)
from app.services.review_observation_persistence_service import (
    get_scope_observation_fact,
    list_scope_observation_facts,
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import PreparedScope

pytestmark = pytest.mark.postgres

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


def _prep(
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    trade_date: date = T,
    diagnostics: tuple[str, ...] = ("ok",),
) -> PreparedScope:
    return PreparedScope(
        scope_type=scope_type,
        scope_key=scope_key,
        scope_name=scope_key,
        trade_date=trade_date,
        canonical_t1=T1,
        pit_member_ids=("m1", "m2"),
        pit_member_ids_t1=("m1",),
        members=("m1", "m2"),
        t1_membership_available=True,
        pit_status_t="historical_pit",
        pit_status_t1="historical_pit",
        diagnostics=diagnostics,
    )


def _obs(marker: str) -> dict:
    return {"scope": {"scope_type": "concept"}, "marker": marker}


async def _count(db: AsyncSession, scope_type: str, scope_key: str, trade_date: date) -> int:
    stmt = (
        select(func.count())
        .select_from(ReviewScopeObservationFact)
        .where(
            ReviewScopeObservationFact.trade_date == trade_date,
            ReviewScopeObservationFact.scope_type == scope_type,
            ReviewScopeObservationFact.scope_key == scope_key,
        )
    )
    return int((await db.execute(stmt)).scalar_one())


async def test_insert(db_session: AsyncSession) -> None:
    prep = _prep(scope_type="concept", scope_key="A")
    await save_scope_observation_fact(db_session, prep, _obs("v1"))
    await db_session.commit()

    fact = await get_scope_observation_fact(db_session, T, "concept", "A")
    assert fact is not None
    assert fact.observation_payload["marker"] == "v1"
    assert await _count(db_session, "concept", "A", T) == 1


async def test_idempotent_update_row_count_stays_one(db_session: AsyncSession) -> None:
    prep = _prep(scope_type="concept", scope_key="A")
    await save_scope_observation_fact(db_session, prep, _obs("v1"))
    await save_scope_observation_fact(db_session, prep, _obs("v2"))
    await db_session.commit()

    assert await _count(db_session, "concept", "A", T) == 1
    fact = await get_scope_observation_fact(db_session, T, "concept", "A")
    assert fact is not None
    assert fact.observation_payload["marker"] == "v2"


async def test_date_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T1), _obs("t1")
    )
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T), _obs("t")
    )
    await db_session.commit()

    # Updating T must not affect T-1.
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T), _obs("t-updated")
    )
    await db_session.commit()
    t1_fact = await get_scope_observation_fact(db_session, T1, "concept", "A")
    assert t1_fact is not None
    assert t1_fact.observation_payload["marker"] == "t1"


async def test_scope_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _obs("a"))
    await save_scope_observation_fact(db_session, _prep(scope_key="B"), _obs("b"))
    await db_session.commit()

    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _obs("a-updated"))
    await db_session.commit()
    b_fact = await get_scope_observation_fact(db_session, T, "concept", "B")
    assert b_fact is not None
    assert b_fact.observation_payload["marker"] == "b"


async def test_family_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A"), _obs("concept-a")
    )
    await save_scope_observation_fact(
        db_session, _prep(scope_type="industry_l1", scope_key="A"), _obs("industry-a")
    )
    await db_session.commit()

    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A"), _obs("concept-updated")
    )
    await db_session.commit()
    ind_fact = await get_scope_observation_fact(db_session, T, "industry_l1", "A")
    assert ind_fact is not None
    assert ind_fact.observation_payload["marker"] == "industry-a"


async def test_diagnostics_readiness_roundtrip(db_session: AsyncSession) -> None:
    diagnostics = ("pit_unavailable_T1:concept/A n/a", "note")
    await save_scope_observation_fact(
        db_session, _prep(scope_type="industry_l2", scope_key="A", diagnostics=diagnostics),
        _obs("x"),
    )
    await db_session.commit()
    fact = await get_scope_observation_fact(db_session, T, "industry_l2", "A")
    assert fact is not None
    assert fact.diagnostics == list(diagnostics)
    assert fact.readiness == "ready"
    assert fact.pit_status_t == "historical_pit"
    assert fact.pit_member_count == 2
    assert fact.provided_member_count == 2


async def test_legacy_scope_snapshots_not_written(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(db_session, _prep(scope_type="concept", scope_key="A"), _obs("v1"))
    await db_session.commit()
    legacy = (
        await db_session.execute(select(func.count()).select_from(MarketReviewScopeSnapshot))
    ).scalar_one()
    assert int(legacy) == 0


async def test_list_scope_observation_facts_filters(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _obs("a"))
    await save_scope_observation_fact(db_session, _prep(scope_key="B"), _obs("b"))
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="C", trade_date=T1), _obs("c")
    )
    await db_session.commit()

    rows = await list_scope_observation_facts(db_session, scope_type="concept")
    assert {r.scope_key for r in rows} == {"A", "B", "C"}
    rows_t = await list_scope_observation_facts(
        db_session, scope_type="concept", from_date=T, to_date=T
    )
    assert {r.scope_key for r in rows_t} == {"A", "B"}
