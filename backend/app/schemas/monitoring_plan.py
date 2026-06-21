"""MonitoringPlan Pydantic schemas - 监控组合方案请求与响应模型（C5/C6/C8）。

提供：
- MonitoringPlanMemberRequest: 成员创建/更新请求
- MonitoringPlanCreateRequest: 创建方案请求（user_id 由上下文注入）
- MonitoringPlanUpdateRequest: 更新方案请求（创建新 revision）
- MonitoringPlanValidateRequest: 验证方案请求
- MonitoringPlanMemberResponse: 成员响应
- MonitoringPlanRevisionResponse: 版本响应
- MonitoringPlanResponse: 方案响应
- MonitoringPlanListResponse: 方案列表响应
- MonitoringPlanValidateResponse: 验证结果响应
- MonitoringPlanStateResponse: 状态响应
- MonitoringPlanStateListResponse: 状态列表响应
- CompositeEventEvidenceResponse: 证据响应
- CompositeMonitorEventResponse: 组合事件响应
- CompositeMonitorEventListResponse: 组合事件列表响应
- CompositeMonitorEventDetailResponse: 组合事件详情响应（含 evidence）

安全约束：
- user_id 不出现在请求体中（由认证上下文注入）
- mode/role/version_policy 使用 Literal 约束合法值
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# 合法值常量（对齐迁移 CHECK 约束）
Mode = Literal["INDEPENDENT", "ANY", "ALL"]
Role = Literal["TRIGGER", "CONFIRM", "VETO", "OBSERVE"]
VersionPolicy = Literal["PINNED", "STABLE_TRACK"]
ProcessEventPolicy = Literal["NONE", "IN_APP_ONLY", "ALL_CHANNELS"]
PlanStatus = Literal["draft", "active", "paused", "archived"]
StateStatus = Literal[
    "WAITING_TRIGGER",
    "WAITING_CONFIRM",
    "CONFIRMED",
    "EXPIRED",
    "VETOED",
    "COOLDOWN",
]


class MonitoringPlanMemberRequest(BaseModel):
    """方案成员请求。

    version_policy=PINNED 时 strategy_version_id 必填。
    position 由调用方指定（用于 ordered 模式）。
    """

    strategy_definition_id: UUID = Field(..., description="策略定义 ID")
    strategy_version_id: UUID | None = Field(
        None, description="策略版本 ID（version_policy=PINNED 时必填）"
    )
    version_policy: VersionPolicy = Field(
        "STABLE_TRACK", description="PINNED/STABLE_TRACK"
    )
    event_type: str = Field(..., min_length=1, description="事件类型")
    role: Role = Field(..., description="TRIGGER/CONFIRM/VETO/OBSERVE")
    position: int = Field(..., ge=0, description="成员顺序（用于 ordered 模式）")
    required: bool = Field(True, description="是否必需（ALL 模式下使用）")
    enabled: bool = Field(True, description="是否启用")
    params: dict[str, Any] = Field(
        default_factory=dict, description="成员参数 JSONB"
    )
    conditions: list[Any] = Field(
        default_factory=list, description="成员条件 JSONB（线性 AND 条件）"
    )

    @field_validator("strategy_version_id")
    @classmethod
    def _validate_pinned_version(cls, v: UUID | None, info) -> UUID | None:
        """PINNED 模式下 strategy_version_id 必填。

        info.data 在 field_validator 中可能尚未填充 version_policy，
        因此该检查在 service 层补充执行；此处仅做基本类型校验。
        """
        return v


class MonitoringPlanCreateRequest(BaseModel):
    """创建监控方案请求。

    user_id 不接受客户端传入（由认证上下文注入）。
    创建时同时创建首个 revision（revision=1）。
    """

    name: str = Field(..., min_length=1, max_length=80, description="方案名称")
    description: str | None = Field(None, description="方案描述")
    mode: Mode = Field(..., description="INDEPENDENT/ANY/ALL")
    confirmation_window_seconds: int = Field(
        0, ge=0, le=86400, description="确认窗口（秒），ALL 模式下使用"
    )
    ordered: bool = Field(False, description="是否按 position 顺序确认")
    cooldown_seconds: int = Field(
        600, ge=0, le=86400, description="冷却时间（秒）"
    )
    process_event_policy: ProcessEventPolicy = Field(
        "IN_APP_ONLY", description="NONE/IN_APP_ONLY/ALL_CHANNELS"
    )
    notification_config: dict[str, Any] = Field(
        default_factory=dict, description="通知配置 JSONB"
    )
    members: list[MonitoringPlanMemberRequest] = Field(
        ..., min_length=1, description="方案成员列表（至少 1 个）"
    )

    @field_validator("members")
    @classmethod
    def _validate_members_unique_position(
        cls, members: list[MonitoringPlanMemberRequest]
    ) -> list[MonitoringPlanMemberRequest]:
        """校验成员 position 不重复。"""
        positions = [m.position for m in members]
        if len(positions) != len(set(positions)):
            raise ValueError("成员 position 不能重复")
        return members

    @field_validator("members")
    @classmethod
    def _validate_pinned_has_version(
        cls, members: list[MonitoringPlanMemberRequest]
    ) -> list[MonitoringPlanMemberRequest]:
        """校验 PINNED 成员必须提供 strategy_version_id。"""
        for m in members:
            if m.version_policy == "PINNED" and m.strategy_version_id is None:
                raise ValueError(
                    f"version_policy=PINNED 的成员（position={m.position}）必须提供 strategy_version_id"
                )
        return members


class MonitoringPlanUpdateRequest(BaseModel):
    """更新监控方案请求（创建新 revision）。

    更新时创建新 revision，原 revision 保留。
    status 可同时更新（如 draft -> active）。
    """

    name: str | None = Field(None, min_length=1, max_length=80, description="方案名称")
    description: str | None = Field(None, description="方案描述")
    mode: Mode | None = Field(None, description="INDEPENDENT/ANY/ALL")
    confirmation_window_seconds: int | None = Field(
        None, ge=0, le=86400, description="确认窗口（秒）"
    )
    ordered: bool | None = Field(None, description="是否按 position 顺序确认")
    cooldown_seconds: int | None = Field(
        None, ge=0, le=86400, description="冷却时间（秒）"
    )
    process_event_policy: ProcessEventPolicy | None = Field(
        None, description="NONE/IN_APP_ONLY/ALL_CHANNELS"
    )
    notification_config: dict[str, Any] | None = Field(
        None, description="通知配置 JSONB"
    )
    members: list[MonitoringPlanMemberRequest] | None = Field(
        None, min_length=1, description="方案成员列表（如提供则替换全部成员）"
    )

    @field_validator("members")
    @classmethod
    def _validate_members(
        cls, members: list[MonitoringPlanMemberRequest] | None
    ) -> list[MonitoringPlanMemberRequest] | None:
        """校验成员 position 不重复 + PINNED 必须有 version。"""
        if members is None:
            return members
        positions = [m.position for m in members]
        if len(positions) != len(set(positions)):
            raise ValueError("成员 position 不能重复")
        for m in members:
            if m.version_policy == "PINNED" and m.strategy_version_id is None:
                raise ValueError(
                    f"version_policy=PINNED 的成员（position={m.position}）必须提供 strategy_version_id"
                )
        return members


class MonitoringPlanMemberResponse(BaseModel):
    """方案成员响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="成员 ID")
    revision_id: UUID = Field(..., description="所属版本 ID")
    strategy_definition_id: UUID = Field(..., description="策略定义 ID")
    strategy_version_id: UUID | None = Field(None, description="策略版本 ID")
    version_policy: VersionPolicy = Field(..., description="PINNED/STABLE_TRACK")
    event_type: str = Field(..., description="事件类型")
    role: Role = Field(..., description="TRIGGER/CONFIRM/VETO/OBSERVE")
    position: int = Field(..., description="成员顺序")
    required: bool = Field(..., description="是否必需")
    enabled: bool = Field(..., description="是否启用")
    params: dict[str, Any] = Field(default_factory=dict, description="成员参数 JSONB")
    conditions: list[Any] = Field(default_factory=list, description="成员条件 JSONB")


