"""Vectorized historical reconstruction facts tests (Review v2.3 optimization).

Verifies the vectorized batch optimization contract:

- Vectorized VolumeContext (numpy) is canonical-equivalent to the pandas SSOT
  (:func:`compute_volume_context_series`) for the 20D/200D rolling fields,
  including missing/None mask semantics (Tests 4-6).
- The batch replay's vectorized member construction matches the canonical
  per-date ``build_member_observation`` field-by-field (float tolerance =
  project convention ``abs=1e-9``, None exact), covering scenarios A-J.
- Batch ``PreparedScope`` (vectorized path) equals the per-date path for the
  common >=200 finite-volume-bar window and stays byte-identical on the
  window-bound fallback (Tests 7-8).
- Membership is resolved exactly once (Test 10), no BoardMembershipHistory is
  introduced (Test 11), repeated execution is deterministic (Test 12).
- Tests 1/2/3 (bulk query count, T-1 mapping, window slicing) and Test 9
  (current-only loader never called) are already covered by
  ``test_review_batch_scope_preparation.py``.

No DB, no network.  All DB-touching helpers are mocked; batch loaders are
monkeypatched with canned canonical data.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from app.domain.review.member_fact import DailyBarFact
from app.domain.review.scope_observation import MemberObservation, StructureEvent
from app.services.review_observation_prep_service import (
    _BAR_LOOKBACK_DAYS,
    _build_member_observations,
    _InstrumentBarSeries,
    _precompute_vectorized_volume,
    prepare_scope_from_member_ids,
    prepare_scope_series_from_member_ids,
)
from app.services.volume_context import (
    compute_volume_context_series,
    compute_volume_context_vectorized,
    extract_volume_context_at,
    vectorized_context_at,
)

pytestmark = pytest.mark.pure_unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bar(inst, d, close, volume=10.0, amount=100.0) -> DailyBarFact:
    return DailyBarFact(
        trade_date=d,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        amount=amount,
    )


def _state(regime: int = 1) -> dict:
    return {"regime_value": regime, "swing_bias": 1, "internal_bias": 0, "sqzmom_val": 1.0}


def _trading_days(start: date, n: int) -> list[date]:
    """The first ``n`` weekdays (calendar snapshot of a trading-day series)."""
    days: list[date] = []
    d = start
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


# MemberObservation fields whose values are floats (None exact; float within the
# project tolerance — the vectorized numpy owner differs from pandas only in the
# last few ULPs, ~1e-15 relative, far inside abs=1e-9).
_FLOAT_FIELDS = [
    "return_1d",
    "amount",
    "vol_ratio20",
    "amt_ratio20",
    "volume_t",
    "vol_ratio200",
    "vol_pct20",
    "vol_pct200",
    "vol_zscore20",
    "vol_zscore200",
    "regime_strength",
    "dsa_dir_bars",
    "dsa_vwap_dev_pct",
    "segment_id",
    "segment_direction",
    "segment_bars",
    "segment_change_pct",
    "segment_slope",
    "seg_vol_ratio",
    "seg_amt_ratio",
    "seg_vol_mean",
    "seg_amt_mean_prev",
    "active_internal_ob_count",
    "active_swing_ob_count",
    "release_volume_ratio",
    "bb_position",
    "bb_width",
    "vwap_ret_total",
    "trailing_top_pct",
    "trailing_bottom_pct",
    "momentum_change",
    "sqzmom_delta",
    "sqzmom_val",
]


def _assert_member_equiv(a: MemberObservation, b: MemberObservation) -> None:
    assert a.member_id == b.member_id
    assert a.price_candidate == b.price_candidate
    # Categorical / enum fields: exact equality (both paths pass through the same
    # canonical semantic adapter; only numeric volume facts are vectorized).
    for f in (
        "trend",
        "swing",
        "internal",
        "momentum",
        "t1_trend",
        "t1_swing",
        "t1_internal",
        "t1_momentum",
        "volatility_phase",
        "momentum_direction_raw",
        "momentum_volume_relation",
        "structure_alignment_categorical",
    ):
        assert getattr(a, f) == getattr(b, f), f"field {f}: {getattr(a, f)!r} vs {getattr(b, f)!r}"
    for f in _FLOAT_FIELDS:
        va, vb = getattr(a, f), getattr(b, f)
        if va is None or vb is None:
            assert va is None and vb is None, f"field {f} None mismatch: {va} vs {vb}"
        else:
            assert va == pytest.approx(vb, abs=1e-9), f"field {f}: {va} vs {vb}"


def _to_series(facts: list[DailyBarFact]) -> _InstrumentBarSeries:
    facts = sorted(facts, key=lambda b: b.trade_date)
    return _InstrumentBarSeries(
        facts=tuple(facts),
        dates=tuple(b.trade_date for b in facts),
    )


def _build_members(
    bars: dict[uuid.UUID, list[DailyBarFact]],
    t: date,
    t1: date | None,
    states: dict[date, dict[uuid.UUID, dict]],
    *,
    vectorized: bool,
) -> list[MemberObservation]:
    """Run the shared member-construction owner with (or without) the precomputed
    vectorized volume context.  ``vectorized=False`` is exactly the per-date
    oracle path (canonical ``build_member_observation``)."""
    bars_series = {i: _to_series(facts) for i, facts in bars.items()}
    vec_volume = (
        _precompute_vectorized_volume(bars_series)
        if vectorized
        else None
    )
    return _build_member_observations(
        list(bars.keys()),
        trade_date=t,
        t1=t1,
        states_t=states.get(t, {}),
        states_t1=states.get(t1, {}) if t1 else {},
        bars=bars_series,
        current_only_facts={},
        vec_volume=vec_volume,
    )


# ---------------------------------------------------------------------------
# Test 4 / 5 / 6 — vectorized VolumeContext == canonical SSOT
# ---------------------------------------------------------------------------


def _assert_series_equiv(vols) -> None:
    vols_arr = np.asarray(vols, dtype=float)
    canon = compute_volume_context_series(pd.DataFrame({"volume": vols_arr}))
    vc = compute_volume_context_vectorized(vols_arr)
    for i in range(len(vols_arr)):
        c = extract_volume_context_at(canon, i)
        v = vectorized_context_at(vc, i)
        assert c is not None and v is not None
        for f in (
            "volume_ma_20",
            "volume_ma_200",
            "volume_ratio_20",
            "volume_ratio_200",
            "volume_zscore_20",
            "volume_zscore_200",
            "volume_percentile_20",
            "volume_percentile_200",
        ):
            a, b = getattr(c, f), getattr(v, f)
            if a is None or b is None:
                assert a is None and b is None, (i, f, a, b)
            else:
                assert a == pytest.approx(b, abs=1e-9), (i, f, a, b)
        assert c.readiness == v.readiness, (i, c.readiness, v.readiness)
        assert c.readiness_20 == v.readiness_20, (i, c.readiness_20, v.readiness_20)
        assert c.readiness_200 == v.readiness_200, (i, c.readiness_200, v.readiness_200)


def test_vectorized_rolling20_matches_canonical() -> None:
    """Test 4: vectorized rolling20 (MA/ratio/z-score/percentile) == old calc."""
    rng = random.Random(7)
    vols = [rng.uniform(1e4, 5e5) for _ in range(250)]
    _assert_series_equiv(vols)


def test_vectorized_rolling200_matches_canonical() -> None:
    """Test 5: vectorized rolling200 == old calculation (incl. boundary n<=200)."""
    for n in (50, 199, 200, 201, 250, 300):
        rng = random.Random(n)
        vols = [rng.uniform(1e4, 5e5) for _ in range(n)]
        _assert_series_equiv(vols)


def test_vectorized_missing_none_mask_semantics() -> None:
    """Test 6: missing/None mask semantics — NaN volume -> NaN row -> None at the
    canonical boundary; window never filled -> readiness False (never NaN->0)."""
    rng = random.Random(3)
    vols = [rng.uniform(1e4, 5e5) for _ in range(250)]
    for idx in (5, 60, 150):
        vols[idx] = float("nan")  # missing volume bar (defensive; real data is None)
    _assert_series_equiv(vols)

    # All-NaN window -> every numeric fact unavailable (None), not zero.
    vc = compute_volume_context_vectorized(np.asarray([float("nan")] * 5))
    row = vectorized_context_at(vc, 4)
    assert row is not None
    assert row.volume_ratio_20 is None
    assert row.volume_ratio_200 is None
    assert row.volume_zscore_20 is None
    assert row.volume_percentile_20 is None
    assert row.readiness_20 is False
    assert row.readiness_200 is False
    assert row.readiness is False

    # Constant volume -> std=0 -> z-score unavailable (mirrors canonical /0 guard).
    vc2 = compute_volume_context_vectorized(np.asarray([100.0] * 250))
    row2 = vectorized_context_at(vc2, 249)
    assert row2 is not None
    assert row2.volume_zscore_20 is None
    assert row2.volume_ratio_20 == pytest.approx(1.0)
    # Overall readiness derives from the MA rows being non-NaN (canonical parity):
    # a satisfied all-zero/constant window is readiness=True even when ratios are
    # degenerate, while per-window readiness_20 stays True here (ratio=1.0 finite).
    assert row2.readiness is True
    assert row2.readiness_20 is True


# ---------------------------------------------------------------------------
# Test 7 / scenarios A-J — vectorized member facts == canonical owner
# ---------------------------------------------------------------------------


def test_vectorized_member_facts_match_canonical() -> None:
    """Test 7 + scenario A (normal member): >= 200 finite-volume bars -> vectorized
    path produces the same MemberObservation as the canonical per-date owner."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    bars = {inst: [_bar(inst, d, float(i + 2), volume=float(1000 + i)) for i, d in enumerate(days)]}
    _member_for_days(bars, days)


