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
from app.services.review_observation_prep_service import (
    PreparedScope,
    ScopeReplaySpec,
    prepare_current_scope_observations_batch,
)

T = date(2026, 8, 11)
T1 = date(2026, 8, 10)


async def _prepare_one(
    session: object,
    scope_type: str,
    scope_key: str,
    trade_date: date,
) -> PreparedScope:
    """Single-scope convenience over the unique batch preparation owner.

    [REVIEW-EXECUTION-PATH-CONSOLIDATION] 测试通过唯一 canonical owner
    ``prepare_current_scope_observations_batch``（batch size = 1）进入，不再
    依赖已删除的 ``prepare_scope`` 单 scope 入口。
    """
    prepared = await prepare_current_scope_observations_batch(
        session,  # type: ignore[arg-type]
        trade_date,
        [
            ScopeReplaySpec(
                scope_type=scope_type,
                scope_key=scope_key,
                scope_name=scope_key,
                member_ids=(),
            )
        ],
    )
    return prepared[scope_key]


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
    continuous: dict | None = None,
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
        continuous=continuous if continuous is not None else {},
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
    # canonical rolling INCLUDES T in the 20D window.  2 priors + T (3 bars) =>
    # window = [10, 20, 30], mean = 20, ratio = 30/20 = 1.5.  Review must match the
    # canonical owner (no second formula).
    mo = build_member_observation(_raw(volume_t=30.0, amount_t=30.0))
    canon = _canonical_series_row([10.0, 20.0, 30.0], amts=[10.0, 20.0, 30.0])
    assert mo.vol_ratio20 == pytest.approx(canon.volume_ratio_20, abs=1e-9)
    assert mo.amt_ratio20 == pytest.approx(canon.amount_ratio_20, abs=1e-9)

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
        event_coverage_member_ids=None,
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
        event_coverage_member_ids=None,
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
        event_coverage_member_ids=None,
    )
    checks = check_observation_invariants(out)
    assert checks, "expected non-empty checks"
    assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]


# ---------------------------------------------------------------------------
# Service: batch preparation owner with mocked canonical batch loaders
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
    current_only=None,
) -> None:
    async def _fake_previous(session, ref_date):
        return t1

    async def _fake_resolve(session, scope_type, scope_key, *, trade_date):
        if resolve is None:
            return ([], scope_key)
        return resolve(scope_type, scope_key, trade_date)

    async def _fake_batch_calendar(session, trade_dates):
        return dict.fromkeys(trade_dates, t1)

    async def _fake_batch_states(session, ids, trade_dates, t1_by_date):
        out = {}
        if T in trade_dates:
            out[T] = states_t or {}
        if T1 in trade_dates or T1 in t1_by_date.values():
            out[T1] = states_t1 or {}
        return out

    def _series(facts):
        return prep_service._InstrumentBarSeries(
            facts=tuple(facts),
            dates=tuple(f.trade_date for f in facts),
        )

    async def _fake_batch_bars(session, ids, trade_dates):
        # Batch loader returns ONE series per instrument covering the whole window
        # [first-400d, last]; merge the per-date stubs and dedupe by trade_date.
        out = {}
        for iid in ids:
            merged: dict[date, object] = {}
            for f in (bar_facts or {}).get(iid, []):
                merged[f.trade_date] = f
            for f in (t1_bar_facts or {}).get(iid, []):
                merged[f.trade_date] = f
            if merged:
                series_facts = [merged[d] for d in sorted(merged)]
                out[iid] = _series(series_facts)
        return out

    async def _fake_batch_events(session, ids, trade_dates):
        return {}

    async def _fake_batch_coverage(session, ids, trade_dates):
        # ROUND-2.2B: default coverage unavailable in pure-unit (no RunItem lineage).
        return {}

    async def _fake_load_current_only(session, ids, trade_date):
        return current_only or {}

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        _fake_previous,
    )
    monkeypatch.setattr(
        "app.services.review_scope_service.resolve_scope_members", _fake_resolve,
    )
    monkeypatch.setattr(prep_service, "_load_batch_calendar", _fake_batch_calendar)
    monkeypatch.setattr(prep_service, "_load_batch_states", _fake_batch_states)
    monkeypatch.setattr(prep_service, "_load_batch_bars", _fake_batch_bars)
    monkeypatch.setattr(prep_service, "_load_batch_events", _fake_batch_events)
    monkeypatch.setattr(
        prep_service, "_load_batch_backfill_event_coverage", _fake_batch_coverage
    )
    monkeypatch.setattr(
        prep_service,
        "_load_current_only_snapshot_facts",
        _fake_load_current_only,
    )


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
        prep = await _prepare_one(_FakeSession(), "industry_l1", "k", T)
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
        return await _prepare_one(_FakeSession(), "market", "market", T)

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
        return await _prepare_one(_FakeSession(), "concept", "c", T)

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
        p1 = await _prepare_one(_FakeSession(), "industry_l1", "s", T)
        p2 = await _prepare_one(_FakeSession(), "industry_l1", "s", T)
        return p1, p2

    p1, p2 = asyncio.run(run())
    assert p1.pit_member_ids == p2.pit_member_ids
    assert p1.members == p2.members
    assert [m.member_id for m in p1.members] == [m.member_id for m in p2.members]


