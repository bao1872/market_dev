"""User Pydantic schemas - 用户认证与响应模型。

提供：
- UserCreate: 创建用户请求（注册/管理员创建）
- UserRegister: 邀请码注册请求（email + password + invite_code）
- UserLogin: 登录请求
- UserResponse: 用户信息响应（不含密码哈希）
- TokenResponse: 登录/刷新成功返回的 access + refresh token
- LoginResponse: 登录成功返回（token + AccessProfile 权限上下文）
- RegisterSuccessResponse: 注册成功返回（token + 订阅开始/到期时间）
- TokenPayload: JWT 解码后的 payload 结构

说明：
- email 字段使用 str + 正则校验，避免引入 email-validator 依赖
- 密码字段最小长度 8 字符（bcrypt 限制 72 字节，服务端截断）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 简易邮箱正则（避免引入 email-validator 依赖）
_EMAIL_PATTERN = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


class UserCreate(BaseModel):
    """创建用户请求。"""

    email: str = Field(..., description="登录邮箱（唯一）")
    password: str = Field(..., min_length=8, max_length=128, description="密码（8-128 字符）")
    timezone: str = Field(default="Asia/Shanghai", description="用户时区")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """校验邮箱格式（简易正则）。"""
        import re

        if not re.match(_EMAIL_PATTERN, v):
            raise ValueError(f"邮箱格式非法: {v!r}")
        return v.lower()


class UserRegister(BaseModel):
    """注册请求 - 需邀请码。"""

    email: str = Field(..., description="登录邮箱（唯一）")
    password: str = Field(..., min_length=8, max_length=128, description="密码（8-128 字符）")
    invite_code: str = Field(..., min_length=8, description="邀请码（明文）")
    timezone: str = Field(default="Asia/Shanghai", description="用户时区")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """校验邮箱格式并转为小写（与存储一致）。"""
        import re

        if not re.match(_EMAIL_PATTERN, v):
            raise ValueError(f"邮箱格式非法: {v!r}")
        return v.lower()


class UserLogin(BaseModel):
    """登录请求。"""

    email: str = Field(..., description="登录邮箱")
    password: str = Field(..., description="明文密码")

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """校验邮箱格式并转为小写（与存储一致）。"""
        import re

        if not re.match(_EMAIL_PATTERN, v):
            raise ValueError(f"邮箱格式非法: {v!r}")
        return v.lower()


class UserResponse(BaseModel):
    """用户信息响应 - 不含密码哈希。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="用户 ID")
    email: str = Field(..., description="登录邮箱")
    status: str = Field(..., description="状态：active/disabled/pending")
    timezone: str = Field(..., description="用户时区")
    roles: list[str] = Field(default_factory=list, description="角色名列表")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class TokenResponse(BaseModel):
    """登录/刷新成功返回的 token 响应。"""

    access_token: str = Field(..., description="Access token（短期，用于 API 认证）")
    refresh_token: str = Field(..., description="Refresh token（长期，用于刷新 access token）")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access token 有效期（秒）")


