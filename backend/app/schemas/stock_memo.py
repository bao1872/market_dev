"""StockMemo Pydantic schemas - 个股备忘录请求与响应模型。

提供：
- StockMemoUpsertRequest: 创建/更新备忘录请求（user_id 由上下文注入）
- StockMemoNotifyToggleRequest: 切换飞书推送开关请求
- StockMemoResponse: 备忘录响应

安全约束：
- user_id 不出现在请求体中（由认证上下文注入）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StockMemoUpsertRequest(BaseModel):
    """创建/更新备忘录请求。user_id 由认证上下文注入。"""

    content: str = Field(..., min_length=1, max_length=5000, description="备忘录内容")
    notify_feishu: bool = Field(default=False, description="是否盘中推送飞书")


class StockMemoNotifyToggleRequest(BaseModel):
    """切换飞书推送开关请求。"""

    notify_feishu: bool = Field(..., description="是否推送飞书")


class StockMemoResponse(BaseModel):
    """备忘录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="备忘录 ID")
    user_id: UUID = Field(..., description="用户 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    content: str = Field(..., description="备忘录内容")
    notify_feishu: bool = Field(..., description="是否盘中推送飞书")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"StockMemoUpsertRequest fields={list(StockMemoUpsertRequest.model_fields.keys())}")
    print(f"StockMemoNotifyToggleRequest fields={list(StockMemoNotifyToggleRequest.model_fields.keys())}")
    print(f"StockMemoResponse fields={list(StockMemoResponse.model_fields.keys())}")
    # 验证 user_id 不在请求体中（安全约束）
    assert "user_id" not in StockMemoUpsertRequest.model_fields, "user_id 不应在请求体中"
    assert "user_id" not in StockMemoNotifyToggleRequest.model_fields, "user_id 不应在请求体中"
    print("OK")
