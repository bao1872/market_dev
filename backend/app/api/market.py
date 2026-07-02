"""市场状态 API（交易时段、交易日判断）。

提供：
- GET /market/status: 获取当前市场状态（交易日、交易时段、状态文本、日历诊断信息）

设计说明：
- 交易日判断：使用 is_trading_day_async 三级降级（DB -> Mootdx -> weekday）
- 交易时段判断：复用 app.services.market_status_service.compute_market_session（6 值枚举）
- 状态文本映射：NON_TRADING_DAY->休市、PRE_OPEN->盘前、MORNING_SESSION->交易中、
  LUNCH_BREAK->午间休市、AFTERNOON_SESSION->交易中、MARKET_CLOSED->已收盘、
  UNKNOWN->交易日历待确认
- 日历诊断：当 DB 中 status=UNKNOWN 时返回 degraded=True，不显示"休市"
- market_session：6 值枚举（NON_TRADING_DAY/PRE_OPEN/MORNING_SESSION/LUNCH_BREAK/AFTERNOON_SESSION/MARKET_CLOSED）
- 时区：统一使用 app.core.time 的上海时区工具，避免散落的 ZoneInfo 实例
"""

from __future__ import annotations

from datetime import date as dt_date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.time import now_shanghai, shanghai_business_date, to_shanghai_iso
from app.models.calendar import TradingCalendar
from app.services.calendar_service import is_trading_day_async
from app.services.market_status_service import compute_market_session

router = APIRouter(prefix="/market", tags=["market"])


class MarketStatusResponse(BaseModel):
    """市场状态响应"""
    is_trading_day: bool
    is_trading_hours: bool
    status_text: str  # "交易中" / "已收盘" / "休市" / "盘前" / "交易日历待确认"
    market_session: str  # 6 值枚举（与 watchlist monitor-status 对齐）
    # [Calendar] - 描述: 交易日历诊断字段
    calendar_date: dt_date = Field(..., description="当前日历日期")
    calendar_status: str | None = Field(None, description="DB 中日历状态 OPEN/CLOSED/UNKNOWN")
    calendar_source: str | None = Field(None, description="DB 中日历来源")
    calendar_verified_at: str | None = Field(None, description="DB 中日历最近确认时间 ISO")
    degraded: bool = Field(False, description="是否处于降级状态")
    degraded_reason: str | None = Field(None, description="降级原因")


@router.get("/status", response_model=MarketStatusResponse)
async def get_market_status(db: AsyncSession = Depends(get_db)):
    """获取当前市场状态

    交易日判断：使用 trading_calendar 表 + Mootdx + weekday 三级降级
    交易时段判断：复用 compute_market_session（6 值枚举，与 watchlist 对齐）
    """
    today = shanghai_business_date()
    now = now_shanghai()

    # 交易日判断（bool，可能经过降级）
    is_trading_day = await is_trading_day_async(db, today)

    # 查询 DB 原始日历记录用于诊断展示
    degraded = False
    degraded_reason: str | None = None
    calendar_status: str | None = None
    calendar_source: str | None = None
    calendar_verified_at: str | None = None

    try:
        stmt = select(
            TradingCalendar.status,
            TradingCalendar.source,
            TradingCalendar.verified_at,
        ).where(
            TradingCalendar.trade_date == today,
            TradingCalendar.market == "A",
        )
        result = await db.execute(stmt)
        row = result.first()
        if row:
            calendar_status, calendar_source, verified_at = row
            if verified_at is not None:
                calendar_verified_at = to_shanghai_iso(verified_at)
            if calendar_status == "UNKNOWN":
                degraded = True
                degraded_reason = "calendar status UNKNOWN"
                # [市场状态] - 描述: UNKNOWN 时不显示休市，返回待确认文案
                market_session = compute_market_session(now, is_trading_day=True)
            else:
                market_session = compute_market_session(now, is_trading_day)
        else:
            # DB 无记录，is_trading_day 已降级到 Mootdx/weekday
            degraded = True
            degraded_reason = "calendar not in DB"
            market_session = compute_market_session(now, is_trading_day)
    except Exception as exc:
        # [市场状态] - 描述: DB 诊断查询失败不影响主体返回，记录降级原因
        degraded = True
        degraded_reason = f"calendar diagnostics unavailable: {exc}"
        market_session = compute_market_session(now, is_trading_day)

    # is_trading_hours：仅上午/下午盘为 True（用于向后兼容）
    is_trading_hours = market_session in ("MORNING_SESSION", "AFTERNOON_SESSION")

    # 状态文本统一映射
    status_text_map = {
        "NON_TRADING_DAY": "休市",
        "PRE_OPEN": "盘前",
        "MORNING_SESSION": "交易中",
        "LUNCH_BREAK": "午间休市",
        "AFTERNOON_SESSION": "交易中",
        "MARKET_CLOSED": "已收盘",
    }
    if degraded and calendar_status == "UNKNOWN":
        status_text = "交易日历待确认"
    else:
        status_text = status_text_map.get(market_session, "未知")

    return MarketStatusResponse(
        is_trading_day=is_trading_day,
        is_trading_hours=is_trading_hours,
        status_text=status_text,
        market_session=market_session,
        calendar_date=today,
        calendar_status=calendar_status,
        calendar_source=calendar_source,
        calendar_verified_at=calendar_verified_at,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
