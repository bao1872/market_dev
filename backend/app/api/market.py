# -*- coding: utf-8 -*-
"""市场状态 API（交易时段、交易日判断）。

提供：
- GET /market/status: 获取当前市场状态（交易日、交易时段、状态文本）

设计说明：
- 交易日判断：使用 is_trading_day_async 三级降级（DB -> Tushare -> weekday）
- 交易时段判断：复用 app.services.market_status_service.compute_market_session（6 值枚举）
- 状态文本：交易中 / 已收盘 / 休市 / 盘前（向后兼容保留）
- market_session：6 值枚举（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）
- 时区：统一使用 app.core.time 的上海时区工具，避免散落的 ZoneInfo 实例
"""

from __future__ import annotations

from datetime import time as dt_time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.time import now_shanghai, shanghai_business_date
from app.services.calendar_service import is_trading_day_async
from app.services.market_status_service import compute_market_session

router = APIRouter(prefix="/market", tags=["market"])


class MarketStatusResponse(BaseModel):
    """市场状态响应"""
    is_trading_day: bool
    is_trading_hours: bool
    status_text: str  # "交易中" / "已收盘" / "休市" / "盘前"（向后兼容）
    market_session: str  # 6 值枚举（与 watchlist monitor-status 对齐）


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status(db: AsyncSession = Depends(get_db)):
    """获取当前市场状态

    交易日判断：使用 trading_calendar 表 + Tushare + weekday 三级降级
    交易时段判断：复用 compute_market_session（6 值枚举，与 watchlist 对齐）
    """
    today = shanghai_business_date()
    now = now_shanghai()

    # 交易日判断
    is_trading_day = await is_trading_day_async(db, today)

    # [市场阶段] - 统一调用 compute_market_session（6 值枚举）
    market_session = compute_market_session(now, is_trading_day)

    # is_trading_hours：仅上午/下午盘为 True（用于向后兼容）
    is_trading_hours = market_session in ("MORNING_SESSION", "AFTERNOON_SESSION")

    # 状态文本（向后兼容：保留中文 status_text）
    if not is_trading_day:
        status_text = "休市"
    elif is_trading_hours:
        status_text = "交易中"
    elif now.time() > dt_time(15, 0):
        status_text = "已收盘"
    else:
        status_text = "盘前"

    return MarketStatusResponse(
        is_trading_day=is_trading_day,
        is_trading_hours=is_trading_hours,
        status_text=status_text,
        market_session=market_session,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
