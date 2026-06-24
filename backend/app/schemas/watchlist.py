"""Watchlist Pydantic schemas - 用户自选股请求与响应模型。

提供：
- WatchlistAddRequest: 加入自选请求（仅 instrument_id + source，user_id 由上下文注入）
- WatchlistItemResponse: 单条自选响应
- WatchlistListResponse: 自选列表响应
- WatchlistMonitorStatusItem: 自选股+监控状态聚合响应（单条）
- WatchlistMonitorStatusResponse: 自选股+监控状态聚合响应（列表）

安全约束：
- user_id 不出现在请求体中（由认证上下文注入）
- instrument_id 为 UUID
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class WatchlistAddRequest(BaseModel):
    """加入自选请求。

    user_id 不接受客户端传入（由认证上下文注入），仅传 instrument_id 与来源。
    """

    instrument_id: UUID = Field(..., description="股票 ID（UUID）")
    source: str = Field(
        default="manual",
        description="加入来源（manual/selection_plan/monitor）",
    )


class WatchlistItemResponse(BaseModel):
    """单条自选响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="自选记录 ID")
    user_id: UUID = Field(..., description="用户 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    source: str = Field(..., description="加入来源")
    active: bool = Field(..., description="是否活跃")
    created_at: datetime = Field(..., description="加入时间")
    removed_at: datetime | None = Field(None, description="移除时间")


class WatchlistListResponse(BaseModel):
    """自选列表响应。"""

    items: list[WatchlistItemResponse] = Field(default_factory=list, description="自选列表")
    total: int = Field(..., description="总记录数")


class WatchlistMonitorStatusItem(BaseModel):
    """自选股+监控状态聚合响应（单条）。"""

    watchlist_item_id: UUID = Field(..., description="自选记录 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    symbol: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    market: str = Field(..., description="市场（SH/SZ/BJ）")
    watchlist_created_at: datetime = Field(..., description="加入自选时间")
    monitor_status: str = Field(
        ..., description="监控状态枚举：WAITING_FIRST_RUN/SUCCEEDED/FAILED/STALE/MARKET_CLOSED"
    )
    evaluation_status: str | None = Field(
        None, description="评估状态（SUCCEEDED/FAILED/PENDING）"
    )
    error_code: str | None = Field(
        None, description="评估错误码（无错误时为 null）"
    )
    source_bar_time: str | None = Field(
        None, description="监控状态对应的 bar 时间"
    )
    metrics: dict[str, Any] | None = Field(
        None, description="监控状态 metrics（MonitorState payload，无状态时为 null）"
    )
    updated_at: datetime | None = Field(
        None, description="监控状态更新时间"
    )


class WatchlistMonitorStatusResponse(BaseModel):
    """自选股+监控状态聚合响应（列表）。"""

    items: list[WatchlistMonitorStatusItem] = Field(
        default_factory=list, description="自选股+监控状态列表"
    )


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"WatchlistAddRequest fields={list(WatchlistAddRequest.model_fields.keys())}")
    print(f"WatchlistItemResponse fields={list(WatchlistItemResponse.model_fields.keys())}")
    print(f"WatchlistListResponse fields={list(WatchlistListResponse.model_fields.keys())}")
    print(f"WatchlistMonitorStatusItem fields={list(WatchlistMonitorStatusItem.model_fields.keys())}")
    print(f"WatchlistMonitorStatusResponse fields={list(WatchlistMonitorStatusResponse.model_fields.keys())}")
    # 验证 user_id 不在请求体中（安全约束）
    assert "user_id" not in WatchlistAddRequest.model_fields, "user_id 不应在请求体中"
    print("OK")
