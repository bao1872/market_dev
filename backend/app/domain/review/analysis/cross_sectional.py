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

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# C1_CORE_FIELDS — the ONLY allowed comparable fields (PRD §7.8.1 C).
# ---------------------------------------------------------------------------
#
# For each field we record:
#   * ``l1_path``  — exact L1 canonical payload dict path (verified against
#                    ``scope_observation.compute_scope_observation`` output).
#   * ``extract``  — how to obtain the comparable *scalar* from the L1 node.
#
# L1 output shapes (verified):
#   price.equal_weight_return             -> float scalar
#   price.amount_weighted_return          -> float scalar
#   trend.continuous.regime_strength      -> float scalar
#   participation.volume.ratio20          -> dict {p25, p50, p75, valid_count}
#   participation.volume.ratio200         -> dict {p25, p50, p75, valid_count}
#   momentum.bb_position                  -> dict {median, p25, p50, p75, ...}
#   momentum.bb_width                     -> dict {median, p25, p50, p75, ...}
#
# Distribution-valued fields are compared by their central tendency ``p50``
# (equal to ``median`` in the L1 producer).  This is the single, explicit,
# fail-closed scalar extraction — no other key is consulted.
#
# The L1 path *string* used as the stable ``field`` key in the output is the
# PRD-facing dotted path (e.g. ``participation.volume.ratio20``), distinct from
# the internal tuple used to deep-read the payload.

_MINIMUM_VALID_PEER_COUNT = 5


def _scalar_direct(node: Any) -> float | None:
    """Return the node itself if it is a finite number, else None."""
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        value = float(node)
        if math.isfinite(value):
            return value
    return None


def _scalar_p50(node: Any) -> float | None:
    """Extract the comparable scalar (p50) from an L1 distribution dict.

    Returns None when the node is not a dict or its ``p50`` is missing/non-finite.
    """
    if not isinstance(node, dict):
        return None
    return _scalar_direct(node.get("p50"))


@dataclass(frozen=True)
class _C1FieldSpec:
    """Single comparable field contract."""

    field_key: str
    l1_path: tuple[str, ...]
    extract: Callable[[Any], float | None]


C1_CORE_FIELDS: tuple[_C1FieldSpec, ...] = (
    _C1FieldSpec("equal_weight_return", ("price", "equal_weight_return"), _scalar_direct),
    _C1FieldSpec("amount_weighted_return", ("price", "amount_weighted_return"), _scalar_direct),
    _C1FieldSpec(
        "trend.continuous.regime_strength",
        ("trend", "continuous", "regime_strength"),
        _scalar_direct,
    ),
    _C1FieldSpec(
        "participation.volume.ratio20",
        ("participation", "volume", "ratio20"),
        _scalar_p50,
    ),
    _C1FieldSpec(
        "participation.volume.ratio200",
        ("participation", "volume", "ratio200"),
        _scalar_p50,
    ),
    _C1FieldSpec("momentum.bb_position", ("momentum", "bb_position"), _scalar_p50),
    _C1FieldSpec("momentum.bb_width", ("momentum", "bb_width"), _scalar_p50),
)

_C1_FIELD_INDEX: dict[str, _C1FieldSpec] = {spec.field_key: spec for spec in C1_CORE_FIELDS}


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
    spec: _C1FieldSpec,
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


def is_known_c1_field(field_key: str) -> bool:
    """True iff ``field_key`` is in the C1_CORE_FIELDS allowlist."""
    return field_key in _C1_FIELD_INDEX


__all__ = [
    "C1_CORE_FIELDS",
    "CrossSectionalFieldResult",
    "compute_cross_sectional",
    "is_known_c1_field",
]
