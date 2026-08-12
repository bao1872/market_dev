"""Modified-scope pure/unit tests for Canonical Observation Data Preparation (Round 1B).

Covers the pure mapping layer (``app.services.observation_prep``), the sanity
invariants, and the DB-aware preparation service (``review_observation_prep_service``)
with mocked canonical loaders.  No DB, no network, no CI.
"""
from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.member_fact import DailyBarFact
from app.domain.review.scope_observation import compute_scope_observation
from app.services import review_observation_prep_service as prep_service
from app.services.observation_prep import (
    RawMemberFacts,
    build_member_observation,
    check_observation_invariants,
    compute_exact_return,
)
from app.services.review_observation_prep_service import prepare_scope

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


def _flat(
    trend: str = "上行",
    swing: str = "上行",
    internal: str = "上行",
    momentum: str = "扩张",
) -> dict:
    return {
        "fp_trend_direction": trend,
        "fp_swing_direction": swing,
        "fp_internal_direction": internal,
        "fp_momentum_direction": momentum,
    }


def _raw(
    mid: str = "a",
    *,
    flat_t: dict | None = None,
    close_t: float | None = 10.0,
    close_t1: float | None = 9.0,
    amount_t: float | None = 100.0,
    volume_t: float | None = 30.0,
    volume_history: tuple[float, ...] = (10.0, 20.0, 30.0),
    amount_history: tuple[float, ...] = (10.0, 20.0, 30.0),
    flat_t1: dict | None = None,
) -> RawMemberFacts:
    return RawMemberFacts(
        member_id=mid,
        flat_t=flat_t if flat_t is not None else _flat(),
        close_t=close_t,
        close_t1=close_t1,
        amount_t=amount_t,
        volume_t=volume_t,
        volume_history=volume_history,
        amount_history=amount_history,
        flat_t1=flat_t1,
    )


# ---------------------------------------------------------------------------
# Pure: semantic adapter mapping (T and exact T-1 share one contract)
# ---------------------------------------------------------------------------


def test_semantic_mapping_current_states() -> None:
    mo = build_member_observation(
        _raw(flat_t=_flat("上行", "下行", "震荡", "扩张"))
    )
    assert mo.trend == Direction.UP
    assert mo.swing == Direction.DOWN
    assert mo.internal == Direction.SIDEWAYS
    assert mo.momentum == MomentumDirection.EXPANDING


def test_semantic_mapping_exact_t1_states() -> None:
    mo = build_member_observation(
        _raw(flat_t=_flat("上行", "上行", "上行", "扩张"),
             flat_t1=_flat("下行", "下行", "震荡", "收缩"))
    )
    assert mo.t1_trend == Direction.DOWN
    assert mo.t1_swing == Direction.DOWN
    assert mo.t1_internal == Direction.SIDEWAYS
    assert mo.t1_momentum == MomentumDirection.CONTRACTING


def test_neutral_and_flat_are_valid() -> None:
    mo = build_member_observation(_raw(flat_t=_flat("震荡", "震荡", "震荡", "平缓")))
    assert mo.trend == Direction.SIDEWAYS
    assert mo.swing == Direction.SIDEWAYS
    assert mo.internal == Direction.SIDEWAYS
    assert mo.momentum == MomentumDirection.FLAT


# ---------------------------------------------------------------------------
# Pure: exact canonical T-1
# ---------------------------------------------------------------------------


def test_exact_return() -> None:
    assert compute_exact_return(10.0, 9.0) == pytest.approx(10.0 / 9.0 - 1.0)
    assert compute_exact_return(None, 9.0) is None
    assert compute_exact_return(10.0, None) is None
    assert compute_exact_return(10.0, 0.0) is None


def test_missing_exact_t1_no_fallback() -> None:
    # close(T-1) missing -> return_1d None; never searches T-2/T-3.
    mo = build_member_observation(_raw(close_t=10.0, close_t1=None))
    assert mo.return_1d is None
    assert mo.t1_trend is None
    assert mo.t1_momentum is None


# ---------------------------------------------------------------------------
# Pure: candidate vs valid (two-layer semantics)
# ---------------------------------------------------------------------------


def test_price_candidate_from_close_t_only() -> None:
    # close(T) available but exact T-1 missing -> candidate, not valid.
    mo = build_member_observation(_raw(close_t=10.0, close_t1=None))
    assert mo.price_candidate is True
    assert mo.return_1d is None

    # no close(T) -> not a candidate at all.
    mo2 = build_member_observation(_raw(close_t=None))
    assert mo2.price_candidate is False
    assert mo2.return_1d is None