# ---------------------------------------------------------------------------
# Wave 1A — L1 §7.2-§7.6 data-contract closure at the prep boundary
# ---------------------------------------------------------------------------


def _canonical_series_row(vols, amts=None):
    """T row of the canonical First Pyramid VolumeContext owner for the given bars.

    The LAST bar is T; compute_volume_context_series uses a rolling window that
    EXCLUDES the current bar, so the last (T) row's window = the strict-prior bars.
    """
    import pandas as pd

    from app.services.volume_context import (
        compute_volume_context_series,
        extract_last_volume_context,
    )

    v = [float(x) for x in vols]
    a = [float(x) for x in amts] if amts is not None else [float("nan")] * len(v)
    df = pd.DataFrame({"volume": v, "amount": a})
    return extract_last_volume_context(compute_volume_context_series(df))


def _review_member_volume(vols, amts=None):
    """Review MemberObservation volume facts produced by build_member_observation."""
    if amts is None:
        amts = [100.0] * len(vols)
    raw = _raw(volume_t=vols[-1], volume_history=tuple(vols[:-1]),
              amount_t=amts[-1], amount_history=tuple(amts[:-1]))
    mo = build_member_observation(raw)
    return mo


# REVIEW-V23-A-CORRECTION-2: Volume SSOT parity contract.  Review T facts MUST be
# bit-identical to compute_volume_context_series(bars) T row.  No second rolling
# formula, no 19/20 or 199/200 one-bar drift, no divergent 0/negative handling.
def _assert_volume_parity(vols, amts=None, *, allow_200=False) -> None:
    canon = _canonical_series_row(vols, amts)
    mo = _review_member_volume(vols, amts)
    # readiness is inferred from whether the 20D/200D fields were produced.
    mo_ready20 = mo.vol_ratio20 is not None
    assert canon.readiness_20 == mo_ready20, "readiness_20 mismatch"
    if allow_200:
        mo_ready200 = mo.vol_ratio200 is not None
        assert canon.readiness_200 == mo_ready200, "readiness_200 mismatch"
        assert _approx(canon.volume_ratio_200, mo.vol_ratio200)
        assert _approx(canon.volume_percentile_200, mo.vol_pct200)
        assert _approx(canon.volume_zscore_200, mo.vol_zscore200)
    else:
        assert mo.vol_ratio200 is None
        assert mo.vol_pct200 is None
        assert mo.vol_zscore200 is None
    assert _approx(canon.volume_ratio_20, mo.vol_ratio20)
    assert _approx(canon.volume_percentile_20, mo.vol_pct20)
    assert _approx(canon.volume_zscore_20, mo.vol_zscore20)


