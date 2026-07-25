"""V2.1 邀请码能力配置 Pydantic schemas。

PRD §6 邀请码能力配置 + §8.1 invite_codes 状态推导。

提供：
- InviteCodeCapabilityItem: 能力配置项（capability_key + limit_value）
- InviteCodeV2CreateRequest: V2.1 创建请求（count + duration_months + capabilities + note）
- InviteCodeV2Response: V2.1 创建响应（含明文，仅生成时返回）
- InviteCodeV2ListItem: V2.1 列表项（不含明文，含能力配置）
- InviteCodeV2ListResponse: V2.1 列表响应（分页）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.constants.capability_keys import (
    ALL_CAPABILITY_KEYS,
    MAX_DURATION_MONTHS,
    MAX_WATCHLIST_STOCK_LIMIT,
    WATCHLIST_MANAGEMENT,
)


class InviteCodeCapabilityItem(BaseModel):
    """邀请码能力配置项（PRD §6.4）。

    - watchlist_management: limit_value 必须为正整数（1 <= x <= MAX_WATCHLIST_STOCK_LIMIT）
    - 其他能力: limit_value 必须为 None
    """

    model_config = ConfigDict(frozen=True)

    capability_key: str = Field(
        ..., description="能力键 watchlist_management/market_screening/review_management"
    )
    limit_value: int | None = Field(
        default=None,
        description="自选额度（仅 watchlist_management 必须正整数；其他能力必须 None）",
    )

    @model_validator(mode="after")
    def _validate_capability(self) -> InviteCodeCapabilityItem:
        if self.capability_key not in ALL_CAPABILITY_KEYS:
            raise ValueError(
                f"capability_key 必须在 {sorted(ALL_CAPABILITY_KEYS)} 中，"
                f"当前={self.capability_key!r}"
            )
        if self.capability_key == WATCHLIST_MANAGEMENT:
            if self.limit_value is None or self.limit_value <= 0:
                raise ValueError(
                    f"watchlist_management 必须提供正整数 limit_value，当前={self.limit_value}"
                )
            if self.limit_value > MAX_WATCHLIST_STOCK_LIMIT:
                raise ValueError(
                    f"limit_value 超过技术安全上限 {MAX_WATCHLIST_STOCK_LIMIT}，"
                    f"当前={self.limit_value}"
                )
        else:
            if self.limit_value is not None:
                raise ValueError(
                    f"非 watchlist_management 能力 limit_value 必须为 None，"
                    f"capability_key={self.capability_key!r}, limit_value={self.limit_value}"
                )
        return self


class InviteCodeV2CreateRequest(BaseModel):
    """V2.1 邀请码创建请求（PRD §6）。

    - count: 生成数量（1-100）
    - duration_months: 授权月数（1-MAX_DURATION_MONTHS）
    - capabilities: 能力配置列表（1-3 项，不重复）
    - note: 批次备注
    """

    count: int = Field(default=1, ge=1, le=100, description="生成数量（1-100）")
    duration_months: int = Field(
        ...,
        ge=1,
        le=MAX_DURATION_MONTHS,
        description=f"授权月数（1-{MAX_DURATION_MONTHS}，按日历月计算）",
    )
    capabilities: list[InviteCodeCapabilityItem] = Field(
        ...,
        min_length=1,
        max_length=len(ALL_CAPABILITY_KEYS),
        description="能力配置列表（1-3 项，不重复）",
    )
    note: str | None = Field(default=None, max_length=200, description="批次备注")

    @model_validator(mode="after")
    def _validate_unique_capabilities(self) -> InviteCodeV2CreateRequest:
        keys = [c.capability_key for c in self.capabilities]
        if len(set(keys)) != len(keys):
            raise ValueError(f"capabilities 存在重复能力键: {keys}")
        return self


class InviteCodeV2Response(BaseModel):
    """V2.1 邀请码创建响应（含明文，仅生成时返回）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="邀请码 ID")
    code: str = Field(..., description="邀请码明文（仅生成时返回，后续不可获取）")
    duration_months: int = Field(..., description="授权月数")
    capabilities: list[InviteCodeCapabilityItem] = Field(
        default_factory=list, description="能力配置列表"
    )
    note: str | None = Field(None, description="批次备注")
    created_at: datetime = Field(..., description="创建时间")