# ---------------------------------------------------------------------------
# Pure: amount independent universe
# ---------------------------------------------------------------------------


def test_amount_independent_universe() -> None:
    # zero amount is valid; None is unavailable.
    mo = build_member_observation(_raw(amount_t=0.0))
    assert mo.amount == 0.0
    mo2 = build_member_observation(_raw(amount_t=None))
    assert mo2.amount is None


def test_vol_amt_ratio20_shared_ssot() -> None:
    mo = build_member_observation(_raw(volume_t=30.0, amount_t=30.0))
    assert mo.vol_ratio20 == pytest.approx(2.0)  # 30 / mean(10,20)
    assert mo.amt_ratio20 == pytest.approx(2.0)

    # no history -> None
    mo2 = build_member_observation(_raw(volume_history=(), amount_history=()))
    assert mo2.vol_ratio20 is None
    assert mo2.amt_ratio20 is None


# ---------------------------------------------------------------------------
# Pure: added member excluded from transition (PIT(T) ∩ PIT(T-1))
# ---------------------------------------------------------------------------


def test_added_member_excluded_from_transition() -> None:
    a = build_member_observation(_raw("a", flat_t1=_flat("上行")))
    added = build_member_observation(
        _raw("b", flat_t=_flat("下行"), flat_t1=_flat("上行"))
    )
    out = compute_scope_observation(
        scope_type="industry_l1", scope_key="k", trade_date=T,
        pit_member_ids=["a", "b"],
        pit_member_ids_t1=["a"],  # b added at T -> not in T-1 membership
        members=[a, added],
    )
    assert out["scope"]["pit_member_count"] == 2
    # even though b has a T-1 state, it is excluded from the transition set.
    assert out["trend"]["transition"]["denominator"] == 1


def test_removed_member_not_in_provided() -> None:
    # b removed at T: present in T-1 set but not in PIT(T) -> not provided.
    a = build_member_observation(_raw("a", flat_t1=_flat("上行")))
    out = compute_scope_observation(
        scope_type="industry_l1", scope_key="k", trade_date=T,
        pit_member_ids=["a"],
        pit_member_ids_t1=["a", "b"],
        members=[a],
    )
    assert out["scope"]["provided_member_count"] == 1
    assert out["scope"]["pit_member_count"] == 1
    assert out["scope"]["pit_member_count_t1"] == 2


# ---------------------------------------------------------------------------
# Pure: invariant sanity checks over a real Core output
# ---------------------------------------------------------------------------


def test_invariant_checks_all_pass() -> None:
    members = [
        build_member_observation(_raw("a", close_t=10.0, close_t1=9.0, amount_t=100.0, flat_t1=_flat("上行"))),
        build_member_observation(_raw("b", close_t=8.0, close_t1=8.0, amount_t=0.0, flat_t=_flat("下行", "下行", "下行", "收缩"), flat_t1=_flat("震荡"))),
    ]
    out = compute_scope_observation(
        scope_type="industry_l1", scope_key="k", trade_date=T,
        pit_member_ids=["a", "b"], pit_member_ids_t1=["a", "b"], members=members,
    )
    checks = check_observation_invariants(out)
    assert checks, "expected non-empty checks"
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


# ---------------------------------------------------------------------------
# Service: prepare_scope with mocked canonical loaders
# ---------------------------------------------------------------------------


class _FakeSession:
    """Stand-in AsyncSession for service tests (no real DB access)."""


def _bar(inst: uuid.UUID, d: date, close: float, amount: float = 100.0,
         volume: float = 10.0) -> DailyBarFact:
    return DailyBarFact(
        trade_date=d, open=close, high=close, low=close,
        close=close, volume=volume, amount=amount,
    )


async def _install_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolve=None,
    t1: date = T1,
    states_t=None,
    states_t1=None,
    bar_facts=None,
    t1_bar_facts=None,
) -> None:
    async def _fake_previous(session, ref_date):
        return t1

    async def _fake_resolve(session, scope_type, scope_key, *, trade_date):
        if resolve is None:
            return ([], scope_key)
        return resolve(scope_type, scope_key, trade_date)

    async def _fake_load_states(session, ids, trade_date):
        if trade_date == T:
            return states_t or {}
        return states_t1 or {}

    async def _fake_load_bar_facts(session, ids, trade_date):
        if trade_date == T:
            return bar_facts or {}
        return t1_bar_facts or {}

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        _fake_previous,
    )
    monkeypatch.setattr(
        "app.services.review_scope_service.resolve_scope_members", _fake_resolve,
    )
    monkeypatch.setattr(prep_service, "_load_states", _fake_load_states)
    monkeypatch.setattr(prep_service, "_load_bar_facts", _fake_load_bar_facts)