def _approx(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return pytest.approx(a) == b


def test_volume_parity_19_bars() -> None:
    # 18 priors + T (19 bars) -> 20D window NOT satisfiable -> Review unavailable.
    # NOTE: canonical rolling INCLUDES the current bar in the window, so an N-bar
    # series satisfies the 20D window when N >= 20.  19 bars => 20D not satisfied.
    vols = [float(10 + i) for i in range(19)]
    canon = _canonical_series_row(vols)
    mo = _review_member_volume(vols)
    assert canon.readiness_20 is False
    assert mo.vol_ratio20 is None
    assert mo.vol_ratio200 is None


def test_volume_parity_20_bars() -> None:
    # 19 priors + T (20 bars) -> 20D window satisfied, 200D not.
    vols = [float(10 + i) for i in range(20)]
    _assert_volume_parity(vols, allow_200=False)


def test_volume_parity_21_bars() -> None:
    vols = [float(10 + i) for i in range(21)]
    _assert_volume_parity(vols, allow_200=False)


def test_volume_parity_199_bars() -> None:
    # 198 priors + T (199 bars) -> 200D window NOT satisfied (needs >=200 bars).
    vols = [float(10 + i) for i in range(199)]
    _assert_volume_parity(vols, allow_200=False)


def test_volume_parity_200_bars() -> None:
    # 199 priors + T (200 bars) -> 200D window satisfied.
    vols = [float(10 + i) for i in range(200)]
    _assert_volume_parity(vols, allow_200=True)


def test_volume_parity_201_bars() -> None:
    vols = [float(10 + i) for i in range(202)]
    _assert_volume_parity(vols, allow_200=True)


def test_volume_parity_zero_volume() -> None:
    # canonical rolling keeps 0-volume bars (no >0 filtering); Review must match.
    vols = [float(10 + i) for i in range(21)]
    vols[5] = 0.0
    vols[15] = 0.0
    _assert_volume_parity(vols, allow_200=False)


def test_volume_parity_constant_volume() -> None:
    vols = [20.0] * 21
    _assert_volume_parity(vols, allow_200=False)


def test_volume_parity_normal_varying() -> None:
    import math

    vols = [float(100 + 50 * math.sin(i / 3.0)) for i in range(201)]
    _assert_volume_parity(vols, allow_200=True)


def test_volume_parity_percentile_ratio_zscore_readiness() -> None:
    # explicit small set to lock percentile/ratio/zscore/readiness semantics.
    vols = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
            11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0,
            35.0]  # 20 priors + T(35)
    _assert_volume_parity(vols, allow_200=False)


def test_prep_carries_continuous_trend_facts() -> None:
    cont = {
        "regime_strength": 0.7,
        "dsa_dir_bars": 3.0,
        "dsa_vwap_dev_pct": -1.5,
        "segment_id": 2.0,
        "segment_direction": 1.0,
        "segment_bars": 8.0,
        "segment_change_pct": 2.5,
        "segment_slope": 0.4,
        "current_vs_prev_volume_mean_ratio": 1.1,
        "current_vs_prev_amount_mean_ratio": 1.2,
        "current_segment_volume_mean": 120.0,
        "prev_segment_amount_mean": 100.0,
        "active_internal_ob_count": 2.0,
        "active_swing_ob_count": 1.0,
        "volatility_phase": 4.0,
        "momentum_direction": 1.0,
        "momentum_change": 1.0,
        "sqzmom_delta": 0.5,
        "sqzmom_val": 1.0,
        "volume_ratio_20": 1.3,
        "volume_percentile_20": 55.0,
        "volume_zscore_20": 0.2,
        "available_bars": 250,
    }
    # structure_alignment_categorical 来自 raw flat_t（canonical categorical 字符串），
    # 非 numeric continuous cast。ROUND-2.1 GAP-L1-STRUCTURE-ALIGNMENT-KEY FIX：
    # 消费 canonical producer key ``fp_structure_alignment``（previous_state_to_flat
    # 输出的正式 key）；旧 key ``structure_alignment`` 永不与 producer 匹配，导致
    # member categorical 恒为 None。
    mo = build_member_observation(_raw(continuous=cont, flat_t={"fp_structure_alignment": "aligned"}))
    assert mo.regime_strength == pytest.approx(0.7)
    assert mo.dsa_dir_bars == pytest.approx(3.0)
    assert mo.dsa_vwap_dev_pct == pytest.approx(-1.5)
    assert mo.segment_bars == pytest.approx(8.0)
    assert mo.segment_change_pct == pytest.approx(2.5)
    assert mo.segment_slope == pytest.approx(0.4)
    assert mo.seg_vol_ratio == pytest.approx(1.1)
    assert mo.seg_amt_ratio == pytest.approx(1.2)
    assert mo.seg_vol_mean == pytest.approx(120.0)
    assert mo.seg_amt_mean_prev == pytest.approx(100.0)
    assert mo.structure_alignment_categorical == "aligned"
    assert mo.active_internal_ob_count == pytest.approx(2.0)
    assert mo.active_swing_ob_count == pytest.approx(1.0)
    assert mo.volatility_phase == pytest.approx(4.0)
    assert mo.momentum_direction_raw == pytest.approx(1.0)
    assert mo.momentum_change == pytest.approx(1.0)
    assert mo.sqzmom_delta == pytest.approx(0.5)
    assert mo.sqzmom_val == pytest.approx(1.0)


