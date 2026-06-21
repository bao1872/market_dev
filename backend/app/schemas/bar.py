"""Bar Pydantic schemas - 行情响应模型。

- BarResponse: 单条行情响应
- BarListResponse: 行情列表响应（含分页元数据）
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field


class BarResponse(BaseModel):
    """单条行情响应。

    timeframe=1d/1w/1mo 时 trade_date 有值、trade_time 为 None；
    timeframe=15m/1h 时 trade_time 有值、trade_date 为 None。
    """

    instrument_id: UUID = Field(..., description="标的 ID")
    trade_date: date | None = Field(None, description="交易日（日线/周线/月线）")
    trade_time: datetime | None = Field(None, description="交易时间（15分钟/60分钟线）")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: float = Field(..., description="成交量")
    amount: float = Field(..., description="成交额")
    adj_factor: float = Field(1.0, description="复权因子")


class BarListResponse(BaseModel):
    """行情列表响应（服务端分页）。"""

    items: list[BarResponse] = Field(..., description="行情列表")
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码（1-based）")
    page_size: int = Field(..., description="每页大小")
    timeframe: str = Field(..., description="周期: 1d | 15m | 1h | 1w | 1mo")
    adj: str = Field(..., description="复权方式: qfq | none")


if __name__ == "__main__":
    # 自测入口：验证 schema 构造（无副作用）
    bar = BarResponse(
        instrument_id=UUID("12345678-1234-1234-1234-123456789012"),
        trade_date=date(2026, 6, 18),
        trade_time=None,
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1000000.0,
        amount=10200000.0,
        adj_factor=1.0,
    )
    print(f"bar.close={bar.close}")
    resp = BarListResponse(
        items=[bar],
        total=1,
        page=1,
        page_size=100,
        timeframe="1d",
        adj="none",
    )
    print(f"resp.total={resp.total}, items={len(resp.items)}")
    print("OK")
