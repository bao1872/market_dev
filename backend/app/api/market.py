# -*- coding: utf-8 -*-
"""市场状态 API（交易时段、交易日判断）。

提供：
- GET /market/status: 获取当前市场状态（交易日、交易时段、状态文本）

设计说明：
- 交易日判断：使用 is_trading_day_async 三级降级（DB -> Tushare -> weekday）
- 交易时段判断：weekday + 9:30-11:30 / 13:00-15:00
- 状态文本：交易中 / 已收盘 / 休市 / 盘前
"""

from __future__ import annotations

from datetime import date, datetime, time as dt_time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.services.calendar_service import is_trading_day_async

router = APIRouter(prefix="/market", tags=["market"])


class MarketStatusResponse(BaseModel):
    """市场状态响应"""
    is_trading_day: bool
    is_trading_hours: bool
    status_text: str  # "交易中" / "已收盘" / "休市" / "盘前"


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status(db: AsyncSession = Depends(get_db)):
    """获取当前市场状态

    交易日判断：使用 trading_calendar 表 + Tushare + weekday 三级降级
    交易时段判断：weekday + 9:30-11:30 / 13:00-15:00
    """
    today = date.today()
    now = datetime.now()

    # 交易日判断
    is_trading_day = await is_trading_day_async(db, today)

    # 交易时段判断（仅在交易日基础上判断时间）
    is_trading_hours = False
    if is_trading_day:
        current_time = now.time()
        morning_session = dt_time(9, 30) <= current_time <= dt_time(11, 30)
        afternoon_session = dt_time(13, 0) <= current_time <= dt_time(15, 0)
        is_trading_hours = morning_session or afternoon_session

    # 状态文本
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
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
