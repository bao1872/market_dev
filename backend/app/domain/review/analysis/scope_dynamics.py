"""Analysis B — Scope Dynamics Canonical Composition Owner.

The single pure-domain composition owner that produces a Scope's Dynamics
Phase from a canonical L1 observation series:

    Canonical L1 Observation Series
        -> EW Historical Position
        -> Historical Dynamics
        -> Dynamics Phase

It ONLY orchestrates the three frozen domain owners:

    historical_position.compute_position_series
    historical_dynamics.compute_historical_dynamics_series
    dynamics_phase.compute_dynamics_phase_series

This module is a composition owner, NOT a second algorithm owner.  It never
re-implements Position percentile / EMA / Velocity / Acceleration / Persistence
/ Phase predicates / Phase thresholds / status merge, and it never adds score /
confidence / trend label / capital confirmation / breadth / volume confirmation
/ Internal Structure / Trading Context.

Input contract
--------------
``observation_series`` must already be a date-complete, canonical, ordered
Scope L1 observation series in the shape consumed by
``historical_position.compute_position_series``:

    [{"trade_date": ISO_DATE, "observation": canonical_l1_payload}, ...]

with ``trade_date`` strictly ASCENDING.  Date-completeness / trading-observation
gap handling is a caller contract — this module does NOT judge calendar, does NOT
fill trading days, and does NOT interpret an absent row as unavailable.  There is
NO persisted-history (``{"payload": ...}``) compatibility branch here; an IO
adapter is responsible for converting that shape upstream.

Ownership boundary
------------------
Pure domain layer.  This module NEVER touches the database / service /
membership / reconstruction / API / persistence / publication / calendar
reconciliation.  It only composes the three frozen domain owners.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.domain.review.analysis.dynamics_phase import compute_dynamics_phase_series
from app.domain.review.analysis.historical_dynamics import (
    compute_historical_dynamics_series,
)
from app.domain.review.analysis.historical_position import compute_position_series

# The unique Phase-driving primitive: Dynamics Phase lifecycle owner = EW Return.
# This is a constant expression, not a fallback — amount_weighted / breadth /
# volume / regime_strength never participate in Dynamics Phase.
DYNAMICS_PHASE_PRIMITIVE_KEY = "equal_weight_return"


def compute_scope_dynamics_analysis(
    observation_series: Sequence[Any],
) -> dict[str, Any]:
    """Compose one Scope's Dynamics Phase chain from a canonical L1 series.

    Args:
        observation_series: canonical Scope L1 observation series, trade_date
            strictly ASCENDING (see module docstring for the input contract).

    Returns (fixed contract):
        ``{"primitive_key": "equal_weight_return",
        "historical_dynamics": <historical_dynamics.compute_historical_dynamics_series
        output>, "dynamics_phase": <dynamics_phase.compute_dynamics_phase_series
        output>}``.

    The math is delegated 1:1 to the three frozen domain owners — this function
    performs no threshold / percentile / EMA / persistence / phase logic of its
    own.  Ascending-date / value / status validation and fail-fast behaviour are
    inherited from those owners (no silent sort is introduced here).
    """
    position_series = compute_position_series(
        observation_series,
        DYNAMICS_PHASE_PRIMITIVE_KEY,
    )
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
