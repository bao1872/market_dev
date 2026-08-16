"""Analysis B — Scope Dynamics Canonical Composition Owner.

The single pure-domain composition owner that produces a Scope's Dynamics
Phase from a formal ObservationSeries:

    ObservationSeries (build_observation_series output)
        -> EW PrimitiveSeries / PrimitivePoints
        -> Historical Position
        -> Historical Dynamics
        -> Dynamics Phase

It ONLY orchestrates the three frozen domain owners:

    historical_position.compute_position_series_from_primitive_series
    historical_dynamics.compute_historical_dynamics_series
    dynamics_phase.compute_dynamics_phase_series

This module is a composition owner, NOT a second algorithm owner.  It never
re-implements Position percentile / EMA / Velocity / Acceleration / Persistence
/ Phase predicates / Phase thresholds / status merge, and it never adds score /
confidence / trend label / capital confirmation / breadth / volume confirmation
/ Internal Structure / Trading Context.

Input contract
--------------
``observation_series`` is the formal ObservationSeries produced by
``build_observation_series`` (PRD §7.7.5).  This function reads ONLY the
``equal_weight_return`` PrimitiveSeries and never touches raw L1 payloads, never
re-derives the timeline and never re-handles snapshot gaps — gap preservation
flows from the Builder straight through Position -> Historical Dynamics ->
Phase.  A missing trading-observation slot therefore survives the whole chain as
``unavailable_current`` (never compressed, never forward-filled, never fallback
to the previous day).

If the ObservationSeries does not carry ``equal_weight_return``, this function
fails fast with ``KeyError`` — there is NO fallback to amount-weighted / breadth
/ volume / regime strength.

Ownership boundary
------------------
Pure domain layer.  This module NEVER touches the database / service /
membership / reconstruction / API / persistence / publication / calendar
reconciliation.  It only composes the three frozen domain owners.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.review.analysis.dynamics_phase import compute_dynamics_phase_series
from app.domain.review.analysis.historical_dynamics import (
    compute_historical_dynamics_series,
)
from app.domain.review.analysis.historical_position import (
    compute_position_series_from_primitive_series,
)

# The unique Phase-driving primitive: Dynamics Phase lifecycle owner = EW Return.
# This is a constant expression, not a fallback — amount_weighted / breadth /
# volume / regime_strength never participate in Dynamics Phase.
DYNAMICS_PHASE_PRIMITIVE_KEY = "equal_weight_return"


def compute_scope_dynamics_analysis(
    observation_series: Mapping[str, Any],
) -> dict[str, Any]:
    """Compose one Scope's Dynamics Phase chain from a formal ObservationSeries.

    Args:
        observation_series: ``build_observation_series`` output (PRD §7.7.5), a
            dict carrying ``primitives`` keyed by registered primitive key.

    Returns (fixed contract):
        ``{"primitive_key": "equal_weight_return",
        "historical_dynamics": <historical_dynamics.compute_historical_dynamics_series
        output>, "dynamics_phase": <dynamics_phase.compute_dynamics_phase_series
        output>}``.

    The math is delegated 1:1 to the three frozen domain owners — this function
    performs no threshold / percentile / EMA / persistence / phase logic of its
    own.  Date / value / status validation and fail-fast behaviour are inherited
    from those owners (no silent sort is introduced here).  Missing EW primitive
    -> ``KeyError`` (fail fast, no fallback).
    """
    primitive_series = observation_series["primitives"][DYNAMICS_PHASE_PRIMITIVE_KEY]
    position_series = compute_position_series_from_primitive_series(primitive_series)
    historical_dynamics = compute_historical_dynamics_series(position_series)
    dynamics_phase = compute_dynamics_phase_series(historical_dynamics)
    return {
        "primitive_key": DYNAMICS_PHASE_PRIMITIVE_KEY,
        "historical_dynamics": historical_dynamics,
        "dynamics_phase": dynamics_phase,
    }


__all__ = [
    "DYNAMICS_PHASE_PRIMITIVE_KEY",
    "compute_scope_dynamics_analysis",
]