class LoginResponse(BaseModel):
    """登录响应 - 含 token + AccessProfile 权限上下文。

    字段语义：
    - token 字段（4 个）：access_token / refresh_token / token_type / expires_in
    - AccessProfile 字段（10 个）：复用 AccessContext 语义，供前端路由分发与 UI 降级
      * is_admin: 是否为管理员（"admin" in roles）
      * roles: 角色名列表
      * subscription_required: 是否需要订阅（admin=False，member=True）
      * subscription_active: 订阅是否有效（admin 豁免=True；member 实时计算）
      * plan_code: 套餐代码（admin/无订阅=None）
      * plan_display_name: 套餐展示名（过期订阅仍保留，便于前端降级提示）
      * expires_at: 订阅过期时间（admin/无订阅=None）
      * features: 功能特性列表
      * limits: 额度限制 dict
      * next_route: 前端下一步路由（admin→/admin/overview；member active→/overview；
        member expired→/subscription-expired；/membership-expired 仅保留兼容重定向）

    设计要点：
    - 替代旧字段 membership_expired（语义等价：subscription_active = not membership_expired）
    - 登录路径只读，不写 DB；权限上下文由 get_access_context 统一计算
    """

    # token 字段
    access_token: str = Field(..., description="Access token")
    refresh_token: str = Field(..., description="Refresh token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access token 有效期（秒）")

    # AccessProfile 字段（10 个）
    is_admin: bool = Field(..., description="是否为管理员")
    roles: list[str] = Field(..., description="角色名列表")
    subscription_required: bool = Field(
        ..., description="是否需要订阅（admin=False，member=True）"
    )
    subscription_active: bool = Field(
        ..., description="订阅是否有效（admin 豁免=True；member 实时计算）"
    )
    plan_code: str | None = Field(default=None, description="套餐代码")
    plan_display_name: str | None = Field(default=None, description="套餐展示名")
    expires_at: datetime | None = Field(default=None, description="订阅过期时间")
    features: list[str] = Field(default_factory=list, description="功能特性列表")
    limits: dict = Field(default_factory=dict, description="额度限制 dict")
    next_route: str = Field(..., description="前端下一步路由")


class RegisterSuccessResponse(BaseModel):
    """注册成功响应 - 含 token + 订阅开始/到期时间（membership_* 字段为 V1.6 API 遗留命名）。"""

    access_token: str = Field(..., description="Access token")
    refresh_token: str = Field(..., description="Refresh token")
    token_type: str = Field(default="bearer", description="Token 类型")
    expires_in: int = Field(..., description="Access token 有效期（秒）")
    membership_started_at: datetime = Field(..., description="订阅开始时间（V1.6 遗留字段名，语义等价于 subscription_started_at）")
    membership_expires_at: datetime = Field(..., description="订阅到期时间（V1.6 遗留字段名，语义等价于 subscription_expires_at）")


class RefreshRequest(BaseModel):
    """刷新 token 请求体 - refresh_token 通过 JSON body 提交（非 query string）。

    改为 body 的原因：refresh_token 较长且为敏感凭证，放在 query string 会被
    access log / 浏览器历史 / referer 头记录，存在泄露风险。
    """

    refresh_token: str = Field(..., description="待刷新的 refresh token")


class TokenPayload(BaseModel):
    """JWT 解码后的 payload 结构。"""

    sub: str = Field(..., description="用户标识（user_id 字符串）")
    exp: int = Field(..., description="过期时间戳（Unix 秒）")
    type: str = Field(..., description="token 类型：access/refresh")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义与邮箱校验
    print(f"UserCreate fields={list(UserCreate.model_fields.keys())}")
    print(f"UserRegister fields={list(UserRegister.model_fields.keys())}")
    print(f"UserLogin fields={list(UserLogin.model_fields.keys())}")
    print(f"UserResponse fields={list(UserResponse.model_fields.keys())}")
    print(f"TokenResponse fields={list(TokenResponse.model_fields.keys())}")
    print(f"LoginResponse fields={list(LoginResponse.model_fields.keys())}")
    print(f"RegisterSuccessResponse fields={list(RegisterSuccessResponse.model_fields.keys())}")
    print(f"TokenPayload fields={list(TokenPayload.model_fields.keys())}")

    # 验证邮箱校验
    u = UserCreate(email="test@example.com", password="password123")
    assert u.email == "test@example.com"
    print(f"valid email: {u.email}")

    reg = UserRegister(email="test@example.com", password="password123", invite_code="ABCD-EFGH-IJKL-MNOP")
    assert reg.email == "test@example.com"
    print(f"valid register email: {reg.email}")

    try:
        UserCreate(email="bad-email", password="password123")
        raise AssertionError("应抛出邮箱格式异常")
    except ValueError as e:
        print(f"invalid email blocked: {e}")

    print("OK")
