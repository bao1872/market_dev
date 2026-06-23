"""基本面事件检测 - V1.1 升级版。

从 ref/交易/event_lib/detectors/fundamental_events.py 迁移。
升级：添加 state_ttl_seconds 声明。

Registered Events:
    - evt_earnings_acceleration: 业绩加速（CONFIRM）
    - evt_earnings_deceleration: 业绩减速（VETO）
    - evt_cashflow_improvement: 现金流改善（CONFIRM）
    - evt_cashflow_deterioration: 现金流恶化（VETO）
    - evt_roe_inflection: ROE拐点（CONFIRM）
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.registry import register_event


def _detect_earnings_acceleration(factors_df: pd.DataFrame) -> pd.Series:
    """业绩加速：q_np_yoy 上升。"""
    if "q_np_yoy" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (factors_df["q_np_yoy"].diff() > 0).astype(int)


def _detect_earnings_deceleration(factors_df: pd.DataFrame) -> pd.Series:
    """业绩减速：q_np_yoy 下降。"""
    if "q_np_yoy" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (factors_df["q_np_yoy"].diff() < 0).astype(int)


def _detect_cashflow_improvement(factors_df: pd.DataFrame) -> pd.Series:
    """现金流改善：cfo_to_np_parent 上升。"""
    if "cfo_to_np_parent" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (factors_df["cfo_to_np_parent"].diff() > 0).astype(int)


def _detect_cashflow_deterioration(factors_df: pd.DataFrame) -> pd.Series:
    """现金流恶化：cfo_to_np_parent 下降。"""
    if "cfo_to_np_parent" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (factors_df["cfo_to_np_parent"].diff() < 0).astype(int)


def _detect_roe_inflection(factors_df: pd.DataFrame) -> pd.Series:
    """ROE拐点：roe_weighted 方向改变。"""
    if "roe_weighted" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    roe = factors_df["roe_weighted"]
    return ((roe.diff() > 0) & (roe.diff().shift(1) <= 0)).astype(int)


register_event(
    name="evt_earnings_acceleration",
    category="基本面事件",
    detect_func=_detect_earnings_acceleration,
    required_factors=["q_np_yoy"],
    description="业绩加速（净利润同比增长率上升）",
    direction="positive",
    is_core=False,
    state_ttl_seconds=1800,
)

register_event(
    name="evt_earnings_deceleration",
    category="基本面事件",
    detect_func=_detect_earnings_deceleration,
    required_factors=["q_np_yoy"],
    description="业绩减速（净利润同比增长率下降）",
    direction="negative",
    is_core=False,
    state_ttl_seconds=3600,
)

register_event(
    name="evt_cashflow_improvement",
    category="基本面事件",
    detect_func=_detect_cashflow_improvement,
    required_factors=["cfo_to_np_parent"],
    description="现金流改善",
    direction="positive",
    is_core=False,
    state_ttl_seconds=1800,
)

register_event(
    name="evt_cashflow_deterioration",
    category="基本面事件",
    detect_func=_detect_cashflow_deterioration,
    required_factors=["cfo_to_np_parent"],
    description="现金流恶化",
    direction="negative",
    is_core=False,
    state_ttl_seconds=3600,
)

register_event(
    name="evt_roe_inflection",
    category="基本面事件",
    detect_func=_detect_roe_inflection,
    required_factors=["roe_weighted"],
    description="ROE拐点",
    direction="positive",
    is_core=False,
    state_ttl_seconds=1800,
)


if __name__ == "__main__":
    from app.strategy.events.registry import list_by_category

    events = list_by_category("基本面事件")
    print(f"基本面事件已注册 {len(events)} 个")
    for e in events:
        print(f"  {e['name']} ttl={e['state_ttl_seconds']}")
    print("OK")
