"""风险破位事件检测 - V1.1 升级版。

从 ref/交易/event_lib/detectors/stage_failure_events.py 迁移。
升级：添加 state_ttl_seconds 和 allowed_roles 声明。

所有事件为 negative 方向，角色为 TRIGGER/VETO 或 VETO。
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.base import EventRole
from app.strategy.events.registry import register_event


def _detect_lower_break_no_reclaim(factors_df: pd.DataFrame) -> pd.Series:
    if "close" not in factors_df.columns or "stage_lower_boundary" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    close = factors_df["close"]
    lower = factors_df["stage_lower_boundary"]
    prev_below = close.shift(1) < lower.shift(1)
    curr_still_below = close < lower
    return (prev_below & curr_still_below).astype(int)


def _detect_trend_down_confirm(factors_df: pd.DataFrame) -> pd.Series:
    required = ["dsa_dir", "dsa_vwap_slope_atr_5", "bbmacd_sign"]
    if not all(c in factors_df.columns for c in required):
        return pd.Series(0, index=factors_df.index)
    dsa_down = factors_df["dsa_dir"] < 0
    slope_neg = factors_df["dsa_vwap_slope_atr_5"] < 0
    bb_neg = factors_df["bbmacd_sign"] < 0
    return (dsa_down & slope_neg & bb_neg).astype(int)


def _detect_weak_rebound(factors_df: pd.DataFrame) -> pd.Series:
    required = ["close", "stage_mid_boundary"]
    if not all(c in factors_df.columns for c in required):
        return pd.Series(0, index=factors_df.index)
    close = factors_df["close"]
    mid = factors_df["stage_mid_boundary"]
    below_mid = close < mid
    vol_shrink = pd.Series(False, index=factors_df.index)
    if "vol_zscore_20" in factors_df.columns:
        vol_shrink = factors_df["vol_zscore_20"] < -1
    return (below_mid & vol_shrink).astype(int)


def _detect_distribution_risk(factors_df: pd.DataFrame) -> pd.Series:
    required = ["dsa_pivot_pos_01", "vol_zscore_20", "close"]
    if not all(c in factors_df.columns for c in required):
        return pd.Series(0, index=factors_df.index)
    high_pos = factors_df["dsa_pivot_pos_01"] > 0.8
    vol_spike = factors_df["vol_zscore_20"] > 2
    price_stall = factors_df["close"].diff() <= 0
    return (high_pos & vol_spike & price_stall).astype(int)


register_event(
    name="evt_lower_break_no_reclaim",
    category="风险破位事件",
    detect_func=_detect_lower_break_no_reclaim,
    required_factors=["close", "stage_lower_boundary"],
    description="跌破下沿不收回（连续2 bar收盘低于下沿）",
    direction="negative",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.VETO],
)

register_event(
    name="evt_trend_down_confirm",
    category="风险破位事件",
    detect_func=_detect_trend_down_confirm,
    required_factors=["dsa_dir", "dsa_vwap_slope_atr_5", "bbmacd_sign"],
    description="趋势确认向下（DSA下行+斜率负+BBMACD负）",
    direction="negative",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.VETO],
)

register_event(
    name="evt_weak_rebound",
    category="风险破位事件",
    detect_func=_detect_weak_rebound,
    required_factors=["close", "stage_mid_boundary", "vol_zscore_20"],
    description="反弹缩量不过中枢",
    direction="negative",
    is_core=False,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.VETO],
)

register_event(
    name="evt_distribution_risk",
    category="风险破位事件",
    detect_func=_detect_distribution_risk,
    required_factors=["dsa_pivot_pos_01", "vol_zscore_20", "close"],
    description="高位放量滞涨",
    direction="negative",
    is_core=False,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.VETO],
)


if __name__ == "__main__":
    from app.strategy.events.registry import list_by_category

    events = list_by_category("风险破位事件")
    print(f"风险破位事件已注册 {len(events)} 个")
    for e in events:
        print(f"  {e['name']} ttl={e['state_ttl_seconds']} roles={e['allowed_roles']}")
    print("OK")
