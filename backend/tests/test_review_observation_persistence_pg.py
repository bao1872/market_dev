"""Targeted PostgreSQL tests for Canonical Observation Fact Persistence (Round 1C).

Covers: insert, idempotent update (row_count stays 1, payload replaced), date /
scope / family isolation, diagnostics+readiness round-trip, legacy isolation
(no write to market_review_scope_snapshots), canonical payload contract
validation at the save path, and seeded legacy P/Q/U/C/V isolation.

All payload fixtures are produced by the real ``compute_scope_observation``
Core so they are legal Canonical Observation shapes (Round 1C correction §7).

Run on the isolated verification DB only (never bz_stock):
    pytest --no-header -q tests/test_review_observation_persistence_pg.py \
        -p no:cacheprovider
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.scope_observation import MemberObservation, compute_scope_observation
from app.models.market_review import (
    MarketReviewRun,
    MarketReviewScopeSnapshot,
    ReviewScopeObservationFact,
)
from app.services.review_observation_persistence_service import (
    ScopeObservationPayloadValidationError,
    get_scope_observation_fact,
    get_scope_observation_fact_by_run,
    list_scope_observation_facts,
    save_scope_observation_fact,
)
from app.services.review_observation_prep_service import PreparedScope

pytestmark = pytest.mark.postgres

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


def _canonical_obs(
    *,
    scope_type: str = "concept",
    scope_key: str = "A",
    trade_date: date = T,
    marker_mean: float = 0.01,
    event_coverage_member_ids: Iterable[str] | None = None,
) -> dict:
    """Legal Canonical Observation payload produced by the real Core.

    ``marker_mean`` lets tests distinguish two legal payloads (v1 vs v2) by a
    genuine computed fact (price return mean) without inventing extra keys.
    """
    members = [
        MemberObservation(
            member_id="m1",
            price_candidate=True,
            return_1d=marker_mean,
            amount=100.0,
            trend=Direction.UP,
            swing=Direction.SIDEWAYS,
            internal=Direction.DOWN,
            momentum=MomentumDirection.FLAT,
        ),
        MemberObservation(
            member_id="m2",
            price_candidate=True,
            return_1d=marker_mean + 0.02,
            amount=200.0,
            trend=Direction.DOWN,
            swing=Direction.UP,
            internal=Direction.UP,
            momentum=MomentumDirection.EXPANDING,
        ),
    ]
    return compute_scope_observation(
        scope_type=scope_type,
        scope_key=scope_key,
        trade_date=trade_date,
        pit_member_ids=["m1", "m2"],
        pit_member_ids_t1=["m1", "m2"],
        members=members,
        event_coverage_member_ids=event_coverage_member_ids,
    )


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
        event_coverage_member_ids=None,
    )


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


def _make_run(*, trade_date: date) -> MarketReviewRun:
    """最小合法 run 行，用于把 fact 绑定到真实 run（满足 review_run_id FK）。

    source_core_run_id / source_board_run_id 为无 FK 的 UUID 列，可任选。
    """
    return MarketReviewRun(
        id=uuid.uuid4(),
        trade_date=trade_date,
        source_core_run_id=uuid.uuid4(),
        source_board_run_id=uuid.uuid4(),
        algorithm_version="review-contract-test",
        filter_version="filters-test",
        baseline_window=120,
        status="created",
        expected_scope_count=0,
        succeeded_scope_count=0,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=Decimal("0.0"),
        metadata_json={},
    )


async def test_insert(db_session: AsyncSession) -> None:
    prep = _prep(scope_type="concept", scope_key="A")
    await save_scope_observation_fact(db_session, prep, _canonical_obs(marker_mean=0.01))
    await db_session.commit()

    fact = await get_scope_observation_fact(db_session, T, "concept", "A")
    assert fact is not None
    assert fact.observation_payload["price"]["return"]["mean"] == pytest.approx(0.02)
    assert await _count(db_session, "concept", "A", T) == 1


async def test_idempotent_update_row_count_stays_one(db_session: AsyncSession) -> None:
    """同 run 内重复 save 幂等更新（row_count 保持 1）——091 run-lineage 契约。

    091 把唯一约束从 (trade_date, scope_type, scope_key) 改为
    (review_run_id, trade_date, scope_type, scope_key)，幂等 grain 变为 per-run。
    运行时写入均带 review_run_id（非空），PG 唯一约束因此能去重：同一 run 内
    重复 save 更新同一行。review_run_id=NULL 仅用于兼容历史无 binding 行，
    按 migration 091 注释设计不去重（故幂等断言必须绑定真实 run）。
    """
    run = _make_run(trade_date=T)
    db_session.add(run)
    await db_session.flush()  # 先落一行 run（满足 fact 的 FK），拿到 run.id

    prep = _prep(scope_type="concept", scope_key="A")
    await save_scope_observation_fact(
        db_session, prep, _canonical_obs(marker_mean=0.01), review_run_id=run.id,
    )
    await save_scope_observation_fact(
        db_session, prep, _canonical_obs(marker_mean=0.05), review_run_id=run.id,
    )
    await db_session.commit()

    assert await _count(db_session, "concept", "A", T) == 1
    fact = await get_scope_observation_fact_by_run(db_session, run.id, T, "concept", "A")
    assert fact is not None
    assert fact.observation_payload["price"]["return"]["mean"] == pytest.approx(0.06)


async def test_date_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T1),
        _canonical_obs(trade_date=T1, marker_mean=0.01),
    )
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T),
        _canonical_obs(marker_mean=0.02),
    )
    await db_session.commit()

    # Updating T must not affect T-1.
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A", trade_date=T),
        _canonical_obs(marker_mean=0.03),
    )
    await db_session.commit()
    t1_fact = await get_scope_observation_fact(db_session, T1, "concept", "A")
    assert t1_fact is not None
    # T1 只写入过 marker_mean=0.01 → 计算 mean = 0.01+0.01 = 0.02；更新 T 不影响 T1。
    assert t1_fact.observation_payload["price"]["return"]["mean"] == pytest.approx(0.02)


async def test_scope_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _canonical_obs(scope_key="A", marker_mean=0.01))
    await save_scope_observation_fact(db_session, _prep(scope_key="B"), _canonical_obs(scope_key="B", marker_mean=0.02))
    await db_session.commit()

    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _canonical_obs(scope_key="A", marker_mean=0.03))
    await db_session.commit()
    b_fact = await get_scope_observation_fact(db_session, T, "concept", "B")
    assert b_fact is not None
    # B 只写入过 marker_mean=0.02 → 计算 mean = 0.02+0.01 = 0.03；更新 A 不影响 B。
    assert b_fact.observation_payload["price"]["return"]["mean"] == pytest.approx(0.03)


async def test_family_isolation(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A"), _canonical_obs(scope_type="concept", scope_key="A", marker_mean=0.01)
    )
    await save_scope_observation_fact(
        db_session, _prep(scope_type="industry_l1", scope_key="A"), _canonical_obs(scope_type="industry_l1", scope_key="A", marker_mean=0.02)
    )
    await db_session.commit()

    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="A"), _canonical_obs(scope_type="concept", scope_key="A", marker_mean=0.03)
    )
    await db_session.commit()
    ind_fact = await get_scope_observation_fact(db_session, T, "industry_l1", "A")
    assert ind_fact is not None
    # industry_l1/A 只写入过 marker_mean=0.02 → 计算 mean = 0.02+0.01 = 0.03；更新 concept/A 不影响。
    assert ind_fact.observation_payload["price"]["return"]["mean"] == pytest.approx(0.03)


async def test_diagnostics_readiness_roundtrip(db_session: AsyncSession) -> None:
    diagnostics = ("pit_unavailable_T1:concept/A n/a", "note")
    await save_scope_observation_fact(
        db_session, _prep(scope_type="industry_l2", scope_key="A", diagnostics=diagnostics),
        _canonical_obs(scope_type="industry_l2", scope_key="A"),
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
    await save_scope_observation_fact(db_session, _prep(scope_type="concept", scope_key="A"), _canonical_obs())
    await db_session.commit()
    legacy = (
        await db_session.execute(select(func.count()).select_from(MarketReviewScopeSnapshot))
    ).scalar_one()
    assert int(legacy) == 0


async def test_save_rejects_non_canonical_payload(db_session: AsyncSession) -> None:
    # Blocker #1: arbitrary / non-canonical payload must be rejected at save and
    # no row written.  (Session is committed at teardown; guard must fail first.)
    prep = _prep(scope_type="concept", scope_key="A")
    with pytest.raises(ScopeObservationPayloadValidationError):
        await save_scope_observation_fact(
            db_session, prep, {"scope": {"scope_type": "concept"}, "marker": "x"}
        )
    assert await _count(db_session, "concept", "A", T) == 0


async def test_save_rejects_scope_identity_mismatch(db_session: AsyncSession) -> None:
    # Blocker #3: prep=concept/A but payload scope=concept/B -> must not persist.
    prep = _prep(scope_type="concept", scope_key="A")
    obs = _canonical_obs(scope_type="concept", scope_key="B")
    with pytest.raises(ScopeObservationPayloadValidationError):
        await save_scope_observation_fact(db_session, prep, obs)
    assert await _count(db_session, "concept", "A", T) == 0


async def test_legal_partial_payload_persists(db_session: AsyncSession) -> None:
    # A legal partial axis (empty price universe) with full canonical structure
    # passes validation and persists; partialness is not an invariant failure.
    members = [
        MemberObservation(
            member_id="m1",
            price_candidate=False,
            return_1d=None,
            amount=100.0,
            trend=Direction.UP,
            swing=Direction.SIDEWAYS,
            internal=Direction.DOWN,
            momentum=MomentumDirection.FLAT,
        )
    ]
    partial = compute_scope_observation(
        scope_type="concept", scope_key="P", trade_date=T,
        pit_member_ids=["m1"], pit_member_ids_t1=["m1"], members=members,
        event_coverage_member_ids=None,
    )
    await save_scope_observation_fact(db_session, _prep(scope_key="P"), partial)
    await db_session.commit()
    fact = await get_scope_observation_fact(db_session, T, "concept", "P")
    assert fact is not None
    assert fact.observation_payload["price"]["valid_count"] == 0
    assert fact.readiness == "ready"


async def test_seeded_legacy_pqucv_unchanged(db_session: AsyncSession) -> None:
    """Round 1C correction §9: a pre-existing legacy P/Q/U/C/V snapshot must be
    completely unchanged after a canonical observation fact is saved.

    No Filter / Discovery / Publication is triggered; the canonical save only
    touches review_scope_observation_facts.
    """
    from sqlalchemy import text

    # Seed a legacy run + scope snapshot with distinct P/Q/U/C/V payloads.
    run_id = (await db_session.execute(
        text(
            "INSERT INTO market_review_runs "
            "(id, trade_date, source_core_run_id, source_board_run_id, algorithm_version, "
            " filter_version, baseline_window, status, expected_scope_count, "
            " succeeded_scope_count, failed_scope_count, signal_count, coverage_ratio, "
            " metadata_json) "
            "VALUES (gen_random_uuid(), :d, gen_random_uuid(), gen_random_uuid(), 'review-1.0.0', "
            " 'filters-1.0.0', 120, 'published', 1, 1, 0, 0, 1.0, '{}'::jsonb) "
            "RETURNING id"
        ),
        {"d": T},
    )).scalar_one()

    snap = MarketReviewScopeSnapshot(
        review_run_id=run_id,
        trade_date=T,
        scope_type="industry_l1",
        scope_key="LEGACY",
        scope_name="Legacy",
        eligible_count=10,
        ready_count=10,
        coverage_ratio=1.0,
        status="ready",
        p_payload={"value": 1.0, "status": "ready"},
        q_payload={"value": 2.0, "status": "ready"},
        u_payload={"value": 3.0, "status": "ready"},
        c_payload={"value": 4.0, "status": "ready"},
        v_payload={"value": 5.0, "status": "ready"},
    )
    db_session.add(snap)
    await db_session.commit()

    # Save a canonical observation fact (must NOT touch the legacy snapshot).
    await save_scope_observation_fact(db_session, _prep(scope_type="concept", scope_key="A"), _canonical_obs())
    await db_session.commit()

    # Re-read the legacy snapshot: all P/Q/U/C/V payloads must be unchanged.
    reloaded = (
        await db_session.execute(
            select(MarketReviewScopeSnapshot).where(
                MarketReviewScopeSnapshot.review_run_id == run_id
            )
        )
    ).scalar_one()
    assert reloaded.p_payload == {"value": 1.0, "status": "ready"}
    assert reloaded.q_payload == {"value": 2.0, "status": "ready"}
    assert reloaded.u_payload == {"value": 3.0, "status": "ready"}
    assert reloaded.c_payload == {"value": 4.0, "status": "ready"}
    assert reloaded.v_payload == {"value": 5.0, "status": "ready"}
    # The canonical fact row was written separately.
    assert await _count(db_session, "concept", "A", T) == 1


async def test_list_scope_observation_facts_filters(db_session: AsyncSession) -> None:
    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _canonical_obs(scope_key="A"))
    await save_scope_observation_fact(db_session, _prep(scope_key="B"), _canonical_obs(scope_key="B"))
    await save_scope_observation_fact(
        db_session, _prep(scope_type="concept", scope_key="C", trade_date=T1), _canonical_obs(scope_key="C", trade_date=T1)
    )
    await db_session.commit()

    rows = await list_scope_observation_facts(db_session, scope_type="concept")
    assert {r.scope_key for r in rows} == {"A", "B", "C"}
    rows_t = await list_scope_observation_facts(
        db_session, scope_type="concept", from_date=T, to_date=T
    )
    assert {r.scope_key for r in rows_t} == {"A", "B"}


async def test_persisted_payload_uses_price_amount_topology(db_session: AsyncSession) -> None:
    """New canonical writer (from this SHA) only writes price.amount shape."""
    await save_scope_observation_fact(db_session, _prep(scope_key="A"), _canonical_obs(marker_mean=0.01))
    await db_session.commit()
    fact = await get_scope_observation_fact(db_session, T, "concept", "A")
    assert fact is not None
    payload = fact.observation_payload
    assert "amount" not in payload
    assert "amount" in payload["price"]
    assert payload["price"]["amount"]["valid_count"] == 2
    assert payload["price"]["amount"]["total_amount"] == pytest.approx(300.0)
    amount_concentration = payload["price"]["amount"]["concentration"]
    # m1=100, m2=200 -> shares 1/3, 2/3 -> raw_hhi = 5/9, normalized = 1/9
    assert amount_concentration["raw_hhi"] == pytest.approx(5.0 / 9.0)
    assert amount_concentration["normalized_hhi"] == pytest.approx(1.0 / 9.0)


async def test_save_rejects_legacy_top_level_amount(db_session: AsyncSession) -> None:
    """A legacy top-level `amount` must be rejected at save (no silent fallback)."""
    prep = _prep(scope_type="concept", scope_key="A")
    obs = _canonical_obs(marker_mean=0.01)
    # simulate a legacy writer that emitted top-level amount
    obs["amount"] = obs["price"].pop("amount")
    with pytest.raises(ScopeObservationPayloadValidationError):
        await save_scope_observation_fact(db_session, prep, obs)
    assert await _count(db_session, "concept", "A", T) == 0
