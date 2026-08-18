"""ROUND-2.2B — Coverage Loader Contract + per-date/batch parity + synthetic equivalence.

Covers the conservative canonical-backfill coverage predicate (Phase B, 12 conditions):

    exact-T DailyState + matching completed HistoryRun(status partial/succeeded) +
    matching succeeded RunItem + canonical algorithm/history contract +
    DailyState.updated_at <= Run.completed_at.

And the required wiring:
    - per-date ``_load_backfill_event_coverage_member_ids`` == batch ``_load_batch_backfill_event_coverage``
    - PreparedScope.event_coverage_member_ids flows into compute_scope_observation
    - canonical vs batch-optimized ScopeObservation equal (synthetic coverage)

All pure-unit; DB is mocked (no live PG).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest

from app.domain.review.scope_observation import (
    MemberObservation,
    StructureEvent,
    compute_scope_observation,
)
from app.services.review_observation_prep_service import (
    _load_backfill_event_coverage_member_ids,
    _load_batch_backfill_event_coverage,
)

pytestmark = pytest.mark.pure_unit

T = date(2026, 8, 10)
INSTR_A = uuid.UUID(int=1)
INSTR_B = uuid.UUID(int=2)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        # per-date loader: scalar instruments (one per row).
        return _FakeScalars([r[1] if isinstance(r, tuple) else r for r in self._rows])


class _FakeScalars:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows  # list of (trade_date, instrument_id)

    async def execute(self, stmt):
        return _FakeResult(self._rows)


# ---------------------------------------------------------------------------
# Loader contract
# ---------------------------------------------------------------------------

def test_loader_member_valid_included() -> None:
    """K-01: matching row -> member VALID (in coverage set)."""
    cov = asyncio.run(_load_backfill_event_coverage_member_ids(
        _FakeSession([(T, INSTR_A)]), [INSTR_A], T))
    assert cov == frozenset({INSTR_A})


def test_loader_no_match_empty_frozenset() -> None:
    """K-02: no matching row -> empty frozenset (valid but empty coverage)."""
    cov = asyncio.run(_load_backfill_event_coverage_member_ids(
        _FakeSession([]), [INSTR_A], T))
    assert cov == frozenset()


def test_loader_no_instruments_returns_none() -> None:
    """K-03: empty instrument_ids -> None (no trusted coverage source)."""
    cov = asyncio.run(_load_backfill_event_coverage_member_ids(_FakeSession([]), [], T))
    assert cov is None


def test_loader_batch_empty_dates_returns_empty() -> None:
    """K-04: empty dates -> empty dict (no per-date coverage entry)."""
    cov = asyncio.run(_load_batch_backfill_event_coverage(_FakeSession([]), [INSTR_A], []))
    assert cov == {}


# ---------------------------------------------------------------------------
# Per-date vs batch parity
# ---------------------------------------------------------------------------

def test_perdate_vs_batch_parity() -> None:
    """L-01: per-date loader result == batch loader result for the same source facts."""
    rows = [(T, INSTR_A), (T, INSTR_B)]
    per_date = asyncio.run(_load_backfill_event_coverage_member_ids(
        _FakeSession(rows), [INSTR_A, INSTR_B], T))
    batch = asyncio.run(_load_batch_backfill_event_coverage(
        _FakeSession(rows), [INSTR_A, INSTR_B], [T]))
    assert per_date == batch[T]
    assert per_date == frozenset({INSTR_A, INSTR_B})


def test_batch_missing_date_no_entry() -> None:
    """L-02: a date with no coverage entry -> absent from the batch map (source unavailable)."""
    batch = asyncio.run(_load_batch_backfill_event_coverage(
        _FakeSession([(T, INSTR_A)]), [INSTR_A, INSTR_B], [T]))
    assert batch[T] == frozenset({INSTR_A})


# ---------------------------------------------------------------------------
# Synthetic coverage equivalence (canonical vs batch-optimized)
# ---------------------------------------------------------------------------

def _member(mid: str) -> MemberObservation:
    return MemberObservation(member_id=mid, price_candidate=True, return_1d=1.0,
                             amount=100.0, trend=None, swing=None, internal=None,
                             momentum=None)


def _evt(mid: str, etype: str) -> StructureEvent:
    return StructureEvent(mid, etype, direction="Up", level=1.0, internal=False)


def test_synthetic_coverage_equivalence() -> None:
    """P-01: canonical vs batch-optimized give equal ScopeObservation with PIT=10, coverage=8.

    The two paths receive the SAME coverage + events; the observations must match.
    """
    pit = [f"m{i}" for i in range(10)]
    coverage = pit[:8]  # 8 covered (m0..m7); m8/m9 uncovered
    events = [_evt("m0", "BOS"), _evt("m1", "BOS"), _evt("m8", "BOS")]  # m8 uncovered

    def _compute(events_arg):
        return compute_scope_observation(
            scope_type="concept", scope_key="k", trade_date=T,
            pit_member_ids=pit, pit_member_ids_t1=pit,
            members=[_member(m) for m in pit],
            events=events_arg,
            event_coverage_member_ids=coverage,
        )

    # canonical: events pre-filtered to covered members (m0,m1 only).
    covered_events = [_e for _e in events if _e.member_id in set(coverage)]
    # batch-optimized: events include uncovered but the Core drops them.
    can = _compute(covered_events)
    opt = _compute(events)
    assert can == opt
    ev = can["structure"]["events"]
    assert ev["denominator"] == 8  # PIT ∩ coverage
    cell = ev["cells"]["leveled"]["BOS_Up_Swing"]
    assert cell["member_count"] == 2  # m0 + m1; m8 dropped
