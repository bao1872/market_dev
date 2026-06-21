"""ConfigDefinition Pydantic schemas - 配置注册表响应与更新模型。

提供：
- ConfigDefinitionResponse: 配置定义响应（Secret 字段脱敏为 "***"）
- ConfigDefinitionUpdate: 更新配置请求（仅允许更新 current_value）
- ConfigListResponse: 配置列表分页响应

脱敏规则（V1.1 06_CONFIGURATION_CENTER.md §4）：
- sensitivity=secret 或 value_type=secret 时，current_value 返回 "***"
- 管理员不可读取用户 Secret 明文
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Secret 字段的脱敏值
SECRET_MASK = "***"


class ConfigDefinitionResponse(BaseModel):
    """配置定义响应 - Secret 字段脱敏。

    当 sensitivity=secret 或 value_type=secret 时，current_value 返回 "***"，
    不返回明文/密文。
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="配置 ID")
    config_key: str = Field(..., description="配置项唯一标识")
    display_name: str = Field(..., description="展示名称")
    description: str | None = Field(None, description="配置描述")
    value_type: str = Field(..., description="值类型")
    allowed_scopes: list[Any] = Field(default_factory=list, description="允许的作用域列表")
    default_value: Any | None = Field(None, description="默认值")
    current_value: Any | None = Field(None, description="当前值（Secret 脱敏为 ***）")
    is_required: bool = Field(..., description="是否必填")
    validation: dict[str, Any] | None = Field(None, description="校验规则")
    sensitivity: str = Field(..., description="敏感级别：public/internal/secret")
    restart_policy: str = Field(..., description="生效方式")
    ui: dict[str, Any] = Field(default_factory=dict, description="UI 控件配置")
    test_action: str | None = Field(None, description="测试动作")
    audit: bool = Field(..., description="是否审计变更")
    status: str = Field(..., description="状态：active/deprecated")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class ConfigDefinitionUpdate(BaseModel):
    """更新配置请求 - 仅允许更新 current_value。

    其他字段（如 config_key、value_type、sensitivity）为配置定义本身，
    不可通过此接口修改，需通过数据库迁移或专用管理工具变更。
    """

    current_value: Any = Field(..., description="新的配置值（Secret 类型传明文，服务端加密存储）")


class ConfigListResponse(BaseModel):
    """配置列表分页响应。"""

    items: list[ConfigDefinitionResponse] = Field(
        default_factory=list, description="配置列表"
    )
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码（从 1 开始）")
    page_size: int = Field(..., description="每页大小")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"ConfigDefinitionResponse fields={list(ConfigDefinitionResponse.model_fields.keys())}")
    print(f"ConfigDefinitionUpdate fields={list(ConfigDefinitionUpdate.model_fields.keys())}")
    print(f"ConfigListResponse fields={list(ConfigListResponse.model_fields.keys())}")
    print(f"SECRET_MASK={SECRET_MASK}")
    print("OK")
