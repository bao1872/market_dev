"""Analysis B — Historical Dynamics Velocity / Signal / Acceleration (PRD §7.9).

Position Foundation = CLOSED.  This module is the next layer of the frozen
PRD §7.9 chain:

    Position -> EMA5 / EMA20 -> Velocity -> Signal -> Acceleration

Frozen contracts consumed here (PRD §7.9 + task spec):

- **EMA Numerical Contract (FROZEN)**: ``alpha = 2 / (span + 1)``, recursive
  ``state = alpha * x + (1 - alpha) * previous_valid_state``, first-valid seed,
  min valid inputs (EMA5 >= 5 / EMA20 >= 20), valid-observation clock, missing
  state-preserve (no decay / no reset / no forward-fill / no zero-fill), gap
  never advances the clock, No Future Leakage.
- **Status Propagation Contract (FROZEN)**: derived-fact availability MUST be
  derived from upstream ``status`` (never from ``value is None``); precedence
  ``unavailable_current > insufficient_history > ready`` applies uniformly to
  EMA5 / EMA20 / Velocity / Signal / Acceleration.
- **Velocity / Signal / Acceleration formulas (FROZEN)**: no interpretation
  label, no threshold, no phase, no score.

Ownership boundary
------------------
Pure domain layer.  This module NEVER touches the database / AsyncSession /
persistence / reconstruction / membership / API / orchestrator.  It consumes
Position fact series (as produced by ``historical_position.compute_position_series``
/ ``compute_historical_positions``) and returns objective derived facts.
``momentum.bb_position`` / ``momentum.bb_width`` are current-only and never
appear in a Position series, hence never here.

There is exactly ONE recursive EMA owner (``compute_ema_series``); Signal
reuses it.  No second status enum, no duplicated path registry, no pandas /
numpy dependence, no hidden library default.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

# PRD §7.9 frozen EMA spans (product shorthand: EMA5 / EMA20 = span 5 / span 20
# valid observations).  Signal = EMA5(Velocity) reuses the fast span.
EMA_FAST_SPAN = 5
EMA_SLOW_SPAN = 20
SIGNAL_SPAN = EMA_FAST_SPAN

# The exact frozen availability vocabulary (same strings as Position facts).
STATUS_READY = "ready"
STATUS_INSUFFICIENT = "insufficient_history"
STATUS_UNAVAILABLE = "unavailable_current"


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
    """Frozen precedence ``unavailable_current > insufficient_history > ready``.

    Status propagation is driven by upstream ``status`` only — never by
    ``value is None``.  ``null`` is a result value, not an availability cause.
    """
    if any(s == STATUS_UNAVAILABLE for s in statuses):
        return STATUS_UNAVAILABLE
    if any(s == STATUS_INSUFFICIENT for s in statuses):
        return STATUS_INSUFFICIENT
    return STATUS_READY


def _ema_input(series: Sequence[Mapping[str, Any]], value_key: str) -> list[dict[str, Any]]:
    """Map a fact series (position or velocity) to the generic EMA input shape.

    Each input item is ``{"trade_date", "value", "status"}`` where ``value`` is
    taken from the caller-selected key (``"position"`` for the Position series,
    ``"value"`` for the Velocity series).
    """
    return [
        {
            "trade_date": item["trade_date"],
            "value": item.get(value_key),
            "status": item["status"],
        }
        for item in series
    ]


# ---------------------------------------------------------------------------
# Generic recursive EMA owner (the single source of the frozen EMA contract)
# ---------------------------------------------------------------------------


def compute_ema_series(
    input_series: Sequence[Mapping[str, Any]],
    span: int,
) -> list[dict[str, Any]]:
    """Compute the frozen recursive EMA over ``input_series`` (single owner).

    Args:
        input_series: ordered facts, trade_date ASCENDING.  Each item carries
            ``trade_date`` (ISO string or ``date``), ``value`` (float | None)
            and ``status`` (one of ``ready`` / ``insufficient_history`` /
            ``unavailable_current`` — the exact upstream status vocabulary).
        span: EMA span N; ``alpha = 2 / (N + 1)`` (must be >= 1).

    Contract (PRD §7.9 EMA Numerical Contract):
        - valid input = upstream ``status == ready`` AND finite ``value``; it is
          the ONLY observation that updates state, increments ``valid_count``
          and advances the EMA clock;
        - first valid input seeds the internal state (never waits for the span-th
          observation);
        - output ``status``: ``ready`` once ``valid_count >= span`` AND the
          current input is ready; ``insufficient_history`` while the warmup count
          is not yet met (or the current input is insufficient_history);
          ``unavailable_current`` when the current input is unavailable_current
          (state is preserved, the clock does not advance);
        - gaps (unavailable / insufficient days) never decay, never reset and
          never advance the clock — the next valid input resumes from the last
          valid state;
        - trade dates must be strictly ascending (fail fast, never re-sort);
        - ``status == ready`` with a non-finite ``value`` is a contract violation
          and fails fast.

    Returns:
        One output per input item, date-aligned (never compressed):
        ``{"trade_date", "value", "status", "valid_count", "span"}``.
    """
    if span < 1:
        raise ValueError(f"span must be >= 1, got {span}")
    if not input_series:
        return []
    pairs: list[tuple[date, Mapping[str, Any]]] = [
        (_trade_date(item["trade_date"]), item) for item in input_series
    ]
    for prev, cur in zip(pairs, pairs[1:], strict=False):
        if not prev[0] < cur[0]:
            raise ValueError(
                f"input_series must be strictly ascending by trade_date; got {prev[0]} -> {cur[0]}"
            )
    alpha = 2.0 / (span + 1.0)
    state: float | None = None
    valid_count = 0
    out: list[dict[str, Any]] = []
    for td, item in pairs:
        status = item.get("status")
        if status == STATUS_UNAVAILABLE:
            out.append(
                {
                    "trade_date": td.isoformat(),
                    "value": None,
                    "status": STATUS_UNAVAILABLE,
                    "valid_count": valid_count,
                    "span": span,
                }
            )
        elif status == STATUS_INSUFFICIENT:
            out.append(
                {
                    "trade_date": td.isoformat(),
                    "value": None,
                    "status": STATUS_INSUFFICIENT,
                    "valid_count": valid_count,
                    "span": span,
                }
            )
        elif status == STATUS_READY:
            value = _finite(item.get("value"))
            if value is None:
                raise ValueError(f"status=ready with non-finite value at {td.isoformat()}")
            state = value if state is None else alpha * value + (1.0 - alpha) * state
            valid_count += 1
            out.append(
                {
                    "trade_date": td.isoformat(),
                    "value": state if valid_count >= span else None,
                    "status": STATUS_READY if valid_count >= span else STATUS_INSUFFICIENT,
                    "valid_count": valid_count,
                    "span": span,
                }
            )
        else:
            raise ValueError(f"unknown upstream status: {status!r}")
    return out


# ---------------------------------------------------------------------------
# Derived facts (Velocity / Acceleration)
# ---------------------------------------------------------------------------


def _compute_velocity(
    position_series: Sequence[Mapping[str, Any]],
    ema5: Sequence[Mapping[str, Any]],
    ema20: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Velocity(T) = EMA5(Position)(T) - EMA20(Position)(T).

    ready iff Position, Fast AND Slow are all ready; upstream statuses drive
    propagation (Position + Fast + Slow) with the frozen precedence.
    """
    out: list[dict[str, Any]] = []
    for p, fast, slow in zip(position_series, ema5, ema20, strict=True):
        status = _merge_status([p["status"], fast["status"], slow["status"]])
        value = (fast["value"] - slow["value"]) if status == STATUS_READY else None
        out.append({"trade_date": p["trade_date"], "value": value, "status": status})
    return out


