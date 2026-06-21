"""交易日历 API 路由。

提供：
- GET /calendar: 查询日历（支持日期范围筛选）
- GET /calendar/is-trading-day/{date}: 查询某日是否为交易日

设计说明：
- 日期范围筛选：start_date / end_date（含端点）
- is-trading-day 使用三级降级判断（DB -> Tushare -> weekday）
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.calendar import TradingCalendar
from app.schemas.calendar import CalendarListResponse, CalendarResponse, TradingDayResponse
from app.services.calendar_service import is_trading_day_async

router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get("", response_model=CalendarListResponse)
async def list_calendar(
    start_date: date | None = Query(None, description="起始日期（含，YYYY-MM-DD）"),
    end_date: date | None = Query(None, description="结束日期（含，YYYY-MM-DD）"),
    market: str | None = Query(None, description="市场筛选：A/HS"),
    db: AsyncSession = Depends(get_db),
) -> CalendarListResponse:
    """查询交易日历，支持日期范围与市场筛选。"""
    stmt = select(TradingCalendar)
    if start_date:
        stmt = stmt.where(TradingCalendar.trade_date >= start_date)
    if end_date:
        stmt = stmt.where(TradingCalendar.trade_date <= end_date)
    if market:
        stmt = stmt.where(TradingCalendar.market == market)
    stmt = stmt.order_by(TradingCalendar.trade_date)

    result = await db.execute(stmt)
    items = result.scalars().all()

    return CalendarListResponse(
        items=[CalendarResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.get("/is-trading-day/{target_date}", response_model=TradingDayResponse)
async def check_trading_day(
    target_date: date,
    db: AsyncSession = Depends(get_db),
) -> TradingDayResponse:
    """查询指定日期是否为交易日（三级降级：DB -> Tushare -> weekday）。"""
    # 三级降级判断，始终返回 bool
    is_trading = await is_trading_day_async(db, target_date)
    return TradingDayResponse(
        trade_date=target_date,
        is_trading_day=is_trading,
        source="db/tushare/weekday",  # 实际来源由服务层日志记录
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
