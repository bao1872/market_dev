"""Leadership T-1→T migration — Review canonical runtime owner (REVIEW-BACKEND-FINAL-CLOSURE Phase 5.5).

Single batch owner that loads REAL [T-1, T] member facts ONCE per scope family
and computes the real Leadership Migration (T-1 vs T).  It does NOT synthesize a
fake ``previous`` snapshot: when T-1 facts are genuinely unavailable the previous
snapshot is honestly ``status="unavailable"`` and the migration is honestly
``unavailable`` — never wrapped to look like a computed migration.

Ownership boundary (single-owner + NO-SECOND-IMPLEMENTATION):
- member facts are loaded by the neutral ``prepare_current_scope_observations_batch``
  owner (one call with ``trade_dates=[T-1, T]``), no per-scope N+1, no second
  reconstruction owner.
- contribution is produced by the single owner ``compute_member_leadership_contributions``.
- ``ew_return`` (scope equal-weight return) is CONSUMED from the single canonical
  owner ``compute_scope_observation(prep)["price"]["equal_weight_return"]`` — this
  service NEVER re-derives EW from member returns.  Re-using the canonical owner
  keeps Leadership and Scope Observation consistent under any future change to
  candidate eligibility / None-NaN rules / denominator / current-only semantics.
- snapshot + migration are produced by the frozen owners
  ``build_leadership_snapshot`` / ``compute_leadership_migration``.
- the ``LeadershipMigrationFacts`` dataclass is the single domain output; the
  application serialization boundary (``serialize_leadership_migration``) is the
  ONLY place it is turned into a dict for Composition/persistence/API.

Pure + deterministic.  No DB write.  No future-leak (T+1 never read).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.analysis.leadership_contribution import (
    LeadershipContributionFacts,
    compute_member_leadership_contributions,
)
from app.domain.review.analysis.leadership_migration import (
    LeadershipMigrationFacts,
    LeadershipSnapshot,
    build_leadership_snapshot,
    compute_leadership_migration,
)
from app.domain.review.scope_observation import compute_scope_observation
from app.services.review_observation_prep_service import (
    PreparedScope,
    prepare_current_scope_observations_batch,
)


def _canonical_equal_weight_return(prep: PreparedScope) -> float | None:
    """Equal-weight return for the leadership snapshot.

    Consumed from the SINGLE canonical owner ``compute_scope_observation`` — never
    re-derived from member returns here.  ``events=None`` / ``event_coverage_member_ids=()``
    is the legal empty-structure-event day for the price EW universe; it does not
    affect the price equal_weight_return the leadership snapshot needs.
    """
    observation = compute_scope_observation(
        scope_type=prep.scope_type,
        scope_key=prep.scope_key,
        trade_date=prep.trade_date,
        pit_member_ids=prep.pit_member_ids,
        pit_member_ids_t1=prep.pit_member_ids_t1,
        members=prep.members,
        events=None,
        event_coverage_member_ids=(),
    )
    return observation["price"]["equal_weight_return"]


def _build_snapshot(prep: PreparedScope) -> LeadershipSnapshot:
    """Build one trade-date Leadership Snapshot from REAL prepared members."""
    contribution_facts: LeadershipContributionFacts = (
        compute_member_leadership_contributions(prep.members)
    )
    ew_return = _canonical_equal_weight_return(prep)
    return build_leadership_snapshot(
        trade_date=prep.trade_date.isoformat(),
        ew_return=ew_return,
        contribution_facts=contribution_facts,
    )


async def compute_scope_leadership_batch(
    session: AsyncSession,
    trade_date: date,
    scope_specs: list[Any],
) -> dict[str, LeadershipMigrationFacts]:
    """Compute real T-1→T Leadership Migration for a batch of scopes.

    Loads [T-1, T] member facts ONCE via the neutral prep owner (no per-scope
    N+1), builds a real T-1 LeadershipSnapshot, the real T snapshot, then the
    migration.  Returns ``scope_key -> LeadershipMigrationFacts`` for every input
    spec (never raises on unavailable T-1; it emits an honest unavailable
    migration instead of faking a previous snapshot).
    """
    from app.services.calendar_service import get_previous_trading_day_async

    t1 = await get_previous_trading_day_async(session, trade_date)
    if t1 is None:
        # No real T-1 exists — every scope gets an honest unavailable migration.
        return {
            spec.scope_key: compute_leadership_migration(
                previous_snapshot=_unavailable_snapshot(trade_date),
                current_snapshot=_unavailable_snapshot(trade_date),
            )
            for spec in scope_specs
        }

    # Single neutral load of [T-1, T] member facts for the whole batch.
    series = await prepare_current_scope_observations_batch(
        session, trade_date, scope_specs, trade_dates=[t1, trade_date]
    )

    result: dict[str, LeadershipMigrationFacts] = {}
    for spec in scope_specs:
        spec_series = series.get(spec.scope_key)
        prev_prep = None
        curr_prep = None
        if isinstance(spec_series, list):
            for prep in spec_series:
                if prep.trade_date == t1:
                    prev_prep = prep
                elif prep.trade_date == trade_date:
                    curr_prep = prep

        prev_snapshot = (
            _build_snapshot(prev_prep)
            if prev_prep is not None
            else _unavailable_snapshot(t1)
        )
        curr_snapshot = (
            _build_snapshot(curr_prep)
            if curr_prep is not None
            else _unavailable_snapshot(trade_date)
        )
        result[spec.scope_key] = compute_leadership_migration(
            previous_snapshot=prev_snapshot,
            current_snapshot=curr_snapshot,
        )
    return result


def _unavailable_snapshot(trade_date: date | None) -> LeadershipSnapshot:
    """Honest unavailable Leadership snapshot (no real member facts)."""
    return LeadershipSnapshot(
        trade_date=(trade_date.isoformat() if trade_date is not None else ""),
        status="unavailable",
        reason="no_real_t_minus_1_member_facts",
        direction=None,
        rankable_count=0,
        leader_set=None,
    )


__all__ = ["compute_scope_leadership_batch"]
