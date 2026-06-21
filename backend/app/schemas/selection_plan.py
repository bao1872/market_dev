"""选股组合方案 Pydantic schemas - 请求/响应模型（C1 + C4）。

提供：
- 方案 CRUD：SelectionPlanCreateRequest / UpdateRequest / CloneRequest / Response / DetailResponse / ListResponse
- 方案子对象：RevisionResponse / MemberResponse / ConditionResponse
- 运行相关：RunRequest / RunResponse / RunListResponse
- 结果相关：ResultResponse / ResultListResponse / EvidenceResponse / MemberResultResponse
- 预览：PreviewRequest / PreviewResponse

设计说明：
- 请求体字段与 selection_plan.schema.json 对齐（operator/missing_member_policy/version_policy 枚举）
- user_id 不在请求体中（由认证上下文注入，V1.1 安全约束）
- 成员 strategy_definition_id/strategy_version_id 由后端根据 strategy_key + version_policy 解析填充
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ============================================================
# 方案 CRUD 请求/响应
# ============================================================


class ConditionSpec(BaseModel):
    """成员条件规格（请求体）- 与 schema 文件对齐。"""

    metric_key: str = Field(..., description="指标名（如 dsa_dir_bars）")
    operator: str = Field(
        ..., description="比较操作：gt/gte/lt/lte/eq/between"
    )
    value: Any = Field(..., description="主值（数值/字符串）")
    value2: Any | None = Field(None, description="上界值（仅 between 操作）")


class MemberSpec(BaseModel):
    """成员规格（请求体）- 与 schema 文件对齐。"""

    strategy_key: str = Field(..., description="策略 key（如 dsa_selector）")
    version_policy: str = Field(
        ..., description="版本策略：PINNED(锁定版本)/STABLE_TRACK(跟踪最新)"
    )
    strategy_version: str | None = Field(
        None, description="策略版本号（PINNED 必填）"
    )
    params: dict[str, Any] = Field(
        default_factory=dict, description="成员参数"
    )
    conditions: list[ConditionSpec] = Field(
        default_factory=list, description="成员条件（线性 AND）"
    )
    enabled: bool = Field(True, description="是否启用")
    position: int | None = Field(None, description="成员顺序（未提供则自动分配）")


class SelectionPlanCreateRequest(BaseModel):
    """创建选股方案请求 - 含完整方案定义。

    user_id 由认证上下文注入，不在请求体中。
    """

    name: str = Field(..., min_length=1, max_length=80, description="方案名称")
    description: str | None = Field(
        None, max_length=500, description="方案描述"
    )
    operator: str = Field(..., description="集合运算：ALL(交集)/ANY(并集)")
    missing_member_policy: str = Field(
        "FAIL_CLOSED",
        description="成员缺失策略：FAIL_CLOSED(失败)/IGNORE_MEMBER(忽略)",
    )
    universe: dict[str, Any] = Field(
        default_factory=dict, description="标的范围配置"
    )
    sort_spec: list[dict[str, Any]] = Field(
        default_factory=list, description="排名规格（白名单表达式）"
    )
    notification: dict[str, Any] = Field(
        default_factory=dict, description="通知配置"
    )
    members: list[MemberSpec] = Field(
        ..., min_length=1, description="方案成员列表"
    )


class SelectionPlanUpdateRequest(BaseModel):
    """更新选股方案请求 - 创建新 revision。

    更新时创建新 revision（不可变快照），current_revision 递增。
    """

    name: str | None = Field(None, min_length=1, max_length=80, description="方案名称")
    description: str | None = Field(
        None, max_length=500, description="方案描述"
    )
    operator: str | None = Field(None, description="集合运算：ALL/ANY")
    missing_member_policy: str | None = Field(
        None, description="成员缺失策略：FAIL_CLOSED/IGNORE_MEMBER"
    )
    universe: dict[str, Any] | None = Field(None, description="标的范围配置")
    sort_spec: list[dict[str, Any]] | None = Field(
        None, description="排名规格"
    )
    notification: dict[str, Any] | None = Field(
        None, description="通知配置"
    )
    members: list[MemberSpec] | None = Field(
        None, min_length=1, description="方案成员列表"
    )


class SelectionPlanCloneRequest(BaseModel):
    """克隆选股方案请求 - 复制方案到新方案。"""

    name: str = Field(..., min_length=1, max_length=80, description="新方案名称")
    description: str | None = Field(
        None, max_length=500, description="新方案描述"
    )


class SelectionMemberConditionResponse(BaseModel):
    """成员条件响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="条件 ID")
    member_id: UUID = Field(..., description="所属成员 ID")
    position: int = Field(..., description="条件顺序")
    metric_key: str = Field(..., description="指标名")
    operator: str = Field(..., description="比较操作")
    value1: Any = Field(..., description="主值")
    value2: Any | None = Field(None, description="上界值（仅 between）")


