"""Canonical Observation Fact Persistence (Round 1C).

Minimal persistence owner for ``review_scope_observation_facts``.

Ownership is only: serialize + validate contract shape + idempotent upsert +
read-back (prompt §1 / §11).  This module NEVER recomputes facts — no ratio /
HHI / transition / percentile / readiness algorithm re-derivation, no NULL
interpretation, no unavailable->0 coercion, no score / opportunity / risk /
strength / recommendation derivation (prompt §8 / §9).

Activation (prompt §15): only ``industry_l1 / industry_l2 / industry_l3 /
concept`` are persisted.  Market is NOT ACTIVATED FOR HISTORICAL PERSISTENCE
(prompt §16) and major_index / style are NOT ACTIVATED (prompt §17): a generic
loop passing them in must be blocked here even if the prep layer already guards.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_prep_service import PreparedScope

# Activated scope families for daily objective-fact persistence (prompt §15).
ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES: frozenset[str] = frozenset(
    {"industry_l1", "industry_l2", "industry_l3", "concept"}
)

# Market is explicitly NOT activated for historical persistence: the current
# active universe cannot be used against a historical trade_date (prompt §16).
MARKET_PERSISTENCE_DIAGNOSTIC = (
    "market_not_activated_for_historical_persistence: "
    "market is current active universe, not historical PIT; fact not persisted"
)

# The ONLY legal top-level sections of a Canonical Observation payload.  Any
# extra key (e.g. a subjective opportunity_score / marker / ranking) or a
# missing canonical section must be rejected before persistence (Round 1C
# correction Blocker #1).  This is the contract shape, not a semantic recompute.
CANONICAL_TOP_LEVEL_SECTIONS: frozenset[str] = frozenset(
    {"scope", "price", "amount", "trend", "structure", "momentum", "participation", "chip"}
)


class ScopePersistenceNotActivatedError(Exception):
    """Raised when a non-activated scope type is passed to persistence."""


class ScopeObservationPayloadValidationError(Exception):
    """Raised when a payload is not a valid Canonical Observation payload.

    Used for both top-level contract-shape violations (missing / extra section,
    non-dict section) and scope identity mismatch (scope_type / scope_key /
    trade_date).  This is contract validation only — never a recompute of
    ratio / HHI / transition / percentile / breadth / readiness / state.
    """


def validate_scope_observation_payload(
    observation: dict[str, Any],
    *,
    scope_type: str,
    scope_key: str,
    trade_date: date,
) -> None:
    """Contract-validate that a payload is a complete, identity-consistent
    Canonical Observation (Round 1C correction Blocker #1 / #3).

    Only checks:
    - top-level key set == the exact canonical section set (no extra subjective
      key, no missing canonical section);
    - every canonical section is a dict;
    - ``observation["scope"]`` identity (scope_type / scope_key / trade_date)
      matches the PreparedScope.

    It does NOT recompute any fact (save-only ownership, prompt §4): a legal
    partial axis (e.g. an empty denominator, an unavailable axis) is accepted as
    long as the full canonical structure and identity are intact.
    """
    if not isinstance(observation, dict):
        raise ScopeObservationPayloadValidationError(
            f"observation must be a dict, got {type(observation).__name__}"
        )
    actual = set(observation)
    if actual != CANONICAL_TOP_LEVEL_SECTIONS:
        missing = sorted(CANONICAL_TOP_LEVEL_SECTIONS - actual)
        extra = sorted(actual - CANONICAL_TOP_LEVEL_SECTIONS)
        raise ScopeObservationPayloadValidationError(
            "non-canonical top-level payload: "
            f"missing={missing or 'none'} extra={extra or 'none'}"
        )
    for section in CANONICAL_TOP_LEVEL_SECTIONS:
        if not isinstance(observation[section], dict):
            raise ScopeObservationPayloadValidationError(
                f"canonical section {section!r} must be a dict"
            )

    scope = observation["scope"]
    if not isinstance(scope, dict):
        raise ScopeObservationPayloadValidationError("scope section must be a dict")
    if scope.get("scope_type") != scope_type:
        raise ScopeObservationPayloadValidationError(
            f"scope_type mismatch: payload={scope.get('scope_type')!r} "
            f"expected={scope_type!r}"
        )
    if scope.get("scope_key") != scope_key:
        raise ScopeObservationPayloadValidationError(
            f"scope_key mismatch: payload={scope.get('scope_key')!r} expected={scope_key!r}"
        )
    if scope.get("trade_date") != trade_date.isoformat():
        raise ScopeObservationPayloadValidationError(
            f"trade_date mismatch: payload={scope.get('trade_date')!r} "
            f"expected={trade_date.isoformat()!r}"
        )


def _snapshot_readiness(prep: PreparedScope) -> str:
    """Snapshot-level readiness derived only from existing explicit states.

    No subjective coverage threshold is introduced (prompt §20).  ``unavailable``
    when PIT(T) is unresolvable; ``no_members`` when PIT(T) resolved but no member
    observation was provided; otherwise ``ready`` (a real observation snapshot
    was computed).  Partial axes inside the Core output never downgrade readiness.
    """
    if prep.pit_status_t == "unavailable":
        return "unavailable"
    if not prep.members:
        return "no_members"
    return "ready"


def _build_fact_values(
    prep: PreparedScope,
    observation: dict[str, Any],
    algorithm_version: str | None,
) -> dict[str, Any]:
    """Serialize PreparedScope metadata + Core observation result into a fact row.

    ``observation`` is stored as-is (same object, no copy / rename / recompute).
    This is the single serialize point and is kept pure for unit testing.
    """
    return {
        "trade_date": prep.trade_date,
        "scope_type": prep.scope_type,
        "scope_key": prep.scope_key,
        "scope_name": prep.scope_name or None,
        "canonical_t1": prep.canonical_t1,
        "pit_member_count": len(prep.pit_member_ids),
        "pit_member_count_t1": len(prep.pit_member_ids_t1),
        "provided_member_count": len(prep.members),
        "t1_membership_available": prep.t1_membership_available,
        "pit_status_t": prep.pit_status_t,
        "pit_status_t1": prep.pit_status_t1,
        "readiness": _snapshot_readiness(prep),
        "observation_payload": observation,
        "diagnostics": list(prep.diagnostics),
        "algorithm_version": algorithm_version,
    }


async def save_scope_observation_fact(
    db: AsyncSession,
    prep: PreparedScope,
    observation: dict[str, Any],
    *,
    algorithm_version: str | None = None,
) -> ReviewScopeObservationFact:
    """Idempotently persist one daily Canonical Observation Fact snapshot.

    Idempotent upsert on the business grain (trade_date, scope_type, scope_key):
    the first save inserts one row; a repeated save with a different payload
    updates that same row (row_count stays 1, payload replaced).

    Guards (in order):
    - activation: only industry_l1/l2/l3 + concept are persisted; market /
      major_index / style raise ``ScopePersistenceNotActivatedError`` even if a
      generic loop passes them in (prompt §16 / §17).
    - failure semantics: PIT(T) unavailable or no members -> not written, raises
      ``ValueError`` (never a fake ``observation_payload={}``) (prompt §19A).
    - contract validation: ``observation`` must be a complete, identity-consistent
      Canonical Observation payload (exact canonical top-level set + scope
      identity match); otherwise ``ScopeObservationPayloadValidationError``
      (Round 1C correction Blocker #1 / #3).
    """
    if prep.scope_type not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES:
        raise ScopePersistenceNotActivatedError(
            f"scope_type={prep.scope_type!r} not activated for observation persistence"
        )
    if prep.pit_status_t == "unavailable" or not prep.members:
        raise ValueError(
            "cannot persist fact for unavailable/incomplete scope: "
            f"pit_status_t={prep.pit_status_t!r}, provided_member_count={len(prep.members)}"
        )
    validate_scope_observation_payload(
        observation,
        scope_type=prep.scope_type,
        scope_key=prep.scope_key,
        trade_date=prep.trade_date,
    )

    values = _build_fact_values(prep, observation, algorithm_version)
    stmt = pg_insert(ReviewScopeObservationFact).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "scope_type", "scope_key"],
        set_={
            "scope_name": stmt.excluded.scope_name,
            "canonical_t1": stmt.excluded.canonical_t1,
            "pit_member_count": stmt.excluded.pit_member_count,
            "pit_member_count_t1": stmt.excluded.pit_member_count_t1,
            "provided_member_count": stmt.excluded.provided_member_count,
            "t1_membership_available": stmt.excluded.t1_membership_available,
            "pit_status_t": stmt.excluded.pit_status_t,
            "pit_status_t1": stmt.excluded.pit_status_t1,
            "readiness": stmt.excluded.readiness,
            "observation_payload": stmt.excluded.observation_payload,
            "diagnostics": stmt.excluded.diagnostics,
            "algorithm_version": stmt.excluded.algorithm_version,
            "updated_at": func.now(),
        },
    )
    await db.execute(stmt)
    await db.flush()
    fact = await get_scope_observation_fact(
        db, prep.trade_date, prep.scope_type, prep.scope_key
    )
    if fact is None:  # pragma: no cover - upsert always yields a row
        raise RuntimeError("scope observation fact missing after upsert")
    return fact


async def get_scope_observation_fact(
    db: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
) -> ReviewScopeObservationFact | None:
    """Read-back a single daily fact snapshot by its business grain."""
    stmt = select(ReviewScopeObservationFact).where(
        ReviewScopeObservationFact.trade_date == trade_date,
        ReviewScopeObservationFact.scope_type == scope_type,
        ReviewScopeObservationFact.scope_key == scope_key,
    )
    return (await db.execute(stmt)).scalars().first()


async def list_scope_observation_facts(
    db: AsyncSession,
    *,
    scope_type: str | None = None,
    scope_key: str | None = None,
    from_date: date | None = None,
    to_date: date | None = None,
) -> list[ReviewScopeObservationFact]:
    """List fact snapshots with optional filters, ordered by grain."""
    stmt = select(ReviewScopeObservationFact)
    if scope_type is not None:
        stmt = stmt.where(ReviewScopeObservationFact.scope_type == scope_type)
    if scope_key is not None:
        stmt = stmt.where(ReviewScopeObservationFact.scope_key == scope_key)
    if from_date is not None:
        stmt = stmt.where(ReviewScopeObservationFact.trade_date >= from_date)
    if to_date is not None:
        stmt = stmt.where(ReviewScopeObservationFact.trade_date <= to_date)
    stmt = stmt.order_by(
        ReviewScopeObservationFact.trade_date,
        ReviewScopeObservationFact.scope_type,
        ReviewScopeObservationFact.scope_key,
    )
    return list((await db.execute(stmt)).scalars())


if __name__ == "__main__":
    # 自测：验证 activation set / readiness 派生逻辑。
    assert ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES == frozenset(
        {"industry_l1", "industry_l2", "industry_l3", "concept"}
    )
    assert "market" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "major_index" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    assert "style" not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES
    print("OK: review_observation_persistence_service imports verified")