def _compute_acceleration(
    velocity: Sequence[Mapping[str, Any]],
    signal: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Acceleration(T) = Velocity(T) - Signal(T).

    ready iff Velocity AND Signal are both ready; upstream statuses drive
    propagation (Velocity + Signal) with the frozen precedence.  No
    interpretation / threshold / phase / score.
    """
    out: list[dict[str, Any]] = []
    for v, sig in zip(velocity, signal, strict=True):
        status = _merge_status([v["status"], sig["status"]])
        value = (v["value"] - sig["value"]) if status == STATUS_READY else None
        out.append({"trade_date": v["trade_date"], "value": value, "status": status})
    return out


# ---------------------------------------------------------------------------
# Per-primitive historical dynamics package
# ---------------------------------------------------------------------------


def compute_historical_dynamics_series(
    position_series: Sequence[dict[str, Any]],
    *,
    span_fast: int = EMA_FAST_SPAN,
    span_slow: int = EMA_SLOW_SPAN,
) -> dict[str, list[dict[str, Any]]]:
    """Compute the full Historical Dynamics chain for ONE Position series.

    Args:
        position_series: the Position fact series for one primitive (as produced
            by ``historical_position.compute_position_series``), trade_date
            ASCENDING.  Each item must carry ``trade_date``, ``position``
            (float | None), ``status`` and ``history``.
        span_fast / span_slow: EMA spans (defaults 5 / 20, the frozen PRD
            numbers).  Signal reuses ``span_fast`` (Signal = EMA5(Velocity)).

    Returns (date-aligned, one entry per input day — never compressed):
        ``{"position", "ema5", "ema20", "velocity", "signal", "acceleration"}``
        where ``position`` is the input series passthrough and every derived
        series carries ``trade_date`` + ``value`` + ``status`` (EMA entries also
        carry ``valid_count`` + ``span``).
    """
    ema5 = compute_ema_series(_ema_input(position_series, "position"), span=span_fast)
    ema20 = compute_ema_series(_ema_input(position_series, "position"), span=span_slow)
    velocity = _compute_velocity(position_series, ema5, ema20)
    signal = compute_ema_series(_ema_input(velocity, "value"), span=span_fast)
    acceleration = _compute_acceleration(velocity, signal)
    return {
        "position": list(position_series),
        "ema5": ema5,
        "ema20": ema20,
        "velocity": velocity,
        "signal": signal,
        "acceleration": acceleration,
    }


def compute_historical_dynamics(
    position_series_by_primitive: Mapping[str, Sequence[dict[str, Any]]],
    *,
    span_fast: int = EMA_FAST_SPAN,
    span_slow: int = EMA_SLOW_SPAN,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Compute Historical Dynamics for every eligible primitive (thin loop).

    ``position_series_by_primitive`` is the output of
    ``historical_position.compute_historical_positions`` (keys inherit the 11
    historical-ready primitives); there is NO second primitive registry here.
    """
    return {
        key: compute_historical_dynamics_series(series, span_fast=span_fast, span_slow=span_slow)
        for key, series in position_series_by_primitive.items()
    }


__all__ = [
    "EMA_FAST_SPAN",
    "EMA_SLOW_SPAN",
    "SIGNAL_SPAN",
    "compute_ema_series",
    "compute_historical_dynamics_series",
    "compute_historical_dynamics",
]
