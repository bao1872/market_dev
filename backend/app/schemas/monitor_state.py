"""MonitorState Pydantic schemas - 监控状态响应模型（M3）。

提供：
- MonitorStateResponse: 单条监控状态响应
- MonitorStateListResponse: 监控状态列表响应
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MonitorStateResponse(BaseModel):
    """单条监控状态响应。"""

    model_config = ConfigDict(from_attributes=True)

    strategy_version_id: UUID = Field(..., description="策略版本 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    bar_time: datetime = Field(..., description="触发该状态的 bar 时间")
    calculation_id: str = Field(..., description="计算批次 ID")
    state_schema_version: int = Field(..., description="状态 schema 版本")
    payload: dict[str, Any] = Field(..., description="监控状态 JSONB")
    updated_at: datetime = Field(..., description="更新时间")


class MonitorStateListResponse(BaseModel):
    """监控状态列表响应。"""

    items: list[MonitorStateResponse] = Field(default_factory=list, description="状态列表")
    total: int = Field(..., description="总数")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"MonitorStateResponse fields={list(MonitorStateResponse.model_fields.keys())}")
    print(f"MonitorStateListResponse fields={list(MonitorStateListResponse.model_fields.keys())}")
    print("OK")