class InviteCodeV2ListItem(BaseModel):
    """V2.1 邀请码列表项（不含明文，含能力配置 + 状态推导）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="邀请码 ID")
    status: str = Field(
        ..., description="V2.1 状态推导：available/redeemed/revoked"
    )
    duration_months: int = Field(..., description="授权月数")
    capabilities: list[InviteCodeCapabilityItem] = Field(
        default_factory=list, description="能力配置列表"
    )
    note: str | None = Field(None, description="批次备注")
    created_by: UUID = Field(..., description="创建者 user_id")
    created_at: datetime = Field(..., description="创建时间")
    redeemed_by_user_id: UUID | None = Field(
        None, description="兑换用户 ID（V2.1 字段，None=未兑换）"
    )
    redeemed_at: datetime | None = Field(
        None, description="兑换时间（V2.1 字段，None=未兑换）"
    )
    revoked_at: datetime | None = Field(
        None, description="撤销时间（None=未撤销）"
    )


class InviteCodeV2ListResponse(BaseModel):
    """V2.1 邀请码列表响应（分页）。"""

    items: list[InviteCodeV2ListItem] = Field(
        default_factory=list, description="邀请码列表"
    )
    total: int = Field(..., description="总数")
    limit: int = Field(..., description="分页大小")
    offset: int = Field(..., description="分页偏移")


class RedeemV2Request(BaseModel):
    """V2.1 邀请码兑换请求（PRD §6.2 + §7）。

    已认证用户提交邀请码明文，后端原子兑换并按能力创建 grant。
    """

    invite_code: str = Field(..., min_length=8, description="邀请码明文")


class RedeemV2GrantItem(BaseModel):
    """兑换后创建的 grant 项。"""

    model_config = ConfigDict(from_attributes=True)

    capability_key: str = Field(..., description="能力键")
    limit_value: int | None = Field(None, description="自选额度（仅 watchlist_management）")
    starts_at: datetime = Field(..., description="生效时间")
    expires_at: datetime = Field(..., description="到期时间（exclusive）")


class RedeemV2Response(BaseModel):
    """V2.1 邀请码兑换响应。"""

    model_config = ConfigDict(from_attributes=True)

    invite_code_id: UUID = Field(..., description="邀请码 ID")
    redeemed_at: datetime = Field(..., description="兑换时间")
    grants: list[RedeemV2GrantItem] = Field(
        default_factory=list, description="本次兑换创建的 grant 列表"
    )


if __name__ == "__main__":
    # [InviteCapabilitySchema] - 描述: 自测入口，验证 schema 字段与校验
    print(f"InviteCodeCapabilityItem fields={list(InviteCodeCapabilityItem.model_fields.keys())}")
    print(f"InviteCodeV2CreateRequest fields={list(InviteCodeV2CreateRequest.model_fields.keys())}")
    print(f"InviteCodeV2Response fields={list(InviteCodeV2Response.model_fields.keys())}")
    print(f"InviteCodeV2ListItem fields={list(InviteCodeV2ListItem.model_fields.keys())}")

    # 合法能力配置
    cap1 = InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=30)
    assert cap1.limit_value == 30

    from app.constants.capability_keys import MARKET_SCREENING

    cap2 = InviteCodeCapabilityItem(capability_key=MARKET_SCREENING, limit_value=None)
    assert cap2.limit_value is None

    # 合法创建请求
    req = InviteCodeV2CreateRequest(
        count=5,
        duration_months=3,
        capabilities=[
            InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=30),
            InviteCodeCapabilityItem(capability_key=MARKET_SCREENING, limit_value=None),
        ],
        note="test batch",
    )
    assert req.count == 5
    assert req.duration_months == 3
    assert len(req.capabilities) == 2

    # 非法 capability_key
    try:
        InviteCodeCapabilityItem(capability_key="invalid_key")
        raise AssertionError("非法 capability_key 应拒绝")
    except ValueError:
        pass

    # watchlist_management 缺少 limit_value
    try:
        InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=None)
        raise AssertionError("watchlist_management 缺少 limit_value 应拒绝")
    except ValueError:
        pass

    # 重复能力键
    try:
        InviteCodeV2CreateRequest(
            count=1,
            duration_months=1,
            capabilities=[
                InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=30),
                InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=50),
            ],
        )
        raise AssertionError("重复能力键应拒绝")
    except ValueError:
        pass

    # duration_months 超过上限
    try:
        InviteCodeV2CreateRequest(
            count=1,
            duration_months=MAX_DURATION_MONTHS + 1,
            capabilities=[
                InviteCodeCapabilityItem(capability_key=WATCHLIST_MANAGEMENT, limit_value=30),
            ],
        )
        raise AssertionError("duration_months 超过上限应拒绝")
    except ValueError:
        pass

    print("OK: invite_capability schema 校验全部通过")