class MonitoringPlanRevisionResponse(BaseModel):
    """方案版本响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="版本 ID")
    monitoring_plan_id: UUID = Field(..., description="所属方案 ID")
    revision: int = Field(..., description="版本号")
    mode: Mode = Field(..., description="INDEPENDENT/ANY/ALL")
    confirmation_window_seconds: int = Field(..., description="确认窗口（秒）")
    ordered: bool = Field(..., description="是否按 position 顺序确认")
    cooldown_seconds: int = Field(..., description="冷却时间（秒）")
    process_event_policy: ProcessEventPolicy = Field(
        ..., description="NONE/IN_APP_ONLY/ALL_CHANNELS"
    )
    notification_config: dict[str, Any] = Field(
        default_factory=dict, description="通知配置 JSONB"
    )
    created_by: UUID = Field(..., description="创建者用户 ID")
    created_at: datetime = Field(..., description="创建时间")
    members: list[MonitoringPlanMemberResponse] = Field(
        default_factory=list, description="成员列表"
    )


class MonitoringPlanResponse(BaseModel):
    """监控方案响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="方案 ID")
    user_id: UUID = Field(..., description="用户 ID")
    name: str = Field(..., description="方案名称")
    description: str | None = Field(None, description="方案描述")
    status: PlanStatus = Field(..., description="draft/active/paused/archived")
    current_revision: int = Field(..., description="当前生效版本号")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")
    current_revision_detail: MonitoringPlanRevisionResponse | None = Field(
        None, description="当前版本详情（含成员，GET /{id} 时填充）"
    )


