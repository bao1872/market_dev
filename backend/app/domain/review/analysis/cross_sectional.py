"""C1 Cross-sectional Analysis — pure deterministic projection (v2.3 §7.8.1).

Ownership boundary (PRD §7.8.1):

    L1 canonical observation payload(s)  (read-only)
        -> pure deterministic projection
        -> cross-sectional position evidence

C1 is a **derived view**.  This module:

- does NOT access the database, bars, ticks, first-pyramid raw, or indicators;
- does NOT recompute any L1/L2 fact;
- does NOT mutate any input (payloads are read, never modified);
- does NOT produce score / rank / direction / opportunity / risk semantics.

It answers one question per comparable field: "where does the current scope's
value sit within the valid peer distribution?" — expressed as an empirical
percentile rank (position evidence), nothing more.

All comparable fields are enumerated in ``C1_CORE_FIELDS``.  Any field path not
in that table is rejected fail-closed (``UNKNOWN_FIELD``).  No semantic mapping
or fallback is performed: a missing path produces ``unavailable``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.review.observation_primitives import (
    OBSERVATION_PRIMITIVES,
    ObservationPrimitiveSpec,
)

# ---------------------------------------------------------------------------
# C1_CORE_FIELDS — the ONLY allowed comparable fields (PRD §7.8.1 C).
# ---------------------------------------------------------------------------
#
# These are a subset of the shared ``OBSERVATION_PRIMITIVES`` registry, in the
# fixed C1 order.  Each entry is an ``ObservationPrimitiveSpec`` carrying the
# canonical ``key`` (PRD-facing dotted path), ``path`` (L1 payload deep-read
# tuple), and ``extract`` (comparable scalar rule).  The registry is the single
# source of truth for path + extraction; C1 does not redefine them.
#
# Backward-compatible aliases kept so existing call sites (and tests) that read
# ``spec.field_key`` / ``spec.l1_path`` continue to work:
#   ``field_key`` -> ``ObservationPrimitiveSpec.key``
#   ``l1_path``   -> ``ObservationPrimitiveSpec.path``

_MINIMUM_VALID_PEER_COUNT = 5

# C1 consumes these registry keys, in this fixed order.
_C1_PRIMITIVE_KEYS: tuple[str, ...] = (
    "equal_weight_return",
    "amount_weighted_return",
    "trend.continuous.regime_strength",
    "participation.volume.ratio20",
    "participation.volume.ratio200",
    "momentum.bb_position",
    "momentum.bb_width",
)

C1_CORE_FIELDS: tuple[ObservationPrimitiveSpec, ...] = tuple(
    OBSERVATION_PRIMITIVES[key] for key in _C1_PRIMITIVE_KEYS
)


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossSectionalFieldResult:
    """Cross-sectional position evidence for a single comparable field.

    ``percentile`` is empirical percentile rank in [0, 100] (position evidence,
    NOT a score/rank).  ``None`` when ``status == "unavailable"``.
    """

    field: str
    value: float | None
    percentile: float | None
    peer_count: int
    valid_peer_count: int
    status: str
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "percentile": self.percentile,
            "peer_count": self.peer_count,
            "valid_peer_count": self.valid_peer_count,
            "status": self.status,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Pure helpers (no IO, no mutation)
# ---------------------------------------------------------------------------


def _deep_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Read a value from ``payload`` by path. Missing path -> None."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _empirical_percentile_rank(value: float, peers: list[float]) -> float:
    """Empirical percentile rank of ``value`` within ``peers`` (same convention as L1).

    ``percentile = (count(p < v) + 0.5 * count(p == v)) / len(peers) * 100``.
    ``peers`` is assumed non-empty and finite (caller guarantees).
    """
    below = sum(1 for p in peers if p < value)
    equal = sum(1 for p in peers if p == value)
    return (below + 0.5 * equal) / len(peers) * 100.0


# ---------------------------------------------------------------------------
# Domain entry point
# ---------------------------------------------------------------------------


def compute_cross_sectional(
    *,
    current_payload: dict[str, Any],
    peer_payloads: dict[str, dict[str, Any]],
    current_scope_key: str,
) -> dict[str, Any]:
    """Compute cross-sectional position evidence for the current scope.

    Pure + deterministic + non-mutating.

    Args:
        current_payload: the current scope's L1 canonical observation payload.
        peer_payloads: mapping ``scope_key -> L1 payload`` for the comparable
            peer cohort (same family, same trade_date), INCLUDING the current
            scope itself.  ``peer_count`` is ``len(peer_payloads)``.
        current_scope_key: the ``scope_key`` of the current scope, used to
            exclude the current scope from ``valid_peer_count`` (PRD §7.8.1 D:
            ``valid_peer_count`` excludes the current scope; the percentile
            denominator still includes it per §7.8.1 B "含自身参与").

    Returns:
        ``{"fields": [CrossSectionalFieldResult.to_dict(), ...]}`` in the fixed
        field order of ``C1_CORE_FIELDS``.
    """
    peer_count = len(peer_payloads)

    results: list[CrossSectionalFieldResult] = []
    for spec in C1_CORE_FIELDS:
        result = _compute_field(
            spec, current_payload, peer_payloads, current_scope_key, peer_count
        )
        results.append(result)

    return {"fields": [r.to_dict() for r in results]}


def _compute_field(
    spec: ObservationPrimitiveSpec,
    current_payload: dict[str, Any],
    peer_payloads: dict[str, dict[str, Any]],
    current_scope_key: str,
    peer_count: int,
) -> CrossSectionalFieldResult:
    """Compute one field's cross-sectional result (fail-closed)."""
    # 1. Current scope's comparable scalar.
    current_node = _deep_get(current_payload, spec.l1_path)
    current_value = spec.extract(current_node)

    if current_value is None:
        return CrossSectionalFieldResult(
            field=spec.field_key,
            value=None,
            percentile=None,
            peer_count=peer_count,
            valid_peer_count=0,
            status="unavailable",
            reason="CURRENT_FIELD_UNAVAILABLE",
        )

    # 2. Peer scalars (finite only), split by whether the payload belongs to the
    #    current scope.  Per PRD §7.8.1 D, ``valid_peer_count`` counts valid
    #    peers EXCLUDING the current scope; the percentile denominator (§B
    #    "含自身参与") includes current.
    valid_peer_excl_current: list[float] = []
    valid_with_current: list[float] = []
    for scope_key, payload in peer_payloads.items():
        node = _deep_get(payload, spec.l1_path)
        scalar = spec.extract(node)
        if scalar is None:
            continue
        valid_with_current.append(scalar)
        if scope_key != current_scope_key:
            valid_peer_excl_current.append(scalar)

    valid_peer_count = len(valid_peer_excl_current)

    # 3. Availability gate (PRD §7.8.1 D): minimum valid peer sample
    #    (current scope excluded).
    if peer_count == 0:
        return CrossSectionalFieldResult(
            field=spec.field_key,
            value=current_value,
            percentile=None,
            peer_count=peer_count,
            valid_peer_count=valid_peer_count,
            status="unavailable",
            reason="NO_PEERS",
        )

    if valid_peer_count < _MINIMUM_VALID_PEER_COUNT:
        return CrossSectionalFieldResult(
            field=spec.field_key,
            value=current_value,
            percentile=None,
            peer_count=peer_count,
            valid_peer_count=valid_peer_count,
            status="unavailable",
            reason="INSUFFICIENT_PEER_SAMPLE",
        )

    # 4. Percentile denominator includes the current scope (§7.8.1 B).
    percentile = _empirical_percentile_rank(current_value, valid_with_current)

    return CrossSectionalFieldResult(
        field=spec.field_key,
        value=current_value,
        percentile=percentile,
        peer_count=peer_count,
        valid_peer_count=valid_peer_count,
        status="ready",
        reason=None,
    )


_C1_FIELD_INDEX: dict[str, ObservationPrimitiveSpec] = {
    spec.key: spec for spec in C1_CORE_FIELDS
}


def is_known_c1_field(field_key: str) -> bool:
    """True iff ``field_key`` is in the C1_CORE_FIELDS allowlist."""
    return field_key in _C1_FIELD_INDEX


__all__ = [
    "C1_CORE_FIELDS",
    "CrossSectionalFieldResult",
    "compute_cross_sectional",
    "is_known_c1_field",
]
