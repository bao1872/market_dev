"""突破事件检测 - V1.1 升级版。

从 ref/交易/event_lib/detectors/breakout_events.py 迁移。
升级：添加 state_ttl_seconds 和 allowed_roles 声明。

Registered Events:
    - evt_cross_above_value_area_high: 价格上穿价值区域高点（TRIGGER/CONFIRM）
    - evt_cross_below_value_area_low: 价格下穿价值区域低点（TRIGGER/VETO）
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.base import EventRole
from app.strategy.events.registry import register_event


def _detect_cross_above_value_area_high(factors_df: pd.DataFrame) -> pd.Series:
    """价格上穿价值区域高点（基于PAVP）。"""
    if "dsa_pivot_pos_01" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    pos = factors_df["dsa_pivot_pos_01"]
    return ((pos > 0.8) & (pos.shift(1) <= 0.8)).astype(int)


def _detect_cross_below_value_area_low(factors_df: pd.DataFrame) -> pd.Series:
    """价格下穿价值区域低点（基于PAVP）。"""
    if "dsa_pivot_pos_01" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    pos = factors_df["dsa_pivot_pos_01"]
    return ((pos < 0.2) & (pos.shift(1) >= 0.2)).astype(int)


register_event(
    name="evt_cross_above_value_area_high",
    category="突破事件",
    detect_func=_detect_cross_above_value_area_high,
    required_factors=["dsa_pivot_pos_01"],
    description="价格上穿价值区域高点（pivot_pos > 0.8）",
    direction="positive",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.CONFIRM],
)

register_event(
    name="evt_cross_below_value_area_low",
    category="突破事件",
    detect_func=_detect_cross_below_value_area_low,
    required_factors=["dsa_pivot_pos_01"],
    description="价格下穿价值区域低点（pivot_pos < 0.2）",
    direction="negative",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.VETO],
)


if __name__ == "__main__":
    from app.strategy.events.registry import list_by_category

    events = list_by_category("突破事件")
    print(f"突破事件已注册 {len(events)} 个")
    for e in events:
        print(f"  {e['name']} ttl={e['state_ttl_seconds']} roles={e['allowed_roles']}")
    print("OK")
