"""Neutral pure math for first-pyramid member rolling facts.

These formulas were originally defined inside the retired Review domain's
``member_fact`` module but are pure numeric helpers with no Review/Scope-Observation
dependency.  They are migrated here (neutral owner) so First Pyramid can use them
without pulling in the retired Review domain.

NO FORMULA CHANGE — the implementations are byte-equivalent to the originals.
"""

from __future__ import annotations

import math


def _ratio(value: float | None, history: list[float], window: int) -> float | None:
    prior = history[-window - 1 : -1]
    if value is None or not prior:
        return None
    mean = sum(prior) / len(prior)
    return value / mean if abs(mean) > 1e-12 else None


def _percentile(value: float | None, history: list[float], window: int) -> float | None:
    prior = history[-window - 1 : -1]
    if value is None or not prior:
        return None
    return sum(item <= value for item in prior) / len(prior) * 100.0


# [CHANGE-20260808] Review rolling facts 共享纯 SSOT。
# LIVE ReviewMemberFact.build 与 Historical stock-major replay 消费同一公式，
# 确保 parity（窗口边界 19/20/21、119/120/121、199/200/201 行为一致）。
def compute_ratio(
    value: float | None,
    history: list[float],
    window: int,
) -> float | None:
    """current / prior window mean（分母不含 current）。与 _ratio 一致。"""
    return _ratio(value, history, window)


def compute_percentile(
    value: float | None,
    history: list[float],
    window: int,
) -> float | None:
    """prior window 中 <= current 的占比（分母不含 current）。与 _percentile 一致。"""
    return _percentile(value, history, window)


def compute_price_position_120d(
    close: float | None,
    recent_lows: list[float],
    recent_highs: list[float],
) -> float | None:
    """120 日价格位置：(close - low_120) / (high_120 - low_120)，含 current。"""
    if close is None:
        return None
    lows = [v for v in recent_lows if v is not None]
    highs = [v for v in recent_highs if v is not None]
    if not lows or not highs:
        return None
    low_value = min(lows)
    high_value = max(highs)
    if high_value - low_value <= 1e-12:
        return None
    return (close - low_value) / (high_value - low_value)
