"""区间结构事件检测 - V1.1 升级版。

从 ref/交易/event_lib/detectors/stage_wash_events.py 迁移。
升级：添加 state_ttl_seconds 和 allowed_roles 声明。
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.base import EventRole
from app.strategy.events.registry import register_event


def _detect_pullback_to_lower(factors_df: pd.DataFrame) -> pd.Series:
    if "price_pos_in_stage_01" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    pos = factors_df["price_pos_in_stage_01"]
    prev_pos = pos.shift(1)
    return ((pos < 0.2) & (prev_pos > 0.5)).astype(int)


def _detect_lower_break_short_hold_long(factors_df: pd.DataFrame) -> pd.Series:
    if "break_lower_intrabar" not in factors_df.columns or "reclaim_lower_close" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (
        (factors_df["break_lower_intrabar"] > 0) & (factors_df["reclaim_lower_close"] > 0)
    ).astype(int)


def _detect_price_cross_above_mid(factors_df: pd.DataFrame) -> pd.Series:
    if "close" not in factors_df.columns or "stage_mid_boundary" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    close = factors_df["close"]
    mid = factors_df["stage_mid_boundary"]
    prev_below = close.shift(1) < mid.shift(1)
    curr_above = close >= mid
    return (prev_below & curr_above).astype(int)


def _detect_range_pullback_reclaim_cycle(factors_df: pd.DataFrame) -> pd.Series:
    if "wash_cycle_count_n" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    count = factors_df["wash_cycle_count_n"]
    return (count > count.shift(1)).astype(int)


def _detect_multi_range_cycle(factors_df: pd.DataFrame) -> pd.Series:
    if "wash_cycle_count_n" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return (factors_df["wash_cycle_count_n"] >= 2).astype(int)


def _detect_upper_touch(factors_df: pd.DataFrame) -> pd.Series:
    required = ["high", "stage_upper_boundary"]
    if not all(c in factors_df.columns for c in required):
        return pd.Series(0, index=factors_df.index)
    upper_ref = factors_df["stage_upper_boundary"].shift(1)
    return (factors_df["high"] >= upper_ref).astype(int)


register_event(
    name="evt_pullback_to_lower",
    category="区间结构事件",
    detect_func=_detect_pullback_to_lower,
    required_factors=["price_pos_in_stage_01"],
    description="从中上部回踩到下沿/低位（pos从>0.5降到<0.2）",
    direction="neutral",
    is_core=True,
    state_ttl_seconds=600,
    allowed_roles=[EventRole.OBSERVE],
)

register_event(
    name="evt_lower_break_short_hold_long",
    category="区间结构事件",
    detect_func=_detect_lower_break_short_hold_long,
    required_factors=["break_lower_intrabar", "reclaim_lower_close"],
    description="小破位但大结构收回（同bar破位+收回）",
    direction="positive",
    is_core=True,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_price_cross_above_mid",
    category="区间结构事件",
    detect_func=_detect_price_cross_above_mid,
    required_factors=["close", "stage_mid_boundary"],
    description="价格从下方穿越中枢",
    direction="positive",
    is_core=True,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_range_pullback_reclaim_cycle",
    category="区间结构事件",
    detect_func=_detect_range_pullback_reclaim_cycle,
    required_factors=["wash_cycle_count_n"],
    description="区间回踩收回循环完成（wash_cycle_count增加）",
    direction="positive",
    is_core=True,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_multi_range_cycle",
    category="区间结构事件",
    detect_func=_detect_multi_range_cycle,
    required_factors=["wash_cycle_count_n"],
    description="多轮区间回踩收回循环（wash_cycle_count >= 2）",
    direction="positive",
    is_core=False,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_upper_touch",
    category="区间结构事件",
    detect_func=_detect_upper_touch,
    required_factors=["high", "stage_upper_boundary"],
    description="最高价触及上一bar上沿",
    direction="neutral",
    is_core=True,
    state_ttl_seconds=600,
    allowed_roles=[EventRole.OBSERVE],
)


if __name__ == "__main__":
    from app.strategy.events.registry import list_by_category

    events = list_by_category("区间结构事件")
    print(f"区间结构事件已注册 {len(events)} 个")
    for e in events:
        print(f"  {e['name']} ttl={e['state_ttl_seconds']} roles={e['allowed_roles']}")
    print("OK")
