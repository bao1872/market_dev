"""Auction V3.2 Scope historical dynamics owner (EW primary, AW confirmation).

Chain (V3.2 §九), computed from the Scope **EW Gap** series::

    EW Gap series
      -> Historical Position (strictly pre-T, window 120 / min 60)
      -> EMA5(Position) and EMA20(Position)
      -> Velocity   = EMA5 - EMA20
      -> Signal     = EMA5(Velocity)
      -> Acceleration = Velocity - Signal

AW Gap is computed through the SAME arithmetic but is a **capital
confirmation** only: it must never become a second competing lifecycle, and no
phase / strength / opportunity label is produced here (V3.2 §十：不继承 Review 阶段、
生命周期、强弱标签).

Amount participation (V3.2 §十四) answers a different question and is kept
separate: Scope Total Amount Position + Amount Multiple (current / pre-T
median).  It is NOT merged with member abnormal breadth into one score.

All math is delegated to the shared primitives (percentile / EMA).  This module
owns only the Auction-specific wiring and status propagation.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.domain.shared.ema import (
    STATUS_INSUFFICIENT,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    compute_ema_series,
)
from app.domain.shared.historical_position import (
    POSITION_MINIMUM_VALID_HISTORY,
    POSITION_WINDOW_SIZE,
    compute_historical_position,
)

__all__ = [
    "DynamicsPoint",
    "ScopeDynamics",
    "compute_position_series",
    "compute_dynamics",
    "compute_amount_participation",
]

FAST_SPAN = 5
SLOW_SPAN = 20


@dataclass(frozen=True)
class DynamicsPoint:
    """One point of the objective dynamics series (no label, no phase)."""

    trade_date: date
    value: float | None
    position: float | None
    ema_fast: float | None
    ema_slow: float | None
    velocity: float | None
    signal: float | None
    acceleration: float | None
    status: str


@dataclass(frozen=True)
class ScopeDynamics:
    industry_or_concept_agnostic: str = "ew"
    points: tuple[DynamicsPoint, ...] = ()

    def latest(self) -> DynamicsPoint | None:
        return self.points[-1] if self.points else None


def compute_position_series(
    values_by_date: Sequence[tuple[date, float | None]],
    *,
    window_size: int = POSITION_WINDOW_SIZE,
    minimum_valid_history: int = POSITION_MINIMUM_VALID_HISTORY,
) -> list[dict[str, Any]]:
    """Per-date Historical Position where the baseline is STRICTLY pre-T.

    ``values_by_date`` must be ascending.  For date ``i`` the baseline is
    ``values[:i]`` — T never enters its own denominator, and no future value
    is visible.
    """
    out: list[dict[str, Any]] = []
    for i, (d, value) in enumerate(values_by_date):
        baseline = [v for _, v in values_by_date[:i]]
        pos = compute_historical_position(
            value,
            baseline,
            window_size=window_size,
            minimum_valid_history=minimum_valid_history,
        )
        out.append(
            {
                "trade_date": d.isoformat(),
                "value": pos["position"],
                "raw_value": pos["value"],
                "status": pos["status"],
                "history": pos["history"],
            }
        )
    return out


def compute_dynamics(
    values_by_date: Sequence[tuple[date, float | None]],
    *,
    fast_span: int = FAST_SPAN,
    slow_span: int = SLOW_SPAN,
) -> ScopeDynamics:
    """EW/AW-shared dynamics arithmetic: Position -> EMA -> Velocity -> Signal.

    The input is a Scope metric series (EW Gap or AW Gap).  The returned series
    is date-aligned and never compressed: a day whose Velocity is unavailable
    keeps its slot with ``None``.
    """
    if not values_by_date:
        return ScopeDynamics(points=())

    positions = compute_position_series(values_by_date)
    ema_input = [
        {"trade_date": p["trade_date"], "value": p["value"], "status": p["status"]}
        for p in positions
    ]

    fast = compute_ema_series(ema_input, fast_span)
    slow = compute_ema_series(ema_input, slow_span)

    # Velocity = fast - slow; available only when BOTH legs are ready.
    velocity_input: list[dict[str, Any]] = []
    for i, p in enumerate(positions):
        f, s = fast[i], slow[i]
        if f["status"] == STATUS_READY and s["status"] == STATUS_READY:
            status = STATUS_READY
            value = f["value"] - s["value"]
        elif p["status"] == STATUS_UNAVAILABLE:
            status = STATUS_UNAVAILABLE
            value = None
        else:
            status = STATUS_INSUFFICIENT
            value = None
        velocity_input.append(
            {"trade_date": p["trade_date"], "value": value, "status": status}
        )

    signal = compute_ema_series(velocity_input, fast_span)

    points: list[DynamicsPoint] = []
    for i, p in enumerate(positions):
        d = date.fromisoformat(p["trade_date"])
        vel = velocity_input[i]["value"]
        sig = signal[i]["value"] if signal[i]["status"] == STATUS_READY else None
        acc = None if (vel is None or sig is None) else vel - sig
        points.append(
            DynamicsPoint(
                trade_date=d,
                value=p["raw_value"],
                position=p["value"],
                ema_fast=fast[i]["value"],
                ema_slow=slow[i]["value"],
                velocity=vel,
                signal=sig,
                acceleration=acc,
                status=p["status"],
            )
        )
    return ScopeDynamics(points=tuple(points))


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def compute_amount_participation(
    amounts_by_date: Sequence[tuple[date, float | None]],
    *,
    window_size: int = POSITION_WINDOW_SIZE,
    minimum_valid_history: int = POSITION_MINIMUM_VALID_HISTORY,
) -> dict[str, Any]:
    """Scope total-amount Position + Multiple (V3.2 §十四).

    - Position: empirical percentile of today's total vs its own strictly-pre-T
      history (shared owner).
    - Multiple: ``current / median(pre-T history)``; ``median <= 0`` or missing
      -> ``None`` (unavailable), never 0.

    This answers "is the whole board's auction turnover unusual today?" and is
    deliberately kept separate from member abnormal breadth.
    """
    if not amounts_by_date:
        return {
            "amount_position": None,
            "amount_position_status": "unavailable_current",
            "amount_multiple": None,
            "history_valid_count": 0,
            "history_candidate_count": 0,
        }

    d, current = amounts_by_date[-1]
    baseline = [v for _, v in amounts_by_date[:-1]]

    pos = compute_historical_position(
        current,
        baseline,
        window_size=window_size,
        minimum_valid_history=minimum_valid_history,
    )
    median = _median([v for v in baseline if v is not None and math.isfinite(v)])

    multiple: float | None = None
    if current is not None and math.isfinite(current) and median is not None and median > 0:
        multiple = current / median

    return {
        "amount_position": pos["position"],
        "amount_position_status": pos["status"],
        "amount_multiple": multiple,
        "history_valid_count": pos["history"]["valid_count"],
        "history_candidate_count": pos["history"]["candidate_count"],
    }
