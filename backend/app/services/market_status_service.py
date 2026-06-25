# -*- coding: utf-8 -*-
"""市场阶段统一计算服务。

提供 compute_market_session 函数，返回 6 种市场阶段枚举。
复用 app.core.time.now_shanghai，不另写第二套时间判断。

用法：
    python -m app.services.market_status_service    # 自测：打印各时段枚举
"""
from __future__ import annotations

from datetime import datetime

# 市场阶段枚举（6 值，advice.md 规范）
MARKET_SESSION_NON_TRADING_DAY = "NON_TRADING_DAY"
MARKET_SESSION_PRE_OPEN = "PRE_OPEN"
MARKET_SESSION_MORNING = "MORNING_SESSION"
MARKET_SESSION_LUNCH = "LUNCH_BREAK"
MARKET_SESSION_AFTERNOON = "AFTERNOON_SESSION"
MARKET_SESSION_CLOSED = "MARKET_CLOSED"

# 盘中交易时段集合（用于 STALE 判定等场景判断是否处于交易时段）
TRADING_SESSIONS = frozenset({MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON})


def compute_market_session(now_cst: datetime, is_trading_day: bool) -> str:
    """根据上海时间和交易日标志计算市场阶段。

    Args:
        now_cst: 上海时区的当前时间（由调用方通过 now_shanghai() 获取）
        is_trading_day: 当前日期是否为交易日

    Returns:
        市场阶段枚举字符串：
        - NON_TRADING_DAY: 非交易日
        - PRE_OPEN: 交易日 09:30 前（盘前）
        - MORNING_SESSION: 交易日 09:30-11:30（上午盘）
        - LUNCH_BREAK: 交易日 11:30-13:00（午休）
        - AFTERNOON_SESSION: 交易日 13:00-15:00（下午盘）
        - MARKET_CLOSED: 交易日 15:00 后（已收盘）
    """
    if not is_trading_day:
        return MARKET_SESSION_NON_TRADING_DAY

    # now_cst 必须是上海时区
    time_val = now_cst.hour * 60 + now_cst.minute

    if time_val < 570:  # < 09:30
        return MARKET_SESSION_PRE_OPEN
    elif time_val <= 690:  # 09:30-11:30
        return MARKET_SESSION_MORNING
    elif time_val < 780:  # 11:30-13:00
        return MARKET_SESSION_LUNCH
    elif time_val <= 900:  # 13:00-15:00
        return MARKET_SESSION_AFTERNOON
    else:  # > 15:00
        return MARKET_SESSION_CLOSED


if __name__ == "__main__":
    # 自测入口：验证各时段枚举
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    cases = [
        (datetime(2026, 6, 20, 10, 0, tzinfo=tz), False, "NON_TRADING_DAY"),
        (datetime(2026, 6, 24, 9, 0, tzinfo=tz), True, "PRE_OPEN"),
        (datetime(2026, 6, 24, 10, 0, tzinfo=tz), True, "MORNING_SESSION"),
        (datetime(2026, 6, 24, 12, 0, tzinfo=tz), True, "LUNCH_BREAK"),
        (datetime(2026, 6, 24, 14, 0, tzinfo=tz), True, "AFTERNOON_SESSION"),
        (datetime(2026, 6, 24, 15, 35, tzinfo=tz), True, "MARKET_CLOSED"),
    ]
    for now, is_td, expected in cases:
        got = compute_market_session(now, is_td)
        assert got == expected, f"{now.time()} is_td={is_td}: got={got} expected={expected}"
        print(f"{now.time()} is_td={is_td} -> {got}")
    print("OK")