def test_prep_no_continuous_defaults_to_none() -> None:
    mo = build_member_observation(_raw())  # continuous={}
    assert mo.regime_strength is None
    assert mo.segment_bars is None
    assert mo.structure_alignment_categorical is None
    assert mo.volatility_phase is None


def test_prep_volume_200_requires_full_window() -> None:
    # 2026-08-13 CORRECTION: 200D fact 仅在完整 >=200 根 history 时产出。
    # 25 根 history (>=20 但 <200) -> 20D 可用，200D 必须 None。
    history = tuple(float(v) for v in range(10, 35))  # 10..34, 25 priors
    mo = build_member_observation(_raw(volume_t=50.0, volume_history=history))
    assert mo.vol_ratio20 is not None
    assert mo.vol_ratio200 is None  # 禁止 25D history 产生 200D fact
    assert mo.vol_pct200 is None
    assert mo.vol_zscore200 is None
    # >=200 根 history -> 200D 可用。  canonical rolling INCLUDES T in the window,
    # so the 200D ratio = T / mean(199 prior + T).  Compute the expected value from
    # the canonical owner directly (do NOT hand-derive the window).
    history200 = tuple(float(v) for v in range(10, 210))  # 200 priors
    mo2 = build_member_observation(_raw(volume_t=50.0, volume_history=history200))
    assert mo2.vol_ratio200 is not None
    canon200 = _canonical_series_row(
        [float(v) for v in history200] + [50.0]
    )
    assert mo2.vol_ratio200 == pytest.approx(canon200.volume_ratio_200, abs=1e-9)
    assert mo2.vol_pct200 is not None
    assert mo2.vol_zscore200 is not None
    assert mo2.vol_ratio20 is not None
    assert mo2.vol_pct20 is not None
    assert mo2.vol_zscore20 is not None


def test_event_type_normalization_compatibility() -> None:
    # 2026-08-13 CORRECTION: CHoCH 大小写是存储 artifact，非产品区分。Scope Core
    # 不应感知 "CHoCH" / "CHOCH" 差异——统一在 loader 边界 normalize。
    assert prep_service._normalize_event_type("CHoCH") == "CHoCH"
    assert prep_service._normalize_event_type("CHOCH") == "CHoCH"
    assert prep_service._normalize_event_type("choch") == "CHoCH"
    assert prep_service._normalize_event_type("BOS") == "BOS"
    assert prep_service._normalize_event_type("ob_entered") == "OB_ENTERED"
    assert prep_service._normalize_event_type("SQZ_RELEASE") == "SQZ_RELEASE"
    assert prep_service._normalize_event_type(None) == ""
    assert prep_service._normalize_event_type("") == ""


@pytest.mark.asyncio
async def test_loader_passes_internal_as_structure_level(monkeypatch) -> None:
    # 2026-08-13 CORRECTION: canonical event 的 ``internal`` 标志（Swing/Internal 独立
    # categorical 维度）必须在 loader 边界透传给 StructureEvent，不修改 producer。
    # [REVIEW-EXECUTION-PATH-CONSOLIDATION] 批次 loader ``_load_batch_events`` 是唯一
    # event loader；它经 ``_map_structure_event`` 透传 internal / level。
    from app.domain.review.scope_observation import StructureEvent, _aggregate_structure_events

    async def _fake_load(session, instrument_ids, trade_dates):
        # 模拟 loader 从 canonical FirstPyramidHistoryEvent row 构造 StructureEvent：
        #  - event_type 经 _normalize_event_type 归一（CHoCH 大小写）；
        #  - internal 标志从 payload 透传为 Structure Level 维度。
        return {
            T: [
                StructureEvent(
                    member_id="INST1",
                    event_type=prep_service._normalize_event_type("CHoCH"),
                    direction="Up",
                    level=12.34,  # price-level evidence 保留
                    internal=True,  # Swing/Internal 维度
                )
            ]
        }

    monkeypatch.setattr(prep_service, "_load_batch_events", _fake_load)

    events_by_date = await prep_service._load_batch_events(None, ["INST1"], [T])
    events = events_by_date[T]
    ev = events[0]
    assert isinstance(ev, StructureEvent)
    assert ev.internal is True  # Swing/Internal 维度透传
    assert ev.level == 12.34  # price-level evidence 保留，不参与聚合
    assert ev.event_type == "CHoCH"  # 大小写已 normalize

    # 内部事件聚合为 Internal；price level 不进入 cell key。
    # ROUND-2.2B: valid_event_members = PIT ∩ coverage = {INST1}.
    agg = _aggregate_structure_events(
        events=events, valid_event_members={"INST1"}
    )
    assert agg["status"] == "ready"
    cell = agg["cells"]["leveled"]["CHoCH_Up_Internal"]
    assert cell["member_count"] == 1
    assert "level" not in cell  # price level 已从聚合 key 移除
    assert cell["structure_level"] == "Internal"


