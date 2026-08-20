"""Leadership Migration — Internal Structure Dynamics (PRD §7.10, FROZEN).

Formal production owner implementing the frozen "Leadership Migration Numerical
Contract (FROZEN)".  It is a PURE two-layer owner:

    Layer 1  build_leadership_snapshot(ew_return, contribution_facts)
             -> per-trade-date Leader Set (direction + aligned_score + 50%
                minimal-prefix).
    Layer 2  compute_leadership_migration(previous_snapshot, current_snapshot)
             -> T-1 vs T comparison (retained / entrants / exits / retention /
                Jaccard / migration).

Ownership boundary (single-owner + NO-MIGRATION):

- It does NOT construct MemberObservation.
- It does NOT compute amount_share (single owner compute_member_amount_contributions).
- It does NOT compute contribution (single owner
  compute_member_leadership_contributions).  It consumes the ALREADY-prepared
  ``LeadershipContributionFacts``.
- It consumes the CANONICAL ``equal_weight_return`` (from
  compute_scope_observation()["price"]["equal_weight_return"]) as the scope
  direction source — it never re-derives an EW from member returns.

Frozen contract (PRD §7.10 Leadership Migration Numerical Contract):

- aligned = contribution × sign(EW); only aligned > 0 enter the leader universe.
- ranking = aligned_score DESC, member_id ASC (deterministic tie-break).
- LEADERSHIP_COVERAGE = 0.50: minimal prefix whose cumulative positive aligned
  reaches >= 50% of the total positive aligned.
- No minimum-member threshold (1 valid leader is a legal Leader Set).
- Snapshot availability: EW None -> unavailable; EW 0 -> unavailable
  (no_prevailing_direction); EW valid but no aligned>0 -> ready with leader_set=[].
- Transition availability: either snapshot unavailable -> migration unavailable;
  either (valid) leader_set empty -> migration unavailable (empty_leader_set),
  fail-closed.
- primary = Jaccard = |A∩B|/|A∪B|; migration = 1 - Jaccard;
  supporting = previous_retention = |A∩B|/|A| (not part of the migration formula).
- transparent set-change facts: previous/current leader count + ids, retained /
  entrant / exit count + ids.
- No categorical label (stable/rotating/...), no composite score, no
  leadership/rotation/risk score.

Pure + deterministic + non-mutating.  No DB, no T+1 (future-leak = 0).
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.review.analysis.leadership_contribution import (
    LeadershipContributionFacts,
)

LEADERSHIP_COVERAGE = 0.50
"""Frozen coverage threshold: leader set must explain >=50% of positive aligned."""


@dataclass(frozen=True)
class AlignedLeadership:
    """A direction-aligned leadership candidate (member + aligned_score)."""

    member_id: str
    contribution: float
    aligned_score: float


@dataclass(frozen=True)
class LeadershipSnapshot:
    """Per-trade-date leadership snapshot (Layer-1 output).

    Availability semantics:
      - ``status == "unavailable"`` with ``reason`` in
        {"no_prevailing_direction", "ew_unavailable"} -> ``leader_set is None``.
      - ``status == "ready"`` with ``leader_set == []`` -> LEGITIMATE empty leader
        set (no member had aligned_score>0).  ``None != []``.
      - ``status == "ready"`` with non-empty ``leader_set`` -> normal case.
    """

    trade_date: str
    status: str  # "ready" | "unavailable"
    reason: str | None
    direction: int | None  # +1 / -1 / None
    rankable_count: int
    leader_set: tuple[AlignedLeadership, ...] | None
    leader_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "leader_ids",
            tuple(x.member_id for x in self.leader_set) if self.leader_set is not None else (),
        )


@dataclass(frozen=True)
class LeadershipMigrationFacts:
    """T-1 vs T Leadership Migration comparison (Layer-2 output).

    ``status``:
      - "unavailable" with ``reason`` in {"unavailable_snapshot",
        "empty_leader_set"} when a required input is not comparable.
      - "ready" when both snapshots are ready and both leader sets are non-empty.
    """

    trade_date: str
    status: str  # "ready" | "unavailable"
    reason: str | None

    coverage: float

    previous_direction: int | None
    current_direction: int | None

    previous_rankable_count: int
    current_rankable_count: int

    # Snapshot-level leader counts / ids: an unknown (unavailable) side is None,
    # never 0; a known side (ready, possibly a legitimately-empty leader set) is a
    # concrete int / tuple.
    previous_leader_count: int | None
    current_leader_count: int | None

    retained_count: int | None
    entrant_count: int | None
    exit_count: int | None

    previous_retention: float | None
    jaccard_stability: float | None
    migration: float | None

    previous_leader_ids: tuple[str, ...] | None
    current_leader_ids: tuple[str, ...] | None
    entrant_ids: tuple[str, ...] | None
    exit_ids: tuple[str, ...] | None


# ---------------------------------------------------------------------------
# Layer 1 — daily leadership snapshot
# ---------------------------------------------------------------------------


def _direction_from_ew(ew_return: float | None) -> tuple[int | None, str | None, str]:
    """Derive scope direction from the canonical equal_weight_return.

    Returns ``(direction, reason, status)``:
      - None / "ew_unavailable" / "unavailable" when EW is None.
      - None / "no_prevailing_direction" / "unavailable" when EW == 0.
      - +1 or -1 / None / "ready" otherwise.
    """
    if ew_return is None:
        return None, "ew_unavailable", "unavailable"
    if ew_return > 0.0:
        return 1, None, "ready"
    if ew_return < 0.0:
        return -1, None, "ready"
    return None, "no_prevailing_direction", "unavailable"


def _aligned_ranking(
    contribution_facts: LeadershipContributionFacts, direction: int
) -> list[AlignedLeadership]:
    """Rank direction-aligned contributors: aligned = contribution × direction.

    Deterministic: aligned_score DESC, member_id ASC.  Only aligned>0 are retained
    downstream; this helper returns the full aligned ordering (callers filter the
    positive-aligned leader universe).
    """
    ranked: list[AlignedLeadership] = []
    for c in contribution_facts.members:
        if c.contribution is None:
            continue
        aligned = c.contribution * direction
        ranked.append(
            AlignedLeadership(
                member_id=c.member_id,
                contribution=c.contribution,
                aligned_score=aligned,
            )
        )
    ranked.sort(key=lambda x: (-x.aligned_score, x.member_id))
    return ranked


def _minimal_prefix_leader_set(
    aligned_ranked: Sequence[AlignedLeadership], coverage: float
) -> list[AlignedLeadership]:
    """Minimal sorted prefix whose cumulative positive aligned reaches coverage.

    Only aligned_score > 0 members participate.  Returns [] if none (legitimate
    empty leader set).  ``coverage`` is the frozen LEADERSHIP_COVERAGE by default.
    """
    pos = [x for x in aligned_ranked if x.aligned_score > 0.0]
    total_pos = sum(x.aligned_score for x in pos)
    if total_pos <= 0.0:
        return []
    cum = 0.0
    leader_set: list[AlignedLeadership] = []
    for x in pos:
        leader_set.append(x)
        cum += x.aligned_score
        if cum / total_pos >= coverage:
            break
    return leader_set


def build_leadership_snapshot(
    *,
    trade_date: str,
    ew_return: float | None,
    contribution_facts: LeadershipContributionFacts,
) -> LeadershipSnapshot:
    """Build one trade-date Leadership Snapshot (Layer-1).

    Consumes the CANONICAL ``equal_weight_return`` (scope direction) and the
    ALREADY-computed ``LeadershipContributionFacts``.  Does NOT re-compute
    amount_share or contribution, and does NOT re-derive EW from member returns.

    The frozen ``LEADERSHIP_COVERAGE = 0.50`` is used internally — the production
    API does NOT expose a coverage override (40/50/60 belongs to the research
    probe only).
    """
    direction, reason, status = _direction_from_ew(ew_return)
    if status == "unavailable":
        return LeadershipSnapshot(
            trade_date=trade_date,
            status="unavailable",
            reason=reason,
            direction=None,
            rankable_count=contribution_facts.rankable_count,
            leader_set=None,
        )
    assert direction is not None
    aligned = _aligned_ranking(contribution_facts, direction)
    leader_set = _minimal_prefix_leader_set(aligned, LEADERSHIP_COVERAGE)
    return LeadershipSnapshot(
        trade_date=trade_date,
        status="ready",
        reason=None,
        direction=direction,
        rankable_count=contribution_facts.rankable_count,
        leader_set=tuple(leader_set),
    )


# ---------------------------------------------------------------------------
# Layer 2 — T-1 vs T migration comparison
# ---------------------------------------------------------------------------


def compute_leadership_migration(
    *,
    previous_snapshot: LeadershipSnapshot,
    current_snapshot: LeadershipSnapshot,
) -> LeadershipMigrationFacts:
    """Compare two leadership snapshots (T-1 vs T) into migration facts (Layer-2).

    Only compares the two prepared snapshots — it does NOT touch MemberObservation,
    amount_share, contribution, or the scope direction.  The output ``trade_date``
    is taken uniquely from ``current_snapshot.trade_date`` (single owner), and
    ``coverage`` is always the frozen ``LEADERSHIP_COVERAGE`` (no override).

    Availability semantics (unavailable != 0, unknown != legitimate empty):

      - Either snapshot unavailable -> status=unavailable(unavailable_snapshot);
        the UNAVAILABLE side's leader_count/ids are None, the READY side's real
        evidence is preserved; transition-derived facts (retained/entrant/exit,
        retention/jaccard/migration) are all None (never zero-filled).
      - Both ready but either leader set legitimately empty -> status=unavailable(
        empty_leader_set); snapshot counts/ids keep their real 0/() values; the
        set-difference (retained/entrant/exit) may be reported but
        retention/jaccard/migration remain None (fail-closed).
    """
    prev = previous_snapshot
    curr = current_snapshot

    def _side_counts(snap: LeadershipSnapshot) -> tuple[int | None, tuple[str, ...] | None]:
        """leader_count/leader_ids for one snapshot; None for unavailable side."""
        if snap.status == "unavailable":
            return None, None
        return len(snap.leader_ids), snap.leader_ids

    prev_count, prev_ids = _side_counts(prev)
    curr_count, curr_ids = _side_counts(curr)

    if prev.status == "unavailable" or curr.status == "unavailable":
        return LeadershipMigrationFacts(
            trade_date=curr.trade_date,
            status="unavailable",
            reason="unavailable_snapshot",
            coverage=LEADERSHIP_COVERAGE,
            previous_direction=prev.direction,
            current_direction=curr.direction,
            previous_rankable_count=prev.rankable_count,
            current_rankable_count=curr.rankable_count,
            previous_leader_count=prev_count,
            current_leader_count=curr_count,
            retained_count=None,
            entrant_count=None,
            exit_count=None,
            previous_retention=None,
            jaccard_stability=None,
            migration=None,
            previous_leader_ids=prev_ids,
            current_leader_ids=curr_ids,
            entrant_ids=None,
            exit_ids=None,
        )

    prev_set = set(prev.leader_ids)
    curr_set = set(curr.leader_ids)

    # Both ready; report real set-difference even if a side is empty.
    retained = prev_set & curr_set
    entrants = curr_set - prev_set
    exits = prev_set - curr_set

    # Legitimate empty leader set on either side -> Migration metrics fail-closed.
    if not prev_set or not curr_set:
        return LeadershipMigrationFacts(
            trade_date=curr.trade_date,
            status="unavailable",
            reason="empty_leader_set",
            coverage=LEADERSHIP_COVERAGE,
            previous_direction=prev.direction,
            current_direction=curr.direction,
            previous_rankable_count=prev.rankable_count,
            current_rankable_count=curr.rankable_count,
            previous_leader_count=len(prev.leader_ids),
            current_leader_count=len(curr.leader_ids),
            retained_count=len(retained),
            entrant_count=len(entrants),
            exit_count=len(exits),
            previous_retention=None,
            jaccard_stability=None,
            migration=None,
            previous_leader_ids=prev.leader_ids,
            current_leader_ids=curr.leader_ids,
            entrant_ids=tuple(sorted(entrants)),
            exit_ids=tuple(sorted(exits)),
        )

    union = prev_set | curr_set

    jaccard = len(retained) / len(union) if union else None
    retention = len(retained) / len(prev_set) if prev_set else None
    migration = 1.0 - jaccard if jaccard is not None else None

    return LeadershipMigrationFacts(
        trade_date=curr.trade_date,
        status="ready",
        reason=None,
        coverage=LEADERSHIP_COVERAGE,
        previous_direction=prev.direction,
        current_direction=current_snapshot.direction,
        previous_rankable_count=previous_snapshot.rankable_count,
        current_rankable_count=current_snapshot.rankable_count,
        previous_leader_count=len(prev_ids),
        current_leader_count=len(curr_ids),
        retained_count=len(retained),
        entrant_count=len(entrants),
        exit_count=len(exits),
        previous_retention=retention,
        jaccard_stability=jaccard,
        migration=migration,
        previous_leader_ids=previous_snapshot.leader_ids,
        current_leader_ids=current_snapshot.leader_ids,
        entrant_ids=tuple(sorted(entrants)),
        exit_ids=tuple(sorted(exits)),
    )


def serialize_leadership_migration(facts: LeadershipMigrationFacts) -> dict[str, Any]:
    """Single application serialization boundary for Leadership migration.

    The ``LeadershipMigrationFacts`` dataclass is the domain owner output and MUST
    stay a dataclass (Member Attribution consumes it directly).  This function is
    the ONLY place the facts are turned into a ``Mapping[str, Any]`` for
    Composition / persistence / API consumption.  Do NOT add a second serializer
    or make the dataclass itself dict-like — that would split the boundary.
    """
    return {
        "status": facts.status,
        "coverage": facts.coverage,
        "previous_direction": facts.previous_direction,
        "current_direction": facts.current_direction,
        "previous_rankable_count": facts.previous_rankable_count,
        "current_rankable_count": facts.current_rankable_count,
        "previous_leader_count": facts.previous_leader_count,
        "current_leader_count": facts.current_leader_count,
        "retained_count": facts.retained_count,
        "entrant_count": facts.entrant_count,
        "exit_count": facts.exit_count,
        "previous_retention": facts.previous_retention,
        "jaccard_stability": facts.jaccard_stability,
        "migration": facts.migration,
        "previous_leader_ids": list(facts.previous_leader_ids)
        if facts.previous_leader_ids is not None else None,
        "current_leader_ids": list(facts.current_leader_ids)
        if facts.current_leader_ids is not None else None,
        "entrant_ids": list(facts.entrant_ids)
        if facts.entrant_ids is not None else None,
        "exit_ids": list(facts.exit_ids)
        if facts.exit_ids is not None else None,
    }


__all__ = [
    "LEADERSHIP_COVERAGE",
    "AlignedLeadership",
    "LeadershipSnapshot",
    "LeadershipMigrationFacts",
    "build_leadership_snapshot",
    "compute_leadership_migration",
    "serialize_leadership_migration",
]
