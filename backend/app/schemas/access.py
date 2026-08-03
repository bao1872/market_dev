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
    """当前用户权限上下文响应 - 12 个字段（与 AccessContext 对齐，含 capabilities）。

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
    - capabilities: 三类独立权限状态（PRD60 PA-01，Phase 5B-2 新增）
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
    capabilities: dict = Field(default_factory=dict, description="三类独立权限状态（PA-01）")
    # [权限模型 V2] 统一权限画像字段（与 EffectiveAccessProfile 对齐）
    default_route: str = Field(..., description="依据权限计算的默认入口（必填，无权限时为 /forbidden）")
    active_capability_keys: list[str] = Field(default_factory=list, description="active 的 capability key 列表")
    capability_source: str = Field(default="none", description="权限来源 admin/user_capabilities/legacy_plan_fallback")
    diagnostics: list[str] = Field(default_factory=list, description="诊断（legacy fallback 标记等）")


# ============================================================
# [权限模型 V2 PV2-B06] 管理员 access-profile 分层响应模型
# ============================================================


class AdminAccountInfo(BaseModel):
    """管理员 access-profile 账户层。"""

    id: str = Field(..., description="用户 ID（字符串化）")
    email: str = Field(..., description="用户邮箱")
    account_status: str = Field(..., description="账户状态 active/disabled/pending")
    roles: list[str] = Field(default_factory=list, description="角色名列表")
    created_at: str | None = Field(None, description="创建时间")
    last_login_at: str | None = Field(None, description="最近登录时间")


class EffectiveAccessInfo(BaseModel):
    """管理员 access-profile 有效权限层。"""

    capabilities: dict[str, dict] = Field(default_factory=dict, description="三类 capability 状态")
    active_capability_keys: list[str] = Field(default_factory=list, description="active 的 capability key 列表")
    has_any_access: bool = Field(False, description="是否有任一有效权限")
    default_route: str = Field("/forbidden", description="默认入口")
    capability_source: str = Field("none", description="权限来源")
    nearest_capability_expires_at: str | None = Field(None, description="最近到期时间")
    legacy_fallback: bool = Field(False, description="是否 legacy plan fallback")
    diagnostics: list[str] = Field(default_factory=list, description="诊断")


class SubscriptionSummaryInfo(BaseModel):
    """管理员 access-profile 商业订阅层（仅展示，不参与判权）。

    status 为受限商业状态（none/pending/active/expired/revoked/cancelled）；
    reason 为诊断原因（异常周期 fail-closed 为 expired + 原因）。
    """

    status: str = Field("none", description="受限商业状态")
    reason: str | None = Field(None, description="诊断原因")
    plan_code: str | None = Field(None, description="套餐代码")
    plan_display_name: str | None = Field(None, description="套餐展示名")
    starts_at: str | None = Field(None, description="生效时间")
    expires_at: str | None = Field(None, description="到期时间")
    source: str | None = Field(None, description="来源")
    entitlement_snapshot: dict | None = Field(None, description="权益快照")


class ExplicitCapabilityRecord(BaseModel):
    """管理员 access-profile 显式 capability 记录层。"""

    capability: str = Field(..., description="权限类型")
    state: str = Field(..., description="active/expired/revoked")
    granted_at: str | None = Field(None, description="授予时间")
    expires_at: str | None = Field(None, description="到期时间")
    watchlist_limit: int | None = Field(None, description="自选数量上限")
    source: str = Field("", description="来源")
    granted_by: str | None = Field(None, description="授予人 user_id")


class AdminAccessProfileResponse(BaseModel):
    """管理员 access-profile 顶层响应（分层、受约束、稳定）。"""

    account: AdminAccountInfo = Field(..., description="账户层")
    effective_access: EffectiveAccessInfo = Field(..., description="有效权限层")
    subscription_summary: SubscriptionSummaryInfo = Field(..., description="商业订阅层")
    explicit_capability_records: list[ExplicitCapabilityRecord] = Field(
        default_factory=list, description="显式 capability 记录层"
    )


if __name__ == "__main__":
    # [Access] - 描述: 自测入口，验证字段集合与默认值（不连接数据库）
    # [权限模型 V2 PV2-B05/B06] 模型已扩展（default_route/active_capability_keys/
    # capability_source/diagnostics），字段数断言必须包含 V2 字段。
    expected_fields = {
        "user_id", "account_status", "roles", "is_admin", "is_member",
        "subscription_active", "plan_code", "plan_display_name",
        "expires_at", "features", "limits", "capabilities",
        "default_route", "active_capability_keys", "capability_source", "diagnostics",
    }
    assert set(AccessProfileResponse.model_fields.keys()) == expected_fields
    assert len(AccessProfileResponse.model_fields) == 16

    # 构造 admin 响应验证默认值（default_route 为必填，需显式提供）
    resp = AccessProfileResponse(
        user_id="test-uuid",
        account_status="active",
        roles=["admin"],
        is_admin=True,
        is_member=False,
        subscription_active=True,
        default_route="/admin/overview",
    )
    assert resp.plan_code is None
    assert resp.plan_display_name is None
    assert resp.expires_at is None
    assert resp.features == []
    assert resp.limits == {}
    assert resp.default_route == "/admin/overview"

    print(f"AccessProfileResponse fields={sorted(AccessProfileResponse.model_fields.keys())}")
    print("OK: access schema 字段验证通过")
