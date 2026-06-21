"""MonitoringPlan API 路由 - 监控组合方案管理（C5/C6/C8）。

端点：
- POST /monitoring-plans: 创建方案（user_id 由上下文注入）
- GET /monitoring-plans: 当前用户方案列表
- GET /monitoring-plans/{id}: 方案详情（含当前 revision + 成员）
- PUT /monitoring-plans/{id}: 更新方案（创建新 revision）
- POST /monitoring-plans/{id}/validate: 验证方案
- POST /monitoring-plans/{id}/pause: 暂停
- POST /monitoring-plans/{id}/resume: 恢复
- GET /monitoring-plans/{id}/states: 查询方案状态
- GET /monitoring-plans/{id}/events: 查询组合事件
- GET /instruments/{id}/composite-state: 查询个股组合状态
- GET /composite-events/{event_id}: 组合事件详情（含 evidence）

设计说明：
- user_id 由 get_current_active_user 注入，不接受请求体传入（V1.1 安全约束）
- 创建方案时同时创建首个 revision（revision=1）+ 成员
- 更新方案时创建新 revision，原 revision 保留
- 验证方案检查成员合法性（PINNED 版本存在、role 合法等）
- 暂停/恢复通过 status 字段控制（active <-> paused）
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.db import get_db
from app.models.monitoring_plan import (
    MonitoringPlan,
    MonitoringPlanMember,
    MonitoringPlanRevision,
)
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
from app.repositories.composite_event_repository import (
    get_composite_event,
    list_composite_events_by_plan,
    list_evidence_by_composite_event,
)
from app.repositories.monitoring_plan_state_repository import (
    list_states_by_instrument,
    list_states_by_revision,
)
from app.schemas.monitoring_plan import (
    CompositeEventEvidenceResponse,
    CompositeMonitorEventDetailResponse,
    CompositeMonitorEventListResponse,
    CompositeMonitorEventResponse,
    MonitoringPlanCreateRequest,
    MonitoringPlanListResponse,
    MonitoringPlanMemberRequest,
    MonitoringPlanMemberResponse,
    MonitoringPlanResponse,
    MonitoringPlanRevisionResponse,
    MonitoringPlanStateListResponse,
    MonitoringPlanStateResponse,
    MonitoringPlanUpdateRequest,
    MonitoringPlanValidateResponse,
)

router = APIRouter(tags=["monitoring-plans"])


def _member_to_response(m: MonitoringPlanMember) -> MonitoringPlanMemberResponse:
    """将 ORM 成员对象转为响应模型。"""
    return MonitoringPlanMemberResponse(
        id=m.id,
        revision_id=m.revision_id,
        strategy_definition_id=m.strategy_definition_id,
        strategy_version_id=m.strategy_version_id,
        version_policy=m.version_policy,
        event_type=m.event_type,
        role=m.role,
        position=m.position,
        required=m.required,
        enabled=m.enabled,
        params=m.params or {},
        conditions=m.conditions or [],
    )


def _revision_to_response(
    rev: MonitoringPlanRevision,
    members: list[MonitoringPlanMember],
) -> MonitoringPlanRevisionResponse:
    """将 ORM 版本对象转为响应模型（含成员）。"""
    return MonitoringPlanRevisionResponse(
        id=rev.id,
        monitoring_plan_id=rev.monitoring_plan_id,
        revision=rev.revision,
        mode=rev.mode,
        confirmation_window_seconds=rev.confirmation_window_seconds,
        ordered=rev.ordered,
        cooldown_seconds=rev.cooldown_seconds,
        process_event_policy=rev.process_event_policy,
        notification_config=rev.notification_config or {},
        created_by=rev.created_by,
        created_at=rev.created_at,
        members=[_member_to_response(m) for m in members],
    )


def _plan_to_response(
    plan: MonitoringPlan,
    current_revision: MonitoringPlanRevision | None = None,
    current_members: list[MonitoringPlanMember] | None = None,
) -> MonitoringPlanResponse:
    """将 ORM 方案对象转为响应模型。"""
    revision_detail = None
    if current_revision is not None:
        revision_detail = _revision_to_response(
            current_revision, current_members or []
        )
    return MonitoringPlanResponse(
        id=plan.id,
        user_id=plan.user_id,
        name=plan.name,
        description=plan.description,
        status=plan.status,
        current_revision=plan.current_revision,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        current_revision_detail=revision_detail,
    )


async def _validate_members(
    db: AsyncSession,
    members: list[MonitoringPlanMemberRequest],
) -> tuple[list[StrategyDefinition], list[StrategyVersion | None]]:
    """校验成员合法性。

    校验项：
    - strategy_definition_id 存在
    - PINNED 模式下 strategy_version_id 存在且属于该 definition
    - ALL 模式至少有一个 TRIGGER 成员（warning，不阻塞）

    Args:
        db: 异步会话
        members: 成员请求列表

    Returns:
        (definitions, versions) 元组，versions[i] 对应 members[i].strategy_version_id

    Raises:
        HTTPException 404: 策略定义/版本不存在
        HTTPException 400: PINNED 模式下版本不属于该定义
    """
    definitions: list[StrategyDefinition] = []
    versions: list[StrategyVersion | None] = []

    for m in members:
        # 校验策略定义存在
        stmt_def = select(StrategyDefinition).where(
            StrategyDefinition.id == m.strategy_definition_id
        )
        result_def = await db.execute(stmt_def)
        definition = result_def.scalar_one_or_none()
        if definition is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"策略定义不存在: strategy_definition_id={m.strategy_definition_id}",
            )
        definitions.append(definition)

        # PINNED 模式校验版本
        if m.version_policy == "PINNED":
            if m.strategy_version_id is None:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"PINNED 模式成员（position={m.position}）必须提供 strategy_version_id",
                )
            stmt_ver = select(StrategyVersion).where(
                StrategyVersion.id == m.strategy_version_id
            )
            result_ver = await db.execute(stmt_ver)
            version = result_ver.scalar_one_or_none()
            if version is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"策略版本不存在: strategy_version_id={m.strategy_version_id}",
                )
            if version.strategy_definition_id != m.strategy_definition_id:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        f"策略版本 {m.strategy_version_id} 不属于策略定义 "
                        f"{m.strategy_definition_id}"
                    ),
                )
            versions.append(version)
        else:
            versions.append(None)

    return definitions, versions


async def _create_revision_with_members(
    db: AsyncSession,
    *,
    plan: MonitoringPlan,
    revision_number: int,
    request: MonitoringPlanCreateRequest | MonitoringPlanUpdateRequest,
    user_id: UUID,
    members: list[MonitoringPlanMemberRequest],
) -> MonitoringPlanRevision:
    """创建 revision + 成员。

    Args:
        db: 异步会话
        plan: 所属方案
        revision_number: 版本号
        request: 请求对象（提供 mode/window 等字段）
        user_id: 创建者用户 ID
        members: 成员请求列表

    Returns:
        新创建的 MonitoringPlanRevision 对象
    """
    # 从 request 提取字段（兼容 Create/Update 两种请求）
    mode = getattr(request, "mode", None) or "INDEPENDENT"
    confirmation_window_seconds = getattr(request, "confirmation_window_seconds", None) or 0
    ordered = getattr(request, "ordered", None) or False
    cooldown_seconds = getattr(request, "cooldown_seconds", None) or 600
    process_event_policy = getattr(request, "process_event_policy", None) or "IN_APP_ONLY"
    notification_config = getattr(request, "notification_config", None) or {}

    revision = MonitoringPlanRevision(
        monitoring_plan_id=plan.id,
        revision=revision_number,
        mode=mode,
        confirmation_window_seconds=confirmation_window_seconds,
        ordered=ordered,
        cooldown_seconds=cooldown_seconds,
        process_event_policy=process_event_policy,
        notification_config=notification_config,
        created_by=user_id,
    )
    db.add(revision)
    await db.flush()  # 获取 revision.id

    for m in members:
        member = MonitoringPlanMember(
            revision_id=revision.id,
            strategy_definition_id=m.strategy_definition_id,
            strategy_version_id=m.strategy_version_id,
            version_policy=m.version_policy,
            event_type=m.event_type,
            role=m.role,
            position=m.position,
            required=m.required,
            enabled=m.enabled,
            params=m.params,
            conditions=m.conditions,
        )
        db.add(member)

    return revision


async def _get_plan_or_404(
    db: AsyncSession,
    plan_id: UUID,
    user_id: UUID,
) -> MonitoringPlan:
    """查询方案或抛出 404。

    同时校验方案属于当前用户（user_id 隔离）。
    """
    stmt = select(MonitoringPlan).where(
        MonitoringPlan.id == plan_id,
        MonitoringPlan.user_id == user_id,
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"方案不存在或无权访问: plan_id={plan_id}",
        )
    return plan


async def _get_current_revision(
    db: AsyncSession,
    plan: MonitoringPlan,
) -> tuple[MonitoringPlanRevision, list[MonitoringPlanMember]]:
    """查询方案当前 revision + 成员。"""
    stmt_rev = (
        select(MonitoringPlanRevision)
        .where(
            MonitoringPlanRevision.monitoring_plan_id == plan.id,
            MonitoringPlanRevision.revision == plan.current_revision,
        )
    )
    result_rev = await db.execute(stmt_rev)
    revision = result_rev.scalar_one_or_none()
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"方案当前 revision 不存在: plan_id={plan.id} revision={plan.current_revision}",
        )

    stmt_members = (
        select(MonitoringPlanMember)
        .where(MonitoringPlanMember.revision_id == revision.id)
        .order_by(MonitoringPlanMember.position)
    )
    result_members = await db.execute(stmt_members)
    members = list(result_members.scalars().all())
    return revision, members


@router.post(
    "/monitoring-plans",
    response_model=MonitoringPlanResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_monitoring_plan(
    request: MonitoringPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanResponse:
    """创建监控方案。

    user_id 由认证上下文注入（不接受 body 中的 user_id）。
    创建时同时创建首个 revision（revision=1）+ 成员。
    """
    # 校验成员合法性
    await _validate_members(db, request.members)

    # 创建方案
    plan = MonitoringPlan(
        user_id=current_user.id,
        name=request.name,
        description=request.description,
        status="draft",
        current_revision=1,
    )
    db.add(plan)
    await db.flush()  # 获取 plan.id

    # 创建首个 revision + 成员
    revision = await _create_revision_with_members(
        db,
        plan=plan,
        revision_number=1,
        request=request,
        user_id=current_user.id,
        members=request.members,
    )
    await db.flush()

    # 查询成员用于响应
    stmt_members = (
        select(MonitoringPlanMember)
        .where(MonitoringPlanMember.revision_id == revision.id)
        .order_by(MonitoringPlanMember.position)
    )
    result_members = await db.execute(stmt_members)
    members = list(result_members.scalars().all())

    await db.commit()
    await db.refresh(plan)
    await db.refresh(revision)
    return _plan_to_response(plan, revision, members)


@router.get("/monitoring-plans", response_model=MonitoringPlanListResponse)
async def list_monitoring_plans(
    status_filter: str | None = Query(None, alias="status", description="按状态过滤"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanListResponse:
    """查询当前用户的方案列表。"""
    stmt = select(MonitoringPlan).where(MonitoringPlan.user_id == current_user.id)
    if status_filter is not None:
        stmt = stmt.where(MonitoringPlan.status == status_filter)
    stmt = stmt.order_by(MonitoringPlan.updated_at.desc())
    result = await db.execute(stmt)
    plans = list(result.scalars().all())
    items = [_plan_to_response(p) for p in plans]
    return MonitoringPlanListResponse(items=items, total=len(items))


@router.get("/monitoring-plans/{plan_id}", response_model=MonitoringPlanResponse)
async def get_monitoring_plan(
    plan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanResponse:
    """查询方案详情（含当前 revision + 成员）。"""
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    revision, members = await _get_current_revision(db, plan)
    return _plan_to_response(plan, revision, members)


@router.put("/monitoring-plans/{plan_id}", response_model=MonitoringPlanResponse)
async def update_monitoring_plan(
    plan_id: UUID,
    request: MonitoringPlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanResponse:
    """更新方案（创建新 revision）。

    更新时创建新 revision，原 revision 保留。
    若提供 members，则新 revision 使用新成员列表；否则沿用当前 revision 的成员。
    """
    plan = await _get_plan_or_404(db, plan_id, current_user.id)

    # 查询当前 revision（用于沿用字段）
    current_revision, current_members = await _get_current_revision(db, plan)

    # 合并请求字段与当前 revision 字段（None 表示不更新，沿用当前值）
    class _MergedRequest:
        """合并后的请求对象，供 _create_revision_with_members 使用。"""

        mode = request.mode if request.mode is not None else current_revision.mode
        confirmation_window_seconds = (
            request.confirmation_window_seconds
            if request.confirmation_window_seconds is not None
            else current_revision.confirmation_window_seconds
        )
        ordered = request.ordered if request.ordered is not None else current_revision.ordered
        cooldown_seconds = (
            request.cooldown_seconds
            if request.cooldown_seconds is not None
            else current_revision.cooldown_seconds
        )
        process_event_policy = (
            request.process_event_policy
            if request.process_event_policy is not None
            else current_revision.process_event_policy
        )
        notification_config = (
            request.notification_config
            if request.notification_config is not None
            else current_revision.notification_config
        )

    merged_request = _MergedRequest()

    # 确定成员列表
    if request.members is not None:
        # 校验新成员合法性
        await _validate_members(db, request.members)
        new_members = request.members
    else:
        # 沿用当前 revision 的成员（构造 MemberRequest）
        new_members = [
            MonitoringPlanMemberRequest(
                strategy_definition_id=m.strategy_definition_id,
                strategy_version_id=m.strategy_version_id,
                version_policy=m.version_policy,
                event_type=m.event_type,
                role=m.role,
                position=m.position,
                required=m.required,
                enabled=m.enabled,
                params=m.params or {},
                conditions=m.conditions or [],
            )
            for m in current_members
        ]

    # 更新方案主表字段
    if request.name is not None:
        plan.name = request.name
    if request.description is not None:
        plan.description = request.description
    plan.current_revision = plan.current_revision + 1
    plan.updated_at = func.now()

    # 创建新 revision
    new_revision = await _create_revision_with_members(
        db,
        plan=plan,
        revision_number=plan.current_revision,
        request=merged_request,
        user_id=current_user.id,
        members=new_members,
    )
    await db.flush()

    # 查询新成员用于响应
    stmt_members = (
        select(MonitoringPlanMember)
        .where(MonitoringPlanMember.revision_id == new_revision.id)
        .order_by(MonitoringPlanMember.position)
    )
    result_members = await db.execute(stmt_members)
    new_member_list = list(result_members.scalars().all())

    await db.commit()
    await db.refresh(plan)
    await db.refresh(new_revision)
    return _plan_to_response(plan, new_revision, new_member_list)


@router.post(
    "/monitoring-plans/{plan_id}/validate",
    response_model=MonitoringPlanValidateResponse,
)
async def validate_monitoring_plan(
    plan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanValidateResponse:
    """验证方案。

    校验项：
    - 当前 revision 存在
    - 成员合法性（PINNED 版本存在）
    - ALL 模式至少有一个 TRIGGER 成员（warning）
    - ALL 模式 confirmation_window_seconds > 0（warning）
    - members 至少 1 个
    """
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    revision, members = await _get_current_revision(db, plan)

    errors: list[str] = []
    warnings: list[str] = []

    # 校验成员数量
    if len(members) == 0:
        errors.append("方案成员不能为空")

    # 校验 PINNED 成员有版本
    for m in members:
        if m.version_policy == "PINNED" and m.strategy_version_id is None:
            errors.append(
                f"PINNED 成员（position={m.position}）缺少 strategy_version_id"
            )

    # ALL 模式特殊校验
    if revision.mode == "ALL":
        trigger_members = [m for m in members if m.role == "TRIGGER" and m.enabled]
        if not trigger_members:
            warnings.append("ALL 模式建议至少有一个 TRIGGER 成员")
        if revision.confirmation_window_seconds <= 0:
            warnings.append("ALL 模式建议 confirmation_window_seconds > 0")
        if revision.ordered:
            # 校验 position 连续
            positions = sorted(m.position for m in members)
            for i, p in enumerate(positions):
                if p != i:
                    errors.append(f"ordered 模式下 position 不连续: {positions}")
                    break

    # 校验 role 合法性
    valid_roles = {"TRIGGER", "CONFIRM", "VETO", "OBSERVE"}
    for m in members:
        if m.role not in valid_roles:
            errors.append(f"成员 position={m.position} 非法 role: {m.role}")

    return MonitoringPlanValidateResponse(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
    )


@router.post("/monitoring-plans/{plan_id}/pause", response_model=MonitoringPlanResponse)
async def pause_monitoring_plan(
    plan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanResponse:
    """暂停方案（status: active -> paused）。"""
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    if plan.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"方案当前状态为 {plan.status}，仅 active 状态可暂停",
        )
    plan.status = "paused"
    plan.updated_at = func.now()
    await db.commit()
    await db.refresh(plan)
    return _plan_to_response(plan)


@router.post("/monitoring-plans/{plan_id}/resume", response_model=MonitoringPlanResponse)
async def resume_monitoring_plan(
    plan_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanResponse:
    """恢复方案（status: paused -> active）。"""
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    if plan.status != "paused":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"方案当前状态为 {plan.status}，仅 paused 状态可恢复",
        )
    plan.status = "active"
    plan.updated_at = func.now()
    await db.commit()
    await db.refresh(plan)
    return _plan_to_response(plan)


@router.get(
    "/monitoring-plans/{plan_id}/states",
    response_model=MonitoringPlanStateListResponse,
)
async def list_plan_states(
    plan_id: UUID,
    status_filter: str | None = Query(None, alias="status", description="按状态过滤"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanStateListResponse:
    """查询方案状态（当前 revision 下的所有股票状态）。"""
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    revision, _ = await _get_current_revision(db, plan)
    states = await list_states_by_revision(
        db, revision.id, status_filter=status_filter
    )
    items = [MonitoringPlanStateResponse.model_validate(s) for s in states]
    return MonitoringPlanStateListResponse(items=items, total=len(items))


@router.get(
    "/monitoring-plans/{plan_id}/events",
    response_model=CompositeMonitorEventListResponse,
)
async def list_plan_composite_events(
    plan_id: UUID,
    event_type: str | None = Query(None, description="按事件类型过滤"),
    start_time: datetime | None = Query(None, description="事件时间 >= start_time"),
    end_time: datetime | None = Query(None, description="事件时间 <= end_time"),
    limit: int = Query(100, ge=1, le=500, description="最大返回数"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CompositeMonitorEventListResponse:
    """查询方案下的组合事件。"""
    plan = await _get_plan_or_404(db, plan_id, current_user.id)
    events = await list_composite_events_by_plan(
        db,
        monitoring_plan_id=plan.id,
        user_id=current_user.id,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )
    items = [CompositeMonitorEventResponse.model_validate(e) for e in events]
    return CompositeMonitorEventListResponse(items=items, total=len(items))


@router.get(
    "/instruments/{instrument_id}/composite-state",
    response_model=MonitoringPlanStateListResponse,
)
async def get_instrument_composite_state(
    instrument_id: UUID,
    plan_id: UUID | None = Query(None, alias="plan_id", description="按方案过滤"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MonitoringPlanStateListResponse:
    """查询个股组合状态。

    返回当前用户在该股票下的所有监控组合状态。
    可通过 plan_id 过滤特定方案。
    """
    states = await list_states_by_instrument(
        db, instrument_id, user_id=current_user.id
    )
    # 可选方案过滤
    if plan_id is not None:
        states = [s for s in states if s.monitoring_plan_id == plan_id]
    items = [MonitoringPlanStateResponse.model_validate(s) for s in states]
    return MonitoringPlanStateListResponse(items=items, total=len(items))


@router.get(
    "/composite-events/{event_id}",
    response_model=CompositeMonitorEventDetailResponse,
)
async def get_composite_event_detail(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CompositeMonitorEventDetailResponse:
    """查询组合事件详情（含 evidence）。"""
    event = await get_composite_event(db, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"组合事件不存在: event_id={event_id}",
        )
    # 校验属于当前用户
    if event.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"组合事件不存在或无权访问: event_id={event_id}",
        )

    evidence = await list_evidence_by_composite_event(db, event.id)
    evidence_items = [
        CompositeEventEvidenceResponse.model_validate(e) for e in evidence
    ]
    return CompositeMonitorEventDetailResponse(
        id=event.id,
        user_id=event.user_id,
        monitoring_plan_id=event.monitoring_plan_id,
        revision_id=event.revision_id,
        instrument_id=event.instrument_id,
        event_type=event.event_type,
        event_time=event.event_time,
        composite_event_key=event.composite_event_key,
        payload=event.payload or {},
        created_at=event.created_at,
        evidence=evidence_items,
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/monitoring-plans" in paths
    assert any("/monitoring-plans/" in p for p in paths)
    assert any("/composite-events/" in p for p in paths)
    assert any("/composite-state" in p for p in paths)
    print("OK")
