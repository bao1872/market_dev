"""Shared Historical Position math — 逐字提取自 Review（NO_FORMULA_CHANGE）。

来源：
- ``app/domain/review/scope_evidence.py`` 的 ``percentile_rank`` / ``_finite_number``；
- ``app/domain/review/analysis/historical_position.py`` 的 ``compute_historical_position``。

AUCTION-V3.2 §十二：这是 Review 与 Auction 共用的**纯数学**（empirical percentile
+ 窗口/最小样本门）。两个明确消费者满足 ``rules/00:67`` Two-Strike，因此提取到
shared；Review 侧改为薄委托。本模块**不得**修改公式、边界或状态词。

冻结语义：
  * ``percentile_rank = below_or_equal / n * 100``，clamp 到 [0, 100]；
  * 样本与当前值均先过滤 None / NaN / ±inf / bool；空样本 -> None；
  * 基线窗口 = ``pre_t_values[-window_size:]``，**只取 T 之前**，
    不向更早日期 reach-back 补足有效值；
  * ``valid_count < minimum_valid_history`` -> ``insufficient_history``；
  * 当前值不可用 -> ``unavailable_current``；
  * ``position`` 仅在 ``ready`` 时有值，否则恒为 ``None``（Missing != Zero）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

POSITION_WINDOW_SIZE = 120
POSITION_MINIMUM_VALID_HISTORY = 60


def finite_number(value: Any) -> float | None:
    """Return a finite float for int/float, else None.

    Booleans are explicitly rejected: ``bool`` must never be treated as numeric
    evidence.  None / NaN / +-inf -> None (never coerced to 0).
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _clamp_0_100(value: float) -> float:
    return min(100.0, max(0.0, value))


def percentile_rank(value: Any, samples: Sequence[Any]) -> float | None:
    """Neutral percentile rank (0..100) of ``value`` within ``samples``.

    Rules (frozen):
      - filters None / NaN / inf from ``samples`` and ``value``;
      - empty (after filter) -> None (unavailable);
      - deterministic tie behavior (values equal to ``value`` all count);
      - output 0..100;
      - no direction, no weight, no negative inversion, no score normalization.

    Only the pure math semantic of the repo's cross-sectional rank convention
    is expressed here: ``below_or_equal / n * 100``, clamped to [0, 100].
    """
    current = finite_number(value)
    finite = [float(s) for s in samples if finite_number(s) is not None]
    if current is None or not finite:
        return None
    below_or_equal = sum(1 for s in finite if s <= current)
    return _clamp_0_100(below_or_equal / len(finite) * 100.0)


def compute_historical_position(
    current_value: Any,
    pre_t_values: Sequence[Any],
    *,
    window_size: int = POSITION_WINDOW_SIZE,
    minimum_valid_history: int = POSITION_MINIMUM_VALID_HISTORY,
) -> dict[str, Any]:
    """Compute one objective Historical Position fact for a single T.

    Args:
        current_value: the primitive value at T (may be None / NaN / inf).
        pre_t_values: primitive values at observations STRICTLY BEFORE T.  Only
            the latest ``window_size`` candidates are used; we never reach past
            that window to accumulate more valid values.
        window_size: candidate window length (default 120 observations).
        minimum_valid_history: minimum valid pre-T observations for a position
            (default 60).  ``valid_count < minimum_valid_history`` -> unavailable.

    Returns (transparent fact, deterministic, non-mutating):
        ``{"value", "position", "history": {window_size, minimum_valid_history,
        candidate_count, valid_count}, "status"}`` where ``status`` is
        ``"ready"`` | ``"insufficient_history"`` | ``"unavailable_current"``.
        ``position`` is ``None`` (never 0) unless ``status == "ready"``.
    """
    candidates = list(pre_t_values)[-window_size:]
    valid = [v for v in candidates if finite_number(v) is not None]
    value = finite_number(current_value)
    if value is None:
        status = "unavailable_current"
    elif len(valid) < minimum_valid_history:
        status = "insufficient_history"
    else:
        status = "ready"
    position = percentile_rank(value, valid) if status == "ready" else None
    return {
        "value": value,
        "position": position,
        "history": {
            "window_size": window_size,
            "minimum_valid_history": minimum_valid_history,
            "candidate_count": len(candidates),
            "valid_count": len(valid),
        },
        "status": status,
    }
