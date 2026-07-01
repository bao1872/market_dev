"""Subscription Pydantic schemas - 订阅状态与账户列表响应模型。

提供：
- MembershipResponse: 当前用户订阅状态响应（status/expires_at/剩余天数）
- RenewSuccessResponse: 邀请码续期成功响应
- MemberListItem: 管理员订阅账户列表项
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MembershipResponse(BaseModel):
    """订阅状态响应。"""

    model_config = ConfigDict(from_attributes=True)

    status: str = Field(..., description="active/expired")
    started_at: datetime = Field(..., description="订阅开始时间")
    expires_at: datetime = Field(..., description="订阅到期时间")
    remaining_days: int = Field(..., description="剩余天数")
    renewal_count: int = Field(..., description="累计续期次数")


class RenewSuccessResponse(BaseModel):
    """续期成功响应。"""

    membership_status: str = Field(..., description="订阅状态：active")
    started_at: datetime = Field(..., description="订阅开始时间")
    old_expires_at: datetime | None = Field(None, description="续期前到期时间")
    new_expires_at: datetime = Field(..., description="续期后到期时间")
    remaining_days: int = Field(..., description="剩余天数")


class MemberListItem(BaseModel):
    """订阅账户列表项。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID = Field(..., description="用户 ID")
    email: str = Field(..., description="用户邮箱")
    account_status: str = Field(..., description="active/disabled/pending")
    membership_status: str | None = Field(None, description="active/expired")
    started_at: datetime | None = Field(None, description="订阅开始时间")
    expires_at: datetime | None = Field(None, description="订阅到期时间")
    remaining_days: int | None = Field(None, description="剩余天数")
    renewal_count: int = Field(..., description="累计续期次数")
    created_at: datetime = Field(..., description="用户创建时间")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义（不连接数据库）
    print(f"MembershipResponse fields={list(MembershipResponse.model_fields.keys())}")
    print(f"RenewSuccessResponse fields={list(RenewSuccessResponse.model_fields.keys())}")
    print(f"MemberListItem fields={list(MemberListItem.model_fields.keys())}")

    resp = MembershipResponse(
        status="active",
        started_at=datetime.now(),
        expires_at=datetime.now(),
        remaining_days=30,
        renewal_count=0,
    )
    assert resp.status == "active"
    print("OK")