def _member_for_days(bars_by_inst, days, states=None):
    """Build the canonical+vectorized members for the last two days (T / T-1) and
    assert every provided member is canonical-equivalent."""
    inst_ids = list(bars_by_inst)
    t, t1 = days[-1], days[-2]
    st = states or {d: {i: _state(1) for i in inst_ids} for d in days}
    canon = _build_members(bars_by_inst, t, t1, st, vectorized=False)
    vec = _build_members(bars_by_inst, t, t1, st, vectorized=True)
    assert len(canon) == len(vec)
    for c, v in zip(canon, vec, strict=True):
        _assert_member_equiv(c, v)
    return canon, vec


def test_scenario_b_insufficient_history() -> None:
    """Scenario B: 30 finite-volume bars (20 <= w < 200) -> vectorized path is now
    engaged (per-window gate) and stays canonical-equivalent."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 6, 1), 30)  # only 30 bars
    bars = {inst: [_bar(inst, d, float(i + 2), volume=float(1000 + i)) for i, d in enumerate(days)]}
    _member_for_days(bars, days)


def test_short_history_vectorized_engaged_equiv(monkeypatch) -> None:
    """Short-history member (20 <= w < 200 finite-volume bars — the real-data
    case where the old full-200 gate never engaged): the per-window gate routes it
    through the vectorized path (canonical fallback NOT called) and the resulting
    MemberObservation is canonical-equivalent.  20D facts computed, 200D facts
    unavailable in BOTH paths (no 200-bar window exists to drift on)."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 150)  # 150 finite-volume bars
    bars = {inst: [_bar(inst, d, float(i + 2), volume=float(1000 + i)) for i, d in enumerate(days)]}

    calls = {"canonical_fallback": 0}
    original = _build_member_observations.__globals__["build_member_observation"]

    def counting(raw):
        calls["canonical_fallback"] += 1
        return original(raw)

    monkeypatch.setattr(
        "app.services.review_observation_prep_service.build_member_observation", counting
    )

    # Vectorized path: 150 bars -> w=150 >= SHORT_WINDOW and hi=149 < 199 -> engaged,
    # canonical per-date fallback must NOT be called.
    vec = _build_members(bars, days[-1], days[-2], {d: {inst: _state(1)} for d in days},
                         vectorized=True)
    assert calls["canonical_fallback"] == 0, (
        "short-history member must be served by the vectorized path, not the "
        "canonical per-date fallback"
    )
    # 20D window satisfied -> ratio20 present; 200D window not satisfiable -> None.
    assert vec[0].vol_ratio20 is not None
    assert vec[0].vol_ratio200 is None
    assert vec[0].vol_pct20 is not None
    assert vec[0].vol_pct200 is None

    # Oracle (canonical per-date) still uses the fallback exactly once.
    canon = _build_members(bars, days[-1], days[-2], {d: {inst: _state(1)} for d in days},
                           vectorized=False)
    assert calls["canonical_fallback"] == 1
    _assert_member_equiv(canon[0], vec[0])


