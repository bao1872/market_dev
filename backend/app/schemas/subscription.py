"""Subscription Pydantic schemas - 订阅状态与账户列表响应模型。

提供：
- MembershipResponse: 当前用户订阅状态响应（status/expires_at/剩余天数）；类名与 /me/membership
  路径保留 V1.6 遗留命名，语义等价于 Subscription 状态响应
- RenewSuccessResponse: 邀请码续期成功响应（membership_status 为 V1.6 遗留字段名）
- MemberListItem: 管理员订阅账户列表项（membership_status 为 V1.6 遗留字段名）
- GrantCapabilityRequest: 管理员直接授予/修改用户 capability 请求（PRD60 PA-20，Gate2）
- CapabilityInfoResponse: 单个 capability 状态响应（active/expires_at/watchlist_limit）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# [Gate2 PRD60 PA-01] 三类独立 capability 固定值（与 user_capability.ALL_CAPABILITIES 对齐）
_VALID_CAPABILITIES = {"self_selection", "market_data", "research_replay"}


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


class CapabilityInfoResponse(BaseModel):
    """[Gate2 PRD60] 单个 capability 状态响应（与 AccessContext.capabilities 对齐）。

    字段语义：
    - active: 是否有效（expires_at > now）
    - expires_at: 过期时间（per-capability 独立自然月，PA-03）
    - watchlist_limit: 自选数量上限（仅 self_selection 使用；其他 capability 为 null）
    """

    active: bool = Field(..., description="是否有效")
    expires_at: datetime | None = Field(None, description="过期时间")
    watchlist_limit: int | None = Field(None, description="自选数量上限（仅 self_selection）")


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
    # [Gate2 PRD60 PA-01] 三类独立 capability 状态（per-capability 独立 expires_at）
    # 旧用户无 user_capabilities 行时为空 dict（fallback 到 plan_code 推断）
    capabilities: dict[str, CapabilityInfoResponse] = Field(
        default_factory=dict,
        description="三类独立 capability 状态",
    )


class GrantSubscriptionRequest(BaseModel):
    """管理员授予用户订阅请求。"""

    plan_code: str = Field(..., description="套餐代码 observe_20/research_50")
    grant_months: int = Field(..., ge=1, le=36, description="授予自然月数（1-36）")


class RenewSubscriptionRequest(BaseModel):
    """管理员续期用户订阅请求。"""

    grant_months: int = Field(..., ge=1, le=36, description="续期自然月数（1-36）")


class ChangePlanRequest(BaseModel):
    """管理员修改用户套餐请求。"""

    plan_code: str = Field(..., description="目标套餐代码 observe_20/research_50")
    grant_months: int = Field(..., ge=1, le=36, description="授予/续期自然月数（1-36）")


class GrantCapabilityRequest(BaseModel):
    """[Gate2 PRD60 PA-20] 管理员直接授予/修改用户 capability 请求。

    管理员可通过用户抽屉直接授予或修改 capability：
    - capability: 权限类型（self_selection/market_data/research_replay）
    - months: 自然月有效期（PA-03）
    - watchlist_limit: 自选数量上限（仅 self_selection 必填，PA-02）

    已有该 capability 时取较晚的 expires_at（不降权），并更新 watchlist_limit（如提供）。
    """

    capability: str = Field(..., description="权限类型 self_selection/market_data/research_replay")
    months: int = Field(default=1, ge=1, le=36, description="自然月有效期（1-36）")
    watchlist_limit: int | None = Field(
        None, ge=1, le=500, description="自选数量上限（仅 self_selection 必填，1-500）"
    )
    reason: str | None = Field(
        None, max_length=500, description="授予原因（审计用，可选；去空白，空转 None）"
    )

    @model_validator(mode="after")
    def _validate_capability(self) -> GrantCapabilityRequest:
        if self.capability not in _VALID_CAPABILITIES:
            raise ValueError(f"无效 capability: {self.capability}，允许: {_VALID_CAPABILITIES}")
        if self.capability == "self_selection" and self.watchlist_limit is None:
            raise ValueError("self_selection capability 必须指定 watchlist_limit（PA-02）")
        if self.capability != "self_selection" and self.watchlist_limit is not None:
            raise ValueError(f"{self.capability} 不支持 watchlist_limit（仅 self_selection）")
        # [权限模型 V2 PV2-B07] reason 去空白、空字符串转 None
        if self.reason is not None:
            trimmed = self.reason.strip()
            self.reason = trimmed if trimmed else None
        return self


class ChangeSelfSelectionQuotaRequest(BaseModel):
    """[权限模型 V2 PV2-B05] 独立调整 self_selection 额度请求。

    只允许 self_selection；仅修改 watchlist_limit，不改变 expires_at（mutation_type=quota_change）。
    """

    watchlist_limit: int = Field(
        ..., ge=1, le=500, description="新自选数量上限（1-500）"
    )
    reason: str | None = Field(
        None, max_length=500, description="调整原因（审计用，可选；去空白，空转 None）"
    )

    @model_validator(mode="after")
    def _validate_reason(self) -> ChangeSelfSelectionQuotaRequest:
        # [权限模型 V2 PV2-B07] reason 去空白、空字符串转 None
        if self.reason is not None:
            trimmed = self.reason.strip()
            self.reason = trimmed if trimmed else None
        return self


class UserCapabilitiesResponse(BaseModel):
    """[Gate2 PRD60] 用户 capability 列表响应（管理员查看/授予/撤销后返回最新状态）。"""

    user_id: UUID = Field(..., description="用户 ID")
    capabilities: dict[str, CapabilityInfoResponse] = Field(
        default_factory=dict,
        description="三类独立 capability 状态",
    )


class RevokeCapabilityRequest(BaseModel):
    """[Gate2 PRD60] 管理员撤销用户 capability 请求。"""

    capability: str = Field(..., description="权限类型 self_selection/market_data/research_replay")
    reason: str | None = Field(
        None, max_length=500, description="撤销原因（审计用，可选；去空白，空转 None）"
    )

    @model_validator(mode="after")
    def _validate_capability(self) -> RevokeCapabilityRequest:
        if self.capability not in _VALID_CAPABILITIES:
            raise ValueError(f"无效 capability: {self.capability}，允许: {_VALID_CAPABILITIES}")
        # [权限模型 V2 PV2-B07] reason 去空白、空字符串转 None
        if self.reason is not None:
            trimmed = self.reason.strip()
            self.reason = trimmed if trimmed else None
        return self


# Schema 注册
__all__ = [
    "MembershipResponse",
    "RenewSuccessResponse",
    "CapabilityInfoResponse",
    "MemberListItem",
    "GrantSubscriptionRequest",
    "RenewSubscriptionRequest",
    "ChangePlanRequest",
    "GrantCapabilityRequest",
    "ChangeSelfSelectionQuotaRequest",
    "UserCapabilitiesResponse",
    "RevokeCapabilityRequest",
]


class SubscriptionResponse(BaseModel):
    """订阅记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="订阅 ID")
    user_id: UUID = Field(..., description="用户 ID")
    plan_code: str = Field(..., description="套餐代码")
    status: str = Field(..., description="active/revoked/cancelled")
    starts_at: datetime = Field(..., description="订阅开始时间")
    expires_at: datetime = Field(..., description="订阅到期时间")
    entitlement_snapshot: dict = Field(..., description="权益快照")
    source: str = Field(..., description="来源 invite/admin_grant/migration")
    created_by: UUID | None = Field(None, description="创建人 user_id")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class SubscriptionRenewResponse(BaseModel):
    """管理员续期订阅响应（含 old/new expires_at）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="订阅 ID")
    user_id: UUID = Field(..., description="用户 ID")
    plan_code: str = Field(..., description="套餐代码")
    status: str = Field(..., description="active/revoked/cancelled")
    starts_at: datetime = Field(..., description="订阅开始时间")
    expires_at: datetime = Field(..., description="续期后到期时间")
    old_expires_at: datetime = Field(..., description="续期前到期时间")
    new_expires_at: datetime = Field(..., description="续期后到期时间")
    entitlement_snapshot: dict = Field(..., description="权益快照")
    source: str = Field(..., description="来源 invite/admin_grant/migration")
    created_by: UUID | None = Field(None, description="创建人 user_id")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


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
