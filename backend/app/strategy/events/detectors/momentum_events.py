"""动量事件检测 - V1.1 升级版。

从 ref/交易/event_lib/detectors/momentum_events.py 迁移。
升级：添加 state_ttl_seconds 和 allowed_roles 声明。

Registered Events:
    - evt_bbmacd_cross_upper: BBMACD上穿上轨（CONFIRM）
    - evt_bbmacd_cross_lower: BBMACD下穿下轨（CONFIRM）
    - evt_macd_golden_cross: MACD金叉（CONFIRM）
    - evt_macd_death_cross: MACD死叉（CONFIRM）
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.base import EventRole
from app.strategy.events.registry import register_event


def _detect_bbmacd_cross_upper(factors_df: pd.DataFrame) -> pd.Series:
    """BBMACD上穿上轨。"""
    if "bbmacd_cross_upper" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return factors_df["bbmacd_cross_upper"].fillna(0).astype(int)


def _detect_bbmacd_cross_lower(factors_df: pd.DataFrame) -> pd.Series:
    """BBMACD下穿下轨。"""
    if "bbmacd_cross_lower" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    return factors_df["bbmacd_cross_lower"].fillna(0).astype(int)


def _detect_macd_golden_cross(factors_df: pd.DataFrame) -> pd.Series:
    """MACD金叉：bbmacd从负变正。"""
    if "bbmacd" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    bbmacd = factors_df["bbmacd"]
    return ((bbmacd > 0) & (bbmacd.shift(1) <= 0)).astype(int)


def _detect_macd_death_cross(factors_df: pd.DataFrame) -> pd.Series:
    """MACD死叉：bbmacd从正变负。"""
    if "bbmacd" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    bbmacd = factors_df["bbmacd"]
    return ((bbmacd < 0) & (bbmacd.shift(1) >= 0)).astype(int)


register_event(
    name="evt_bbmacd_cross_upper",
    category="动量事件",
    detect_func=_detect_bbmacd_cross_upper,
    required_factors=["bbmacd_cross_upper"],
    description="BBMACD上穿上轨",
    direction="positive",
    is_core=True,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_bbmacd_cross_lower",
    category="动量事件",
    detect_func=_detect_bbmacd_cross_lower,
    required_factors=["bbmacd_cross_lower"],
    description="BBMACD下穿下轨",
    direction="negative",
    is_core=True,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_macd_golden_cross",
    category="动量事件",
    detect_func=_detect_macd_golden_cross,
    required_factors=["bbmacd"],
    description="MACD金叉（bbmacd从负变正）",
    direction="positive",
    is_core=False,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)

register_event(
    name="evt_macd_death_cross",
    category="动量事件",
    detect_func=_detect_macd_death_cross,
    required_factors=["bbmacd"],
    description="MACD死叉（bbmacd从正变负）",
    direction="negative",
    is_core=False,
    state_ttl_seconds=1800,
    allowed_roles=[EventRole.CONFIRM],
)


if __name__ == "__main__":
    from app.strategy.events.registry import list_by_category

    events = list_by_category("动量事件")
    print(f"动量事件已注册 {len(events)} 个")
    for e in events:
        print(f"  {e['name']} ttl={e['state_ttl_seconds']} roles={e['allowed_roles']}")
    print("OK")