def test_service_exact_t1_historical_pit_run(monkeypatch) -> None:
    import asyncio

    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    ids_t = [id_a, id_b]
    ids_t1 = [id_a]
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}

    def resolve(scope_type, scope_key, trade_date):
        if trade_date == T:
            return (ids_t, "电子")
        return (ids_t1, "电子")

    states_t = {id_a: state, id_b: state}
    states_t1 = {id_a: state}
    bar_facts = {
        id_a: [_bar(id_a, T, 10.0)],
        id_b: [_bar(id_b, T, 8.0)],
    }
    t1_bar_facts = {id_a: [_bar(id_a, T1, 9.0)]}

    async def scenario():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t=states_t, states_t1=states_t1,
            bar_facts=bar_facts, t1_bar_facts=t1_bar_facts,
        )
        prep = await prepare_scope(_FakeSession(), "industry_l1", "k", T)
        return prep

    prep = asyncio.run(scenario())
    assert prep.canonical_t1 == T1
    assert prep.pit_status_t == "historical_pit"
    assert prep.t1_membership_available is True
    assert set(prep.pit_member_ids) == {str(id_a), str(id_b)}
    assert set(prep.pit_member_ids_t1) == {str(id_a)}
    assert len(prep.members) == 2
    # b is an added member -> its exact T-1 state missing (not in states_t1).
    by_id = {m.member_id: m for m in prep.members}
    assert by_id[str(id_b)].t1_trend is None
    assert by_id[str(id_a)].t1_trend == Direction.UP
    assert by_id[str(id_a)].return_1d == pytest.approx(10.0 / 9.0 - 1.0)


def test_service_market_historical_guard_skips_shadow(monkeypatch) -> None:
    import asyncio

    id_a = uuid.uuid4()

    def resolve(scope_type, scope_key, trade_date):
        # resolve_scope_members("market") returns current active universe and
        # ignores trade_date — exactly the behavior the guard must reject.
        return ([id_a], "全市场")

    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}

    async def scenario():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t={id_a: state}, states_t1={id_a: state},
            bar_facts={id_a: [_bar(id_a, T, 10.0)]},
            t1_bar_facts={id_a: [_bar(id_a, T1, 9.0)]},
        )
        return await prepare_scope(_FakeSession(), "market", "market", T)

    prep = asyncio.run(scenario())
    # Historical Market shadow must NOT be computed from current active universe.
    assert prep.pit_status_t == "unavailable"
    assert prep.pit_member_ids == ()
    assert prep.members == ()
    assert prep.t1_membership_available is False
    assert any(
        "historical_market_membership_unresolved" in d for d in prep.diagnostics
    )


def test_service_pit_unavailable_industry(monkeypatch) -> None:
    import asyncio

    from app.services.review_scope_service import OptionalScopeUnavailableError

    async def fail_resolve(session, scope_type, scope_key, *, trade_date):
        raise OptionalScopeUnavailableError(
            reason="pit_membership_unavailable", scope_type=scope_type,
            scope_key=scope_key,
        )

    async def scenario():
        await _install_mocks(monkeypatch, resolve=None)
        monkeypatch.setattr(
            "app.services.review_scope_service.resolve_scope_members", fail_resolve,
        )
        return await prepare_scope(_FakeSession(), "concept", "c", T)

    prep = asyncio.run(scenario())
    assert prep.pit_status_t == "unavailable"
    assert prep.members == ()
    assert any("pit_unavailable_T" in d for d in prep.diagnostics)


def test_service_preparation_deterministic(monkeypatch) -> None:
    import asyncio

    id_a = uuid.uuid4()
    state = {"regime_value": -1, "swing_bias": -1, "internal_bias": -1, "sqzmom_val": -1.0}

    def resolve(scope_type, scope_key, trade_date):
        return ([id_a], "s")

    async def run():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t={id_a: state}, states_t1={id_a: state},
            bar_facts={id_a: [_bar(id_a, T, 10.0)]},
            t1_bar_facts={id_a: [_bar(id_a, T1, 9.0)]},
        )
        p1 = await prepare_scope(_FakeSession(), "industry_l1", "s", T)
        p2 = await prepare_scope(_FakeSession(), "industry_l1", "s", T)
        return p1, p2

    p1, p2 = asyncio.run(run())
    assert p1.pit_member_ids == p2.pit_member_ids
    assert p1.members == p2.members
    assert [m.member_id for m in p1.members] == [m.member_id for m in p2.members]