def test_scenario_c_missing_bar_mid_series() -> None:
    """Scenario C: a mid-series bar with missing volume -> excluded from both
    compact arrays; vectorized indexing stays aligned with the canonical one."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    bars = {inst: [_bar(inst, d, float(i + 2), volume=float(1000 + i)) for i, d in enumerate(days)]}
    for i in (40, 120, 180):
        bars[inst][i] = _bar(inst, bars[inst][i].trade_date, float(i + 2), volume=None)
    _member_for_days(bars, days)


def test_scenario_d_t_no_state() -> None:
    """Scenario D (ROUND-2 GAP-L1-MEMBER-GATE): a member with bars but no state
    at T is STILL a PIT member -> included in both paths (identical), with its
    state-derived facts unavailable but price facts preserved."""
    inst_a, inst_b = uuid.uuid4(), uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    t, t1 = days[-1], days[-2]
    bars = {
        inst_a: [_bar(inst_a, d, 2.0, volume=1000.0) for d in days],
        inst_b: [_bar(inst_b, d, 3.0, volume=2000.0) for d in days],
    }
    states = {d: {inst_a: _state(1), inst_b: _state(1)} for d in days}
    states[t] = {inst_a: _state(1)}  # inst_b has NO state at T
    canon = _build_members(bars, t, t1, states, vectorized=False)
    vec = _build_members(bars, t, t1, states, vectorized=True)
    # inst_b is still included (it is a PIT member); both paths identical.
    assert [m.member_id for m in canon] == [str(inst_a), str(inst_b)]
    assert [m.member_id for m in vec] == [str(inst_a), str(inst_b)]
    _assert_member_equiv(canon[0], vec[0])
    # inst_b carries bars-driven price facts; its state-derived facts are None.
    assert canon[1].price_candidate is True
    assert canon[1].trend is None


def test_scenario_e_t1_no_state() -> None:
    """Scenario E: T-1 state missing -> T-1 categorical facts None in both paths."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    t, t1 = days[-1], days[-2]
    bars = {inst: [_bar(inst, d, 2.0, volume=1000.0) for d in days]}
    states = {d: {inst: _state(1)} for d in days}
    states[t1] = {}  # no T-1 state
    canon = _build_members(bars, t, t1, states, vectorized=False)
    vec = _build_members(bars, t, t1, states, vectorized=True)
    assert len(canon) == 1 and len(vec) == 1
    assert canon[0].t1_trend is None and vec[0].t1_trend is None
    _assert_member_equiv(canon[0], vec[0])


