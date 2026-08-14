"""Analysis C — Internal Structure Foundation — pure derived view (PRD §14).

Ownership boundary (PRD §14 / §5.5):

    L1 canonical observation payload  (read-only)
        -> pure deterministic projection
        -> Internal Structure foundation facts

This module implements the frozen, history-free core of Analysis C:

    * Breadth        (PRD §14.1) — equal_weight_return / advance_ratio /
                     decline_ratio / unchanged_ratio / return_dispersion
    * Capital Tilt   (PRD §14.2) — amount_weighted_return - equal_weight_return
    * Concentration  (PRD §14.3) — price / amount normalized HHI

It is a **derived view**:

- does NOT access the database, bars, ticks, or indicators;
- does NOT recompute any L1 fact (Breadth / Concentration are read verbatim;
  Capital Tilt is the ONLY derived number and it is computed from the canonical
  EW / AW inputs, never written back to L1 — PRD §5.5 "Capital Tilt 不进入 L1");
- does NOT mutate its input (payloads are read, never modified);
- does NOT produce score / rank / direction / opportunity / risk / strong / weak
  semantics, and does NOT emit Leadership Migration or any interpretation.

Canonical inputs are consumed exclusively through the shared
``OBSERVATION_PRIMITIVES`` registry (the same single source of truth for
path + extraction used by Analysis A / C1).  There is deliberately NO second
path registry in this module.
"""
from __future__ import annotations

from typing import Any

from app.domain.review.observation_primitives import (
    ObservationPrimitiveSpec,
    get_primitive,
)

# Analysis C consumes exactly these registry keys, in this fixed order.  Every
# key is sourced from the shared canonical registry; none is redefined here.
_ANALYSIS_C_KEYS: tuple[str, ...] = (
    "equal_weight_return",
    "amount_weighted_return",
    "advance_ratio",
    "decline_ratio",
    "unchanged_ratio",
    "return_dispersion",
    "price_normalized_hhi",
    "amount_normalized_hhi",
)

_SPECS: dict[str, ObservationPrimitiveSpec] = {
    key: get_primitive(key) for key in _ANALYSIS_C_KEYS
}


# ---------------------------------------------------------------------------
# Pure helpers (no IO, no mutation)
# ---------------------------------------------------------------------------


def _deep_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Read a value from ``payload`` by path.  Missing path -> None."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _extract_scalar(spec: ObservationPrimitiveSpec, payload: dict[str, Any]) -> float | None:
    """Extract the comparable scalar for ``spec`` from the L1 payload.

    ``spec.extract`` already fail-closes on missing / non-finite nodes,
    returning ``None`` (never a silent 0 / fallback).
    """
    return spec.extract(_deep_get(payload, spec.path))


# ---------------------------------------------------------------------------
# Domain entry point
# ---------------------------------------------------------------------------


def compute_internal_structure(payload: dict[str, Any]) -> dict[str, Any]:
    """Compute the Analysis C Internal Structure Foundation facts.

    Pure + deterministic + non-mutating.

    Args:
        payload: the scope's canonical L1 observation payload (as produced by
            ``scope_observation.compute_scope_observation``).

    Returns:
        ``{"breadth": ..., "capital_tilt": ..., "concentration": ...}`` — a
        transparent fact structure.  ``capital_tilt`` is ``None`` (never ``0``)
        unless BOTH canonical EW and AW are available and finite.
    """
    ew = _extract_scalar(_SPECS["equal_weight_return"], payload)
    aw = _extract_scalar(_SPECS["amount_weighted_return"], payload)
    capital_tilt = aw - ew if (ew is not None and aw is not None) else None

    return {
        "breadth": {
            "equal_weight_return": ew,
            "advance_ratio": _extract_scalar(_SPECS["advance_ratio"], payload),
            "decline_ratio": _extract_scalar(_SPECS["decline_ratio"], payload),
            "unchanged_ratio": _extract_scalar(_SPECS["unchanged_ratio"], payload),
            "return_dispersion": _extract_scalar(_SPECS["return_dispersion"], payload),
        },
        "capital_tilt": {
            "equal_weight_return": ew,
            "amount_weighted_return": aw,
            "capital_tilt": capital_tilt,
        },
        "concentration": {
            "price_normalized_hhi": _extract_scalar(
                _SPECS["price_normalized_hhi"], payload
            ),
            "amount_normalized_hhi": _extract_scalar(
                _SPECS["amount_normalized_hhi"], payload
            ),
        },
    }


__all__ = [
    "compute_internal_structure",
]
