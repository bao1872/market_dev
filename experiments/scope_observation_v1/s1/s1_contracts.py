"""S1 minimal contract helpers (pure, no DB) for Scope Data & Semantic Baseline.

这些纯函数封装 §16 要求的最小不变式，供 tests 直接验证：
- CURRENT_ONLY membership 不得进入历史样本
- PIT membership date <= observation trade_date
- denominator = 当日有效 PIT members
- state ratios 使用同一 denominator
- 互斥分类状态 ratio 求和一致
- 无 future membership / facts
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass(frozen=True)
class MembershipWindow:
    """PIT membership 有效期窗口。"""

    effective_from: date
    effective_to: date | None  # None = 至今有效


@dataclass(frozen=True)
class ScopeObsRow:
    """单个 scope-observation 行的最小事实。"""

    scope_id: str
    trade_date: date
    member_count: int  # denominator = 当日有效 PIT members
    state_counts: dict[str, int]  # 互斥分类状态 -> 计数


def membership_applies_at(window: MembershipWindow, trade_date: date) -> bool:
    """PIT membership 是否适用于某观察日（effective_from <= trade_date <= effective_to）。"""
    if trade_date < window.effective_from:
        return False
    if window.effective_to is not None and trade_date > window.effective_to:
        return False
    return True


def is_current_only(window: MembershipWindow, observation_trade_date: date) -> bool:
    """true 表示该 membership 相对某历史观察日是 CURRENT_ONLY，禁止进入该历史样本。

    CURRENT_ONLY = membership 的 effective_from 晚于观察日，无法 PIT 覆盖该历史日期；
    若试图用它构造历史样本即构成 current-membership backfill。
    """
    return window.effective_from > observation_trade_date


def membership_constructed_pit(
    memberships: list[MembershipWindow],
    observation_trade_date: date,
) -> bool:
    """能严格构造 PIT membership 的判定：observation 必须落在每个必要窗口内。"""
    return all(membership_applies_at(m, observation_trade_date) for m in memberships)


def valid_denominator(row: ScopeObsRow) -> int:
    """denominator = 当日有效 PIT members（禁止用 current only 成员）。"""
    return row.member_count


def ratio_contract(state_counts: dict[str, int], denominator: int) -> dict[str, float]:
    """state ratios 使用同一 denominator。返回每个 state 的 ratio。"""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return {k: v / denominator for k, v in state_counts.items()}


def mutually_exclusive_ratios_sum_ok(state_counts: dict[str, int], denominator: int, tol: float = 1e-9) -> bool:
    """互斥分类状态（如 regime up/neutral/down）的 ratio 之和必须 ≈ 1。"""
    if sum(state_counts.values()) != denominator:
        return False
    total = sum(v / denominator for v in state_counts.values())
    return abs(total - 1.0) <= tol


def no_future_membership(window: MembershipWindow, observation_trade_date: date) -> bool:
    """无 future membership：effective_from 不得晚于观察日。"""
    return window.effective_from <= observation_trade_date


def no_future_facts(fact_trade_date: date, observation_trade_date: date) -> bool:
    """无 future facts：事实日期不得晚于观察日。"""
    return fact_trade_date <= observation_trade_date


# ============================================================================
# S1 Correction 新增纯函数（S1 Correction §7 regression tests）
# ============================================================================


def resolve_pit_definition_at(
    versions: list[MembershipWindow],
    trade_date: date,
) -> MembershipWindow | None:
    """按 resolve_board_membership_at() 语义选出 trade_date 上有效的 definition 版本。

    只允许 effective_from <= trade_date 且 (effective_to IS NULL or effective_to > trade_date)；
    多个可选时取 effective_from 最大者（最新版本）。
    """
    candidates = [
        v for v in versions
        if v.effective_from <= trade_date
        and (v.effective_to is None or v.effective_to > trade_date)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: v.effective_from)


def one_snapshot_cannot_backfill_newer_versions(
    versions: list[MembershipWindow],
    trade_date: date,
    snapshot: MembershipWindow,
) -> bool:
    """若存在比 snapshot 更新的有效版本，则不能把 snapshot 复用为该 trade_date 的 membership。

    返回 True 表示复用合法（snapshot 就是该日最新有效版本），False 表示应选用其它新版本。
    """
    resolved = resolve_pit_definition_at(versions, trade_date)
    if resolved is None:
        return False
    return resolved == snapshot


def swing_neutral_is_valid(state: str | None) -> bool:
    """swing_bias=0 是合法 neutral 状态（1=上行/-1=下行/0=震荡），不是 invalid。"""
    return state in ("1", "0", "-1")


def momentum_flat_is_valid(state: str | None) -> bool:
    """momentum flat 是合法状态（expanding/flat/contracting），不是 invalid。"""
    return state in ("expanding", "flat", "contracting")


def categorical_sum_to_axis_denominator(state_counts: dict[str, int], denominator: int) -> bool:
    """互斥分类状态的完整分布之和必须等于 axis denominator（up+neutral+down / expanding+flat+contracting）。"""
    return sum(state_counts.values()) == denominator