# ---------------------------------------------------------------------------
# REVIEW-V23-A-CORRECTION-3 — END-TO-END volume boundary parity
# ---------------------------------------------------------------------------
# The previous round only compared the helper against the canonical owner using
# hand-built inputs (``history = vols[:-1]``, ``current = vols[-1]``).  That does
# NOT exercise how the real batch owner (``prepare_current_scope_observations_batch``)
# actually passes the series, so a production-path defect (T counted twice, or
# volume/amount desynchronised) could pass unit tests while being wrong end to end.
#
# These tests drive the REAL path:
#   _load_batch_bar_facts (mocked at the DB edge only)
#     -> prepare_current_scope_observations_batch (batch size = 1)
#       -> build_member_observation
#         -> canonical VolumeContext owner
# and assert exact parity against ``compute_volume_context_series`` computed over
# the SAME bars, whose last row is T.


def _volume_parity_expected(volumes: list[float]) -> dict[str, float | None]:
    """Canonical expectation for the T row, from the canonical owner."""
    import pandas as pd

    from app.services.volume_context import compute_volume_context_series

    series = compute_volume_context_series(pd.DataFrame({"volume": volumes}))
    row = series.iloc[-1]

    def _v(key: str) -> float | None:
        value = row[key]
        return None if pd.isna(value) else float(value)

    return {
        "ratio20": _v("volume_ratio_20"),
        "ratio200": _v("volume_ratio_200"),
        "pct20": _v("volume_percentile_20"),
        "pct200": _v("volume_percentile_200"),
        "z20": _v("volume_zscore_20"),
        "z200": _v("volume_zscore_200"),
    }


def _run_batch_prepare_with_bars(monkeypatch, bars: list[DailyBarFact]):
    """Drive the batch owner (size 1) with a member whose bar history is ``bars``."""
    import asyncio

    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 1, "sqzmom_val": 1.0}

    def resolve(scope_type, scope_key, trade_date):
        return ([id_a], "s")

    async def scenario():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t={id_a: state}, states_t1={id_a: state},
            # The real loader uses ``trade_date <= T``, so the T bar IS included.
            bar_facts={id_a: bars},
            t1_bar_facts={id_a: [b for b in bars if b.trade_date == T1]},
        )
        return await _prepare_one(_FakeSession(), "industry_l1", "k", T)

    prep = asyncio.run(scenario())
    assert len(prep.members) == 1
    return prep.members[0]


@pytest.mark.parametrize("n_bars", [19, 20, 21, 199, 200, 201])
def test_batch_prepare_volume_parity_end_to_end(monkeypatch, n_bars: int) -> None:
    """Real batch owner (size 1) must match the canonical owner exactly.

    Critically this also proves T is counted EXACTLY ONCE: if the batch owner
    passed a history that still contained T (while ``volume_t`` re-appended it),
    the effective window would be shifted and these medians would diverge.
    """
    id_a = uuid.uuid4()
    # Ascending bars ending at T; varying volumes so ratio/zscore/percentile differ.
    volumes = [100.0 + 7.0 * i for i in range(n_bars)]
    bars: list[DailyBarFact] = []
    for i, vol in enumerate(volumes):
        # Only the LAST bar is T; the one before it is T1.
        if i == n_bars - 1:
            d = T
        elif i == n_bars - 2:
            d = T1
        else:
            d = date(2026, 1, 1) + __import__("datetime").timedelta(days=i)
        bars.append(_bar(id_a, d, close=10.0, amount=vol * 10.0, volume=vol))

    member = _run_batch_prepare_with_bars(monkeypatch, bars)
    expected = _volume_parity_expected(volumes)

    assert member.volume_t == pytest.approx(volumes[-1])

    # 20D facts follow the canonical owner directly.
    for attr, key in (
        ("vol_ratio20", "ratio20"),
        ("vol_pct20", "pct20"),
        ("vol_zscore20", "z20"),
    ):
        actual = getattr(member, attr)
        if expected[key] is None:
            assert actual is None, f"{attr} must be None when window insufficient"
        else:
            assert actual == pytest.approx(expected[key]), attr

    # 200D facts additionally honour the readiness_200 gate kept from the previous
    # round: a short history must NOT emit 200D facts even though the canonical
    # percentile helper only needs 5 values.  readiness_200 is defined by
    # volume_ratio_200 being produced (i.e. a full 200-bar window).
    ready_200 = expected["ratio200"] is not None
    for attr, key in (
        ("vol_ratio200", "ratio200"),
        ("vol_pct200", "pct200"),
        ("vol_zscore200", "z200"),
    ):
        actual = getattr(member, attr)
        if not ready_200:
            assert actual is None, f"{attr} must be gated off without a 200D window"
        else:
            assert actual == pytest.approx(expected[key]), attr


