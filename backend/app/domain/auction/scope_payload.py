"""Auction V3.2 canonical payload assembly + read-back (zero schema change).

Persistence decision (V3.2 §十四): the existing ``auction_scope_results`` table
already offers ``scope_type`` (which carries the family), ``scope_id``,
``scope_name``, ``payload`` (JSONB) and ``reason_codes``, and
``auction_analysis_publications`` already offers ``algorithm_version``.

Therefore V3.2 needs **no migration**: the whole canonical payload is carried
inside ``payload``, and both ``schema_version`` and ``algorithm_version`` are
recorded inside it.  The publication pointer stays the visibility owner.

This module is pure: it builds/validates dicts and touches no ORM session.
The service layer decides how to persist them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "build_scope_payload",
    "parse_scope_payload",
    "validate_scope_identity",
    "canonical_scope_key",
    "canonical_scope_name",
]

#: Bumped whenever the canonical payload shape changes.  Readers MUST refuse a
#: payload whose schema_version they do not understand (never silently guess).
SCHEMA_VERSION = "auction-scope-v3.2"


def build_scope_payload(
    *,
    algorithm_version: str,
    repricing: dict[str, Any],
    historical_dynamics: dict[str, Any],
    participation: dict[str, Any],
    cross_sectional: dict[str, Any],
    member_attribution: dict[str, Any],
    identity: dict[str, Any],
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the canonical V3.2 payload, including stable scope identity.

    ``identity`` MUST carry ``scope_key`` (the stable business identity, i.e.
    ``MarketBoard.externalCode``) and ``scope_name`` (display label).  They are
    two DIFFERENT things: the name is a human label and must never be used as
    the lookup key.  Carrying both here avoids any migration while keeping the
    identity lossless from computation through persistence to the API.
    """
    if not identity.get("scope_key"):
        raise ValueError("identity.scope_key is required (stable business scope key)")
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm_version": algorithm_version,
        "identity": dict(identity),
        "repricing": dict(repricing),
        "historical_dynamics": dict(historical_dynamics),
        "participation": dict(participation),
        "cross_sectional": dict(cross_sectional),
        "member_attribution": dict(member_attribution),
        "diagnostics": dict(diagnostics or {}),
    }


def validate_scope_identity(identity: Any) -> str:
    """Validate a canonical scope identity and return its scope_key.

    Fail-closed rules:
      - identity must be a Mapping;
      - scope_key must be a non-empty string (never a UUID, never empty);
      - scope_name is a display label only and is never required as identity.
    """
    if not isinstance(identity, Mapping):
        raise ValueError("auction scope payload identity must be a mapping")
    scope_key = identity.get("scope_key")
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError(
            "auction scope payload identity.scope_key must be a non-empty string"
        )
    return scope_key

def canonical_scope_key(payload: Mapping[str, Any]) -> str:
    """Read the canonical ``scope_key`` from a validated V3.2 payload.

    Never falls back to ``scope_name`` and never falls back to a UUID: guessing
    the identity at the API layer is exactly the defect this owner prevents.
    """
    return validate_scope_identity(payload.get("identity"))


def canonical_scope_name(payload: Mapping[str, Any]) -> str | None:
    identity = payload.get("identity")
    if not isinstance(identity, Mapping):
        return None
    name = identity.get("scope_name")
    return str(name) if name is not None else None


def parse_scope_payload(payload: Any) -> dict[str, Any]:
    """Validate and return a V3.2 canonical payload.

    Fails fast on an unknown schema_version instead of guessing at keys —
    a payload written by a different contract must never be read as V3.2.
    """
    if not isinstance(payload, dict):
        raise ValueError("auction scope payload must be a JSON object")

    schema_version = payload.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported auction scope payload schema_version: {schema_version!r} "
            f"(expected {SCHEMA_VERSION!r})"
        )

    required = (
        "identity",
        "repricing",
        "historical_dynamics",
        "participation",
        "cross_sectional",
        "member_attribution",
    )
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"auction scope payload missing groups: {missing}")

    # Fail-closed on identity: a payload that reaches persistence must already
    # carry a usable canonical scope_key, otherwise persistence would accept it
    # and only the API reader would fail later.
    validate_scope_identity(payload["identity"])
    return payload
