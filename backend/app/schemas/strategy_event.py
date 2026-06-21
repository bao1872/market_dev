"""StrategyEvent Pydantic schemas - 策略事件响应模型（M4）。

提供：
- StrategyEventResponse: 单条策略事件响应（不含 snapshot，列表查询用）
- StrategyEventDetailResponse: 事件详情响应（含 snapshot）
- StrategyEventListResponse: 事件列表响应
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrategyEventResponse(BaseModel):
    """单条策略事件响应（列表查询用，不含 snapshot 以减少负载）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="事件 ID")
    event_key: str = Field(..., description="事件唯一键")
    strategy_version_id: UUID = Field(..., description="策略版本 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    event_type: str = Field(..., description="事件类型")
    event_time: datetime = Field(..., description="事件发生时间")
    logical_entity_id: str | None = Field(None, description="逻辑实体")
    schema_version: int = Field(..., description="事件 schema 版本")
    payload: dict[str, Any] = Field(..., description="事件负载 JSONB")
    created_at: datetime = Field(..., description="创建时间")


class StrategyEventDetailResponse(StrategyEventResponse):
    """策略事件详情响应（含 snapshot 快照）。"""

    snapshot: dict[str, Any] = Field(default_factory=dict, description="事件发生时上下文快照")


class StrategyEventListResponse(BaseModel):
    """策略事件列表响应。"""

    items: list[StrategyEventResponse] = Field(default_factory=list, description="事件列表")
    total: int = Field(..., description="总数")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"StrategyEventResponse fields={list(StrategyEventResponse.model_fields.keys())}")
    print(f"StrategyEventDetailResponse fields={list(StrategyEventDetailResponse.model_fields.keys())}")
    print(f"StrategyEventListResponse fields={list(StrategyEventListResponse.model_fields.keys())}")
    print("OK")
