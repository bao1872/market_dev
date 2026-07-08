"""Capture 链路常量。

用途：避免服务层直接 import FastAPI deps 模块（app.core.deps）带来的循环依赖风险，
统一维护 capture token / screenshot 相关的纯常量。
"""

from __future__ import annotations

# [Capture] - 描述: stock_detail 截图链路作用域（advice.md 第六节硬规则）
CAPTURE_SCOPE_STOCK_DETAIL = "stock_detail_capture"


if __name__ == "__main__":
    # 自测入口：验证常量值
    assert CAPTURE_SCOPE_STOCK_DETAIL == "stock_detail_capture"
    print(f"CAPTURE_SCOPE_STOCK_DETAIL={CAPTURE_SCOPE_STOCK_DETAIL}")
    print("OK")
