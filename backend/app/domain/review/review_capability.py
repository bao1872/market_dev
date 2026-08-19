"""Scope Capability — the single owner of per-Scope-Family product capability.

REVIEW-CANONICAL-RUNTIME-REPLACEMENT.  Every Scope Family (market / major_index
/ style / industry_l1 / industry_l2 / industry_l3 / concept) is a PARALLEL
Scope Family in the new Review authority chain.  Whether a scope can persist a
canonical observation, resolve current / historical membership, and run
member attribution is an *implementation capability*, not a frozen permanent
architecture.  This module declares that capability declaratively so the
orchestrator treats a limited capability as a structured skip/reason and never
as a reason to fall back to legacy P/Q/U/C/V.

Pure domain.  No DB, no bundling of a todays-only limitation into a permanent
architecture switch.  The capability values are the product's *current*
implementation facts and are allowed to change (exploration mode) without any
legacy fallback being re-introduced.

The canonical persistence activation set (frozen SSOT): only
``industry_l1 / industry_l2 / industry_l3 / concept`` are persisted
(``ReviewScopeObservationFact``).  market / major_index / style are NOT
activated; hittng ``save_scope_observation_fact`` with them raises
``ScopePersistenceNotActivatedError``.  That activation boundary is decided
HERE (domain) and consumed by the persistence service so the guard and the
activation set can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The ONLY scope families in the new Review authority chain (parallel families).
ALL_SCOPE_FAMILIES: frozenset[str] = frozenset(
    {"market", "major_index", "style", "industry_l1", "industry_l2"}
    | {"industry_l3", "concept"}
)

# Canonical persistence activation set (frozen SSOT — must equal the set the
# persistence service enforces).  market / major_index / style are NOT activated.
SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES: frozenset[str] = frozenset(
    {"industry_l1", "industry_l2", "industry_l3", "concept"}
)


def is_scope_observation_persistence_activated(scope_type: str) -> bool:
    """True iff ``scope_type`` is in the canonical observation persistence set."""
    return scope_type in SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES


# Structured reason when a scope family cannot persist a canonical observation.
PERSISTENCE_NOT_ACTIVATED_REASON = (
    "canonical_persistence_not_activated: {scope_type} is not in the activated "
    "observation persistence set (industry_l1/industry_l2/industry_l3/concept); "
    "no canonical fact is persisted and this is a legal skip, NOT a failure and "
    "NOT a fallback to legacy P/Q/U/C/V"
)
# market historical-PIT limitation (implementation gap, not permanent prefix).
MARKET_HISTORICAL_MEMBERSHIP_PIT_GAP_REASON = (
    "market_historical_membership_pit_gap: the market resolver can only supply "
    "the CURRENT active universe, not a reliable historical/ASOF PIT member set; "
    "historical Dynamics / Persistence for market is unavailable until an exact-T "
    "market member source is implemented"
)
# Historical Dynamics runtime wiring status.  ``compute_scope_dynamics_analysis``
# is a frozen pure-domain owner, but the orchestrator runtime does NOT integrate
# the Current-Static Reconstruction -> ObservationSeries -> Dynamics application
# chain in this round (deferred to the next implementation; SCALE GATE required).
# Until that chain is wired, ``historical_dynamics_runtime_wired=False`` for EVERY
# family so composition readiness never falsely requires a layer the runtime does
# not produce.  It flips per-family (board families first) when the series
# integration lands.  This is honest exploration-state, not a permanent switch.
HISTORICAL_DYNAMICS_NOT_RUNTIME_WIRED_REASON = (
    "historical_dynamics_not_runtime_wired: the orchestrator canonical composition "
    "phase does not yet integrate the ObservationSeries -> Scope Dynamics chain; "
    "Historical Dynamics is explicitly unavailable_current (not a legacy fallback)"
)


@dataclass(frozen=True)
class ScopeCapability:
    """Declarative, per-scope-family product capability (single source).

    Attributes
    ----------
    scope_type / scope_name:
        Family identifier.
    persistence_activated:
        True iff canonical observation persistence is activated for this family
        (== membership in ``SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES``).
    current_membership_available:
        True iff exact-T current membership can be resolved today.
    historical_membership_available:
        True iff a reliable historical/ASOF PIT member set can be resolved
        today.  This is an implementation capability — it may change.
    historical_dynamics_runtime_wired:
        True iff the orchestrator canonical composition phase actually integrates
        the ObservationSeries -> Scope Dynamics computation for this family in
        the CURRENT runtime.  False this round for every family (deferred, SCALE
        GATE required) — so composition readiness never demands a layer the
        runtime does not produce.
    canonical_observation_available:
        ``persistence_activated and current_membership_available``.
    historical_dynamics_available:
        ``current_membership_available and historical_membership_available and
        historical_dynamics_runtime_wired`` (Historical Dynamics needs the
        current-static membership crossed with historical member facts AND a
        runtime that actually computes the series).
    member_attribution_available:
        whether member attribution is available for this family.
    reason:
        Structured availability reason when any capability is False, else None.
    """

    scope_type: str
    scope_name: str
    persistence_activated: bool
    current_membership_available: bool
    historical_membership_available: bool
    historical_dynamics_runtime_wired: bool = False
    member_attribution_available: bool = False
    reason: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def canonical_observation_available(self) -> bool:
        return self.persistence_activated and self.current_membership_available

    @property
    def historical_dynamics_available(self) -> bool:
        return (
            self.current_membership_available
            and self.historical_membership_available
            and self.historical_dynamics_runtime_wired
        )


def _scope_reason(scope_type: str) -> tuple[str, ...]:
    """Structured reasons for the families that are NOT fully capable today.

    ``concept`` A-class mechanism/event labels and tiny concepts are handled by
    the concrete observation layer (persistence exclusion), not here.
    """
    reasons: list[str] = []
    if scope_type == "market":
        reasons.append(MARKET_HISTORICAL_MEMBERSHIP_PIT_GAP_REASON)
    return tuple(reasons)


def resolve_scope_capability(*, scope_type: str, scope_name: str) -> ScopeCapability:
    """Resolve the ScopeFamily capability record for one scope.

    Slow-path agnostic: this is a small closed matrix (7 families), so a
    dataclass lookup is deterministic and trivially unit-testable.  Unknown
    scope_type fails fast (never silently defaults to full capability).
    """
    if scope_type not in ALL_SCOPE_FAMILIES:
        raise ValueError(f"unknown scope family: {scope_type!r}")

    persistence_activated = is_scope_observation_persistence_activated(scope_type)
    # Current membership is resolvable for every family today (market via active
    # universe; board families via PIT membership).  Only historical membership
    # for market is not yet resolvable (implementation gap).
    current_membership_available = True
    historical_membership_available = scope_type != "market"
    member_attribution_available = current_membership_available and persistence_activated
    # Historical Dynamics is NOT wired into the orchestrator runtime this round
    # for any family (ObservationSeries -> Dynamics chain deferred, SCALE GATE).
    historical_dynamics_runtime_wired = False

    reasons = list(_scope_reason(scope_type))
    if not persistence_activated:
        reasons.append(
            PERSISTENCE_NOT_ACTIVATED_REASON.format(scope_type=scope_type)
        )
    if not historical_dynamics_runtime_wired:
        reasons.append(HISTORICAL_DYNAMICS_NOT_RUNTIME_WIRED_REASON)

    return ScopeCapability(
        scope_type=scope_type,
        scope_name=scope_name,
        persistence_activated=persistence_activated,
        current_membership_available=current_membership_available,
        historical_membership_available=historical_membership_available,
        historical_dynamics_runtime_wired=historical_dynamics_runtime_wired,
        member_attribution_available=member_attribution_available,
        reason=reasons[0] if reasons else None,
        reasons=tuple(reasons),
    )


def capability_to_json(cap: ScopeCapability) -> dict[str, Any]:
    """Deterministic serializable view of a ScopeCapability (no secrets, sorted)."""
    return {
        "scope_type": cap.scope_type,
        "scope_name": cap.scope_name,
        "persistence_activated": cap.persistence_activated,
        "current_membership_available": cap.current_membership_available,
        "historical_membership_available": cap.historical_membership_available,
        "historical_dynamics_runtime_wired": cap.historical_dynamics_runtime_wired,
        "member_attribution_available": cap.member_attribution_available,
        "canonical_observation_available": cap.canonical_observation_available,
        "historical_dynamics_available": cap.historical_dynamics_available,
        "reason": cap.reason,
        "reasons": list(cap.reasons),
    }


__all__ = [
    "ALL_SCOPE_FAMILIES",
    "SCOPE_OBSERVATION_PERSISTENCE_ACTIVATED_TYPES",
    "MARKET_HISTORICAL_MEMBERSHIP_PIT_GAP_REASON",
    "HISTORICAL_DYNAMICS_NOT_RUNTIME_WIRED_REASON",
    "PERSISTENCE_NOT_ACTIVATED_REASON",
    "ScopeCapability",
    "is_scope_observation_persistence_activated",
    "resolve_scope_capability",
    "capability_to_json",
]