def test_prepare_scope_volume_amount_stay_bar_aligned(monkeypatch) -> None:
    """A bar missing ``amount`` must NOT desynchronise the volume/amount series.

    Filtering volume and amount independently (``[b.volume for b in facts if
    b.volume is not None]`` / same for amount) silently drops different positions
    from each series, so index ``i`` no longer refers to the same trade_date and
    the amount facts get computed against a shifted window.  Building both series
    from the SAME prior bars keeps them aligned.
    """
    id_a = uuid.uuid4()
    n = 25
    volumes = [100.0 + 5.0 * i for i in range(n)]
    bars: list[DailyBarFact] = []
    for i, vol in enumerate(volumes):
        if i == n - 1:
            d = T
        elif i == n - 2:
            d = T1
        else:
            d = date(2026, 1, 1) + __import__("datetime").timedelta(days=i)
        # One mid-history bar has volume but NO amount -> the desync trigger.
        amount = None if i == 5 else vol * 10.0
        bars.append(_bar(id_a, d, close=10.0, amount=amount, volume=vol))

    member = _run_batch_prepare_with_bars(monkeypatch, bars)

    # Volume facts must still match the canonical owner over the full volume series:
    # the missing amount must not truncate or shift the volume series at all.
    expected = _volume_parity_expected(volumes)
    assert member.vol_ratio20 == pytest.approx(expected["ratio20"])
    assert member.vol_zscore20 == pytest.approx(expected["z20"])
    assert member.vol_pct20 == pytest.approx(expected["pct20"])
    assert member.volume_t == pytest.approx(volumes[-1])

    # Direct assertion on the prep boundary contract: both series are built from the
    # SAME strict-prior bars, so they have EQUAL length and index i refers to the
    # same trade_date in both.  The amount gap is carried in place (as a hole) rather
    # than shortening the amount series -- which is what used to shift every later
    # amount value onto the wrong trade_date.
    prior_bars = [b for b in bars if b.trade_date != T]
    raw = _raw(
        volume_history=tuple(b.volume for b in prior_bars),
        amount_history=tuple(b.amount for b in prior_bars),
        volume_t=volumes[-1],
    )
    assert len(raw.volume_history) == len(raw.amount_history)
    assert len(raw.volume_history) == len(prior_bars)
    # The hole sits at its own original index, not collapsed away.
    assert raw.amount_history[5] is None
    assert raw.volume_history[5] == pytest.approx(volumes[5])


# =============================================================================
# C1a contract tests — freeze the current-only loader call contract.
#
# These tests exist because the existing tests above monkeypatch
# ``_load_current_only_snapshot_facts`` wholesale (``_fake_load_current_only``)
# WITHOUT asserting what the third positional argument actually is. That mock is
# exactly what masked C1a: a list was being passed where a scalar ``date`` is
# required, failing only under the real PG adapter. The tests below spy on the
# real call boundary instead of replacing it, so the scalar-T contract is locked.
# =============================================================================


def _contract_current_only_fake(member_ids):
    """Build the same scoped shape ``_fake_load_current_only`` returns, but the
    loader below records the (session, ids, third_arg) triple so the call
    contract can be asserted."""

    async def _loader(session, ids, third_arg):
        # Record the exact third positional argument the production caller passes.
        _loader.last_call = (session, ids, third_arg)
        # Same ``dict[str, dict]`` shape the real loader returns and the existing
        # tests build via ``current_only={str(id): {...}}``.
        scoped: dict[str, dict] = {}
        for mid in member_ids:
            scoped[str(mid)] = {
                "bb_position": 0.5,
                "bb_width": 1.0,
                "release_volume_ratio": 2.5,
                "main_current_contract_position": 1,
                "main_force_net_position": 1,
                "main_current_contract_turnover_rate": 0.1,
                "main_force_turnover_rate": 0.1,
            }
        return scoped

    _loader.last_call = None  # type: ignore[attr-defined]
    return _loader


