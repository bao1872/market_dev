"""Stage 5-A1 validation harness unit tests (PRD §7.10 / prompt.md A1-1..A1-3).

Locks the two Stage-5C validation-gate fixes in
``scripts.review_scope_dynamics_probe``:

  1. ``_snapshot_unavailable_coercion`` — an unavailable LeadershipSnapshot
     (``leader_set=None``, derived ``leader_ids=()``) is NOT counted as an
     unavailable->0 coercion; only a contract-violating unavailable snapshot
     (``leader_set is not None``) is.
  2. ``_prefix_migration_facts_mismatch`` — full ``LeadershipMigrationFacts``
     exact-equal comparison detects ANY field change (a real prefix mismatch
     would make the hard gate FAIL).
  3. ``_compute_prefix_migration_facts`` — REAL prefix-bound recomputation:
     opening T+1 must not change ``LeadershipMigrationFacts(T)``.

Pure unit: no DB, no network.  Exercises only shared production owners plus the
harness helpers.  ``_FakePreparedScope`` is a structural data-carrier stand-in
for the DB-bound ``PreparedScope`` (the pure chain it feeds is the real
production code).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.leadership_migration import (
    AlignedLeadership,
    LeadershipSnapshot,
)
from app.domain.review.scope_observation import MemberObservation
from scripts.review_scope_dynamics_probe import (
    _compute_prefix_migration_facts,
    _prefix_migration_facts_mismatch,
    _snapshot_unavailable_coercion,
)

pytestmark = pytest.mark.pure_unit


@dataclass(frozen=True)
class _FakePreparedScope:
    """Structural stand-in for ``PreparedScope`` (data carrier only)."""

    scope_type: str
    scope_key: str
    scope_name: str
    trade_date: date
    canonical_t1: date | None
    pit_member_ids: tuple[str, ...]
    pit_member_ids_t1: tuple[str, ...]
    members: tuple[MemberObservation, ...]
    t1_membership_available: bool
    pit_status_t: str
    pit_status_t1: str
    diagnostics: tuple[str, ...]
    event_coverage_member_ids: tuple[str, ...] | None
    events: tuple[Any, ...]


def _member(mid: str, *, return_1d: float, amount: float) -> MemberObservation:
    return MemberObservation(
        member_id=mid,
        price_candidate=True,
        return_1d=return_1d,
        amount=amount,
        trend=Direction.UP,
        swing=Direction.UP,
        internal=Direction.UP,
        momentum=MomentumDirection.EXPANDING,
    )


def _scope(trade_date: date, members: list[MemberObservation]) -> _FakePreparedScope:
    return _FakePreparedScope(
        scope_type="concept",
        scope_key="test-scope",
        scope_name="Test Scope",
        trade_date=trade_date,
        canonical_t1=None,
        pit_member_ids=tuple(m.member_id for m in members),
        pit_member_ids_t1=tuple(m.member_id for m in members),
        members=tuple(members),
        t1_membership_available=False,
        pit_status_t="current_static",
        pit_status_t1="current_static",
        diagnostics=(),
        event_coverage_member_ids=None,
        events=(),
    )


def _ready_series() -> list[_FakePreparedScope]:
    """3 dates; leader set stays {"a"} so migration(T) is READY."""
    return [
        _scope(
            date(2026, 8, 12),
            [
                _member("a", return_1d=0.05, amount=100.0),
                _member("b", return_1d=-0.02, amount=50.0),
            ],
        ),
        _scope(
            date(2026, 8, 13),
            [
                _member("a", return_1d=0.05, amount=100.0),
                _member("b", return_1d=0.01, amount=50.0),
            ],
        ),
        _scope(
            date(2026, 8, 14),
            [
                _member("a", return_1d=0.05, amount=100.0),
                _member("b", return_1d=0.15, amount=50.0),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Test 1 — unavailable Snapshot is not falsely counted as unavailable->0
# ---------------------------------------------------------------------------


def test_unavailable_snapshot_not_counted_as_coercion() -> None:
    unavailable = LeadershipSnapshot(
        trade_date="2026-08-14",
        status="unavailable",
        reason="ew_unavailable",
        direction=None,
        rankable_count=0,
        leader_set=None,  # derived leader_ids == ()
    )
    assert unavailable.leader_ids == ()
    assert _snapshot_unavailable_coercion(unavailable) is False

    empty_ready = LeadershipSnapshot(
        trade_date="2026-08-14",
        status="ready",
        reason=None,
        direction=1,
        rankable_count=2,
        leader_set=(),  # legitimate empty leader set
    )
    assert _snapshot_unavailable_coercion(empty_ready) is False


def test_unavailable_with_non_none_leader_set_is_detected() -> None:
    violating = LeadershipSnapshot(
        trade_date="2026-08-14",
        status="unavailable",
        reason="ew_unavailable",
        direction=None,
        rankable_count=1,
        leader_set=(AlignedLeadership(member_id="m1", contribution=1.0, aligned_score=0.5),),
    )
    assert _snapshot_unavailable_coercion(violating) is True


# ---------------------------------------------------------------------------
# Test 2 — prefix migration mismatch is really detected (gate can FAIL)
# ---------------------------------------------------------------------------


def test_prefix_mismatch_detection_identical_facts() -> None:
    series = _ready_series()
    before = _compute_prefix_migration_facts(series, 1, 1)
    after = _compute_prefix_migration_facts(series, 1, 1)  # identical recomputation
    assert _prefix_migration_facts_mismatch(before, after) is False


def test_prefix_mismatch_detection_tampered_facts() -> None:
    series = _ready_series()
    before = _compute_prefix_migration_facts(series, 1, 1)
    tampered = replace(before, jaccard_stability=0.25, migration=0.75)
    assert _prefix_migration_facts_mismatch(before, tampered) is True


# ---------------------------------------------------------------------------
# Test 3 — REAL prefix future-leak: opening T+1 must not change migration(T)
# ---------------------------------------------------------------------------


def test_prefix_future_leak_stability() -> None:
    series = _ready_series()
    # T = index 1 (window_dates[-2]); open T+1 (index 2) and re-check migration(T).
    cut_at_t = _compute_prefix_migration_facts(series, 1, 1)  # series[:2]
    open_t1 = _compute_prefix_migration_facts(series, 2, 1)  # series[:3]
    assert _prefix_migration_facts_mismatch(cut_at_t, open_t1) is False
    # Sanity: migration(T) is genuinely READY with a non-empty leader set.
    assert cut_at_t.status == "ready"
    assert cut_at_t.jaccard_stability == 1.0
    assert cut_at_t.migration == 0.0
