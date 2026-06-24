"""策略 Pydantic schemas - 策略与版本的请求/响应模型。

提供：
- StrategyResponse: 策略定义响应
- StrategyListResponse: 策略列表响应
- StrategyVersionResponse: 策略版本响应
- StrategyVersionListResponse: 版本列表响应
- StrategySchemaResponse: 版本 schema 响应
- CreateStrategyRequest: 创建策略请求
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrategyResponse(BaseModel):
    """策略定义响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="策略定义 ID")
    strategy_key: str = Field(..., description="策略唯一标识")
    kind: str = Field(..., description="selector/monitor")
    display_name: str = Field(..., description="策略展示名称")
    created_at: datetime = Field(..., description="创建时间")
    environment: str = Field("production", description="环境：production/test")
    is_user_visible: bool = Field(True, description="是否对普通用户可见")
    is_scheduled: bool = Field(True, description="是否参与定时调度")


class StrategyVersionResponse(BaseModel):
    """策略版本响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="版本 ID")
    strategy_definition_id: UUID = Field(..., description="所属策略定义 ID")
    version: str = Field(..., description="版本号")
    status: str = Field(..., description="draft/released/archived")
    build_hash: str = Field(..., description="构建哈希")
    released_at: datetime | None = Field(None, description="发布时间")
    manifest: dict[str, Any] = Field(..., description="策略 Manifest")


class StrategyListResponse(BaseModel):
    """策略列表响应。"""

    items: list[StrategyResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class StrategyVersionListResponse(BaseModel):
    """版本列表响应。"""

    items: list[StrategyVersionResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class StrategySchemaResponse(BaseModel):
    """版本 schema 响应 - 返回 manifest 中的 parameters/outputs/input/capabilities。"""

    strategy_id: str = Field(..., description="策略 ID")
    version: str = Field(..., description="版本号")
    kind: str = Field(..., description="selector/monitor")
    parameters: list[dict[str, Any]] = Field(default_factory=list, description="参数定义")
    outputs: list[dict[str, Any]] = Field(default_factory=list, description="输出定义")
    input: dict[str, Any] = Field(default_factory=dict, description="输入要求")
    capabilities: dict[str, Any] = Field(default_factory=dict, description="能力声明")


class CreateStrategyRequest(BaseModel):
    """创建策略请求 - 提交完整 Manifest。"""

    manifest: dict[str, Any] = Field(..., description="策略 Manifest")
    strategy_schema: dict[str, Any] | None = Field(
        None, description="策略 schema（可选）", alias="schema"
    )


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"StrategyResponse fields={list(StrategyResponse.model_fields.keys())}")
    print(f"StrategyVersionResponse fields={list(StrategyVersionResponse.model_fields.keys())}")
    print(f"CreateStrategyRequest fields={list(CreateStrategyRequest.model_fields.keys())}")
    print("OK")
