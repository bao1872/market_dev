"""会员与邀请码 Pydantic schemas - V1.6 会员系统。

提供：
- UserRegister: 注册请求（email + password + invite_code）
- InviteCodeRenew: 续期请求（invite_code）
- MembershipResponse: 会员状态响应（status/expires_at/剩余天数）
- InviteCodeCreate: 邀请码生成请求（数量/备注）
- InviteCodeResponse: 邀请码响应（含明文，仅生成时返回）
- InviteCodeListItem: 邀请码列表项（不含明文）
- InviteRedemptionResponse: 兑换记录响应
- MemberListItem: 会员账户列表项
- LoginResponse: 登录响应（含 token + 会员状态）
- RegisterSuccessResponse: 注册成功响应
- RenewSuccessResponse: 续期成功响应
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_PATTERN = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


class UserRegister(BaseModel):
    """注册请求 - 需邀请码。"""

    email: str = Field(..., description="登录邮箱（唯一）")
    password: str = Field(..., min_length=8, max_length=128, description="密码（8-128 字符）")
    invite_code: str = Field(..., min_length=8, description="邀请码（明文）")
    timezone: str = Field(default="Asia/Shanghai", description="用户时区")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        import re

        if not re.match(_EMAIL_PATTERN, v):
            raise ValueError(f"邮箱格式非法: {v!r}")
        return v.lower()


class InviteCodeRenew(BaseModel):
    """续期请求 - 需邀请码。"""

    invite_code: str = Field(..., min_length=8, description="邀请码（明文）")


class MembershipResponse(BaseModel):
    """会员状态响应。"""

    model_config = ConfigDict(from_attributes=True)

    status: str = Field(..., description="active/expired")
    started_at: datetime = Field(..., description="会员开始时间")
    expires_at: datetime = Field(..., description="会员到期时间")
    remaining_days: int = Field(..., description="剩余天数")
    renewal_count: int = Field(..., description="累计续期次数")


class InviteCodeCreate(BaseModel):
    """邀请码生成请求 - 绑定 plan_code/grant_months。

    plan_code 默认 observe_20，grant_months 默认 1（保持向后兼容）。
    """

    count: int = Field(default=1, ge=1, le=100, description="生成数量（1-100）")
    note: str | None = Field(default=None, max_length=200, description="批次备注")
    plan_code: str = Field(
        default="observe_20",
        description="套餐代码 observe_20/research_50",
    )
    grant_months: int = Field(
        default=1,
        ge=1,
        le=36,
        description="兑换后增加的自然月数（1-36）",
    )


class InviteCodeResponse(BaseModel):
    """邀请码响应 - 含明文（仅生成时返回）+ 套餐快照。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="邀请码 ID")
    code: str = Field(..., description="邀请码明文（仅生成时返回）")
    grant_days: int = Field(..., description="兑换后增加天数（旧字段，保留兼容性）")
    plan_code: str | None = Field(None, description="套餐代码 observe_20/research_50")
    monitor_limit: int | None = Field(None, description="监控数量上限快照")
    grant_months: int | None = Field(None, description="兑换后增加的自然月数")
    note: str | None = Field(None, description="批次备注")
    created_at: datetime = Field(..., description="创建时间")


class InviteCodeListItem(BaseModel):
    """邀请码列表项 - 不含明文，含套餐快照。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="邀请码 ID")
    status: str = Field(..., description="unused/used/revoked")
    grant_days: int = Field(..., description="兑换后增加天数（旧字段，保留兼容性）")
    plan_code: str | None = Field(None, description="套餐代码 observe_20/research_50")
    monitor_limit: int | None = Field(None, description="监控数量上限快照")
    grant_months: int | None = Field(None, description="兑换后增加的自然月数")
    note: str | None = Field(None, description="批次备注")
    created_by: UUID = Field(..., description="创建者 user_id")
    created_at: datetime = Field(..., description="创建时间")
    used_by: UUID | None = Field(None, description="使用者 user_id")
    used_at: datetime | None = Field(None, description="使用时间")
    usage_type: str | None = Field(None, description="registration/renewal")


class InviteRedemptionResponse(BaseModel):
    """兑换记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="记录 ID")
    invite_code_id: UUID = Field(..., description="邀请码 ID")
    user_id: UUID = Field(..., description="兑换者 user_id")
    usage_type: str = Field(..., description="registration/renewal")
    old_expires_at: datetime | None = Field(None, description="兑换前到期时间")
    new_expires_at: datetime = Field(..., description="兑换后到期时间")
    redeemed_at: datetime = Field(..., description="兑换时间")


class MemberListItem(BaseModel):
    """会员账户列表项。"""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID = Field(..., description="用户 ID")
    email: str = Field(..., description="用户邮箱")
    account_status: str = Field(..., description="active/disabled/pending")
    membership_status: str | None = Field(None, description="active/expired")
    started_at: datetime | None = Field(None, description="会员开始时间")
    expires_at: datetime | None = Field(None, description="会员到期时间")
    remaining_days: int | None = Field(None, description="剩余天数")
    renewal_count: int = Field(..., description="累计续期次数")
    created_at: datetime = Field(..., description="用户创建时间")


class LoginResponse(BaseModel):
    """登录响应 - 含 token + 会员状态。"""

    access_token: str = Field(..., description="Access token")
    refresh_token: str = Field(..., description="Refresh token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access token 有效期（秒）")
    membership_expired: bool = Field(..., description="会员是否已到期")


class RegisterSuccessResponse(BaseModel):
    """注册成功响应 - 含 token + 会员信息。"""

    access_token: str = Field(..., description="Access token")
    refresh_token: str = Field(..., description="Refresh token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access token 有效期（秒）")
    membership_started_at: datetime = Field(..., description="会员开始时间")
    membership_expires_at: datetime = Field(..., description="会员到期时间")


class RenewSuccessResponse(BaseModel):
    """续期成功响应。"""

    membership_status: str = Field(..., description="会员状态：active")
    started_at: datetime = Field(..., description="会员开始时间")
    old_expires_at: datetime | None = Field(None, description="续期前到期时间")
    new_expires_at: datetime = Field(..., description="续期后到期时间")
    remaining_days: int = Field(..., description="剩余天数")


if __name__ == "__main__":
    print(f"UserRegister fields={list(UserRegister.model_fields.keys())}")
    print(f"InviteCodeRenew fields={list(InviteCodeRenew.model_fields.keys())}")
    print(f"MembershipResponse fields={list(MembershipResponse.model_fields.keys())}")
    print(f"LoginResponse fields={list(LoginResponse.model_fields.keys())}")

    reg = UserRegister(email="test@example.com", password="password123", invite_code="ABCD-EFGH-IJKL-MNOP")
    assert reg.email == "test@example.com"

    try:
        InviteCodeCreate(count=0)
        raise AssertionError("应抛出数量异常")
    except ValueError:
        print("count=0 blocked")

    print("OK")
