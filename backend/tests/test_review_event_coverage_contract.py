"""ROUND-2.2B — Event Coverage & Denominator Contract (EVT-COV-01..07).

The event denominator is ``PIT(T) ∩ coverage``, NOT ``len(pit_set)``.

    EVT-COV-01 Full        : PIT=10, coverage=10, BOS members=2  -> denom=10, ratio=.20
    EVT-COV-02 Partial     : PIT=10, coverage=8,  BOS members=2  -> denom=8,  ratio=.25
    EVT-COV-03 Zero event  : PIT=10, coverage=10, 0 events       -> ready, denom=10, empty cells
    EVT-COV-04 Source unavail : PIT=10, coverage=None, any events-> unavailable, denom=None
    EVT-COV-05 Uncovered event member : coverage=8, event outside -> NOT counted in numerator
    EVT-COV-06 Coverage outside PIT    : coverage contains non-PIT -> denominator uses PIT∩coverage only
    EVT-COV-07 Duplicate raw events    : same member same cell ×2  -> event_count=2, member_count=1

member_count dedupe and event_count raw count are preserved (existing logic).
Coverage source unavailable -> structure.events.status="unavailable", denominator=None
(never 0); coverage valid + zero-event -> status="ready", real denominator, empty cells
(no pre-generated zero-cell grid).

All pure-unit, no DB.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.review.scope_observation import (
    MemberObservation,
    StructureEvent,
    compute_scope_observation,
)

pytestmark = pytest.mark.pure_unit

TRADE = date(2026, 8, 5)


def _m(mid: str) -> MemberObservation:
    return MemberObservation(member_id=mid, price_candidate=True, return_1d=1.0,
                             amount=100.0, trend=None, swing=None, internal=None,
                             momentum=None)


def _obs(pit_ids: list[str], coverage, events: list[StructureEvent]) -> dict:
    return compute_scope_observation(
        scope_type="concept",
        scope_key="k",
        trade_date=TRADE,
        pit_member_ids=pit_ids,
        pit_member_ids_t1=pit_ids,
        members=[_m(m) for m in pit_ids],
        events=events,
        event_coverage_member_ids=coverage,
    )


def _evt(mid: str, etype: str, direction="Up", internal=False) -> StructureEvent:
    return StructureEvent(mid, etype, direction=direction, level=1.0, internal=internal)


def _events(obs: dict) -> dict:
    return obs["structure"]["events"]


def test_evt_cov_01_full() -> None:
    """EVT-COV-01: PIT=10, coverage=10, BOS members=2 -> denom=10, ratio=.20."""
    pit = [f"m{i}" for i in range(10)]
    ev = [_evt("m0", "BOS"), _evt("m1", "BOS")]
    out = _events(_obs(pit, pit, ev))
    assert out["status"] == "ready"
    assert out["denominator"] == 10
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 2
    assert cell["member_ratio"] == pytest.approx(0.20)


def test_evt_cov_02_partial() -> None:
    """EVT-COV-02: PIT=10, coverage=8, BOS members=2 -> denom=8, ratio=.25."""
    pit = [f"m{i}" for i in range(10)]
    coverage = pit[:8]
    ev = [_evt("m0", "BOS"), _evt("m1", "BOS")]
    out = _events(_obs(pit, coverage, ev))
    assert out["denominator"] == 8
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 2
    assert cell["member_ratio"] == pytest.approx(0.25)


def test_evt_cov_03_zero_event_ready() -> None:
    """EVT-COV-03: PIT=10, coverage=10, 0 events -> ready, denom=10, empty cells."""
    pit = [f"m{i}" for i in range(10)]
    out = _events(_obs(pit, pit, []))
    assert out["status"] == "ready"
    assert out["denominator"] == 10
    assert out["cells"] == {"leveled": {}, "extreme": {}}


def test_evt_cov_04_source_unavailable() -> None:
    """EVT-COV-04: coverage=None -> unavailable, denom=None (never 0)."""
    pit = [f"m{i}" for i in range(10)]
    out = _events(_obs(pit, None, [_evt("m0", "BOS")]))
    assert out["status"] == "unavailable"
    assert out["denominator"] is None
    assert out["cells"] == {"leveled": {}, "extreme": {}}


def test_evt_cov_05_uncovered_event_member_not_counted() -> None:
    """EVT-COV-05: coverage=8, event for an uncovered member -> numerator excluded."""
    pit = [f"m{i}" for i in range(10)]
    coverage = pit[:8]
    # m8/m9 are PIT but NOT covered; an event on m9 must not enter the numerator.
    ev = [_evt("m1", "BOS"), _evt("m9", "BOS")]
    out = _events(_obs(pit, coverage, ev))
    assert out["denominator"] == 8
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 1  # only m1 (covered); m9 excluded
    assert cell["event_count"] == 1


def test_evt_cov_06_coverage_outside_pit() -> None:
    """EVT-COV-06: coverage contains non-PIT members -> denominator = PIT∩coverage only."""
    pit = [f"m{i}" for i in range(10)]
    coverage = pit + ["m99", "m100"]  # 2 extra non-PIT
    ev = [_evt("m0", "BOS"), _evt("m99", "BOS")]  # m99 outside PIT, event excluded
    out = _events(_obs(pit, coverage, ev))
    assert out["denominator"] == 10  # PIT ∩ coverage = all 10 PIT
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 1  # only m0; m99 excluded (outside PIT)


def test_evt_cov_07_duplicate_raw_events() -> None:
    """EVT-COV-07: same member same cell ×2 -> event_count=2, member_count=1."""
    pit = ["m0", "m1"]
    ev = [_evt("m0", "BOS"), _evt("m0", "BOS")]  # m0 fires BOS twice same day
    out = _events(_obs(pit, pit, ev))
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["event_count"] == 2  # raw count preserved
    assert cell["member_count"] == 1  # dedupe by member


def test_evt_cov_empty_effective_coverage_unavailable() -> None:
    """AUDIT-FIX F2/F3: EMPTY coverage set -> effective universe is None -> unavailable.

    An empty coverage set (or coverage entirely outside PIT) has NO valid event
    universe, so the Core must emit ``status=unavailable`` / ``denominator=None`` —
    NEVER a fake ``ready / denominator=0``.  This is the three-state correction:
    None / empty effective universe -> unavailable.
    """
    pit = ["m0", "m1"]
    out = _events(_obs(pit, [], [_evt("m0", "BOS")]))
    assert out["status"] == "unavailable"
    assert out["denominator"] is None
    assert out["cells"] == {"leveled": {}, "extreme": {}}


def test_evt_cov_coverage_outside_pit_all_unavailable() -> None:
    """AUDIT-FIX F2/F3: coverage entirely OUTSIDE PIT -> PIT∩coverage = ∅ -> unavailable."""
    pit = ["m0", "m1"]
    out = _events(_obs(pit, ["m99", "m100"], [_evt("m99", "BOS")]))
    assert out["status"] == "unavailable"
    assert out["denominator"] is None
    assert out["cells"] == {"leveled": {}, "extreme": {}}


def test_evt_cov_coverage_partial_overlap_keeps_ready() -> None:
    """Guard: coverage with SOME PIT overlap -> non-empty intersection -> ready."""
    pit = [f"m{i}" for i in range(10)]
    coverage = pit[:5] + ["m99", "m100"]  # 5 PIT + 2 outsiders
    ev = [_evt("m0", "BOS")]
    out = _events(_obs(pit, coverage, ev))
    assert out["status"] == "ready"
    assert out["denominator"] == 5  # PIT ∩ coverage = first 5 PIT members
    cell = out["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 1
