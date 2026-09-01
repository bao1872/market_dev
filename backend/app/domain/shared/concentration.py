"""Shared abs-value concentration primitive — 逐字提取自 Review scope_observation。

来源：``backend/app/domain/review/scope_observation.py`` 的 ``_price_concentration``
（AUCTION-V3.2 §4.4 EXTRACT_TO_SHARED；两个明确消费者 Review + Auction，满足
``rules/00:67`` Two-Strike）。

冻结语义（NO_FORMULA_CHANGE，任何改动须先改 PRD 并同步 parity test）：
  * share_i = abs(value_i) / Σ abs(value)；
  * 零值成员**仍是**合法 universe 成员 -> ``member_count = len(values)``
    （全部 valid 成员，不是非零计数）；
  * ``total <= _EPSILON`` -> 双 None，status 冻结为 ``"zero_abs_return"``；
  * ``member_count <= 1`` -> normalized None，status ``"insufficient_member_count"``；
  * 否则 status ``"ready"``。

注意：``"zero_abs_return"`` 字面量沿用 Review 历史命名（已被 Review 测试断言），
虽在本通用模块中语义偏窄，但为保持 before == after 不得重命名。
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.domain.shared.hhi import normalized_hhi, raw_hhi

_EPSILON = 1e-12


def abs_value_concentration(values: Sequence[float]) -> dict[str, Any]:
    """abs-value share based raw + normalized HHI over the value universe.

    A zero-value member is still a valid concentration universe member, so
    ``member_count = len(values)`` (all valid members), NOT the non-zero count.
    """
    abs_values = sorted(abs(v) for v in values)
    member_count = len(values)
    total = sum(abs_values)

    if total <= _EPSILON:
        return {
            "raw_hhi": None,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "zero_abs_return",
        }

    shares = [value / total for value in abs_values]
    raw = raw_hhi(shares)

    if member_count <= 1:
        return {
            "raw_hhi": raw,
            "normalized_hhi": None,
            "member_count": member_count,
            "status": "insufficient_member_count",
        }

    return {
        "raw_hhi": raw,
        "normalized_hhi": normalized_hhi(raw, member_count),
        "member_count": member_count,
        "status": "ready",
    }
