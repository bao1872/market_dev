"""Observation Primitive Registry (v2.3 shared infrastructure).

Single source of truth for **canonical observation fact primitives**: the stable
field key, the exact L1 canonical payload path, and the rule for extracting the
comparable scalar from the L1 node.

Ownership boundary
-------------------
This registry owns:

    * canonical field key          (stable dotted path, PRD-facing)
    * canonical L1 payload path    (tuple used to deep-read the payload)
    * scalar extraction rule       (how to obtain the comparable scalar)
    * availability extraction      (None when the scalar is missing/non-finite)

It does NOT own:

    * percentile calculation               -> Analysis A (cross-sectional)
    * historical calculation               -> Analysis B (historical dynamics)
    * velocity / acceleration / persistence -> Analysis B
    * trend / structure-change judgment    -> Analysis B / C
    * persistence / storage                -> observation persistence service
    * signal / opportunity / risk          -> Discovery / screening (out of scope)

Consumers
---------
    * C1 Cross-sectional Analysis (§7.8.1)
    * Future Historical Dynamics   (§7.9) — consumes the same primitives over a
      historical series
    * Future Internal Structure    (§7.10) — consumes structure primitives

L1 output shapes (verified against ``scope_observation.compute_scope_observation``):

    price.equal_weight_return             -> float scalar
    price.amount_weighted_return          -> float scalar
    price.breadth.advance_ratio           -> float scalar (or None)
    price.breadth.decline_ratio           -> float scalar (or None)
    price.breadth.unchanged_ratio         -> float scalar (or None)
    price.return_dispersion               -> float scalar (or None)
    price.concentration.normalized_hhi    -> float scalar (or None)
    price.amount.concentration.normalized_hhi -> float scalar (or None)
    trend.continuous.regime_strength      -> float scalar
    participation.volume.ratio20          -> dict {p25, p50, p75, valid_count}
    participation.volume.ratio200         -> dict {p25, p50, p75, valid_count}
    momentum.bb_position                  -> dict {median, p25, p75, valid_count}
    momentum.bb_width                    -> dict {median, p25, p75, valid_count}

Note the distribution fields are NOT uniform: ``participation`` exposes ``p50``
while ``momentum`` exposes ``median`` (both equal the 50th percentile of the
member distribution).  The central-tendency extractor therefore reads ``p50``
first and falls back to ``median`` — a single, explicit, fail-closed rule.  No
other key is ever consulted.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ObservationPrimitiveSpec:
    """A single canonical observation primitive contract.

    Attributes:
        key: stable dotted path used as the PRD-facing field identifier
            (e.g. ``"participation.volume.ratio20"``).
        path: exact L1 canonical payload dict path (tuple of keys) used to
            deep-read the node from the payload.
        extract: callable ``(node: Any) -> float | None`` returning the
            comparable scalar, or ``None`` when the primitive is unavailable
            (missing node, missing/non-finite central value).

    Backward-compatible aliases (used by existing call sites / tests):
        ``field_key`` -> ``key``
        ``l1_path``   -> ``path``
    """

    key: str
    path: tuple[str, ...]
    extract: Callable[[Any], float | None]

    @property
    def field_key(self) -> str:
        return self.key

    @property
    def l1_path(self) -> tuple[str, ...]:
        return self.path


# ---------------------------------------------------------------------------
# Extraction rules
# ---------------------------------------------------------------------------


def _scalar_direct(node: Any) -> float | None:
    """Return the node itself if it is a finite number, else None."""
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        value = float(node)
        if math.isfinite(value):
            return value
    return None


def _scalar_central_tendency(node: Any) -> float | None:
    """Extract the comparable scalar from an L1 distribution dict.

    Distribution-valued primitives are compared by their central tendency.
    ``participation`` distributions expose ``p50``; ``momentum`` distributions
    expose ``median`` (equal to the 50th percentile).  Read ``p50`` first, then
    fall back to ``median``.  Returns None when the node is not a dict or its
    central value is missing/non-finite.
    """
    if not isinstance(node, dict):
        return None
    central: float | None = _scalar_direct(node.get("p50"))
    if central is None:
        central = _scalar_direct(node.get("median"))
    return central


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

OBSERVATION_PRIMITIVES: dict[str, ObservationPrimitiveSpec] = {
    spec.key: spec
    for spec in (
        ObservationPrimitiveSpec(
            "equal_weight_return",
            ("price", "equal_weight_return"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "amount_weighted_return",
            ("price", "amount_weighted_return"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "advance_ratio",
            ("price", "breadth", "advance_ratio"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "decline_ratio",
            ("price", "breadth", "decline_ratio"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "unchanged_ratio",
            ("price", "breadth", "unchanged_ratio"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "return_dispersion",
            ("price", "return_dispersion"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "price_normalized_hhi",
            ("price", "concentration", "normalized_hhi"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "amount_normalized_hhi",
            ("price", "amount", "concentration", "normalized_hhi"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "trend.continuous.regime_strength",
            ("trend", "continuous", "regime_strength"),
            _scalar_direct,
        ),
        ObservationPrimitiveSpec(
            "participation.volume.ratio20",
            ("participation", "volume", "ratio20"),
            _scalar_central_tendency,
        ),
        ObservationPrimitiveSpec(
            "participation.volume.ratio200",
            ("participation", "volume", "ratio200"),
            _scalar_central_tendency,
        ),
        ObservationPrimitiveSpec(
            "momentum.bb_position",
            ("momentum", "bb_position"),
            _scalar_central_tendency,
        ),
        ObservationPrimitiveSpec(
            "momentum.bb_width",
            ("momentum", "bb_width"),
            _scalar_central_tendency,
        ),
    )
}


def get_primitive(key: str) -> ObservationPrimitiveSpec:
    """Return the registered primitive for ``key`` (raises KeyError if unknown)."""
    return OBSERVATION_PRIMITIVES[key]


def list_primitive_keys() -> list[str]:
    """Return all registered primitive keys in registration order."""
    return list(OBSERVATION_PRIMITIVES.keys())
