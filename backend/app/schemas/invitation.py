"""Invitation Pydantic schemas - 邀请码生成、兑换与列表响应模型。

提供：
- InviteCodeRenew: 续期请求（invite_code）
- InviteCodeCreate: 邀请码生成请求（数量/备注/套餐/capability 组合）
- InviteCodeResponse: 邀请码响应（含明文，仅生成时返回）
- InviteCodeListItem: 邀请码列表项（不含明文）
- InviteRedemptionResponse: 兑换记录响应
- CapabilityGrant: 单个 capability 授权配置（PRD60 PA-20）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# PRD60 PA-01: 三类独立 capability 固定值（与 user_capability.ALL_CAPABILITIES 对齐）
_VALID_CAPABILITIES = {"self_selection", "market_data", "research_replay"}


class CapabilityGrant(BaseModel):
    """单个 capability 授权配置（PRD60 PA-20）。

    邀请码生成时，管理员可指定 capability 组合：
    - capability: 权限类型（self_selection/market_data/research_replay）
    - months: 30 天周期有效期（PA-03，1 = 30 天）
    - watchlist_limit: 自选数量上限（仅 self_selection 必填，PA-02）
    """

    capability: str = Field(..., description="权限类型 self_selection/market_data/research_replay")
    months: int = Field(default=1, ge=1, le=36, description="30 天周期有效期（1-36，1 = 30 天）")
    watchlist_limit: int | None = Field(None, ge=1, le=500, description="自选数量上限（仅 self_selection 必填）")

    @model_validator(mode="after")
    def _validate_capability(self) -> CapabilityGrant:
        if self.capability not in _VALID_CAPABILITIES:
            raise ValueError(f"无效 capability: {self.capability}，允许: {_VALID_CAPABILITIES}")
        if self.capability == "self_selection" and self.watchlist_limit is None:
            raise ValueError("self_selection capability 必须指定 watchlist_limit（PA-02）")
        if self.capability != "self_selection" and self.watchlist_limit is not None:
            raise ValueError(f"{self.capability} 不支持 watchlist_limit（仅 self_selection）")
        return self


class InviteCodeRenew(BaseModel):
    """续期请求 - 需邀请码。"""

    invite_code: str = Field(..., min_length=8, description="邀请码（明文）")


class InviteCodeCreate(BaseModel):
    """邀请码生成请求 - 支持 capability 组合（PRD60 PA-20）或旧 plan_code 兼容。

    两种模式：
    1. 新模式（PA-20）：提供 capabilities 列表，每个 capability 独立授权与有效期
    2. 旧模式（兼容）：提供 plan_code + grant_months，从 plans 表读取 monitor_limit

    capabilities 优先于 plan_code；两者都为默认值时使用旧模式（observe_20 + 1 月）。
    """

    count: int = Field(default=1, ge=1, le=100, description="生成数量（1-100）")
    note: str | None = Field(default=None, max_length=200, description="批次备注")
    plan_code: str = Field(
        default="observe_20",
        description="套餐代码 observe_20/research_50（旧模式，capabilities 为空时使用）",
    )
    grant_months: int = Field(
        default=1,
        ge=1,
        le=36,
        description="兑换后增加的 30 天周期数（旧模式，1-36）",
    )
    # [Phase 5B-2 PRD60 PA-20] capability 组合（新模式）
    capabilities: list[CapabilityGrant] | None = Field(
        default=None,
        description="capability 组合（PA-20）；提供时优先于 plan_code",
    )


class InviteCodeResponse(BaseModel):
    """邀请码响应 - 含明文（仅生成时返回）+ 套餐快照。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="邀请码 ID")
    code: str = Field(..., description="邀请码明文（仅生成时返回）")
    grant_days: int = Field(..., description="兑换后增加天数（旧字段，保留兼容性）")
    plan_code: str | None = Field(None, description="套餐代码 observe_20/research_50")
    monitor_limit: int | None = Field(None, description="监控数量上限快照")
    grant_months: int | None = Field(None, description="兑换后增加的 30 天周期数")
    capabilities: list[dict[str, Any]] | None = Field(None, description="capability 组合（PA-20）")
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
    grant_months: int | None = Field(None, description="兑换后增加的 30 天周期数")
    capabilities: list[dict[str, Any]] | None = Field(None, description="capability 组合（PA-20）")
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


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义与校验（不连接数据库）
    print(f"InviteCodeRenew fields={list(InviteCodeRenew.model_fields.keys())}")
    print(f"InviteCodeCreate fields={list(InviteCodeCreate.model_fields.keys())}")
    print(f"InviteCodeResponse fields={list(InviteCodeResponse.model_fields.keys())}")
    print(f"InviteCodeListItem fields={list(InviteCodeListItem.model_fields.keys())}")
    print(f"InviteRedemptionResponse fields={list(InviteRedemptionResponse.model_fields.keys())}")

    renew = InviteCodeRenew(invite_code="ABCD-EFGH-IJKL-MNOP")
    assert renew.invite_code == "ABCD-EFGH-IJKL-MNOP"

    try:
        InviteCodeCreate(count=0)
        raise AssertionError("应抛出数量异常")
    except ValueError:
        print("count=0 blocked")

    print("OK")