class MonitoringPlanListResponse(BaseModel):
    """方案列表响应。"""

    items: list[MonitoringPlanResponse] = Field(
        default_factory=list, description="方案列表"
    )
    total: int = Field(..., description="总数")


class MonitoringPlanValidateResponse(BaseModel):
    """方案验证结果响应。"""

    valid: bool = Field(..., description="是否通过验证")
    errors: list[str] = Field(default_factory=list, description="错误信息列表")
    warnings: list[str] = Field(default_factory=list, description="警告信息列表")


class MonitoringPlanStateResponse(BaseModel):
    """监控组合状态响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="状态 ID")
    user_id: UUID = Field(..., description="用户 ID")
    monitoring_plan_id: UUID = Field(..., description="方案 ID")
    revision_id: UUID = Field(..., description="方案版本 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    status: StateStatus = Field(
        ..., description="WAITING_TRIGGER/WAITING_CONFIRM/CONFIRMED/EXPIRED/VETOED/COOLDOWN"
    )
    window_started_at: datetime | None = Field(None, description="窗口开始时间")
    window_deadline_at: datetime | None = Field(None, description="窗口截止时间")
    cooldown_until: datetime | None = Field(None, description="冷却截止时间")
    confirmed_member_ids: list[UUID] = Field(
        default_factory=list, description="已确认成员 ID 列表"
    )
    vetoed_by_member_id: UUID | None = Field(None, description="否决成员 ID")
    state_payload: dict[str, Any] = Field(
        default_factory=dict, description="状态附加信息 JSONB"
    )
    lock_version: int = Field(..., description="乐观锁版本号")
    updated_at: datetime = Field(..., description="更新时间")


class MonitoringPlanStateListResponse(BaseModel):
    """状态列表响应。"""

    items: list[MonitoringPlanStateResponse] = Field(
        default_factory=list, description="状态列表"
    )
    total: int = Field(..., description="总数")


class CompositeEventEvidenceResponse(BaseModel):
    """组合事件证据响应。"""

    model_config = ConfigDict(from_attributes=True)

    composite_event_id: UUID = Field(..., description="组合事件 ID")
    member_id: UUID = Field(..., description="成员 ID")
    strategy_event_id: UUID = Field(..., description="原始策略事件 ID")
    summary: dict[str, Any] = Field(
        default_factory=dict, description="证据摘要 JSONB（冻结信息）"
    )


class CompositeMonitorEventResponse(BaseModel):
    """组合监控事件响应（列表用，不含 evidence）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="组合事件 ID")
    user_id: UUID = Field(..., description="用户 ID")
    monitoring_plan_id: UUID = Field(..., description="方案 ID")
    revision_id: UUID = Field(..., description="方案版本 ID")
    instrument_id: UUID = Field(..., description="股票 ID")
    event_type: str = Field(..., description="组合事件类型")
    event_time: datetime = Field(..., description="组合事件时间")
    composite_event_key: str = Field(..., description="组合事件唯一键")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="组合事件负载 JSONB"
    )
    created_at: datetime = Field(..., description="创建时间")


class CompositeMonitorEventDetailResponse(CompositeMonitorEventResponse):
    """组合监控事件详情响应（含 evidence）。"""

    evidence: list[CompositeEventEvidenceResponse] = Field(
        default_factory=list, description="证据列表"
    )


class CompositeMonitorEventListResponse(BaseModel):
    """组合事件列表响应。"""

    items: list[CompositeMonitorEventResponse] = Field(
        default_factory=list, description="组合事件列表"
    )
    total: int = Field(..., description="总数")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    for cls in [
        MonitoringPlanMemberRequest, MonitoringPlanCreateRequest,
        MonitoringPlanUpdateRequest, MonitoringPlanMemberResponse,
        MonitoringPlanRevisionResponse, MonitoringPlanResponse,
        MonitoringPlanListResponse, MonitoringPlanValidateResponse,
        MonitoringPlanStateResponse, MonitoringPlanStateListResponse,
        CompositeEventEvidenceResponse, CompositeMonitorEventResponse,
        CompositeMonitorEventDetailResponse, CompositeMonitorEventListResponse,
    ]:
        print(f"{cls.__name__} fields={list(cls.model_fields.keys())}")

    # 验证 user_id 不在请求体中（安全约束）
    assert "user_id" not in MonitoringPlanCreateRequest.model_fields, \
        "user_id 不应在请求体中"
    assert "user_id" not in MonitoringPlanUpdateRequest.model_fields, \
        "user_id 不应在请求体中"
    print("OK")
