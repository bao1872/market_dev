"""权限上下文 Pydantic schemas - GET /me/access 端点响应。

提供：
- AccessProfileResponse: 当前用户完整权限上下文（11 个字段，与 AccessContext 对齐）

设计说明：
- 字段语义与 app.services.access_control_service.AccessContext 完全一致，
  仅作为 API 响应模型（解耦内部模型与外部契约）
- 不复用 LoginResponse：LoginResponse 含 token + next_route + subscription_required，
  语义不同；本响应只暴露 AccessContext 的 11 个字段
- 端点只读：不写 DB，由 get_access_context 统一计算（唯一真源）
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AccessProfileResponse(BaseModel):
    """当前用户权限上下文响应 - 11 个字段（与 AccessContext 对齐）。

    字段语义：
    - user_id: 用户 ID（字符串化 UUID，与 JWT sub 声明一致）
    - account_status: 用户状态（active/disabled/pending）
    - roles: 角色名列表
    - is_admin: 是否为管理员（"admin" in roles）
    - is_member: 是否为普通会员（"member" in roles）
    - subscription_active: 订阅是否有效（admin 豁免=True；member 实时计算）
    - plan_code: 套餐代码（admin/无订阅=None；过期订阅仍保留）
    - plan_display_name: 套餐展示名（admin/无订阅=None；过期订阅仍保留）
    - expires_at: 订阅过期时间（admin/无订阅=None）
    - features: 功能特性列表（admin/无订阅=[]）
    - limits: 额度限制 dict（monitor_limit/notification_channel_limit/message_retention_days）
    """

    model_config = ConfigDict(from_attributes=True)

    user_id: str = Field(..., description="用户 ID（字符串化 UUID）")
    account_status: str = Field(..., description="用户状态 active/disabled/pending")
    roles: list[str] = Field(..., description="角色名列表")
    is_admin: bool = Field(..., description="是否为管理员")
    is_member: bool = Field(..., description="是否为普通会员")
    subscription_active: bool = Field(
        ..., description="订阅是否有效（admin 豁免=True；member 实时计算）"
    )
    plan_code: str | None = Field(default=None, description="套餐代码")
    plan_display_name: str | None = Field(default=None, description="套餐展示名")
    expires_at: datetime | None = Field(default=None, description="订阅过期时间")
    features: list[str] = Field(default_factory=list, description="功能特性列表")
    limits: dict = Field(default_factory=dict, description="额度限制 dict")


if __name__ == "__main__":
    # [Access] - 描述: 自测入口，验证字段集合与默认值（不连接数据库）
    expected_fields = {
        "user_id", "account_status", "roles", "is_admin", "is_member",
        "subscription_active", "plan_code", "plan_display_name",
        "expires_at", "features", "limits",
    }
    assert set(AccessProfileResponse.model_fields.keys()) == expected_fields
    assert len(AccessProfileResponse.model_fields) == 11

    # 构造 admin 响应验证默认值
    resp = AccessProfileResponse(
        user_id="test-uuid",
        account_status="active",
        roles=["admin"],
        is_admin=True,
        is_member=False,
        subscription_active=True,
    )
    assert resp.plan_code is None
    assert resp.plan_display_name is None
    assert resp.expires_at is None
    assert resp.features == []
    assert resp.limits == {}

    print(f"AccessProfileResponse fields={sorted(AccessProfileResponse.model_fields.keys())}")
    print("OK: access schema 字段验证通过")
