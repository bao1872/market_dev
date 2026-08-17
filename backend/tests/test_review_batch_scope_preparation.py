"""Batch historical Scope preparation tests (Review v2.3 optimization).

Covers the batch reconstruction mechanics introduced by
``prepare_scope_series_from_member_ids``:

- Batch/per-date equivalence: the replay path produces byte-identical
  ``PreparedScope`` for every T as the per-date ``prepare_scope_from_member_ids``.
- Window slicing: ``_InstrumentBarSeries.window`` reproduces exactly the
  per-date ``_load_bar_facts`` window (``[hi-400d, hi]``, ascending).
- T-1 mapping: ``_build_t1_map`` matches ``calendar_service`` predicates
  (strictly-lower trading day; None when no predecessor).
- Query count: the whole series is one bulk read (each batch loader invoked
  exactly once), not O(N) per-date reloads.
- Current-only guard: ``load_current_only=False`` default means the current-only
  snapshot loader is never invoked for historical T.

No DB, no network.  All DB-touching helpers are mocked.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, timedelta

import numpy as np

import pytest
from unittest.mock import patch

from app.domain.review.member_fact import DailyBarFact
from app.domain.review.scope_observation import MemberObservation, StructureEvent
from app.services.review_observation_prep_service import (
    _BAR_LOOKBACK_DAYS,
    _build_member_observations,
    _build_t1_map,
    _InstrumentBarSeries,
    _VectorizedMemberVolume,
    prepare_scope_from_member_ids,
    prepare_scope_series_from_member_ids,
)
from app.services.volume_context import compute_volume_context_vectorized

pytestmark = pytest.mark.pure_unit

T1 = date(2026, 8, 3)
T2 = date(2026, 8, 4)
PREV = date(2026, 7, 31)  # canonical T-1 of T1


class _FakeSession:
    """Stand-in AsyncSession (tests never touch a real DB)."""


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


# ---------------------------------------------------------------------------
# Window slicing: _InstrumentBarSeries.window reproduces _load_bar_facts
# ---------------------------------------------------------------------------


def test_instrument_bar_series_last_bar_is_o1_and_matches_window_tail() -> None:
    """``last_bar`` must return the single T-row bar (window tail) in O(log n),
    reproducing exactly what the vec fast path consumed via ``facts[-1]`` before."""
    inst = uuid.uuid4()
    early = _bar(inst, T1 - timedelta(days=_BAR_LOOKBACK_DAYS + 5), 1.0)
    inside = [_bar(inst, T1 - timedelta(days=k), float(k + 2)) for k in range(5)]
    inside.reverse()
    after = _bar(inst, T2 + timedelta(days=1), 99.0)
    series = _InstrumentBarSeries(
        facts=tuple(sorted([early, *inside, after], key=lambda b: b.trade_date)),
        dates=tuple(
            sorted([early.trade_date, *(b.trade_date for b in inside), after.trade_date])
        ),
    )
    assert series.last_bar(T1) == series.window(T1)[-1]
    assert series.last_bar(PREV) == series.window(PREV)[-1]
    # Beyond the series -> None (no bar <= hi).
    assert series.last_bar(T1 - timedelta(days=_BAR_LOOKBACK_DAYS + 50)) is None


def test_instrument_bar_series_exact_bar_requires_exact_date() -> None:
    """``exact_bar`` must match EXACTLY: a bar whose trade_date differs from the
    target (e.g. an earlier T-2 bar) is never returned for a missing target date.

    This locks the canonical exact-T / exact-T1 contract: when the instrument is
    suspended on the exact T-1 (no bar), ``close_t1`` must be None and callers MUST
    NOT fall back to the T-2 bar the way ``last_bar`` (``<= hi``) would.
    """
    inst = uuid.uuid4()
    # T1 has a bar; PREV (=T1's exact T-1) has NO bar; PREV - 1d has a bar.
    bars = [
        _bar(inst, PREV - timedelta(days=1), 9.0),
        _bar(inst, T1, 10.0),
        _bar(inst, T2, 11.0),
    ]
    series = _InstrumentBarSeries(
        facts=tuple(sorted(bars, key=lambda b: b.trade_date)),
        dates=tuple(sorted(b.trade_date for b in bars)),
    )
    # Exact hit returns the bar at that date.
    assert series.exact_bar(T1) is not None
    assert series.exact_bar(T1).close == 10.0
    # Exact T-1 missing -> None, even though a T-2 (PREV-1d) bar exists (no fallback).
    assert series.exact_bar(PREV) is None
    # Target beyond the series -> None.
    assert series.exact_bar(T2 + timedelta(days=5)) is None
    # Target before the series -> None.
    assert series.exact_bar(PREV - timedelta(days=10)) is None


def test_member_observation_exact_t1_missing_returns_none() -> None:
    """Boundary regression (P0 exact-bar fix): T has a bar, exact T-1 is missing
    (suspended), but T-2 has a bar -> ``close_t1`` source is None, so the built
    observation must have ``return_1d=None`` (NEVER ``close_T / close_T2 - 1``).

    The old ``last_bar`` (``<= hi``) would have fallen back to T-2 and produced a
    spurious 1d return; ``exact_bar`` restores the canonical contract.
    """
    m = uuid.uuid4()
    TRADE = date(2026, 8, 5)
    T_MINUS1 = date(2026, 8, 4)  # suspended: no bar
    T_MINUS2 = date(2026, 8, 3)  # has a bar, must NOT be used as T-1
    series = _InstrumentBarSeries(
        facts=(_bar(m, T_MINUS2, 9.0), _bar(m, TRADE, 10.0)),
        dates=(T_MINUS2, TRADE),
    )
    states_t = {m: {"regime_value": 1, "is_suspended": False}}
    built = _build_member_observations(
        [m],
        trade_date=TRADE,
        t1=T_MINUS1,
        states_t=states_t,
        states_t1=states_t,
        bars={m: series},
        current_only_facts={},
        # No vec_volume -> canonical fallback owner, but close_t/close_t1 are still
        # resolved by exact_bar at the top of the loop.
    )
    assert len(built) == 1
    obs = built[0]
    # close(T) exists -> candidate; exact T-1 missing -> no return_1d.
    assert obs.price_candidate is True
    assert obs.return_1d is None


def test_member_observation_exact_t_missing_price_not_candidate() -> None:
    """Boundary regression (P0 exact-bar fix): T itself has no bar (suspended), but
    T-1 has a bar -> ``current`` must be None, so ``price_candidate`` must be False.

    The old ``last_bar`` (``<= hi``) would have fallen back to the T-1 bar as the
    "current" row and wrongly marked the member as a price candidate.
    """
    m = uuid.uuid4()
    TRADE = date(2026, 8, 5)  # suspended: no bar
    T_MINUS1 = date(2026, 8, 4)  # has a bar, must NOT be used as current
    series = _InstrumentBarSeries(
        facts=(_bar(m, T_MINUS1, 9.0),),
        dates=(T_MINUS1,),
    )
    states_t = {m: {"regime_value": 1, "is_suspended": False}}
    built = _build_member_observations(
        [m],
        trade_date=TRADE,
        t1=None,
        states_t=states_t,
        states_t1={},
        bars={m: series},
        current_only_facts={},
    )
    assert len(built) == 1
    obs = built[0]
    # No exact T bar -> not a price candidate; no return either.
    assert obs.price_candidate is False
    assert obs.return_1d is None


def test_instrument_bar_series_window_reproduces_per_date_window() -> None:
    inst = uuid.uuid4()
    # One bar before the 400d lookback (must be excluded), bars inside, and one
    # after hi (must be excluded).
    early = _bar(inst, T1 - timedelta(days=_BAR_LOOKBACK_DAYS + 5), 1.0)
    inside = [_bar(inst, T1 - timedelta(days=k), float(k + 2)) for k in range(5)]
    inside.reverse()  # descending -> the series must sort ascending
    after = _bar(inst, T2 + timedelta(days=1), 99.0)
    series = _InstrumentBarSeries(
        facts=tuple(sorted([early, *inside, after], key=lambda b: b.trade_date)),
        dates=tuple(
            sorted([early.trade_date, *(b.trade_date for b in inside), after.trade_date])
        ),
    )

    window = series.window(T1)
    # Exactly the 5 bars in [T1-400d, T1], ascending; early/after excluded.
    assert [b.trade_date for b in window] == [b.trade_date for b in inside]
    assert [b.trade_date for b in window] == sorted(b.trade_date for b in inside)
    # Reproduces the per-date SQL predicate: hi-400d <= trade_date <= hi.
    expected = [
        b for b in [early, *inside, after]
        if T1 - timedelta(days=_BAR_LOOKBACK_DAYS) <= b.trade_date <= T1
    ]
    assert window == expected

    # T-1 window slices the same series at the earlier boundary.
    t1_window = series.window(PREV)
    assert all(b.trade_date <= PREV for b in t1_window)
    assert t1_window == [
        b for b in [early, *inside, after]
        if PREV - timedelta(days=_BAR_LOOKBACK_DAYS) <= b.trade_date <= PREV
    ]


# ---------------------------------------------------------------------------
# T-1 mapping: _build_t1_map matches the calendar predicates
# ---------------------------------------------------------------------------


def test_build_t1_map_strictly_lower_trading_day() -> None:
    trading_days = [PREV, T1, T2]
    out = _build_t1_map([T1, T2], trading_days)
    assert out[T1] == PREV  # strictly-lower trading day
    assert out[T2] == T1


def test_build_t1_map_no_predecessor_returns_none() -> None:
    out = _build_t1_map([T1], [T1, T2])
    # T1 is the earliest trading day in the window -> no strictly-lower day.
    assert out[T1] is None


# ---------------------------------------------------------------------------
# Batch/per-date equivalence
# ---------------------------------------------------------------------------


def test_batch_series_equals_per_date_path(monkeypatch) -> None:
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    member_ids = [id_a, id_b]
    dates = [T1, T2]
    trading_days = [PREV, T1, T2]

    # Canned canonical data across the whole window (incl. one excluded early bar).
    all_bars = {
        id_a: [_bar(id_a, PREV - timedelta(days=_BAR_LOOKBACK_DAYS + 3), 1.0),
              _bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_b: [_bar(id_b, PREV, 8.0), _bar(id_b, T1, 8.5), _bar(id_b, T2, 9.0)],
    }
    states = {
        PREV: {id_a: _state(1), id_b: _state(1)},
        T1: {id_a: _state(1), id_b: _state(-1)},
        T2: {id_a: _state(1), id_b: _state(1)},
    }
    events = {
        T1: [StructureEvent(member_id=str(id_a), event_type="BOS", direction="bullish",
                            level=1.0, internal=False, release_volume_ratio=None)],
        T2: [],
    }

    # ---- BATCH path: mock the four bulk loaders once each ----
    async def fake_calendar(session, trade_dates):
        return {t: trading_days[trading_days.index(t) - 1]
                if trading_days.index(t) > 0 else None for t in trade_dates}

    async def fake_batch_states(session, instrument_ids, trade_dates, t1_by_date):
        return {d: states.get(d, {}) for d in set(trade_dates) | set(t1_by_date.values())}

    async def fake_batch_bars(session, instrument_ids, trade_dates):
        return {
            i: _InstrumentBarSeries(
                facts=tuple(sorted(facts, key=lambda b: b.trade_date)),
                dates=tuple(sorted(b.trade_date for b in facts)),
            )
            for i, facts in all_bars.items()
        }

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

    # ---- PER-DATE path: mock the per-date loaders with equivalent windows ----
    async def fake_prev(session, ref_date):
        return trading_days[trading_days.index(ref_date) - 1] \
            if trading_days.index(ref_date) > 0 else None

    async def fake_load_states(session, instrument_ids, trade_date):
        return states.get(trade_date, {})

    async def fake_load_bar_facts(session, instrument_ids, trade_date):
        lo = trade_date - timedelta(days=_BAR_LOOKBACK_DAYS)
        out = {}
        for i, facts in all_bars.items():
            out[i] = sorted(
                (b for b in facts if lo <= b.trade_date <= trade_date),
                key=lambda b: b.trade_date,
            )
        return out

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
    # Byte-identical to the per-date path for every T (payload contract preserved).
    assert len(batch) == len(per_date)
    for b, p in zip(batch, per_date, strict=True):
        assert b.scope_type == p.scope_type
        assert b.scope_key == p.scope_key
        assert b.trade_date == p.trade_date
        assert b.canonical_t1 == p.canonical_t1
        assert b.pit_member_ids == p.pit_member_ids
        assert b.pit_member_ids_t1 == p.pit_member_ids_t1
        assert b.members == p.members
        assert b.events == p.events
    assert batch[0].canonical_t1 == PREV
    assert batch[1].canonical_t1 == T1


# ---------------------------------------------------------------------------
# PERF-1: lazy-window invariant + vectorized VolumeContext instrumentation
# ---------------------------------------------------------------------------


def _make_member_bar_series(
    member_id: uuid.UUID, end: date, n_daily: int, volume: float = 10.0
) -> _InstrumentBarSeries:
    """Build a contiguous daily bar series of length ``n_daily`` ending at ``end``.

    The contiguous daily grid means ``[end-400d, end]`` contains exactly ``n_daily``
    finite-volume bars, so ``w == n_daily`` — convenient for forcing the vectorized
    hit (``w >= SHORT_WINDOW`` and ``hi < LONG_WINDOW - 1``) vs the window-bound
    fallback (``w < SHORT_WINDOW``).
    """
    bars = [
        _bar(member_id, end - timedelta(days=n_daily - 1 - k), float(10 + k), volume=volume)
        for k in range(n_daily)
    ]
    bars_sorted = sorted(bars, key=lambda b: b.trade_date)
    return _InstrumentBarSeries(
        facts=tuple(bars_sorted),
        dates=tuple(b.trade_date for b in bars_sorted),
    )


def _make_vec_volume(member_id: uuid.UUID, series: _InstrumentBarSeries):
    vols = np.asarray([float(f.volume) for f in series.facts if f.volume is not None])
    return _VectorizedMemberVolume(
        dates=tuple(f.trade_date for f in series.facts),
        volumes=vols,
        context=compute_volume_context_vectorized(vols),
    )


def test_batch_replay_does_not_materialize_window_on_vec_path():
    """PERF-1 invariant lock: on the vectorized fast path the full strict-prior
    history window is NOT materialized — ``_InstrumentBarSeries.window`` must be
    called zero times because ``last_bar`` (O(log n)) is used instead.

    This locks the R1 lazy-window optimization and prevents a regression back to the
    O(dates x members x ~400) list-copy hotspot.
    """
    members = [uuid.uuid4() for _ in range(3)]
    trade_dates = [date(2024, 3, 1) + timedelta(days=k) for k in range(5)]
    states_t = {m: {"regime_value": 1, "is_suspended": False} for m in members}
    states_t1 = dict(states_t)
    bars = {
        m: _make_member_bar_series(m, trade_dates[-1], n_daily=30) for m in members
    }
    vec_volume = {m: _make_vec_volume(m, s) for m, s in bars.items()}

    window_calls = {"n": 0}
    real_window = _InstrumentBarSeries.window

    def counting_window(self, td):
        window_calls["n"] += 1
        return real_window(self, td)

    with patch.object(_InstrumentBarSeries, "window", counting_window):
        _build_member_observations(
            members,
            trade_date=trade_dates[-1],
            t1=None,
            states_t=states_t,
            states_t1=states_t1,
            bars=bars,
            current_only_facts={},
            vec_volume=vec_volume,
        )

    assert window_calls["n"] == 0


def test_vectorized_volume_hit_count_and_fallback_reason():
    """PERF-1 instrumentation lock: the batch owner must report vectorized
    VolumeContext hit vs canonical-fallback counts and the first fallback reason,
    without changing the constructed MemberObservations.

    Member A: 30 daily bars -> vec hit.
    Member B: 10 daily bars (``w < SHORT_WINDOW``) -> window-bound fallback.
    Member C: absent from ``vec_volume`` but has bars -> no_finite_volume fallback.
    """
    m_a, m_b, m_c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    members = [m_a, m_b, m_c]
    trade_dates = [date(2024, 3, 1) + timedelta(days=k) for k in range(5)]
    states_t = {m: {"regime_value": 1, "is_suspended": False} for m in members}
    states_t1 = dict(states_t)

    bars = {
        m_a: _make_member_bar_series(m_a, trade_dates[-1], n_daily=30),
        m_b: _make_member_bar_series(m_b, trade_dates[-1], n_daily=10),
        m_c: _make_member_bar_series(m_c, trade_dates[-1], n_daily=30),
    }
    vec_volume = {
        m_a: _make_vec_volume(m_a, bars[m_a]),
        m_b: _make_vec_volume(m_b, bars[m_b]),
        # m_c intentionally absent -> no_finite_volume fallback
    }

    counters: dict[str, int] = {}
    fallback_reasons: list[str] = []
    built = _build_member_observations(
        members,
        trade_date=trade_dates[-1],
        t1=None,
        states_t=states_t,
        states_t1=states_t1,
        bars=bars,
        current_only_facts={},
        vec_volume=vec_volume,
        counters=counters,
        fallback_reasons=fallback_reasons,
    )

    # 1 hit (A), 2 fallbacks (B window-bound, C no_finite_volume)
    assert counters.get("vec_hit", 0) == 1
    assert counters.get("vec_fallback", 0) == 2
    # Both distinct fallback reasons recorded, no duplicate of the same reason.
    assert "w_insufficient" in fallback_reasons
    assert "no_finite_volume" in fallback_reasons
    # Semantic output unchanged: 3 MemberObservations built.
    assert len(built) == 3


# ---------------------------------------------------------------------------
# Query count: one bulk read per series, not O(N) per-date reloads
# ---------------------------------------------------------------------------


def test_batch_series_loads_each_bulk_reader_exactly_once(monkeypatch) -> None:
    calls: dict[str, int] = {"calendar": 0, "states": 0, "bars": 0, "events": 0}

    async def fake_calendar(session, trade_dates):
        calls["calendar"] += 1
        return {t: t - timedelta(days=1) for t in trade_dates}

    async def fake_states(session, instrument_ids, trade_dates, t1_by_date):
        calls["states"] += 1
        return {}

    async def fake_bars(session, instrument_ids, trade_dates):
        calls["bars"] += 1
        return {}

    async def fake_events(session, instrument_ids, trade_dates):
        calls["events"] += 1
        return {}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_events
    )

    many_dates = [T1, T1 + timedelta(days=1), T1 + timedelta(days=2), T1 + timedelta(days=3)]

    async def scenario():
        return await prepare_scope_series_from_member_ids(
            _FakeSession(), "industry_l1", "k", "s", many_dates, [uuid.uuid4()],
            load_current_only=False,
        )

    out = asyncio.run(scenario())
    assert len(out) == len(many_dates)
    # Each bulk reader is invoked exactly once regardless of the number of dates.
    assert calls == {"calendar": 1, "states": 1, "bars": 1, "events": 1}


# ---------------------------------------------------------------------------
# Current-only guard: historical batch never invokes the snapshot loader
# ---------------------------------------------------------------------------


def test_batch_series_never_loads_current_only_facts(monkeypatch) -> None:
    async def fake_calendar(session, trade_dates):
        return dict.fromkeys(trade_dates, PREV)

    async def fake_states(session, instrument_ids, trade_dates, t1_by_date):
        # State payloads for the ACTUAL members passed to the batch prep.
        return {
            T1: {m: _state(1) for m in instrument_ids},
            PREV: {m: _state(1) for m in instrument_ids},
        }

    async def fake_bars(session, instrument_ids, trade_dates):
        return {}

    async def fake_events(session, instrument_ids, trade_dates):
        return {}

    async def boom(session, instrument_ids, trade_date):
        raise AssertionError("current-only snapshot loader must not run for historical T")

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_events
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_current_only_snapshot_facts",
        boom,
    )

    async def scenario():
        return await prepare_scope_series_from_member_ids(
            _FakeSession(), "concept", "k", "s", [T1], [uuid.uuid4()],
        )

    out = asyncio.run(scenario())
    assert len(out) == 1
    # With load_current_only defaulting to False, all current-only facts stay None.
    member: MemberObservation = out[0].members[0]
    assert member.bb_position is None
    assert member.release_volume_ratio is None


# ---------------------------------------------------------------------------
# PERF-2: bounded scope batch + unique-member shared fact context
# ---------------------------------------------------------------------------


def test_reconstruct_scope_series_batch_loads_union_once_and_matches_per_scope(
    monkeypatch,
) -> None:
    """PERF-2 equivalence lock.

    Two scopes share one member (simulating concept overlap, avg 12.89 boards/
    member in production).  ``reconstruct_scope_series_batch`` must:

    1. Load the union member set exactly ONCE (each bulk reader invoked once for
       the shared ``prepare_union_fact_context``), not once per scope — this is
       the storage-layer dedup that removes the redundant per-scope reload.
    2. Produce, per scope, byte-identical results to calling
       ``reconstruct_scope_series`` independently for that scope.
    """
    from app.services.review_historical_scope_reconstruction_service import (
        CurrentStaticMembership,
        reconstruct_scope_series,
        reconstruct_scope_series_batch,
        resolve_current_membership,
    )

    # Scope A: {a, shared}; Scope B: {b, shared} -> union {a, b, shared}
    id_a, id_b, id_shared = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]
    members_b = [id_b, id_shared]

    # Resolve membership per scope (current-static semantic owner, unchanged).
    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        if scope_key == "A":
            return CurrentStaticMembership(
                member_ids=tuple(members_a), scope_name="Scope A",
                asof_date=asof_date, member_count=len(members_a),
            )
        return CurrentStaticMembership(
            member_ids=tuple(members_b), scope_name="Scope B",
            asof_date=asof_date, member_count=len(members_b),
        )

    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service."
        "resolve_current_membership",
        fake_resolve,
    )

    calls: dict[str, int] = {"calendar": 0, "states": 0, "bars": 0, "events": 0}

    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_b: [_bar(id_b, PREV, 8.0), _bar(id_b, T1, 8.5), _bar(id_b, T2, 9.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_b: _state(-1), id_shared: _state(1)},
        T2: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
    }
    events = {
        T1: [StructureEvent(member_id=str(id_shared), event_type="BOS",
                            direction="bullish", level=1.0, internal=False,
                            release_volume_ratio=None)],
        T2: [],
    }
    trading_days = [PREV, T1, T2]

    async def fake_calendar(session, trade_dates):
        calls["calendar"] = calls.get("calendar", 0) + 1
        return {t: trading_days[trading_days.index(t) - 1]
                if trading_days.index(t) > 0 else None for t in trade_dates}

    async def fake_states(session, instrument_ids, trade_dates, t1_by_date):
        calls["states"] = calls.get("states", 0) + 1
        return {d: states.get(d, {}) for d in set(trade_dates) | set(t1_by_date.values())}

    async def fake_bars(session, instrument_ids, trade_dates):
        calls["bars"] = calls.get("bars", 0) + 1
        return {
            i: _InstrumentBarSeries(
                facts=tuple(sorted(facts, key=lambda b: b.trade_date)),
                dates=tuple(sorted(b.trade_date for b in facts)),
            )
            for i, facts in all_bars.items()
        }

    async def fake_events(session, instrument_ids, trade_dates):
        calls["events"] = calls.get("events", 0) + 1
        return {d: events.get(d, []) for d in trade_dates}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_events
    )

    async def scenario():
        # Batch first: union of {A,B} loads each bulk reader exactly ONCE.
        batch = await reconstruct_scope_series_batch(
            _FakeSession(), "concept", ["A", "B"], [T1, T2],
            asof_date=T2,
        )
        batch_calls = dict(calls)
        # Reset; per-scope reconstruction reloads each scope independently (2x).
        calls.clear()
        per_a = await reconstruct_scope_series(
            _FakeSession(), "concept", "A", [T1, T2], asof_date=T2,
        )
        per_b = await reconstruct_scope_series(
            _FakeSession(), "concept", "B", [T1, T2], asof_date=T2,
        )
        per_calls = dict(calls)
        return batch, per_a, per_b, batch_calls, per_calls

    batch, per_a, per_b, batch_calls, per_calls = asyncio.run(scenario())

    # 1) Batch path loads the union ONCE (shared), not once per scope.
    assert batch_calls == {"calendar": 1, "states": 1, "bars": 1, "events": 1}
    # Per-scope path (baseline) reloads each scope independently -> 2x.
    assert per_calls == {"calendar": 2, "states": 2, "bars": 2, "events": 2}

    # 2) Per-scope results byte-identical to independent per-scope reconstruction.
    by_key = {r["scope"]["scope_key"]: r for r in batch}
    assert set(by_key) == {"A", "B"}
    for got, expected in ((by_key["A"], per_a), (by_key["B"], per_b)):
        assert got["scope"] == expected["scope"]
        assert got["membership"]["member_count"] == expected["membership"]["member_count"]
        assert len(got["series"]) == len(expected["series"])
        for g, e in zip(got["series"], expected["series"], strict=True):
            assert g == e


def test_reconstruct_scope_series_batch_chunks_when_union_exceeds_cap(
    monkeypatch,
) -> None:
    """PERF-2 bounded-batch lock: when the union of members across a chunk exceeds
    ``union_member_cap``, the batch entry must split scope_keys into multiple
    chunks, each triggering its own union bulk load (readers invoked >1 time).
    """
    from app.services.review_historical_scope_reconstruction_service import (
        CurrentStaticMembership,
        reconstruct_scope_series_batch,
        resolve_current_membership,
    )

    scopes = {f"S{i}": [uuid.uuid4()] for i in range(4)}  # disjoint members

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        mids = scopes[scope_key]
        return CurrentStaticMembership(
            member_ids=tuple(mids), scope_name=scope_key,
            asof_date=asof_date, member_count=len(mids),
        )

    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service."
        "resolve_current_membership",
        fake_resolve,
    )

    calls: dict[str, int] = {"calendar": 0, "states": 0, "bars": 0, "events": 0}

    async def fake_calendar(session, trade_dates):
        calls["calendar"] += 1
        return dict.fromkeys(trade_dates, PREV)

    async def fake_states(session, instrument_ids, trade_dates, t1_by_date):
        calls["states"] += 1
        return {}

    async def fake_bars(session, instrument_ids, trade_dates):
        calls["bars"] += 1
        return {}

    async def fake_events(session, instrument_ids, trade_dates):
        calls["events"] += 1
        return {}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar", fake_calendar
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states", fake_states
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars", fake_bars
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events", fake_events
    )

    async def scenario():
        # cap=1 -> each disjoint scope (1 member) is its own chunk -> 4 loads.
        return await reconstruct_scope_series_batch(
            _FakeSession(), "concept", list(scopes), [T1],
            asof_date=T1, union_member_cap=1,
        )

    out = asyncio.run(scenario())
    assert len(out) == 4
    assert calls == {"calendar": 4, "states": 4, "bars": 4, "events": 4}


# ---------------------------------------------------------------------------
# PERF-VEC-1 — union member-day deduplication
#
# Locks the VEC-1 loop-order invariant: canonical MemberObservation construction
# drops from scope-member-date to unique-member-date.  ``prepare_scopes_from_union``
# must build the union once per trade_date and slice per scope WITHOUT re-running
# ``_build_member_observations``, while every PreparedScope / observation stays
# byte-identical to the dedicated single-scope path.
# ---------------------------------------------------------------------------


def _install_union_mocks(
    monkeypatch,
    all_bars,
    states,
    events,
    trading_days,
    calls=None,
):
    """Shared batch-loader mocks (VEC-1 tests).  ``calls`` is optional and counts
    bulk-reader invocations."""

    def _calls(key):
        if calls is not None:
            calls[key] = calls.get(key, 0) + 1

    async def fake_calendar(session, trade_dates):
        _calls("calendar")
        return {
            t: trading_days[trading_days.index(t) - 1]
            if trading_days.index(t) > 0 else None
            for t in trade_dates
        }

    async def fake_states(session, instrument_ids, trade_dates, t1_by_date):
        _calls("states")
        return {
            d: states.get(d, {})
            for d in set(trade_dates) | set(t1_by_date.values())
        }

    async def fake_bars(session, instrument_ids, trade_dates):
        _calls("bars")
        return {
            i: _InstrumentBarSeries(
                facts=tuple(sorted(facts, key=lambda b: b.trade_date)),
                dates=tuple(sorted(b.trade_date for b in facts)),
            )
            for i, facts in all_bars.items()
        }

    async def fake_events(session, instrument_ids, trade_dates):
        _calls("events")
        return {d: events.get(d, []) for d in trade_dates}

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_calendar",
        fake_calendar,
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_states",
        fake_states,
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_bars",
        fake_bars,
    )
    monkeypatch.setattr(
        "app.services.review_observation_prep_service._load_batch_events",
        fake_events,
    )


def test_vec1_shared_member_built_once_per_date(monkeypatch):
    """VEC-1 physical invariant: a member shared by N scopes is canonical-built
    exactly once per trade_date (unique-member-day), not once per scope per date
    (scope-member-day).  ``_build_member_observations`` remains the single
    member-construction owner and receives the union member set each date."""
    from app.services.review_historical_scope_reconstruction_service import (
        CurrentStaticMembership,
        reconstruct_scope_series_batch,
        resolve_current_membership,
    )

    id_a, id_b, id_shared = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]
    members_b = [id_b, id_shared]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        mids = members_a if scope_key == "A" else members_b
        return CurrentStaticMembership(
            member_ids=tuple(mids), scope_name=scope_key,
            asof_date=asof_date, member_count=len(mids),
        )

    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service."
        "resolve_current_membership",
        fake_resolve,
    )

    calls = {"build": 0, "union_sizes": []}
    original = _build_member_observations

    def counting(pit_ids_t, **kwargs):
        calls["build"] += 1
        calls["union_sizes"].append(len(pit_ids_t))
        return original(pit_ids_t, **kwargs)

    monkeypatch.setattr(
        "app.services.review_observation_prep_service._build_member_observations",
        counting,
    )

    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_b: [_bar(id_b, PREV, 8.0), _bar(id_b, T1, 8.5), _bar(id_b, T2, 9.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_b: _state(-1), id_shared: _state(1)},
        T2: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
    }
    trading_days = [PREV, T1, T2]
    _install_union_mocks(monkeypatch, all_bars, states, {}, trading_days)

    async def scenario():
        return await reconstruct_scope_series_batch(
            _FakeSession(), "concept", ["A", "B"], [T1, T2], asof_date=T2,
        )

    out = asyncio.run(scenario())
    # VEC-1: one union build per trade_date (2 dates), union size = 3 members —
    # NOT 2 scopes x 2 dates = 4 builds of scope sizes [2,2,2,2].
    assert calls["build"] == 2
    assert calls["union_sizes"] == [3, 3]
    assert {r["scope"]["scope_key"] for r in out} == {"A", "B"}


def test_vec1_prepared_scope_equals_single_scope_path(monkeypatch):
    """VEC-1: for a single scope, the shared-union slice yields a PreparedScope
    series identical (every field) to the dedicated single-scope batch owner."""
    from app.services.review_observation_prep_service import (
        prepare_scopes_from_union,
        prepare_union_fact_context,
    )

    id_a, id_shared = uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]
    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_shared: _state(1)},
        T2: {id_a: _state(1), id_shared: _state(1)},
    }
    trading_days = [PREV, T1, T2]
    events = {
        T1: [
            StructureEvent(
                member_id=str(id_shared), event_type="BOS", direction="bullish",
                level=1.0, internal=False, release_volume_ratio=None,
            )
        ],
        T2: [],
    }
    _install_union_mocks(monkeypatch, all_bars, states, events, trading_days)

    async def scenario():
        union_ctx = await prepare_union_fact_context(
            _FakeSession(), trading_days, [id_a, id_shared]
        )
        prepared = await prepare_scopes_from_union(
            _FakeSession(), "concept", trading_days,
            {"A": (members_a, "Scope A")}, union_ctx,
        )
        single = await prepare_scope_series_from_member_ids(
            _FakeSession(), "concept", "A", "Scope A", trading_days, members_a,
            load_current_only=False,
        )
        return prepared["A"], single

    got, expected = asyncio.run(scenario())
    assert len(got) == len(expected) == len(trading_days)
    for p, s in zip(got, expected, strict=True):
        assert p.scope_type == s.scope_type
        assert p.scope_key == s.scope_key
        assert p.scope_name == s.scope_name
        assert p.trade_date == s.trade_date
        assert p.canonical_t1 == s.canonical_t1
        assert p.pit_member_ids == s.pit_member_ids
        assert p.pit_member_ids_t1 == s.pit_member_ids_t1
        assert p.members == s.members
        assert p.t1_membership_available == s.t1_membership_available
        assert p.pit_status_t == s.pit_status_t
        assert p.pit_status_t1 == s.pit_status_t1
        assert p.diagnostics == s.diagnostics
        assert p.events == s.events


def test_vec1_scope_observation_equals_single_scope_path(monkeypatch):
    """VEC-1: computing the canonical Scope Observation from the shared-union
    PreparedScope yields the identical observation dict as the single-scope path
    (same ``compute_scope_observation`` owner, unchanged algorithm)."""
    from app.domain.review.scope_observation import compute_scope_observation
    from app.services.review_observation_prep_service import (
        prepare_scopes_from_union,
        prepare_union_fact_context,
    )

    id_a, id_shared = uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]
    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_shared: _state(1)},
        T2: {id_a: _state(1), id_shared: _state(1)},
    }
    trading_days = [PREV, T1, T2]
    _install_union_mocks(monkeypatch, all_bars, states, {}, trading_days)

    async def scenario():
        union_ctx = await prepare_union_fact_context(
            _FakeSession(), trading_days, [id_a, id_shared]
        )
        prepared = await prepare_scopes_from_union(
            _FakeSession(), "concept", trading_days,
            {"A": (members_a, "Scope A")}, union_ctx,
        )
        single = await prepare_scope_series_from_member_ids(
            _FakeSession(), "concept", "A", "Scope A", trading_days, members_a,
            load_current_only=False,
        )
        return prepared["A"], single

    got, expected = asyncio.run(scenario())
    for p, s in zip(got, expected, strict=True):
        obs_g = compute_scope_observation(
            scope_type=p.scope_type, scope_key=p.scope_key,
            trade_date=p.trade_date, pit_member_ids=p.pit_member_ids,
            pit_member_ids_t1=p.pit_member_ids_t1, members=p.members,
            events=p.events,
        )
        obs_s = compute_scope_observation(
            scope_type=s.scope_type, scope_key=s.scope_key,
            trade_date=s.trade_date, pit_member_ids=s.pit_member_ids,
            pit_member_ids_t1=s.pit_member_ids_t1, members=s.members,
            events=s.events,
        )
        assert obs_g == obs_s, f"observation mismatch at {p.trade_date}"


def test_vec1_scope_isolation_missing_state_boundary(monkeypatch):
    """VEC-1 boundary: a member with no state at T is absent from the union build
    and therefore excluded from every scope slice; a member of scope A is never
    leaked into scope B (scope isolation preserved)."""
    from app.services.review_observation_prep_service import (
        prepare_scopes_from_union,
        prepare_union_fact_context,
    )

    id_a, id_b, id_shared = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]
    members_b = [id_b, id_shared]
    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_b: [_bar(id_b, PREV, 8.0), _bar(id_b, T1, 8.5), _bar(id_b, T2, 9.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_b: _state(1), id_shared: _state(1)},
        # At T2 the shared member has NO valid state -> must be excluded everywhere.
        T2: {id_a: _state(1), id_b: _state(1)},
    }
    trading_days = [PREV, T1, T2]
    _install_union_mocks(monkeypatch, all_bars, states, {}, trading_days)

    async def scenario():
        union_ctx = await prepare_union_fact_context(
            _FakeSession(), trading_days, [id_a, id_b, id_shared]
        )
        return await prepare_scopes_from_union(
            _FakeSession(), "concept", trading_days,
            {"A": (members_a, "Scope A"), "B": (members_b, "Scope B")}, union_ctx,
        )

    prepared = asyncio.run(scenario())
    # T2 (last): shared has no state -> absent from both A and B; no cross-leak.
    a_t2 = prepared["A"][-1]
    b_t2 = prepared["B"][-1]
    assert [m.member_id for m in a_t2.members] == [str(id_a)]
    assert [m.member_id for m in b_t2.members] == [str(id_b)]
    # Earlier dates still include the shared member in both scopes.
    a_t1 = prepared["A"][1]
    b_t1 = prepared["B"][1]
    assert [m.member_id for m in a_t1.members] == [str(id_a), str(id_shared)]
    assert [m.member_id for m in b_t1.members] == [str(id_b), str(id_shared)]


def test_vec1_union_deterministic_repeat(monkeypatch):
    """VEC-1 determinism: running the union batch twice with identical input
    produces identical result dicts (order, members, observations)."""
    from app.services.review_historical_scope_reconstruction_service import (
        CurrentStaticMembership,
        reconstruct_scope_series_batch,
        resolve_current_membership,
    )

    id_a, id_shared = uuid.uuid4(), uuid.uuid4()
    members_a = [id_a, id_shared]

    async def fake_resolve(session, scope_type, scope_key, *, asof_date):
        return CurrentStaticMembership(
            member_ids=tuple(members_a), scope_name="A",
            asof_date=asof_date, member_count=len(members_a),
        )

    monkeypatch.setattr(
        "app.services.review_historical_scope_reconstruction_service."
        "resolve_current_membership",
        fake_resolve,
    )

    all_bars = {
        id_a: [_bar(id_a, PREV, 9.0), _bar(id_a, T1, 10.0), _bar(id_a, T2, 11.0)],
        id_shared: [
            _bar(id_shared, PREV, 5.0),
            _bar(id_shared, T1, 5.5),
            _bar(id_shared, T2, 6.0),
        ],
    }
    states = {
        PREV: {id_a: _state(1), id_shared: _state(1)},
        T1: {id_a: _state(1), id_shared: _state(1)},
        T2: {id_a: _state(1), id_shared: _state(1)},
    }
    trading_days = [PREV, T1, T2]
    _install_union_mocks(monkeypatch, all_bars, states, {}, trading_days)

    async def scenario():
        first = await reconstruct_scope_series_batch(
            _FakeSession(), "concept", ["A"], [T1, T2], asof_date=T2,
        )
        second = await reconstruct_scope_series_batch(
            _FakeSession(), "concept", ["A"], [T1, T2], asof_date=T2,
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first == second
