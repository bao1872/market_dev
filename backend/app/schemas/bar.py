"""Bar Pydantic schemas - 行情响应模型。

- BarResponse: 单条行情响应
- BarListResponse: 行情列表响应（含分页元数据）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class QuoteResponse(BaseModel):
    """实时行情报价响应（可信来源与新鲜度）。"""

    instrument_id: UUID = Field(..., description="标的 ID")
    symbol: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    current_price: float = Field(..., description="最新价")
    open: float = Field(..., description="开盘价")
    high: float = Field(..., description="最高价")
    low: float = Field(..., description="最低价")
    close: float = Field(..., description="收盘价")
    volume: float = Field(..., description="成交量")
    prev_close: float = Field(..., description="昨收")
    change_pct: float = Field(..., description="涨跌幅(%)")
    update_time: datetime | None = Field(None, description="数据更新时间（ISO 8601）")
    source: Literal["pytdx", "daily_fallback"] = Field(
        ..., description="数据来源: pytdx 实时 | daily_fallback 日线回退"
    )
    is_realtime: bool = Field(..., description="是否为实时行情")
    freshness_seconds: float = Field(..., description="数据新鲜度（秒）")
    degraded: bool = Field(..., description="是否降级")
    degraded_reason: str | None = Field(None, description="降级原因")
    amount: float | None = Field(None, description="成交额")
    # CHANGE-20260713-010: 总市值/流通市值（数据源不可用时返回 null）
    total_market_cap: float | None = Field(None, description="总市值（元）")
    float_market_cap: float | None = Field(None, description="流通市值（元）")
    market_cap_as_of: date | None = Field(None, description="市值数据日期")
    market_cap_source: str | None = Field(None, description="市值数据来源")
    market_cap_degraded_reason: str | None = Field(
        None, description="市值降级原因（如 market_cap_data_unavailable）"
    )


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
    """行情列表响应（服务端分页 + 数据源诊断）。"""

    items: list[BarResponse] = Field(..., description="行情列表")
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码（1-based）")
    page_size: int = Field(..., description="每页大小")
    timeframe: str = Field(..., description="周期: 1d | 15m | 1h | 1w | 1mo")
    adj: str = Field(..., description="复权方式: qfq | none")
    # [bars] - 数据源诊断字段（Phase 4 行情聚合 SSOT）
    data_source: str = Field("db", description="数据来源: db | hybrid | pytdx | degraded")
    as_of: datetime | None = Field(None, description="数据生成时间（ISO 8601）")
    is_partial: bool = Field(False, description="最后一根 bar 是否为未完成 partial bar")
    last_persisted_bar_time: datetime | None = Field(None, description="最后一条持久化 bar 时间")
    last_live_bar_time: datetime | None = Field(None, description="最后一条实时 bar 时间")
    freshness_seconds: float = Field(0.0, description="数据新鲜度（秒）")
    degraded: bool = Field(False, description="是否降级（Pytdx 失败等）")
    degraded_reason: str | None = Field(None, description="降级原因")
    # [CHANGE-20260717-002 SSOT] - MDAS v2 契约诊断字段（跨调用方一致性校验）
    # source_bar_hash/adj_factor_hash 校验同一标的/周期/结束日下 bars API、
    # indicator/SMC、feature snapshot 返回的 OHLCV 与因子是否完全一致
    source_bar_hash: str | None = Field(None, description="bars OHLCV SHA256 前 16 字符（跨调用方一致性校验）")
    adj_factor_hash: str | None = Field(None, description="复权因子序列 SHA256 前 16 字符（adj=none 时为空串）")
    market_data_contract_version: str | None = Field(None, description="行情数据契约版本（当前 v2）")
    completed_through: datetime | None = Field(None, description="最新已完成 bar 时间（不含 partial/realtime）")
    adjustment_as_of: date | None = Field(None, description="复权锚点（None=最新；point-in-time 回算时为业务日）")


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
