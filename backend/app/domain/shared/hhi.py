"""Shared HHI primitive — 逐字提取自 Review scope_observation（NO_FORMULA_CHANGE）。

来源：``backend/app/domain/review/scope_observation.py`` 的 ``_raw_hhi`` / ``_normalized_hhi``
（AUCTION-V3.2 §22 EXTRACT_TO_SHARED）。本模块**不得**修改公式、边界或异常语义；
任何改动都必须先改 PRD 并同时更新 Review parity test。
"""
from __future__ import annotations

from collections.abc import Sequence

# 与 Review scope_observation 完全一致
_EPSILON = 1e-12


def raw_hhi(shares: Sequence[float]) -> float:
    """Raw HHI = Σ share²。sorted() 用于消除浮点非结合性（确定性）。"""
    return sum(share * share for share in sorted(shares))


def normalized_hhi(
    raw_hhi_value: float | None,
    member_count: int,
) -> float | None:
    """Member-count-normalized HHI (ACCEPTED CONTRACT, PRD §7.2).

    ``normalized_hhi = (raw_hhi - 1/N) / (1 - 1/N)``, N = member_count > 1.

    Equal distribution -> 0; single-member-dominant -> 1; removes the mechanical
    lower bound that raw HHI imposes on a larger N.  Boundaries (frozen):
    - ``raw_hhi is None`` -> None (unavailable upstream);
    - ``member_count <= 1`` -> None (no internal concentration space, denominator 0);
    - ``1 - 1/N <= _EPSILON`` -> None (numerical degenerate floor);
    - ``raw_hhi`` out of [0, 1] after extraction -> ``ValueError`` (never silent).
    """
    if raw_hhi_value is None:
        return None

    if member_count <= 1:
        return None

    floor = 1.0 / member_count
    denominator = 1.0 - floor

    if denominator <= _EPSILON:
        return None

    value = (raw_hhi_value - floor) / denominator

    # Only float-rounding near endpoints; must NOT mask an algorithmic error.
    if value < 0.0 and value >= -_EPSILON:
        value = 0.0
    elif value > 1.0 and value <= 1.0 + _EPSILON:
        value = 1.0

    if value < 0.0 or value > 1.0:
        raise ValueError(
            f"normalized HHI out of range: "
            f"raw_hhi={raw_hhi_value}, member_count={member_count}, value={value}"
        )

    return value
