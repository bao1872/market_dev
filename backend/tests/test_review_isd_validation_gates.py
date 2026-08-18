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

A2 (side-aware MigrationFacts availability): ``_migration_facts_side_violation``
and ``_migration_transition_metrics_violation`` lock that a ready snapshot's
legitimate empty Leader Set (0 / ()) is legal, an unavailable side must be None,
and rate metrics are a separate layer from snapshot-side leader evidence.

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
    compute_leadership_migration,
)
from app.domain.review.scope_observation import MemberObservation
from scripts.review_scope_dynamics_probe import (
    _compute_prefix_migration_facts,
    _migration_facts_side_violation,
    _migration_transition_metrics_violation,
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


# ---------------------------------------------------------------------------
# A2 — MigrationFacts availability is side-aware (unknown != legitimate 0)
#
# These lock the A2-1/A2-2 helpers against the FROZEN contract:
#   unavailable snapshot side  -> leader_count / leader_ids == None
#   ready snapshot (empty set) -> leader_count == 0 / leader_ids == ()  (legal)
#   mf.status == "unavailable" -> previous_retention / jaccard / migration == None
# All four build facts through the production owner compute_leadership_migration.
# ---------------------------------------------------------------------------


def _snap(
    trade_date: str,
    *,
    status: str,
    leader_set: tuple[AlignedLeadership, ...] | None,
    direction: int | None = None,
    rankable_count: int = 2,
) -> LeadershipSnapshot:
    return LeadershipSnapshot(
        trade_date=trade_date,
        status=status,
        reason=None if status == "ready" else "ew_unavailable",
        direction=direction,
        rankable_count=rankable_count,
        leader_set=leader_set,
    )


def _leader(mid: str) -> AlignedLeadership:
    return AlignedLeadership(member_id=mid, contribution=1.0, aligned_score=0.5)


def test_migration_facts_prev_unavailable_curr_ready_empty_is_legal() -> None:
    # Test 1 (A2): previous unavailable / current ready-empty -> no coercion.
    prev_unavailable = _snap("2026-08-13", status="unavailable", leader_set=None)
    curr_ready_empty = _snap("2026-08-14", status="ready", leader_set=())
    mf = compute_leadership_migration(
        previous_snapshot=prev_unavailable, current_snapshot=curr_ready_empty
    )
    assert mf.status == "unavailable" and mf.reason == "unavailable_snapshot"
    # unavailable side stays None; ready-empty side keeps its legal 0 / ().
    assert mf.previous_leader_count is None and mf.previous_leader_ids is None
    assert mf.current_leader_count == 0 and mf.current_leader_ids == ()
    assert not _migration_facts_side_violation(
        prev_unavailable, mf.previous_leader_count, mf.previous_leader_ids
    )
    assert not _migration_facts_side_violation(
        curr_ready_empty, mf.current_leader_count, mf.current_leader_ids
    )
    assert not _migration_transition_metrics_violation(mf)


def test_migration_facts_prev_ready_empty_curr_unavailable_is_legal() -> None:
    # Test 2 (A2): previous ready-empty / current unavailable -> no coercion.
    prev_ready_empty = _snap("2026-08-13", status="ready", leader_set=())
    curr_unavailable = _snap("2026-08-14", status="unavailable", leader_set=None)
    mf = compute_leadership_migration(
        previous_snapshot=prev_ready_empty, current_snapshot=curr_unavailable
    )
    assert mf.status == "unavailable" and mf.reason == "unavailable_snapshot"
    assert mf.previous_leader_count == 0 and mf.previous_leader_ids == ()
    assert mf.current_leader_count is None and mf.current_leader_ids is None
    assert not _migration_facts_side_violation(
        prev_ready_empty, mf.previous_leader_count, mf.previous_leader_ids
    )
    assert not _migration_facts_side_violation(
        curr_unavailable, mf.current_leader_count, mf.current_leader_ids
    )
    assert not _migration_transition_metrics_violation(mf)


def test_migration_facts_ready_empty_to_ready_nonempty_allows_legal_zero() -> None:
    # Test 3 (A2): previous ready-empty -> current ready-nonempty is a LEGAL
    # empty_leader_set; count 0 / ids () must never be flagged as coercion, and
    # the real set-difference is preserved while rate metrics stay None.
    prev_ready_empty = _snap("2026-08-13", status="ready", leader_set=())
    curr_ready_nonempty = _snap(
        "2026-08-14", status="ready", leader_set=(_leader("a"),), direction=1
    )
    mf = compute_leadership_migration(
        previous_snapshot=prev_ready_empty, current_snapshot=curr_ready_nonempty
    )
    assert mf.status == "unavailable" and mf.reason == "empty_leader_set"
    assert mf.previous_leader_count == 0 and mf.previous_leader_ids == ()
    assert mf.current_leader_count == 1 and mf.current_leader_ids == ("a",)
    assert mf.retained_count == 0 and mf.entrant_count == 1 and mf.exit_count == 0
    assert (
        mf.previous_retention is None
        and mf.jaccard_stability is None
        and mf.migration is None
    )
    assert not _migration_facts_side_violation(
        prev_ready_empty, mf.previous_leader_count, mf.previous_leader_ids
    )
    assert not _migration_facts_side_violation(
        curr_ready_nonempty, mf.current_leader_count, mf.current_leader_ids
    )
    assert not _migration_transition_metrics_violation(mf)


def test_migration_facts_unavailable_side_tampered_to_zero_detected() -> None:
    # Test 4 (A2): deliberately coerce an unavailable side's None count to 0 —
    # the side-aware helper MUST detect it (the Snapshot/transition layers do
    # not cover this, proving the two layers are separate).
    prev_unavailable = _snap("2026-08-13", status="unavailable", leader_set=None)
    curr_ready = _snap("2026-08-14", status="ready", leader_set=(_leader("a"),), direction=1)
    mf = compute_leadership_migration(
        previous_snapshot=prev_unavailable, current_snapshot=curr_ready
    )
    assert mf.previous_leader_count is None and mf.previous_leader_ids is None
    tampered = replace(mf, previous_leader_count=0, previous_leader_ids=())
    assert _migration_facts_side_violation(
        prev_unavailable, tampered.previous_leader_count, tampered.previous_leader_ids
    ) is True
    # rate metrics still None -> the transition-metric layer alone cannot catch it.
    assert not _migration_transition_metrics_violation(tampered)
