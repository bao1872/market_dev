"""Canonical Review Composition — the single Review composition owner.

REVIEW-CANONICAL-RUNTIME-REPLACEMENT.  The unique orchestrator composition
contract that wires the frozen layers into one deterministic package:

    scope_observation
        -> historical_dynamics
        -> internal_structure_facts
        -> leadership
        -> member_attribution
        -> composition_readiness

This module owns ONLY composition + readiness aggregation.  It NEVER re-derives
any algorithm: each layer is produced by its single canonical owner upstream
(scope_observation from ``compute_scope_observation`` / PreparedScope;
historical_dynamics from ``compute_scope_dynamics_analysis``;
internal_structure_facts from ``compute_internal_structure``; leadership from
``compute_leadership_migration``; member_attribution from
``compute_member_attribution``).  There is no per-attribution recompute, no
score / threshold / Internal Structure Type / Trading Context, no second fast
implementation.

Contract
--------
Each composed layer is a dict carrying the canonical availability ``status``
vocabulary: ``ready`` / ``insufficient_history`` / ``unavailable_current``
(FROZEN, PRD §7.9 — no new statuses like warming / stale / paused), OR ``None``
when the layer is not applicable.  ``composition_readiness`` is the
deterministic merge of the *required* layers' statuses with the frozen
precedence ``unavailable_current > insufficient_history > ready``.

Fail-closed (REVIEW-ATOMIC-BUSINESS-CUTOVER): if a layer that the scope's
capability marks as required is absent (``None``) or carries a failed status,
the composition is NOT ready — the caller must not substitute a legacy result.
A genuinely capable-but-not-yet-present gap is expressed as
``unavailable_current`` (or the scope's capability reports it as a structured
skip upstream), never as a silent legacy fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.review.review_capability import ScopeCapability

# Frozen availability vocabulary (same strings as Position facts / Persistence).
STATUS_READY = "ready"
STATUS_INSUFFICIENT = "insufficient_history"
STATUS_UNAVAILABLE = "unavailable_current"

# Fixed composition output keys (the ONLY top-level keys the owner returns).
COMPOSITION_LAYER_KEYS = (
    "scope_observation",
    "historical_dynamics",
    "internal_structure_facts",
    "leadership",
    "member_attribution",
)

# Leadership is a frozen pure-domain owner (LeadershipMigration), but the
# orchestrator runtime does not yet compute LeadershipContributionFacts /
# previous-snapshot -> migration this round, so it is carried as a structured
# unavailable_current layer (never a legacy fallback).
LEADERSHIP_NOT_RUNTIME_WIRED_REASON = (
    "leadership_not_runtime_wired: the orchestrator canonical composition phase "
    "does not yet compute LeadershipContributionFacts / LeadershipMigration in "
    "the runtime; Leadership is explicitly unavailable_current"
)


class ReviewCompositionError(RuntimeError):
    """A required composition layer is absent/failed and must not be faked."""


def _layer_status(layer: Mapping[str, Any] | None) -> str | None:
    """Read the canonical status of one layer (None when layer not provided)."""
    if layer is None:
        return None
    status = layer.get("status")
    if status not in (_STATUS_SET):
        raise ReviewCompositionError(f"layer has unknown status: {status!r}")
    return status


def structured_unavailable_layer(reason: str) -> dict[str, str]:
    """Deterministic present-but-unavailable layer for a runtime-unwired stage.

    Used by the orchestrator composition phase to carry a layer the runtime does
    not yet compute (e.g. Historical Dynamics / Leadership) as an explicit
    ``unavailable_current`` with a structured reason — never a legacy fallback,
    never a fabricated ``0``/``ready``.
    """
    return {"status": STATUS_UNAVAILABLE, "reason": reason}


_STATUS_SET = frozenset({STATUS_READY, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE})


def _merge_status(statuses: list[str]) -> str:
    """Frozen precedence ``unavailable_current > insufficient_history > ready``."""
    if any(s == STATUS_UNAVAILABLE for s in statuses):
        return STATUS_UNAVAILABLE
    if any(s == STATUS_INSUFFICIENT for s in statuses):
        return STATUS_INSUFFICIENT
    return STATUS_READY


def _required_layers(capability: ScopeCapability) -> frozenset[str]:
    """Layers required for a ready composition given this scope's capability.

    - scope_observation is always required (the primary canonical fact).
    - historical_dynamics is required only when the family can resolve
      current-static membership x historical member facts AND the orchestrator
      runtime actually wires the ObservationSeries -> Dynamics chain
      (``historical_dynamics_available`` folds in
      ``historical_dynamics_runtime_wired``).  This round it is not wired for
      any family, so dynamics is propagated as a present-but-unavailable layer
      and never falsely gates readiness.
    - member_attribution is required when the family is persistence-activated
      (attribution consumes canonical scope aggregate + members).
    - internal_structure_facts / leadership stay present-only (propagated when
      provided); the report does not gate readiness on them.
    """
    required = {"scope_observation"}
    if capability.historical_dynamics_available:
        required.add("historical_dynamics")
    if capability.member_attribution_available:
        required.add("member_attribution")
    return frozenset(required)


def compose_canonical_review_scope(
    *,
    scope_type: str,
    scope_key: str,
    trade_date: str,
    capability: ScopeCapability,
    scope_observation: Mapping[str, Any] | None = None,
    historical_dynamics: Mapping[str, Any] | None = None,
    internal_structure_facts: Mapping[str, Any] | None = None,
    leadership: Mapping[str, Any] | None = None,
    member_attribution: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the fixed 6-key Canonical Review Composition for one scope.

    Args:
        scope_type / scope_key / trade_date: deterministic scope identity.
        capability: the scope-family capability record (single owner).
        scope_observation / historical_dynamics / internal_structure_facts /
            leadership / member_attribution: the frozen layer outputs.  Each
            layer dict must carry ``status`` ∈ {ready, insufficient_history,
            unavailable_current}, or ``None`` (not applicable).

    Returns (fixed contract):
        ``{"scope", "trade_date", "capability", "scope_observation",
        "historical_dynamics", "internal_structure_facts", "leadership",
        "member_attribution", "composition_readiness"}``.

    Fail-closed: a required layer that is missing (None) or whose producer did
    not attach a status fails fast with ``ReviewCompositionError`` — it is never
    substituted by a legacy/fallback result.
    """
    layers = {
        "scope_observation": scope_observation,
        "historical_dynamics": historical_dynamics,
        "internal_structure_facts": internal_structure_facts,
        "leadership": leadership,
        "member_attribution": member_attribution,
    }
    required = _required_layers(capability)
    produced = {k: v for k, v in layers.items() if v is not None}

    # Fail-closed: every required layer must be produced AND carry a valid status.
    missing = required - produced.keys()
    unstatused = [
        k
        for k in required.intersection(produced.keys())
        if _layer_status(produced[k]) is None
    ]
    if missing or unstatused:
        raise ReviewCompositionError(
            f"required composition layer missing/unstatused for {scope_type}/{scope_key}: "
            f"missing={sorted(missing)} unstatused={unstatused}; "
            f"refusing to substitute a legacy result"
        )

    # Readiness = frozen merge over the produced layers that are required.
    required_statuses: list[str] = []
    for k in COMPOSITION_LAYER_KEYS:
        if k in required and k in produced:
            st = _layer_status(produced[k])
            assert st is not None  # required produced layers are statused (above)
            required_statuses.append(st)
    readiness = _merge_status(required_statuses)

    return {
        "scope": {"scope_type": scope_type, "scope_key": scope_key},
        "trade_date": trade_date,
        "capability": {
            "scope_type": capability.scope_type,
            "persistence_activated": capability.persistence_activated,
            "historical_dynamics_available": capability.historical_dynamics_available,
            "member_attribution_available": capability.member_attribution_available,
            "reason": capability.reason,
        },
        "scope_observation": scope_observation,
        "historical_dynamics": historical_dynamics,
        "internal_structure_facts": internal_structure_facts,
        "leadership": leadership,
        "member_attribution": member_attribution,
        "composition_readiness": readiness,
    }


__all__ = [
    "COMPOSITION_LAYER_KEYS",
    "LEADERSHIP_NOT_RUNTIME_WIRED_REASON",
    "STATUS_READY",
    "STATUS_INSUFFICIENT",
    "STATUS_UNAVAILABLE",
    "ReviewCompositionError",
    "compose_canonical_review_scope",
    "structured_unavailable_layer",
]
