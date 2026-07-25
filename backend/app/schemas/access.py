"""权限上下文 Pydantic schemas - GET /me/access 端点响应。

提供：
- CapabilityStatusResponse: 单项能力状态（V2.1）
- WatchlistLimitInfoResponse: 自选额度信息（V2.1）
- AccessProfileResponse: 当前用户完整权限上下文（V1 11 字段 + V2.1 capabilities/limits）

设计说明：
- V1 字段（subscription_active/plan_code/...）保留用于过渡期兼容和迁移核对（PRD §15.3）
- V2.1 字段（capabilities/limits）为权限真源，前端逐步迁移到 V2.1
- 端点只读：不写 DB，由 get_access_context + get_capability_access_context 统一计算
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CapabilityStatusResponse(BaseModel):
    """单项能力状态（V2.1，PRD §9 capabilities[key]）。

    - active: 当前是否有效（revoked_at IS NULL AND starts_at <= now AND expires_at > now）
    - expires_at: 最晚到期时间（active=false 时为 None）
    """

    model_config = ConfigDict(from_attributes=True)

    active: bool = Field(..., description="当前是否有效")
    expires_at: datetime | None = Field(default=None, description="最晚到期时间")


class WatchlistLimitInfoResponse(BaseModel):
    """自选额度信息（V2.1，PRD §9 limits）。

    - watchlist_stock_limit: 有效额度（None=无权限或 admin unlimited）
    - watchlist_current_count: 当前 active 自选数量
    - watchlist_over_limit: 当前数量是否超过额度
    - is_admin_unlimited: admin 无限额度标识
    """

    model_config = ConfigDict(from_attributes=True)

    watchlist_stock_limit: int | None = Field(
        default=None, description="有效自选额度（None=无权限或 admin unlimited）"
    )
    watchlist_current_count: int = Field(
        default=0, description="当前 active 自选数量"
    )
    watchlist_over_limit: bool = Field(
        default=False, description="当前数量是否超过额度"
    )
    is_admin_unlimited: bool = Field(
        default=False, description="admin 无限额度标识"
    )


class AccessProfileResponse(BaseModel):
    """当前用户权限上下文响应 - V1 11 字段 + V2.1 capabilities/limits。

    V1 字段（保留用于过渡期兼容和迁移核对，PRD §15.3）：
    - user_id, account_status, roles, is_admin, is_member
    - subscription_active, plan_code, plan_display_name, expires_at, features, limits

    V2.1 字段（权限真源，PRD §9）：
    - capabilities: 三能力状态字典（watchlist_management/market_screening/review_management）
    - watchlist_limits: 自选额度信息
    """

    model_config = ConfigDict(from_attributes=True)

    # V1 字段（过渡期保留）
    user_id: str = Field(..., description="用户 ID（字符串化 UUID）")
    account_status: str = Field(..., description="用户状态 active/disabled/pending")
    roles: list[str] = Field(..., description="角色名列表")
    is_admin: bool = Field(..., description="是否为管理员")
    is_member: bool = Field(..., description="是否为普通会员")
    subscription_active: bool = Field(
        ..., description="订阅是否有效（admin 豁免=True；member 实时计算）"
    )
    plan_code: str | None = Field(default=None, description="套餐代码（V1 遗留）")
    plan_display_name: str | None = Field(
        default=None, description="套餐展示名（V1 遗留）"
    )
    expires_at: datetime | None = Field(
        default=None, description="订阅过期时间（V1 遗留）"
    )
    features: list[str] = Field(default_factory=list, description="功能特性列表（V1 遗留）")
    limits: dict = Field(default_factory=dict, description="额度限制 dict（V1 遗留）")

    # V2.1 字段（权限真源）
    capabilities: dict[str, CapabilityStatusResponse] = Field(
        default_factory=dict,
        description="V2.1 三能力状态（watchlist_management/market_screening/review_management）",
    )
    watchlist_limits: WatchlistLimitInfoResponse = Field(
        default_factory=WatchlistLimitInfoResponse,
        description="V2.1 自选额度信息",
    )


if __name__ == "__main__":
    # [Access] - 描述: 自测入口，验证字段集合与默认值（不连接数据库）
    v1_fields = {
        "user_id", "account_status", "roles", "is_admin", "is_member",
        "subscription_active", "plan_code", "plan_display_name",
        "expires_at", "features", "limits",
    }
    v2_fields = {"capabilities", "watchlist_limits"}
    expected_fields = v1_fields | v2_fields
    assert set(AccessProfileResponse.model_fields.keys()) == expected_fields
    assert len(AccessProfileResponse.model_fields) == 13

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
    assert resp.capabilities == {}
    assert resp.watchlist_limits.watchlist_stock_limit is None

    print(f"AccessProfileResponse fields={sorted(AccessProfileResponse.model_fields.keys())}")
    print("OK: access schema 字段验证通过（V1 + V2.1）")
