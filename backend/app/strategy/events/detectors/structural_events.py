"""结构事件检测 - V1.1 升级版（已修复占位实现）。

从 ref/交易/event_lib/detectors/structural_events.py 迁移。

升级内容：
1. 添加 state_ttl_seconds 和 allowed_roles 声明
2. 修复 _detect_support_broken / _detect_resistance_broken 占位实现：
   原实现返回全 0（占位），现使用 pandas 向量化计算支撑/阻力位突破。
   - 支撑位 = rolling min(low, 20)，支撑跌破 = close < 前一 bar 支撑位
   - 阻力位 = rolling max(high, 20)，阻力突破 = close > 前一 bar 阻力位

Registered Events:
    - evt_break_sell_stop_cluster: 跌破卖出止损聚类（20日新低）
    - evt_break_buy_stop_cluster: 突破买入止损聚类（20日新高）
    - evt_support_broken: 支撑跌破（向量化实现）
    - evt_resistance_broken: 阻力突破（向量化实现）
"""

from __future__ import annotations

import pandas as pd

from app.strategy.events.base import EventRole
from app.strategy.events.registry import register_event

# 支撑/阻力计算窗口
_SR_WINDOW = 20


def _detect_break_sell_stop_cluster(factors_df: pd.DataFrame) -> pd.Series:
    """跌破卖出止损聚类：价格创20日新低。"""
    if "close" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    low_20 = factors_df["close"].rolling(window=_SR_WINDOW, min_periods=1).min()
    return (factors_df["close"] <= low_20).astype(int)


def _detect_break_buy_stop_cluster(factors_df: pd.DataFrame) -> pd.Series:
    """突破买入止损聚类：价格创20日新高。"""
    if "close" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    high_20 = factors_df["close"].rolling(window=_SR_WINDOW, min_periods=1).max()
    return (factors_df["close"] >= high_20).astype(int)


def _detect_support_broken(factors_df: pd.DataFrame) -> pd.Series:
    """支撑跌破（向量化实现，已修复占位）。

    逻辑：
    1. 优先使用 support_ref 列作为支撑位（若存在）
    2. 否则从 low 列计算支撑位 = 过去 20 日最低价的滚动最小值
    3. 使用前一 bar 的支撑位作为参考（避免未来函数）
    4. 支撑跌破 = 当日收盘价 < 前一 bar 支撑位
    """
    if "close" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    # 优先使用 support_ref
    if "support_ref" in factors_df.columns:
        support = factors_df["support_ref"].shift(1)
        return (factors_df["close"] < support).astype(int)
    # 从 low 列计算支撑位
    if "low" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    support = factors_df["low"].rolling(window=_SR_WINDOW, min_periods=1).min().shift(1)
    return (factors_df["close"] < support).astype(int)


def _detect_resistance_broken(factors_df: pd.DataFrame) -> pd.Series:
    """阻力突破（向量化实现，已修复占位）。

    逻辑：
    1. 优先使用 resistance_ref 列作为阻力位（若存在）
    2. 否则从 high 列计算阻力位 = 过去 20 日最高价的滚动最大值
    3. 使用前一 bar 的阻力位作为参考（避免未来函数）
    4. 阻力突破 = 当日收盘价 > 前一 bar 阻力位
    """
    if "close" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    # 优先使用 resistance_ref
    if "resistance_ref" in factors_df.columns:
        resistance = factors_df["resistance_ref"].shift(1)
        return (factors_df["close"] > resistance).astype(int)
    # 从 high 列计算阻力位
    if "high" not in factors_df.columns:
        return pd.Series(0, index=factors_df.index)
    resistance = factors_df["high"].rolling(window=_SR_WINDOW, min_periods=1).max().shift(1)
    return (factors_df["close"] > resistance).astype(int)


# 注册结构事件（含 state_ttl_seconds 和 allowed_roles 声明）
register_event(
    name="evt_break_sell_stop_cluster",
    category="结构事件",
    detect_func=_detect_break_sell_stop_cluster,
    required_factors=["close"],
    description="跌破卖出止损聚类（20日新低）",
    direction="negative",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.VETO],
)

