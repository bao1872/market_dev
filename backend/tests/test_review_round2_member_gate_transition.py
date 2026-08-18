"""Round 2 Fix — Member Gate (GAP-L1-MEMBER-GATE) & Transition (GAP-L1-TRANSITION-T1).

Pure-unit tests against the SINGLE production owners:

* Member Gate -> ``_build_member_observations`` (member existence is decided by
  PIT membership, NOT by daily_state availability).  A PIT member whose state is
  missing is still constructed; only its state-derived fact families become
  unavailable, while bars-driven Price and snapshot-driven Current-only facts
  stay available.

* Transition gate -> ``compute_scope_observation(..., t1_membership_available)``.
  When the previous PIT membership is not reliably available, all Transition
  distributions are forced unavailable (never a current-static T-1->T forgery);
  Historical Dynamics current-static mode (t1_membership_available=True default)
  is preserved byte-for-byte.

No DB, no network.  All DB-touching helpers are mocked / bypassed.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.member_fact import DailyBarFact
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
)
from app.services.review_observation_prep_service import (
    _build_member_observations,
    _InstrumentBarSeries,
)

pytestmark = pytest.mark.pure_unit

TRADE = date(2026, 8, 5)
T1 = date(2026, 8, 4)


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
    """A minimal daily_state payload that yields a non-trivial flat."""
    return {
        "regime_value": regime,
        "swing_bias": 1,
        "internal_bias": 1,
        "sqzmom_val": 1.0,
        "structure_alignment": "aligned",
    }


def _series(inst, close, close_t1=None) -> _InstrumentBarSeries:
    bars = []
    if close_t1 is not None:
        bars.append(_bar(inst, T1, close_t1))
    bars.append(_bar(inst, TRADE, close))
    return _InstrumentBarSeries(
        facts=tuple(bars),
        dates=tuple(b.trade_date for b in bars),
    )


# ---------------------------------------------------------------------------
# TEST-MG — Member Gate (GAP-L1-MEMBER-GATE)
# ---------------------------------------------------------------------------


def _build(*, state: bool, bars: bool, snapshot: bool, inst=None):
    inst = inst or uuid.uuid4()
    states_t = {inst: _state()} if state else {}
    bars_map = {inst: _series(inst, 10.0, close_t1=9.0)} if bars else {}
    current_only = {str(inst): {"bb_position": 0.5, "bb_width": 0.2}} if snapshot else {}
    built = _build_member_observations(
        [inst],
        trade_date=TRADE,
        t1=T1,
        states_t=states_t,
        states_t1={},
        bars=bars_map,
        current_only_facts=current_only,
    )
    return built


def test_mg_01_member_exists_without_state() -> None:
    """TEST-MG-01: PIT member + bars + snapshot + NO state -> member MUST exist."""
    built = _build(state=False, bars=True, snapshot=True)
    assert len(built) == 1


def test_mg_02_no_state_bars_preserved() -> None:
    """TEST-MG-02: no state + bars -> Price inputs preserved (close / candidate)."""
    built = _build(state=False, bars=True, snapshot=False)
    assert len(built) == 1
    obs = built[0]
    # Bars are present even without state -> price candidate / amount preserved.
    assert obs.price_candidate is True
    assert obs.return_1d is not None


def test_mg_03_no_state_current_only_preserved() -> None:
    """TEST-MG-03: no state + current-only snapshot -> Current-only inputs preserved."""
    built = _build(state=False, bars=False, snapshot=True)
    assert len(built) == 1
    obs = built[0]
    assert obs.bb_position == 0.5
    assert obs.bb_width == 0.2


def test_mg_04_no_state_state_facts_unavailable() -> None:
    """TEST-MG-04: no state -> Trend / Structure State / Momentum remain unavailable."""
    built = _build(state=False, bars=True, snapshot=True)
    assert len(built) == 1
    obs = built[0]
    # State-derived categorical facts must be unavailable (not forged).
    assert obs.trend is None
    assert obs.swing is None
    assert obs.internal is None
    assert obs.momentum is None
    # Continuous state-derived facts also unavailable.
    assert obs.regime_strength is None


def test_mg_05_state_exists_unchanged() -> None:
    """TEST-MG-05: state exists -> existing behavior unchanged (all families present)."""
    built = _build(state=True, bars=True, snapshot=True)
    assert len(built) == 1
    obs = built[0]
    assert obs.trend is not None
    assert obs.price_candidate is True
    assert obs.bb_position == 0.5


def test_mg_06_mixed_members_no_silent_drop() -> None:
    """TEST-MG-06: mixed members (some with state, some without) -> no member silently
    dropped solely because its state is missing."""
    inst_a = uuid.uuid4()  # has state
    inst_b = uuid.uuid4()  # no state, but has bars+snapshot
    states_t = {inst_a: _state()}
    bars_map = {inst_a: _series(inst_a, 10.0, close_t1=9.0),
                inst_b: _series(inst_b, 11.0, close_t1=10.0)}
    current_only = {str(inst_a): {"bb_position": 0.1},
                    str(inst_b): {"bb_position": 0.9}}
    built = _build_member_observations(
        [inst_a, inst_b],
        trade_date=TRADE,
        t1=T1,
        states_t=states_t,
        states_t1={},
        bars=bars_map,
        current_only_facts=current_only,
    )
    ids = {obs.member_id for obs in built}
    assert ids == {str(inst_a), str(inst_b)}
    by_id = {obs.member_id: obs for obs in built}
    # Both members present; the state-less one still has Price + Current-only.
    assert by_id[str(inst_a)].trend is not None
    assert by_id[str(inst_b)].trend is None
    assert by_id[str(inst_b)].price_candidate is True
    assert by_id[str(inst_b)].bb_position == 0.9


# ---------------------------------------------------------------------------
# TEST-T1 — Transition Gate (GAP-L1-TRANSITION-T1)
# ---------------------------------------------------------------------------


def _member(member_id, *, trend, t1_trend) -> MemberObservation:
    return MemberObservation(
        member_id=member_id,
        price_candidate=True,
        return_1d=0.0,
        amount=100.0,
        trend=trend,
        swing=None,
        internal=None,
        momentum=None,
        t1_trend=t1_trend,
        t1_swing=None,
        t1_internal=None,
        t1_momentum=None,
    )


def _trend_transition(obs: dict) -> dict:
    return obs.get("trend", {}).get("transition", {})


def test_t1_01_membership_t1_unavailable_transition_unavailable() -> None:
    """TEST-T1-01: T membership ok, T-1 membership unavailable -> transition unavailable."""
    m = _member("m1", trend=Direction.UP, t1_trend=Direction.DOWN)
    obs = compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],  # caller still passes T-1 set
        members=[m],
        t1_membership_available=False,
        event_coverage_member_ids=None,
    )
    tr = _trend_transition(obs)
    # Transition must be empty (denominator 0) — never a forged UP<-DOWN migration.
    assert tr.get("denominator") == 0
    assert "Down→Up" not in tr


def test_t1_02_membership_t1_available_transition_normal() -> None:
    """TEST-T1-02: T membership ok, T-1 membership ok -> transition normal."""
    m = _member("m1", trend=Direction.UP, t1_trend=Direction.DOWN)
    obs = compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],
        members=[m],
        t1_membership_available=True,
        event_coverage_member_ids=None,
    )
    tr = _trend_transition(obs)
    assert tr.get("denominator") == 1
    assert tr.get("Down→Up", {}).get("count") == 1


def test_t1_03_t1_states_but_membership_unavailable_still_unavailable() -> None:
    """TEST-T1-03: T-1 states exist but T-1 membership unavailable -> still unavailable."""
    # Member carries a real t1_trend (T-1 state exists), but the caller declares
    # the T-1 MEMBERSHIP unavailable -> must NOT be proxied.
    m = _member("m1", trend=Direction.UP, t1_trend=Direction.DOWN)
    obs = compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],
        members=[m],
        t1_membership_available=False,
        event_coverage_member_ids=None,
    )
    tr = _trend_transition(obs)
    assert tr.get("denominator") == 0
    assert "Down→Up" not in tr


def test_t1_04_historical_dynamics_current_static_preserved() -> None:
    """TEST-T1-04: current-static mode (t1_membership_available default True) for
    Historical Dynamics is preserved byte-for-byte."""
    # Historical Dynamics passes no t1_membership_available -> default True.
    m = _member("m1", trend=Direction.UP, t1_trend=Direction.DOWN)
    default_obs = compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],
        members=[m],
        event_coverage_member_ids=None,
    )
    explicit_obs = compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=["m1"],
        pit_member_ids_t1=["m1"],
        members=[m],
        t1_membership_available=True,
        event_coverage_member_ids=None,
    )
    assert _trend_transition(default_obs) == _trend_transition(explicit_obs)
    assert _trend_transition(default_obs).get("denominator") == 1
