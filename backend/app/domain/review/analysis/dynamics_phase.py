"""Analysis B — Dynamics Phase pure-domain classifier (PRD §7.11 FROZEN).

Consumes the four frozen EW Historical Dynamics inputs — Position, Velocity,
Acceleration and Persistence — and produces the scope-level Dynamics Phase
(six product labels) plus a transparent evidence payload.

Frozen contract (PRD §7.11 "Dynamics Phase Numerical Contract"):

- exact constants (never approximated / never caller-overridable):
  ``DYNAMICS_PHASE_VELOCITY_GATE = 2.0``,
  ``DYNAMICS_PHASE_ACCELERATION_GATE = 1.0``,
  ``DYNAMICS_PHASE_POSITION_HIGH = 70.0``,
  ``DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE = 0.20``,
  ``DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE = 0.30``;
- exact state helpers: ``velocity_state`` (V_NEG / V_MID / V_POS) and
  ``acceleration_state`` (A_NEG / A_ZERO / A_POS) — derived numerical states,
  never new product phases;
- exact context helpers: ``high_regime`` (position >= 70.0 AND
  upper_occupancy >= 0.20) and ``bottom_recovery_context`` (position < 70.0
  AND lower_occupancy >= 0.30) — ``position < 70`` alone is NOT bottom;
- exact phase conditions (by construction mutually exclusive, priority = NONE):
  Weakening / Decelerating / Sustained / Early Lift / Repairing / Strengthening;
  no priority chain, no tie-break, no rank;
- availability FIRST (``unavailable_current > insufficient_history > ready``)
  driven by upstream ``status`` only — never by ``value is None`` (null is a
  result value, not an availability cause);
- ready value validation: Position finite in [0, 100]; Velocity / Acceleration
  finite; upper_occupancy / lower_occupancy finite in [0, 1]; a ``ready`` input
  with a non-finite / out-of-range value is an upstream contract violation and
  fails fast (never silently unavailable / phase=None / clamped);
- ready but unclassified -> ``status = ready`` / ``phase = None`` (not a
  seventh phase, not unavailable — no forced full coverage);
- no ``phase_score`` / ``confidence_score`` / ``strength_score`` /
  ``composite_score``.

Ownership boundary
------------------
Pure domain layer.  This module NEVER touches the database / service /
membership / reconstruction / API / frontend / orchestrator.  It consumes
the already-computed frozen EW dynamics inputs (as produced by
``historical_dynamics.compute_historical_dynamics_series``) and returns
objective Phase facts.  This round deliberately stops at the pure-domain
slice: no scope-level EW synthesis, no persistence, no API.

Canonical label owner
---------------------
The six phase product labels live here (the single canonical vocabulary —
there is no other enum / Literal in the repository to reuse).  No other file
should hard-code these strings.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.domain.review.analysis.historical_dynamics import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
)

# PRD §7.11 frozen constants — exact, never approximated.
DYNAMICS_PHASE_VELOCITY_GATE = 2.0
DYNAMICS_PHASE_ACCELERATION_GATE = 1.0
DYNAMICS_PHASE_POSITION_HIGH = 70.0
DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE = 0.20
DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE = 0.30

# Phase product labels — the single canonical vocabulary (PRD §7.11).
PHASE_EARLY_LIFT = "Early Lift"
PHASE_STRENGTHENING = "Strengthening"
PHASE_SUSTAINED = "Sustained"
PHASE_DECELERATING = "Decelerating"
PHASE_WEAKENING = "Weakening"
PHASE_REPAIRING = "Repairing"
VALID_PHASES: frozenset[str] = frozenset(
    {
        PHASE_EARLY_LIFT,
        PHASE_STRENGTHENING,
        PHASE_SUSTAINED,
        PHASE_DECELERATING,
        PHASE_WEAKENING,
        PHASE_REPAIRING,
    }
)

# Derived numerical state strings (never product phases).
V_STATE_NEG = "V_NEG"
V_STATE_MID = "V_MID"
V_STATE_POS = "V_POS"
A_STATE_NEG = "A_NEG"
A_STATE_ZERO = "A_ZERO"
A_STATE_POS = "A_POS"


# ---------------------------------------------------------------------------
# Pure helpers (no IO, no mutation)
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or None when non-finite / non-numeric."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _trade_date(td: Any) -> date:
    """Normalize a trade date (ISO string or ``datetime.date``) to ``date``."""
    if isinstance(td, str):
        return date.fromisoformat(td)
    return td


def _merge_status(statuses: Sequence[str]) -> str:
    """Frozen precedence ``unavailable_current > insufficient_history > ready``."""
    if any(s == STATUS_UNAVAILABLE for s in statuses):
        return STATUS_UNAVAILABLE
    if any(s == STATUS_INSUFFICIENT for s in statuses):
        return STATUS_INSUFFICIENT
    return STATUS_READY


def _known_status(status: str) -> bool:
    return status in (STATUS_READY, STATUS_INSUFFICIENT, STATUS_UNAVAILABLE)


# ---------------------------------------------------------------------------
# Exact state helpers (derived numerical states, not new phases)
# ---------------------------------------------------------------------------


def velocity_state(velocity: float) -> str:
    """Exact derived state (PRD §7.11): V_NEG / V_MID / V_POS."""
    if velocity <= -DYNAMICS_PHASE_VELOCITY_GATE:
        return V_STATE_NEG
    if velocity <= DYNAMICS_PHASE_VELOCITY_GATE:
        return V_STATE_MID
    return V_STATE_POS


def acceleration_state(acceleration: float) -> str:
    """Exact derived state (PRD §7.11): A_NEG / A_ZERO / A_POS."""
    if acceleration <= -DYNAMICS_PHASE_ACCELERATION_GATE:
        return A_STATE_NEG
    if acceleration <= DYNAMICS_PHASE_ACCELERATION_GATE:
        return A_STATE_ZERO
    return A_STATE_POS


# ---------------------------------------------------------------------------
# Exact context helpers
# ---------------------------------------------------------------------------


def high_regime(position: float, upper_occupancy: float) -> bool:
    """HIGH_REGIME = position >= 70.0 AND upper_occupancy >= 0.20."""
    return position >= DYNAMICS_PHASE_POSITION_HIGH and (
        upper_occupancy >= DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE
    )


def bottom_recovery_context(position: float, lower_occupancy: float) -> bool:
    """BOTTOM_RECOVERY_CONTEXT = position < 70.0 AND lower_occupancy >= 0.30.

    ``position < 70`` alone is NOT bottom; it is a joint eligibility context.
    """
    return position < DYNAMICS_PHASE_POSITION_HIGH and (
        lower_occupancy >= DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE
    )


# ---------------------------------------------------------------------------
# Exact classifier (priority = NONE, by construction mutually exclusive)
# ---------------------------------------------------------------------------


def _classify_phase(
    *,
    position: float,
    velocity: float,
    acceleration: float,
    upper_occupancy: float,
    lower_occupancy: float,
) -> str | None:
    """Return the single matching phase, or None (ready but unclassified).

    All six raw predicates are evaluated independently; mutual exclusion is a
    mathematical property of the frozen conditions — NOT an artifact of an
    if/elif ordering.  If the invariant is ever violated the implementation
    fails fast instead of silently picking one.
    """
    hr = high_regime(position, upper_occupancy)
    brc = bottom_recovery_context(position, lower_occupancy)

    weakening = velocity <= -DYNAMICS_PHASE_VELOCITY_GATE
    decelerating = (
        hr
        and velocity > -DYNAMICS_PHASE_VELOCITY_GATE
        and acceleration <= -DYNAMICS_PHASE_ACCELERATION_GATE
    )
    sustained = (
        hr
        and velocity > -DYNAMICS_PHASE_VELOCITY_GATE
        and -DYNAMICS_PHASE_ACCELERATION_GATE < acceleration <= DYNAMICS_PHASE_ACCELERATION_GATE
    )
    early_lift = (
        brc
        and velocity > DYNAMICS_PHASE_VELOCITY_GATE
        and acceleration > DYNAMICS_PHASE_ACCELERATION_GATE
    )
    repairing = (
        brc
        and velocity > -DYNAMICS_PHASE_VELOCITY_GATE
        and acceleration <= DYNAMICS_PHASE_ACCELERATION_GATE
    )
    strengthening = (
        velocity > DYNAMICS_PHASE_VELOCITY_GATE
        and acceleration > DYNAMICS_PHASE_ACCELERATION_GATE
        and not brc
    )

    matched = [
        phase
        for phase, condition in (
            (PHASE_WEAKENING, weakening),
            (PHASE_DECELERATING, decelerating),
            (PHASE_SUSTAINED, sustained),
            (PHASE_EARLY_LIFT, early_lift),
            (PHASE_REPAIRING, repairing),
            (PHASE_STRENGTHENING, strengthening),
        )
        if condition
    ]
    if len(matched) > 1:
        raise RuntimeError(
            f"dynamics phase mutual exclusion violated (priority = NONE): matched={matched}"
        )
    return matched[0] if matched else None


# ---------------------------------------------------------------------------
# Single-observation classifier
# ---------------------------------------------------------------------------


def compute_dynamics_phase(
    *,
    position: float | None,
    velocity: float | None,
    acceleration: float | None,
    upper_occupancy: float | None,
    lower_occupancy: float | None,
    position_status: str,
    velocity_status: str,
    acceleration_status: str,
    persistence_status: str,
) -> dict[str, Any]:
    """Classify one Dynamics Phase observation (pure domain, stateless).

    Args (keyword-only):
        position: EW Position value (the 0-100 percentile).  Only consumed when
            ``position_status == STATUS_READY``.
        velocity: EW Velocity value.  Only consumed when ready.
        acceleration: EW Acceleration value.  Only consumed when ready.
        upper_occupancy: EW Persistence ``upper_occupancy`` (0-1).  Only
            consumed when ready.
        lower_occupancy: EW Persistence ``lower_occupancy`` (0-1).  Only
            consumed when ready.
        position_status / velocity_status / acceleration_status /
        persistence_status: the exact upstream status vocabulary
            (``ready`` / ``insufficient_history`` / ``unavailable_current``).

    Availability is derived from upstream ``status`` ONLY — never from
    ``value is None``.

    Returns (transparent fact, deterministic, non-mutating):
        ``{"phase", "status", "position", "velocity", "acceleration",
        "upper_occupancy", "lower_occupancy", "velocity_state",
        "acceleration_state", "high_regime", "bottom_recovery_context"}``.

        ``phase`` is one of the six product labels, or None when all inputs are
        ready but no condition matches (ready-but-unclassified — never a
        seventh phase / never unavailable).  ``status`` is ``ready`` /
        ``insufficient_history`` / ``unavailable_current``.  Evidence fields are
        only populated on the ready path; no score fields are ever emitted.
    """
    all_statuses = (
        position_status,
        velocity_status,
        acceleration_status,
        persistence_status,
    )
    for name, status in (
        ("position", position_status),
        ("velocity", velocity_status),
        ("acceleration", acceleration_status),
        ("persistence", persistence_status),
    ):
        if not _known_status(status):
            raise ValueError(f"unknown {name} status: {status!r}")
    status = _merge_status(all_statuses)

    fact: dict[str, Any] = {
        "phase": None,
        "status": status,
        "position": None,
        "velocity": None,
        "acceleration": None,
        "upper_occupancy": None,
        "lower_occupancy": None,
        "velocity_state": None,
        "acceleration_state": None,
        "high_regime": None,
        "bottom_recovery_context": None,
    }
    if status != STATUS_READY:
        return fact

    # Ready-value validation — fail fast on upstream contract violations.
    pos = _finite(position)
    if pos is None:
        raise ValueError("status=ready with non-finite Position value")
    if not 0.0 <= pos <= 100.0:
        raise ValueError(f"status=ready with out-of-range Position: {pos} (must be in [0, 100])")
    vel = _finite(velocity)
    if vel is None:
        raise ValueError("status=ready with non-finite Velocity value")
    acc = _finite(acceleration)
    if acc is None:
        raise ValueError("status=ready with non-finite Acceleration value")
    upper = _finite(upper_occupancy)
    if upper is None:
        raise ValueError("status=ready with non-finite upper_occupancy value")
    if not 0.0 <= upper <= 1.0:
        raise ValueError(
            f"status=ready with out-of-range upper_occupancy: {upper} (must be in [0, 1])"
        )
    lower = _finite(lower_occupancy)
    if lower is None:
        raise ValueError("status=ready with non-finite lower_occupancy value")
    if not 0.0 <= lower <= 1.0:
        raise ValueError(
            f"status=ready with out-of-range lower_occupancy: {lower} (must be in [0, 1])"
        )

    fact.update(
        {
            "position": pos,
            "velocity": vel,
            "acceleration": acc,
            "upper_occupancy": upper,
            "lower_occupancy": lower,
            "velocity_state": velocity_state(vel),
            "acceleration_state": acceleration_state(acc),
            "high_regime": high_regime(pos, upper),
            "bottom_recovery_context": bottom_recovery_context(pos, lower),
            "phase": _classify_phase(
                position=pos,
                velocity=vel,
                acceleration=acc,
                upper_occupancy=upper,
                lower_occupancy=lower,
            ),
        }
    )
    return fact


# ---------------------------------------------------------------------------
# Series integration (date-aligned over the frozen Historical Dynamics package)
# ---------------------------------------------------------------------------


def compute_dynamics_phase_series(
    dynamics_package: Mapping[str, Sequence[Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """Compute the Dynamics Phase series aligned to the four frozen inputs.

    Args:
        dynamics_package: the output of
            ``historical_dynamics.compute_historical_dynamics_series`` for ONE
            primitive (keys ``position`` / ``velocity`` / ``acceleration`` /
            ``persistence``), each series trade_date ASCENDING.

    Contract:
        - each T output consumes ONLY the same-day T facts of the four frozen
          inputs — T+1 / future rows are never consulted (no future leakage);
        - a missing required key, a length mismatch, a non-ascending series or
          a trade_date misalignment across series fails fast;
        - the per-row classification delegates to :func:`compute_dynamics_phase`
          (single canonical owner — no duplicated classifier here).

    Returns:
        One fact per input day, date-aligned (never compressed):
        ``{"trade_date", "phase", "status", ...evidence}`` (the single-
        observation fact plus ``trade_date``).
    """
    required = ("position", "velocity", "acceleration", "persistence")
    for key in required:
        if key not in dynamics_package:
            raise KeyError(f"dynamics_package missing required key: {key}")
    position = dynamics_package["position"]
    velocity = dynamics_package["velocity"]
    acceleration = dynamics_package["acceleration"]
    persistence = dynamics_package["persistence"]

    n = len(position)
    if not (len(velocity) == n and len(acceleration) == n and len(persistence) == n):
        raise ValueError(
            "position / velocity / acceleration / persistence must share the same length"
        )

    pos_dates = [_trade_date(item["trade_date"]) for item in position]
    for prev, cur in zip(pos_dates, pos_dates[1:], strict=False):
        if not prev < cur:
            raise ValueError(
                f"dynamics package must be strictly ascending by trade_date; got {prev} -> {cur}"
            )
    for name, series in (
        ("velocity", velocity),
        ("acceleration", acceleration),
        ("persistence", persistence),
    ):
        for i, item in enumerate(series):
            if _trade_date(item["trade_date"]) != pos_dates[i]:
                raise ValueError(f"{name} series not aligned with position series at index {i}")

    out: list[dict[str, Any]] = []
    for i in range(n):
        p = position[i]
        v = velocity[i]
        a = acceleration[i]
        per = persistence[i]
        fact = compute_dynamics_phase(
            position=p["position"],
            velocity=v["value"],
            acceleration=a["value"],
            upper_occupancy=per["upper_occupancy"],
            lower_occupancy=per["lower_occupancy"],
            position_status=p["status"],
            velocity_status=v["status"],
            acceleration_status=a["status"],
            persistence_status=per["status"],
        )
        out.append({"trade_date": p["trade_date"], **fact})
    return out


__all__ = [
    "DYNAMICS_PHASE_VELOCITY_GATE",
    "DYNAMICS_PHASE_ACCELERATION_GATE",
    "DYNAMICS_PHASE_POSITION_HIGH",
    "DYNAMICS_PHASE_UPPER_OCCUPANCY_GATE",
    "DYNAMICS_PHASE_LOWER_OCCUPANCY_GATE",
    "PHASE_EARLY_LIFT",
    "PHASE_STRENGTHENING",
    "PHASE_SUSTAINED",
    "PHASE_DECELERATING",
    "PHASE_WEAKENING",
    "PHASE_REPAIRING",
    "VALID_PHASES",
    "velocity_state",
    "acceleration_state",
    "high_regime",
    "bottom_recovery_context",
    "compute_dynamics_phase",
    "compute_dynamics_phase_series",
]