register_event(
    name="evt_break_buy_stop_cluster",
    category="结构事件",
    detect_func=_detect_break_buy_stop_cluster,
    required_factors=["close"],
    description="突破买入止损聚类（20日新高）",
    direction="positive",
    is_core=True,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.CONFIRM],
)

register_event(
    name="evt_support_broken",
    category="结构事件",
    detect_func=_detect_support_broken,
    required_factors=["close"],
    description="支撑跌破（收盘价跌破 support_ref 或前20日最低价支撑位）",
    direction="negative",
    is_core=False,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.VETO],
)

register_event(
    name="evt_resistance_broken",
    category="结构事件",
    detect_func=_detect_resistance_broken,
    required_factors=["close"],
    description="阻力突破（收盘价突破 resistance_ref 或前20日最高价阻力位）",
    direction="positive",
    is_core=False,
    state_ttl_seconds=3600,
    allowed_roles=[EventRole.TRIGGER, EventRole.CONFIRM],
)


if __name__ == "__main__":
    # 自测入口：验证结构事件检测（含占位实现修复）
    from app.strategy.events.registry import get_event, list_by_category

    # 1. 验证注册元数据
    for evt_name in [
        "evt_break_sell_stop_cluster", "evt_break_buy_stop_cluster",
        "evt_support_broken", "evt_resistance_broken",
    ]:
        meta = get_event(evt_name)
        assert meta["state_ttl_seconds"] > 0, f"{evt_name} state_ttl_seconds 应 > 0"
        assert EventRole.OBSERVE not in meta["allowed_roles"] or len(meta["allowed_roles"]) > 1, \
            f"{evt_name} 应有明确角色"
        print(f"{evt_name}: ttl={meta['state_ttl_seconds']}, roles={meta['allowed_roles']}")

    # 2. 验证支撑跌破检测（向量化实现）
    df = pd.DataFrame(
        {
            "close": [10.0, 10.5, 11.0, 10.8, 9.5, 8.0, 9.0, 10.0, 11.0, 12.0,
                      11.5, 11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5],
            "low": [9.5, 10.0, 10.5, 10.3, 9.0, 7.5, 8.5, 9.5, 10.5, 11.5,
                    11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0, 6.5, 6.0],
            "high": [10.5, 11.0, 11.5, 11.0, 10.0, 8.5, 9.5, 10.5, 11.5, 12.5,
                     12.0, 11.5, 11.0, 10.5, 10.0, 9.5, 9.0, 8.5, 8.0, 7.5, 7.0],
            "support_resistance_zones": [None] * 21,
        },
        index=pd.to_datetime(pd.date_range("2026-06-01", periods=21, freq="D")),
    )
    support_broken = _detect_support_broken(df)
    print(f"support_broken sum={support_broken.sum()}")
    # 最后几日 close 持续下跌，应检测到支撑跌破
    assert support_broken.sum() > 0, "应检测到支撑跌破事件"

    # 3. 验证阻力突破检测
    df_up = pd.DataFrame(
        {
            "close": [10.0, 10.5, 11.0, 10.8, 10.2, 10.0, 10.5, 11.0, 11.5, 12.0,
                      12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0, 17.5],
            "low": [9.5, 10.0, 10.5, 10.3, 9.8, 9.5, 10.0, 10.5, 11.0, 11.5,
                    12.0, 12.5, 13.0, 13.5, 14.0, 14.5, 15.0, 15.5, 16.0, 16.5, 17.0],
            "high": [10.5, 11.0, 11.5, 11.0, 10.5, 10.2, 10.8, 11.2, 11.8, 12.2,
                     12.8, 13.2, 13.8, 14.2, 14.8, 15.2, 15.8, 16.2, 16.8, 17.2, 17.8],
            "support_resistance_zones": [None] * 21,
        },
        index=pd.to_datetime(pd.date_range("2026-06-01", periods=21, freq="D")),
    )
    resistance_broken = _detect_resistance_broken(df_up)
    print(f"resistance_broken sum={resistance_broken.sum()}")
    assert resistance_broken.sum() > 0, "应检测到阻力突破事件"

    # 4. 验证事件列表
    events = list_by_category("结构事件")
    print(f"结构事件已注册 {len(events)} 个")
    print("OK")
