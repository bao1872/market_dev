"""Stage 5-A1 / 5C-A2 leadership-migration validation gates.

Locks the two production-owner contracts that the old probe harness used to
guard via internal helpers:

  1. Unavailable ``LeadershipSnapshot`` (``leader_set=None``) derives
     ``leader_ids == ()`` — it is NOT coerced into a fake "0 leaders"
     unavailable->0 coercion.  A ready snapshot with a legitimate empty
     ``leader_set=()`` also derives ``leader_ids == ()``.

  2. ``LeadershipMigrationFacts`` is an exact-equal dataclass: any field change
     (a real prefix mismatch) makes two facts objects differ.

  3. ``compute_leadership_migration`` is a PURE function of (T-1, T) only —
     opening T+1 cannot change the facts already locked at T (no future leak).

  A2 (side-aware MigrationFacts availability): a ready snapshot's legitimate
  empty Leader Set (0 / ()) is legal; an unavailable side stays ``None``
  (unknown != 0); rate metrics (retention / jaccard / migration) are a
  SEPARATE layer that must be ``None`` whenever the migration is unavailable.

These now exercise the single production owner
``app.domain.review.analysis.leadership_migration`` directly — no probe
helper is required.  ``_FakePreparedScope`` is a structural data-carrier
stand-in for the DB-bound ``PreparedScope``; the chain it feeds
(``compute_scope_observation`` -> ``compute_member_leadership_contributions``
-> ``build_leadership_snapshot`` -> ``compute_leadership_migration``) is the
real production code.

Pure unit: no DB, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import pytest

from app.domain.first_pyramid_semantics import Direction, MomentumDirection
from app.domain.review.analysis.leadership_contribution import (
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import (
    AlignedLeadership,
    LeadershipSnapshot,
    build_leadership_snapshot,
    compute_leadership_migration,
)
from app.domain.review.scope_observation import (
    MemberObservation,
    compute_scope_observation,
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


def _build_snapshot(ps: _FakePreparedScope) -> LeadershipSnapshot:
    """Build one ``LeadershipSnapshot`` through the production owner chain."""
    obs = compute_scope_observation(
        scope_type=ps.scope_type,
        scope_key=ps.scope_key,
        trade_date=ps.trade_date,
        pit_member_ids=ps.pit_member_ids,
        members=ps.members,
        event_coverage_member_ids=ps.event_coverage_member_ids,
    )
    ew = obs["price"]["equal_weight_return"]
    contribution_facts = compute_member_leadership_contributions(ps.members)
    return build_leadership_snapshot(
        trade_date=ps.trade_date.isoformat(),
        ew_return=ew,
        contribution_facts=contribution_facts,
    )


def _migration_facts(
    series: list[_FakePreparedScope], target_idx: int
) -> Any:
    """``LeadershipMigrationFacts`` at ``target_idx`` over the production chain."""
    snapshots = [_build_snapshot(ps) for ps in series[: target_idx + 1]]
    return compute_leadership_migration(
        previous_snapshot=snapshots[target_idx - 1],
        current_snapshot=snapshots[target_idx],
    )


# ---------------------------------------------------------------------------
# Test 1 — unavailable / ready-empty Snapshot derives leader_ids == () (no 0-coercion)
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
    # Production contract: an unavailable snapshot with no leader set derives an
    # EMPTY leader id tuple — it is NOT coerced into a fake "0 leaders" coercion.
    assert unavailable.leader_ids == ()
    assert unavailable.leader_set is None
    empty_ready = LeadershipSnapshot(
        trade_date="2026-08-14",
        status="ready",
        reason=None,
        direction=1,
        rankable_count=2,
        leader_set=(),  # legitimate empty leader set
    )
    assert empty_ready.leader_ids == ()


# ---------------------------------------------------------------------------
# Test 2 — prefix migration mismatch is really detected (gate can FAIL)
# ---------------------------------------------------------------------------


def test_prefix_mismatch_detection_identical_facts() -> None:
    series = _ready_series()
    before = _migration_facts(series, 1)
    after = _migration_facts(series, 1)  # identical recomputation
    assert before == after


def test_prefix_mismatch_detection_tampered_facts() -> None:
    series = _ready_series()
    before = _migration_facts(series, 1)
    tampered = replace(before, jaccard_stability=0.25, migration=0.75)
    assert tampered != before


# ---------------------------------------------------------------------------
# Test 3 — REAL prefix future-leak: opening T+1 must not change migration(T)
# ---------------------------------------------------------------------------


def test_prefix_future_leak_stability() -> None:
    series = _ready_series()
    # REAL prefix future-leak proof: build the production chain up to T (prefix
    # [:2]) and up to T+1 (prefix [:3]); migration(T) must be IDENTICAL either way
    # — opening T+1 cannot leak into the facts already locked at T.
    snap_t = [_build_snapshot(ps) for ps in series[:2]]
    snap_t1 = [_build_snapshot(ps) for ps in series[:3]]
    mf_cut = compute_leadership_migration(
        previous_snapshot=snap_t[0], current_snapshot=snap_t[1]
    )
    mf_open = compute_leadership_migration(
        previous_snapshot=snap_t1[0], current_snapshot=snap_t1[1]
    )
    assert mf_cut == mf_open
    # Sanity: migration(T) is genuinely READY with a non-empty leader set.
    assert mf_cut.status == "ready"
    assert mf_cut.jaccard_stability == 1.0
    assert mf_cut.migration == 0.0
    assert mf_cut.current_leader_ids == ("a",)


# ---------------------------------------------------------------------------
# A2 — MigrationFacts availability is side-aware (unknown != legitimate 0)
#
# These lock the frozen contract directly through compute_leadership_migration:
#   unavailable snapshot side  -> leader_count / leader_ids == None
#   ready snapshot (empty set) -> leader_count == 0 / leader_ids == ()  (legal)
#   mf.status == "unavailable" -> previous_retention / jaccard / migration == None
# All four build facts through the production owner.
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
    # previous unavailable / current ready-empty -> no coercion.
    prev_unavailable = _snap("2026-08-13", status="unavailable", leader_set=None)
    curr_ready_empty = _snap("2026-08-14", status="ready", leader_set=())
    mf = compute_leadership_migration(
        previous_snapshot=prev_unavailable, current_snapshot=curr_ready_empty
    )
    assert mf.status == "unavailable" and mf.reason == "unavailable_snapshot"
    # unavailable side stays None; ready-empty side keeps its legal 0 / ().
    assert mf.previous_leader_count is None and mf.previous_leader_ids is None
    assert mf.current_leader_count == 0 and mf.current_leader_ids == ()
    # Side-aware availability: the unavailable side must be None (unknown != 0);
    # the ready side must exactly equal its snapshot evidence (0 / () is legal).
    assert mf.previous_leader_count is None  # not coerced to 0
    assert mf.current_leader_count == len(curr_ready_empty.leader_ids)
    assert mf.current_leader_ids == curr_ready_empty.leader_ids
    # Transition metrics are a separate layer: unavailable => all rate metrics None.
    assert (
        mf.previous_retention is None
        and mf.jaccard_stability is None
        and mf.migration is None
    )


def test_migration_facts_prev_ready_empty_curr_unavailable_is_legal() -> None:
    # previous ready-empty / current unavailable -> no coercion.
    prev_ready_empty = _snap("2026-08-13", status="ready", leader_set=())
    curr_unavailable = _snap("2026-08-14", status="unavailable", leader_set=None)
    mf = compute_leadership_migration(
        previous_snapshot=prev_ready_empty, current_snapshot=curr_unavailable
    )
    assert mf.status == "unavailable" and mf.reason == "unavailable_snapshot"
    assert mf.previous_leader_count == 0 and mf.previous_leader_ids == ()
    assert mf.current_leader_count is None and mf.current_leader_ids is None
    assert mf.current_leader_count is None  # unavailable side not coerced to 0
    assert mf.previous_leader_count == len(prev_ready_empty.leader_ids)
    assert mf.previous_leader_ids == prev_ready_empty.leader_ids
    assert (
        mf.previous_retention is None
        and mf.jaccard_stability is None
        and mf.migration is None
    )


def test_migration_facts_ready_empty_to_ready_nonempty_allows_legal_zero() -> None:
    # previous ready-empty -> current ready-nonempty is a LEGAL empty_leader_set;
    # count 0 / ids () must never be flagged as coercion, the real set-difference
    # is preserved, and rate metrics stay None.
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
    assert mf.previous_leader_count == len(prev_ready_empty.leader_ids)
    assert mf.previous_leader_ids == prev_ready_empty.leader_ids
    assert mf.current_leader_count == len(curr_ready_nonempty.leader_ids)
    assert mf.current_leader_ids == curr_ready_nonempty.leader_ids


def test_migration_facts_unavailable_side_not_coerced_to_zero() -> None:
    # previous unavailable / current ready -> the unavailable side's leader
    # evidence MUST stay None (unknown != legitimate 0).  The production owner
    # guarantees this; a tampered facts object that coerces the None side to 0
    # is a distinct, invalid contract and must not equal the real facts.
    prev_unavailable = _snap("2026-08-13", status="unavailable", leader_set=None)
    curr_ready = _snap(
        "2026-08-14", status="ready", leader_set=(_leader("a"),), direction=1
    )
    mf = compute_leadership_migration(
        previous_snapshot=prev_unavailable, current_snapshot=curr_ready
    )
    assert mf.previous_leader_count is None and mf.previous_leader_ids is None
    assert mf.previous_retention is None  # rate metrics stay None
    tampered = replace(mf, previous_leader_count=0, previous_leader_ids=())
    assert tampered != mf  # coercion to 0 is a distinct, invalid contract