class SelectionPlanMemberResponse(BaseModel):
    """方案成员响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="成员 ID")
    revision_id: UUID = Field(..., description="所属版本 ID")
    strategy_definition_id: UUID = Field(..., description="策略定义 ID")
    strategy_version_id: UUID | None = Field(
        None, description="策略版本 ID（PINNED 必填）"
    )
    version_policy: str = Field(..., description="版本策略：PINNED/STABLE_TRACK")
    position: int = Field(..., description="成员顺序")
    enabled: bool = Field(..., description="是否启用")
    params: dict[str, Any] = Field(default_factory=dict, description="成员参数")
    conditions: list[SelectionMemberConditionResponse] = Field(
        default_factory=list, description="成员条件列表"
    )


class SelectionPlanRevisionResponse(BaseModel):
    """方案版本响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="版本 ID")
    selection_plan_id: UUID = Field(..., description="所属方案 ID")
    revision: int = Field(..., description="版本号")
    operator: str = Field(..., description="集合运算：ALL/ANY")
    missing_member_policy: str = Field(
        ..., description="成员缺失策略：FAIL_CLOSED/IGNORE_MEMBER"
    )
    universe: dict[str, Any] = Field(default_factory=dict, description="标的范围配置")
    sort_spec: list[Any] = Field(default_factory=list, description="排名规格")
    notification_config: dict[str, Any] = Field(
        default_factory=dict, description="通知配置"
    )
    created_by: UUID = Field(..., description="创建者 ID")
    created_at: datetime = Field(..., description="创建时间")
    members: list[SelectionPlanMemberResponse] = Field(
        default_factory=list, description="成员列表"
    )


class SelectionPlanResponse(BaseModel):
    """选股方案响应（主表字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="方案 ID")
    user_id: UUID = Field(..., description="所属用户 ID")
    name: str = Field(..., description="方案名称")
    description: str | None = Field(None, description="方案描述")
    status: str = Field(..., description="方案状态：draft/active/archived")
    current_revision: int = Field(..., description="当前生效版本号")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class SelectionPlanDetailResponse(SelectionPlanResponse):
    """选股方案详情响应（含当前 revision + members + conditions）。"""

    current_revision_data: SelectionPlanRevisionResponse | None = Field(
        None, description="当前版本详情（含成员与条件）"
    )


class SelectionPlanListResponse(BaseModel):
    """选股方案列表响应。"""

    items: list[SelectionPlanResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


# ============================================================
# 运行/结果/证据 请求/响应（C4）
# ============================================================


class SelectionPlanRunRequest(BaseModel):
    """执行选股方案请求。

    trigger_kind 为运行时参数（不持久化为列，参与幂等键计算）。
    """

    trade_date: date = Field(..., description="交易日")
    trigger_kind: str = Field(
        "manual", description="触发方式：manual/scheduled/replay（参与幂等键）"
    )


class SelectionPlanRunResponse(BaseModel):
    """选股方案运行响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="运行 ID")
    user_id: UUID = Field(..., description="触发用户 ID")
    selection_plan_id: UUID = Field(..., description="方案 ID")
    revision_id: UUID = Field(..., description="方案版本 ID")
    trade_date: date = Field(..., description="交易日")
    status: str = Field(..., description="运行状态：pending/running/succeeded/failed")
    input_run_set_hash: str = Field(..., description="输入运行集哈希")
    idempotency_key: str = Field(..., description="幂等键")
    started_at: datetime | None = Field(None, description="开始时间")
    finished_at: datetime | None = Field(None, description="完成时间")