async def _contract_resolve(session, scope_type, scope_key, *, trade_date):
    # Same ``(list[uuid.UUID], str)`` shape as the real
    # ``review_scope_service.resolve_scope_members``.
    return [_CONTRACT_MEMBER_ID], scope_key


_CONTRACT_MEMBER_ID = uuid.uuid4()


def _install_contract_mocks(monkeypatch, current_only_loader):
    """Minimal pure-unit mock set for the C1a contract tests. The current-only
    loader is the REAL boundary we spy on; everything else is faked so no DB is
    touched."""
    async def _fake_previous(session, ref_date):
        return date(2026, 1, 1)

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async",
        _fake_previous,
    )
    monkeypatch.setattr(
        "app.services.review_scope_service.resolve_scope_members", _contract_resolve
    )
    monkeypatch.setattr(
        prep_service,
        "_load_current_only_snapshot_facts",
        current_only_loader,
    )
    async def _fake_empty(*a, **k):
        return {}

    monkeypatch.setattr(
        prep_service, "_load_batch_calendar", _fake_empty
    )
    monkeypatch.setattr(prep_service, "_load_batch_states", _fake_empty)
    monkeypatch.setattr(prep_service, "_load_batch_bars", _fake_empty)
    monkeypatch.setattr(
        prep_service, "_load_batch_events", _fake_empty
    )
    monkeypatch.setattr(
        prep_service,
        "_load_batch_backfill_event_coverage",
        _fake_empty,
    )


def _contract_spec():
    return ScopeReplaySpec(
        scope_type="industry_l3",
        scope_key="C1A_TEST",
        scope_name="C1a Test",
        member_ids=(),
    )


@pytest.mark.pure_unit
async def test_c1a_single_date_loader_called_with_scalar_t(monkeypatch):
    """T1: single-date preparation must call the current-only loader exactly once
    with the scalar ``trade_date`` (NOT a one-element list)."""
    loader = _contract_current_only_fake([_CONTRACT_MEMBER_ID])
    _install_contract_mocks(monkeypatch, loader)

    result = await prep_service.prepare_current_scope_observations_batch(
        session=None, trade_date=T, scope_specs=[_contract_spec()]
    )

    assert loader.last_call is not None, "current-only loader was never called"
    _session, _ids, third_arg = loader.last_call
    # Must be a scalar date, never a list.
    assert isinstance(third_arg, date)
    assert not isinstance(third_arg, list)
    assert third_arg == T
    assert third_arg != [T]
    # Single-date owner contract returns dict[str, PreparedScope].
    assert "C1A_TEST" in result
    assert result["C1A_TEST"].trade_date == T


@pytest.mark.pure_unit
async def test_c1a_leadership_multidate_still_scalar_t(monkeypatch):
    """T2: a ``[T-1, T]`` Leadership preparation must STILL call the current-only
    loader with scalar ``trade_date == T`` (NOT the multi-element
    ``trade_dates`` list)."""
    loader = _contract_current_only_fake([_CONTRACT_MEMBER_ID])
    _install_contract_mocks(monkeypatch, loader)

    result = await prep_service.prepare_current_scope_observations_batch(
        session=None,
        trade_date=T,
        scope_specs=[_contract_spec()],
        trade_dates=[T1, T],
    )

    assert loader.last_call is not None, "current-only loader was never called"
    _session, _ids, third_arg = loader.last_call
    assert isinstance(third_arg, date)
    assert not isinstance(third_arg, list)
    # Current-only facts are exact-T only; the loader must never receive the
    # multi-date window.
    assert third_arg == T
    assert third_arg != [T1, T]
    assert third_arg != [T - __import__("datetime").timedelta(days=1), T]


@pytest.mark.pure_unit
async def test_c1a_multidate_placement_t_minus_1_unavailable(monkeypatch):
    """T3: in a multi-date series, the T-1 PreparedScope must NOT carry any
    current-only facts (they are exact-T only), while the T PreparedScope must
    carry the supplied exact-T current-only values."""
    loader = _contract_current_only_fake([_CONTRACT_MEMBER_ID])
    _install_contract_mocks(monkeypatch, loader)

    result = await prep_service.prepare_current_scope_observations_batch(
        session=None,
        trade_date=T,
        scope_specs=[_contract_spec()],
        trade_dates=[T1, T],
    )

    scopes = result.get("C1A_TEST")
    assert scopes is not None and len(scopes) == 2
    # Ordered by effective_dates: [T1, T].
    scope_t1, scope_t = scopes[0], scopes[1]
    assert scope_t1.trade_date == T1
    assert scope_t.trade_date == T

    assert scope_t1.members, "T-1 scope should still have its membership"
    assert scope_t.members, "T scope should still have its membership"

    # T-1 current-only fields must be None (exact-T only, never backfilled).
    for member in scope_t1.members:
        assert member.release_volume_ratio is None
        assert member.bb_position is None

    # T carries the exact-T current-only values supplied by the loader.
    for member in scope_t.members:
        assert member.release_volume_ratio == pytest.approx(2.5)
        assert member.bb_position == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# REVIEW-V23-A-CORRECTION-3 — Current-only exact-T source wiring