def test_scenario_f_volume_amount_none_at_t() -> None:
    """Scenario F: current-bar volume/amount None -> volume_t/amount None, context
    indexes the last finite-volume prior bar in both paths."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    t, t1 = days[-1], days[-2]
    bars = {inst: [_bar(inst, d, 2.0, volume=1000.0, amount=5000.0) for d in days]}
    bars[inst][-1] = _bar(inst, t, 2.0, volume=None, amount=None)
    canon = _build_members(bars, t, t1, {d: {inst: _state(1)} for d in days}, vectorized=False)
    vec = _build_members(bars, t, t1, {d: {inst: _state(1)} for d in days}, vectorized=True)
    assert canon[0].volume_t is None and vec[0].volume_t is None
    assert canon[0].amount is None and vec[0].amount is None
    _assert_member_equiv(canon[0], vec[0])


def test_scenario_g_zero_volume() -> None:
    """Scenario G: zero-volume bars are valid bars (included), not missing."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    bars = {inst: [_bar(inst, d, 2.0, volume=0.0) for d in days]}
    bars[inst][0] = _bar(inst, days[0], 2.0, volume=1000.0)
    _member_for_days(bars, days)


def test_scenario_i_non_finite_values() -> None:
    """Scenario I: non-finite volume treated as missing (mirrors canonical _finite)."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    bars = {inst: [_bar(inst, d, 2.0, volume=float(1000 + i)) for i, d in enumerate(days)]}
    bars[inst][50] = _bar(inst, days[50], 2.0, volume=float("nan"))
    bars[inst][51] = _bar(inst, days[51], 2.0, volume=float("inf"))
    _member_for_days(bars, days)


def test_scenario_j_date_window_boundary() -> None:
    """Scenario J: bars strictly before [T-400d, T] are excluded; a window with no
    in-window bars yields unavailable volume facts in both paths."""
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    t = days[-1]
    t1 = days[-2]
    # Bars placed strictly BEFORE the 400d lookback of T -> no in-window bar.
    early_days = _trading_days(t - timedelta(days=_BAR_LOOKBACK_DAYS + 60), 10)
    assert all(d < t - timedelta(days=_BAR_LOOKBACK_DAYS) for d in early_days)
    bars = {inst: [_bar(inst, d, 2.0, volume=1000.0) for d in early_days]}
    states = {d: {inst: _state(1)} for d in (t, t1)}
    canon = _build_members(bars, t, t1, states, vectorized=False)
    vec = _build_members(bars, t, t1, states, vectorized=True)
    assert len(canon) == 1 and len(vec) == 1
    assert canon[0].vol_ratio20 is None and vec[0].vol_ratio20 is None
    assert canon[0].vol_ratio200 is None and vec[0].vol_ratio200 is None
    _assert_member_equiv(canon[0], vec[0])


# ---------------------------------------------------------------------------
# Test 8 — batch PreparedScope (vectorized path) == per-date PreparedScope
# ---------------------------------------------------------------------------


def test_batch_vectorized_path_matches_per_date(monkeypatch) -> None:
    """Test 8: batch replay with the vectorized volume context produces a
    canonical-equivalent PreparedScope for every T as the per-date path."""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    dates = [days[-2], days[-1]]
    member_ids = [id_a, id_b]

    all_bars = {
        id_a: [_bar(id_a, d, float(i + 2), volume=float(1000 + i), amount=float(5000 + i))
               for i, d in enumerate(days)],
        id_b: [_bar(id_b, d, float(3 + i), volume=float(2000 + i), amount=float(8000 + i))
               for i, d in enumerate(days)],
    }
    states = {d: {id_a: _state(1), id_b: _state(1)} for d in days}
    events = {
        dates[1]: [
            StructureEvent(
                member_id=str(id_a), event_type="BOS", direction="bullish",
                level=1.0, internal=False, release_volume_ratio=None,
            )
        ],
        dates[0]: [],
    }

    # ---- BATCH path (one bulk read each) ----
    async def fake_calendar(session, trade_dates):
        return {t: days[days.index(t) - 1] if days.index(t) > 0 else None
                for t in trade_dates}

    async def fake_batch_states(session, instrument_ids, trade_dates, t1_by_date):
        all_dates = set(trade_dates) | {d for d in t1_by_date.values() if d is not None}
        return {d: states.get(d, {}) for d in all_dates}

    async def fake_batch_bars(session, instrument_ids, trade_dates):
        return {i: _to_series(facts) for i, facts in all_bars.items()}

    async def fake_batch_events(session, instrument_ids, trade_dates):
        return {d: events.get(d, []) for d in trade_dates}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_batch_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_batch_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_batch_events
    )

    # ---- PER-DATE path (canonical oracle) ----
    async def fake_prev(session, ref_date):
        return days[days.index(ref_date) - 1] if days.index(ref_date) > 0 else None

    async def fake_load_states(session, instrument_ids, trade_date):
        return states.get(trade_date, {})

    async def fake_load_bar_facts(session, instrument_ids, trade_date):
        lo = trade_date - timedelta(days=_BAR_LOOKBACK_DAYS)
        return {i: [b for b in facts if lo <= b.trade_date <= trade_date]
                for i, facts in all_bars.items()}

    async def fake_load_events(session, instrument_ids, trade_date):
        return events.get(trade_date, [])

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async", fake_prev
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_states", fake_load_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_bar_facts", fake_load_bar_facts
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_structure_events",
        fake_load_events,
    )

    async def scenario():
        batch = await prepare_scope_series_from_member_ids(
            _FakeSession(), "industry_l3", "k", "s", dates, member_ids,
            load_current_only=False,
        )
        per_date = [
            await prepare_scope_from_member_ids(
                _FakeSession(), "industry_l3", "k", "s", t, member_ids,
                load_current_only=False,
            )
            for t in dates
        ]
        return batch, per_date

    batch, per_date = asyncio.run(scenario())

    assert [p.trade_date for p in batch] == dates
    assert len(batch) == len(per_date)
    for b, p in zip(batch, per_date, strict=True):
        assert b.scope_type == p.scope_type
        assert b.scope_key == p.scope_key
        assert b.trade_date == p.trade_date
        assert b.canonical_t1 == p.canonical_t1
        assert b.pit_member_ids == p.pit_member_ids
        assert b.pit_member_ids_t1 == p.pit_member_ids_t1
        assert b.t1_membership_available == p.t1_membership_available
        assert b.pit_status_t == p.pit_status_t
        assert b.pit_status_t1 == p.pit_status_t1
        assert b.diagnostics == p.diagnostics
        # Events (scenario H) propagate identically.
        assert b.events == p.events
        # Members: canonical-equivalent (vectorized floats within project tolerance).
        assert len(b.members) == len(p.members)
        for bm, pm in zip(b.members, p.members, strict=True):
            _assert_member_equiv(bm, pm)


def test_batch_fallback_path_byte_identical(monkeypatch) -> None:
    """Window-bound fallback (< SHORT_WINDOW finite-volume bars) is byte-identical
    to the per-date path (both sides use the canonical owner)."""
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    days = _trading_days(date(2026, 6, 1), 15)  # 15 bars -> w < 20 -> fallback everywhere
    dates = [days[-1]]
    member_ids = [id_a, id_b]
    all_bars = {
        id_a: [_bar(id_a, d, float(i + 2), volume=float(1000 + i)) for i, d in enumerate(days)],
        id_b: [_bar(id_b, d, float(3 + i), volume=float(2000 + i)) for i, d in enumerate(days)],
    }
    states = {d: {id_a: _state(1), id_b: _state(1)} for d in days}

    async def fake_calendar(session, trade_dates):
        return {t: days[days.index(t) - 1] if days.index(t) > 0 else None
                for t in trade_dates}

    async def fake_batch_states(session, instrument_ids, trade_dates, t1_by_date):
        all_dates = set(trade_dates) | {d for d in t1_by_date.values() if d is not None}
        return {d: states.get(d, {}) for d in all_dates}

    async def fake_batch_bars(session, instrument_ids, trade_dates):
        return {i: _to_series(facts) for i, facts in all_bars.items()}

    async def fake_batch_events(session, instrument_ids, trade_dates):
        return {d: [] for d in trade_dates}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_batch_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_batch_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_batch_events
    )

    async def fake_prev(session, ref_date):
        return days[days.index(ref_date) - 1] if days.index(ref_date) > 0 else None

    async def fake_load_states(session, instrument_ids, trade_date):
        return states.get(trade_date, {})

    async def fake_load_bar_facts(session, instrument_ids, trade_date):
        lo = trade_date - timedelta(days=_BAR_LOOKBACK_DAYS)
        return {i: [b for b in facts if lo <= b.trade_date <= trade_date]
                for i, facts in all_bars.items()}

    async def fake_load_events(session, instrument_ids, trade_date):
        return []

    monkeypatch.setattr(
        "app.services.calendar_service.get_previous_trading_day_async", fake_prev
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_states", fake_load_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_bar_facts", fake_load_bar_facts
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_structure_events",
        fake_load_events,
    )

    async def scenario():
        batch = await prepare_scope_series_from_member_ids(
            _FakeSession(), "industry_l3", "k", "s", dates, member_ids,
            load_current_only=False,
        )
        per_date = [
            await prepare_scope_from_member_ids(
                _FakeSession(), "industry_l3", "k", "s", t, member_ids,
                load_current_only=False,
            )
            for t in dates
        ]
        return batch, per_date

    batch, per_date = asyncio.run(scenario())
    assert batch[0].members == per_date[0].members  # byte-identical (fallback)


# ---------------------------------------------------------------------------
# Test 10 — current membership resolved exactly once
# ---------------------------------------------------------------------------


def test_current_membership_resolved_exactly_once(monkeypatch) -> None:
    from app.services.review_historical_scope_reconstruction_service import (
        CurrentStaticMembership,
        reconstruct_scope_series,
    )

    calls = {"resolve": 0}
    member_ids = [uuid.uuid4()]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        calls["resolve"] += 1
        return CurrentStaticMembership(
            member_ids=tuple(member_ids),
            scope_name="s",
            asof_date=asof_date,
            member_count=len(member_ids),
        )

    async def fake_prepare(session, scope_type, scope_key, scope_name, trade_dates,
                           member_ids_, **kwargs):
        return [
            _prepared(scope_type, scope_key, scope_name, t)
            for t in trade_dates
        ]

    def _prepared(st, sk, sn, t):
        from app.services.review_observation_prep_service import PreparedScope
        return PreparedScope(
            scope_type=st, scope_key=sk, scope_name=sn, trade_date=t,
            canonical_t1=None, pit_member_ids=(), pit_member_ids_t1=(),
            members=(), t1_membership_available=True, pit_status_t="current_static",
            pit_status_t1="current_static", diagnostics=(),
        )

    def fake_compute(scope_type, scope_key, trade_date, pit_member_ids,
                     pit_member_ids_t1, members, events, *, t1_membership_available=True):
        return {"scope": {"scope_type": scope_type, "provided_member_count": 0}}

    def fake_validate(payload, *, scope_type, scope_key, trade_date):
        return None

    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service.resolve_current_membership",
        fake_resolve,
    )
    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service.prepare_scope_series_from_member_ids",
        fake_prepare,
    )
    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service.compute_scope_observation",
        fake_compute,
    )
    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service.validate_scope_observation_payload",
        fake_validate,
    )

    async def scenario():
        return await reconstruct_scope_series(
            _FakeSession(), "industry_l3", "k", [date(2026, 8, 3), date(2026, 8, 4)],
            asof_date=date(2026, 8, 4),
        )

    out = asyncio.run(scenario())
    # Membership resolved exactly once for the whole series, not per date.
    assert calls["resolve"] == 1
    assert len(out["series"]) == 2


# ---------------------------------------------------------------------------
# Test 11 — no BoardMembershipHistory introduced
# ---------------------------------------------------------------------------


def test_no_board_membership_history_reference() -> None:
    """Test 11: no BoardMembershipHistory query/import introduced in the batch or
    reconstruction services (historical/PIT membership stays forbidden)."""
    import pathlib

    base = pathlib.Path(__file__).resolve().parents[1] / "app" / "services"
    for name in (
        "review_observation_prep_service.py",
        "review_historical_scope_reconstruction_service.py",
    ):
        src = (base / name).read_text(encoding="utf-8")
        assert "BoardMembershipHistory" not in src, f"{name} references BoardMembershipHistory"
        assert "board_membership_history" not in src.lower(), (
            f"{name} references board_membership_history"
        )


# ---------------------------------------------------------------------------
# Test 12 — deterministic repeated execution
# ---------------------------------------------------------------------------


def test_batch_deterministic_repeat(monkeypatch) -> None:
    inst = uuid.uuid4()
    days = _trading_days(date(2026, 1, 5), 220)
    dates = [days[-1]]
    member_ids = [inst]
    all_bars = {inst: [_bar(inst, d, float(i + 2), volume=float(1000 + i))
                       for i, d in enumerate(days)]}
    states = {d: {inst: _state(1)} for d in days}

    async def fake_calendar(session, trade_dates):
        return {t: days[days.index(t) - 1] if days.index(t) > 0 else None
                for t in trade_dates}

    async def fake_batch_states(session, instrument_ids, trade_dates, t1_by_date):
        all_dates = set(trade_dates) | {d for d in t1_by_date.values() if d is not None}
        return {d: states.get(d, {}) for d in all_dates}

    async def fake_batch_bars(session, instrument_ids, trade_dates):
        return {i: _to_series(facts) for i, facts in all_bars.items()}

    async def fake_batch_events(session, instrument_ids, trade_dates):
        return {d: [] for d in trade_dates}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_batch_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_batch_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_batch_events
    )

    async def scenario():
        first = await prepare_scope_series_from_member_ids(
            _FakeSession(), "concept", "k", "s", dates, member_ids,
            load_current_only=False,
        )
        second = await prepare_scope_series_from_member_ids(
            _FakeSession(), "concept", "k", "s", dates, member_ids,
            load_current_only=False,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second  # deterministic: identical floats, members, order


class _FakeSession:
    """Stand-in AsyncSession (tests never touch a real DB)."""
