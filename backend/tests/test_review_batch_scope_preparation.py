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

import pytest

from app.domain.review.member_fact import DailyBarFact
from app.domain.review.scope_observation import MemberObservation, StructureEvent
from app.services.review_observation_prep_service import (
    _BAR_LOOKBACK_DAYS,
    _build_t1_map,
    _InstrumentBarSeries,
    prepare_scope_from_member_ids,
    prepare_scope_series_from_member_ids,
)

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
