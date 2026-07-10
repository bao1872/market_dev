"""Capture 链路常量。

用途：避免服务层直接 import FastAPI deps 模块（app.core.deps）带来的循环依赖风险，
统一维护 capture token / screenshot 相关的纯常量。
"""

from __future__ import annotations

# [Capture] - 描述: stock_detail 截图链路作用域（advice.md 第六节硬规则）
CAPTURE_SCOPE_STOCK_DETAIL = "stock_detail_capture"

# [Feishu] - 描述: 飞书盘中截图业务默认周期（盘迹硬规则，CHANGE-20260710-002 确立）
# 盘中监控触发只依赖最新已完成 1m bar；飞书盘中截图默认展示 1d（日线）。
# 实时性由 Capture Snapshot 1d + include_realtime=True 的 partial daily 合成保证，
# 截图修复不得改变 watchlist_monitor 事件计算口径。
# Capture API 支持多周期（1d/15m/1h/...）是能力，不等于飞书业务默认 15m。
FEISHU_CAPTURE_TIMEFRAME = "1d"


if __name__ == "__main__":
    # 自测入口：验证常量值
    assert CAPTURE_SCOPE_STOCK_DETAIL == "stock_detail_capture"
    assert FEISHU_CAPTURE_TIMEFRAME == "1d"
    print(f"CAPTURE_SCOPE_STOCK_DETAIL={CAPTURE_SCOPE_STOCK_DETAIL}")
    print(f"FEISHU_CAPTURE_TIMEFRAME={FEISHU_CAPTURE_TIMEFRAME}")
    print("OK")