class SelectionPlanRunListResponse(BaseModel):
    """运行列表响应。"""

    items: list[SelectionPlanRunResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class SelectionResultEvidenceResponse(BaseModel):
    """结果证据响应 - 单个成员对单个标的的命中证据。"""

    model_config = ConfigDict(from_attributes=True)

    selection_result_id: UUID = Field(..., description="所属结果 ID")
    member_id: UUID = Field(..., description="成员 ID")
    strategy_result_id: UUID | None = Field(
        None, description="原始策略结果 ID"
    )
    matched: bool = Field(..., description="该成员是否命中该标的")
    reason_code: str | None = Field(
        None, description="缺失原因：NO_RESULT/FILTERED_OUT/DATA_MISSING"
    )
    summary: dict[str, Any] = Field(
        default_factory=dict, description="指标摘要"
    )


class SelectionPlanResultResponse(BaseModel):
    """方案运行结果响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="结果 ID")
    plan_run_id: UUID = Field(..., description="所属运行 ID")
    instrument_id: UUID = Field(..., description="标的 ID")
    matched: bool = Field(..., description="是否最终命中")
    matched_member_ids: list[UUID] = Field(
        default_factory=list, description="命中该标的的成员 ID 数组"
    )
    rank_value: float | None = Field(None, description="排名分值")
    summary: dict[str, Any] = Field(
        default_factory=dict, description="指标摘要"
    )


class SelectionPlanResultListResponse(BaseModel):
    """方案运行结果列表响应（分页）。"""

    items: list[SelectionPlanResultResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")
    page: int = Field(..., description="当前页码（从 1 开始）")
    page_size: int = Field(..., description="每页大小")


class SelectionPlanMemberResultResponse(BaseModel):
    """成员级结果响应（证据链）- 按成员聚合的证据列表。"""

    member_id: UUID = Field(..., description="成员 ID")
    evidences: list[SelectionResultEvidenceResponse] = Field(
        default_factory=list, description="该成员的证据列表"
    )
    total: int = Field(..., description="证据总数")


# ============================================================
# 预览 请求/响应（C4）
# ============================================================


class SelectionPlanPreviewRequest(BaseModel):
    """预览选股方案结果请求（不落库）。"""

    trade_date: date = Field(..., description="交易日")
    revision_id: UUID | None = Field(
        None, description="指定版本 ID（None 则用当前版本）"
    )


class SelectionPlanPreviewResponse(BaseModel):
    """预览响应 - 不持久化，只返回数量、样本和成员命中统计。"""

    total: int = Field(..., description="命中标的总数")
    sample: list[SelectionPlanResultResponse] = Field(
        default_factory=list, description="样本结果（最多 20 条）"
    )
    member_hit_stats: dict[str, int] = Field(
        default_factory=dict, description="成员命中统计（member_id -> 命中数）"
    )


class SelectionPlanValidateResponse(BaseModel):
    """方案校验响应。"""

    valid: bool = Field(..., description="是否通过校验")
    errors: list[dict[str, Any]] = Field(
        default_factory=list, description="校验错误列表（valid=True 时为空）"
    )


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"SelectionPlanCreateRequest fields={list(SelectionPlanCreateRequest.model_fields.keys())}")
    print(f"SelectionPlanUpdateRequest fields={list(SelectionPlanUpdateRequest.model_fields.keys())}")
    print(f"SelectionPlanResponse fields={list(SelectionPlanResponse.model_fields.keys())}")
    print(f"SelectionPlanDetailResponse fields={list(SelectionPlanDetailResponse.model_fields.keys())}")
    print(f"SelectionPlanRunRequest fields={list(SelectionPlanRunRequest.model_fields.keys())}")
    print(f"SelectionPlanRunResponse fields={list(SelectionPlanRunResponse.model_fields.keys())}")
    print(f"SelectionPlanResultResponse fields={list(SelectionPlanResultResponse.model_fields.keys())}")
    print(f"SelectionResultEvidenceResponse fields={list(SelectionResultEvidenceResponse.model_fields.keys())}")
    print(f"SelectionPlanPreviewResponse fields={list(SelectionPlanPreviewResponse.model_fields.keys())}")

    # 验证枚举默认值
    req = SelectionPlanCreateRequest(
        name="test", operator="ALL",
        members=[MemberSpec(strategy_key="dsa_selector", version_policy="STABLE_TRACK")],
    )
    assert req.missing_member_policy == "FAIL_CLOSED"
    assert req.universe == {}
    assert req.sort_spec == []
    print(f"默认值校验: missing_member_policy={req.missing_member_policy} ✓")
    print("OK")
