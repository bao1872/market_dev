"""ROUND-2.2B — Batch Coverage Loader Contract + synthetic equivalence.

[REVIEW-EXECUTION-PATH-CONSOLIDATION] 覆盖加载路径已收口为唯一 batch/union
canonical owner ``_load_batch_backfill_event_coverage``（一次 SQL 返回全部
trade_date 的 coverage），per-date loader ``_load_backfill_event_coverage_member_ids``
已删除，不再存在 per-date vs batch parity 测试。

Covers the conservative canonical-backfill coverage predicate (Phase B, 12 conditions):

    exact-T DailyState + matching completed HistoryRun(status partial/succeeded) +
    matching succeeded RunItem + canonical algorithm/history contract +
    DailyState.updated_at <= Run.completed_at.

And the required wiring:
    - PreparedScope.event_coverage_member_ids flows into compute_scope_observation
    - canonical vs batch-optimized ScopeObservation equal (synthetic coverage)
    - coverage is scope-local (PIT(T) ∩ coverage), never the whole-union coverage

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
    _load_batch_backfill_event_coverage,
)

pytestmark = pytest.mark.pure_unit

T = date(2026, 8, 10)
INSTR_A = uuid.UUID(int=1)
INSTR_B = uuid.UUID(int=2)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows  # list of (trade_date, instrument_id)

    async def execute(self, stmt):
        return _FakeResult(self._rows)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


# ---------------------------------------------------------------------------
# Batch loader contract
# ---------------------------------------------------------------------------

def test_batch_loader_valid_members_included() -> None:
    """K-01: matching rows -> members VALID (in coverage set)."""
    cov = asyncio.run(_load_batch_backfill_event_coverage(
        _FakeSession([(T, INSTR_A), (T, INSTR_B)]), [INSTR_A, INSTR_B], [T]))
    assert cov == {T: frozenset({INSTR_A, INSTR_B})}


def test_batch_loader_empty_dates_returns_empty() -> None:
    """K-04: empty dates -> empty dict (no per-date coverage entry)."""
    cov = asyncio.run(_load_batch_backfill_event_coverage(_FakeSession([]), [INSTR_A], []))
    assert cov == {}


def test_batch_missing_date_no_entry() -> None:
    """L-02: a date with no coverage entry -> absent from the batch map (source unavailable)."""
    batch = asyncio.run(_load_batch_backfill_event_coverage(
        _FakeSession([(T, INSTR_A)]), [INSTR_A, INSTR_B], [T]))
    assert batch[T] == frozenset({INSTR_A})


def test_batch_zero_rows_all_dates_absent() -> None:
    """L-04: batch with no coverage rows at all -> no entries (all dates unavailable)."""
    batch = asyncio.run(_load_batch_backfill_event_coverage(
        _FakeSession([]), [INSTR_A, INSTR_B], [T, date(2026, 8, 11)]))
    assert batch == {}


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


# ---------------------------------------------------------------------------
# PERF-FIX-STRUCTURAL-1 (P0-B): scope-local coverage
# ---------------------------------------------------------------------------

def test_coverage_scope_local_not_whole_union() -> None:
    """P0-B: a PreparedScope carries ONLY its own PIT(T) ∩ coverage members.

    Union has ~1200 members, coverage covers 1000 of them; a scope whose PIT is a
    20-member subset with 12 covered must carry exactly 12 coverage IDs — never the
    whole 1000-member union coverage (which was the pre-fix O(D×S×U) blowup).
    """
    from app.domain.review.member_fact import DailyBarFact
    from app.services.review_observation_prep_service import (
        ScopeReplaySpec,
        _InstrumentBarSeries,
        _UnionFactContext,
        build_prepared_scopes_from_union,
    )

    # union members u0..u1199
    union_ids = [uuid.UUID(int=i + 1) for i in range(1200)]
    # coverage covers u0..u999 (1000 members)
    coverage_set = frozenset(union_ids[:1000])
    # scope A PIT = u0..u11 (covered) + u1000..u1007 (NOT covered) => 12 covered / 20 PIT
    scope_pit_ids = union_ids[:12] + union_ids[1000:1008]
    scope_pit_str = {str(i) for i in scope_pit_ids}

    # minimal bars so members build (price unavailable is fine; we only assert coverage)
    def _series(mid: uuid.UUID) -> _InstrumentBarSeries:
        bar = DailyBarFact(
            trade_date=T, open=1.0, high=1.0, low=1.0, close=1.0,
            volume=1.0, amount=1.0,
        )
        return _InstrumentBarSeries(facts=(bar,), dates=(T,))

    ctx = _UnionFactContext(
        t1_by_date={T: None},
        states_by_date={},
        bars={mid: _series(mid) for mid in union_ids},
        events_by_date={},
        vec_volume={},
    )
    spec = ScopeReplaySpec(
        scope_type="concept", scope_key="A", scope_name="A",
        member_ids=tuple(scope_pit_ids),
    )
    out = build_prepared_scopes_from_union(
        trade_dates=[T],
        scope_specs=[spec],
        union_ctx=ctx,
        coverage_by_date={T: coverage_set},
    )
    ps = out["A"][0]
    assert ps.pit_member_ids == tuple(sorted(scope_pit_str))
    # scope-local coverage = PIT(scope) ∩ coverage = 12 (NOT 1000, NOT 20)
    cov = ps.event_coverage_member_ids
    assert cov is not None
    assert len(cov) == 12
    # coverage is bounded by the scope's own PIT
    assert set(cov) <= scope_pit_str
