"""Shared recursive EMA primitive — 逐字提取自 Review（NO_FORMULA_CHANGE）。

来源：``app/domain/review/analysis/historical_dynamics.py`` 的 ``compute_ema_series``。
AUCTION-V3.2 §十二：EMA 递归是 Review 与 Auction 共用的纯数学，两个明确消费者
满足 ``rules/00:67`` Two-Strike，因此提取到 shared；Review 侧改为薄委托。
本模块不得修改公式、状态词或异常语义。

Frozen contract (PRD §7.9 EMA Numerical Contract):
- valid input = upstream ``status == ready`` AND finite ``value``; it is the ONLY
  observation that updates state, increments ``valid_count`` and advances the
  EMA clock;
- first valid input seeds the internal state (never waits for the span-th
  observation);
- output ``status``: ``ready`` once ``valid_count >= span`` AND the current input
  is ready; ``insufficient_history`` while the warmup count is not yet met (or
  the current input is insufficient_history); ``unavailable_current`` when the
  current input is unavailable_current (state preserved, clock does not advance);
- gaps (unavailable / insufficient days) never decay, never reset and never
  advance the clock — the next valid input resumes from the last valid state;
- trade dates must be strictly ascending (fail fast, never re-sort);
- ``status == ready`` with a non-finite ``value`` is a contract violation and
  fails fast.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from app.domain.shared.historical_position import finite_number

__all__ = [
    "STATUS_READY",
    "STATUS_INSUFFICIENT",
    "STATUS_UNAVAILABLE",
    "compute_ema_series",
]

STATUS_READY = "ready"
STATUS_INSUFFICIENT = "insufficient_history"
STATUS_UNAVAILABLE = "unavailable_current"


def _trade_date(value: Any) -> date:
    if isinstance(value, str):
        return date.fromisoformat(value)
    return value


def compute_ema_series(
    input_series: Sequence[Mapping[str, Any]],
    span: int,
) -> list[dict[str, Any]]:
    """Compute the frozen recursive EMA over ``input_series`` (single owner).

    Args:
        input_series: ordered facts, trade_date ASCENDING.  Each item carries
            ``trade_date`` (ISO string or ``date``), ``value`` (float | None)
            and ``status`` — the exact upstream status vocabulary.
        span: EMA span N; ``alpha = 2 / (N + 1)`` (must be >= 1).

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
            value = finite_number(item.get("value"))
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
