"""Calendar Pydantic schemas - 交易日历响应模型。

提供：
- CalendarResponse: 单条日历响应
- CalendarListResponse: 日历列表响应
- TradingDayResponse: 是否交易日响应
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CalendarResponse(BaseModel):
    """单条交易日历响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="主键 UUID")
    trade_date: date = Field(..., description="交易日期")
    is_trading_day: bool = Field(..., description="是否为交易日")
    market: str = Field(..., description="市场标识：A/HS")
    created_at: datetime = Field(..., description="创建时间")


class CalendarListResponse(BaseModel):
    """交易日历列表响应。"""

    items: list[CalendarResponse] = Field(default_factory=list, description="日历列表")
    total: int = Field(..., description="总记录数")


class TradingDayResponse(BaseModel):
    """是否交易日查询响应。"""

    trade_date: date = Field(..., description="查询日期")
    is_trading_day: bool = Field(..., description="是否为交易日")
    source: str = Field(..., description="判断来源：db/tushare/weekday")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"CalendarResponse fields={list(CalendarResponse.model_fields.keys())}")
    print(f"CalendarListResponse fields={list(CalendarListResponse.model_fields.keys())}")
    print(f"TradingDayResponse fields={list(TradingDayResponse.model_fields.keys())}")
    print("OK")