# ---------------------------------------------------------------------------


def test_current_only_snapshot_fields_match_flatten_producer_keys() -> None:
    """The wiring must reference the producer's ACTUAL flat keys.

    A typo here would silently degrade every Current-only fact to unavailable
    (a false-negative that no aggregate assertion would notice), so the mapping is
    pinned against the real flatten producer.
    """
    from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS
    from app.services.review_observation_prep_service import (
        _CURRENT_ONLY_SNAPSHOT_FIELDS,
    )

    for attr, flat_key in _CURRENT_ONLY_SNAPSHOT_FIELDS.items():
        assert flat_key in FP_QUERY_FIELD_SPECS, (
            f"{attr} -> {flat_key} is not produced by first_pyramid_flatten"
        )

    # And every mapped attribute must exist on MemberObservation.
    import dataclasses

    from app.domain.review.scope_observation import MemberObservation

    observation_fields = {f.name for f in dataclasses.fields(MemberObservation)}
    for attr in _CURRENT_ONLY_SNAPSHOT_FIELDS:
        assert attr in observation_fields, attr


def test_current_only_facts_flow_into_member_observation(monkeypatch) -> None:
    """Exact-T snapshot facts must reach MemberObservation through the batch owner."""
    import asyncio

    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 1}

    def resolve(scope_type, scope_key, trade_date):
        return ([id_a], "s")

    async def scenario():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t={id_a: state}, states_t1={id_a: state},
            bar_facts={id_a: [_bar(id_a, T1, 9.0), _bar(id_a, T, 10.0)]},
            current_only={
                str(id_a): {
                    "release_volume_ratio": 2.5,
                    "momentum_volume_relation": "共振",
                    "bb_position": 0.75,
                    "bb_width": 0.12,
                    "vwap_ret_total": 3.5,
                    "trailing_top_pct": -2.0,
                    "trailing_bottom_pct": 8.0,
                }
            },
        )
        return await _prepare_one(_FakeSession(), "industry_l1", "k", T)

    member = asyncio.run(scenario()).members[0]
    assert member.release_volume_ratio == pytest.approx(2.5)
    assert member.momentum_volume_relation == "共振"
    assert member.bb_position == pytest.approx(0.75)
    assert member.bb_width == pytest.approx(0.12)
    assert member.vwap_ret_total == pytest.approx(3.5)
    assert member.trailing_top_pct == pytest.approx(-2.0)
    assert member.trailing_bottom_pct == pytest.approx(8.0)


def test_current_only_facts_absent_snapshot_yields_none(monkeypatch) -> None:
    """No exact-T snapshot -> None (unavailable).  NEVER a fallback value.

    This is the time-key guard: a member without an exact-T snapshot must not
    inherit a "latest"/T+1 value, which would be future leakage.
    """
    import asyncio

    id_a = uuid.uuid4()
    state = {"regime_value": 1, "swing_bias": 1, "internal_bias": 1}

    def resolve(scope_type, scope_key, trade_date):
        return ([id_a], "s")

    async def scenario():
        await _install_mocks(
            monkeypatch, resolve=resolve, t1=T1,
            states_t={id_a: state}, states_t1={id_a: state},
            bar_facts={id_a: [_bar(id_a, T1, 9.0), _bar(id_a, T, 10.0)]},
            current_only={},  # no consumable exact-T snapshot for this member
        )
        return await _prepare_one(_FakeSession(), "industry_l1", "k", T)

    member = asyncio.run(scenario()).members[0]
    assert member.release_volume_ratio is None
    assert member.momentum_volume_relation is None
    assert member.bb_position is None
    assert member.bb_width is None
    assert member.vwap_ret_total is None
    assert member.trailing_top_pct is None
    assert member.trailing_bottom_pct is None
