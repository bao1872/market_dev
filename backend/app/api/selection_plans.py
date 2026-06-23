"""选股组合方案 API 路由（C1 CRUD + C4 运行/结果）。

端点（C1 方案管理）：
- GET /selection-plans: 当前用户方案列表（user_id 由认证上下文注入）
- POST /selection-plans: 创建方案（user_id 由上下文注入）
- GET /selection-plans/{id}: 方案详情（含 revision + members + conditions）
- PUT /selection-plans/{id}: 更新方案（创建新 revision）
- POST /selection-plans/{id}/clone: 克隆方案

端点（C4 运行/结果）：
- POST /selection-plans/{id}/validate: 验证方案
- POST /selection-plans/{id}/preview: 预览结果（不落库）
- POST /selection-plans/{id}/run: 执行方案（幂等）
- GET /selection-plans/{id}/runs: 运行列表
- GET /selection-plan-runs/{run_id}/results: 运行结果（分页）
- GET /selection-plan-runs/{run_id}/member-results/{member_id}: 成员级结果（证据链）

设计说明：
- user_id 由 get_current_active_user 注入，不接受请求体传入（V1.1 安全约束）
- 创建/更新方案时先校验符合 selection_plan.schema.json，再解析 strategy_key 为 definition_id
- 更新方案创建新 revision（不可变快照），current_revision 递增
- 运行绑定不可变 revision，幂等键防重复执行
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_active_user
from app.db import get_db
from app.models.selection_plan import (
    SelectionMemberCondition,
    SelectionPlan,
    SelectionPlanMember,
    SelectionPlanRevision,
)
from app.models.selection_plan_run import (
    SelectionPlanResult,
    SelectionPlanRun,
    SelectionResultEvidence,
)
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
# [LEGACY] combo schemas removed - from app.schemas.selection_plan import ...
from app.services.selection_plan_validator import (
    SelectionPlanValidationError,
    validate_plan,
)

logger = logging.getLogger("api.selection_plans")

router = APIRouter(tags=["selection-plans"])


# ============================================================
# 辅助函数
# ============================================================


def _build_plan_dict_for_validation(
    request: SelectionPlanCreateRequest | SelectionPlanUpdateRequest,
) -> dict:
    """将请求体转为 selection_plan.schema.json 格式的字典用于校验。

    schema 文件要求字段：name, description, operator, missing_member_policy,
    universe, sort_spec, notification, members（每个成员含 strategy_key/version_policy/
    strategy_version/params/conditions/enabled/position）。
    """
    # 兼容 Create（全字段）与 Update（部分字段）两种请求
    plan_dict: dict = {}
    if request.name is not None:
        plan_dict["name"] = request.name
    if hasattr(request, "description") and request.description is not None:
        plan_dict["description"] = request.description
    if request.operator is not None:
        plan_dict["operator"] = request.operator
    if request.missing_member_policy is not None:
        plan_dict["missing_member_policy"] = request.missing_member_policy
    if request.universe is not None:
        plan_dict["universe"] = request.universe
    if request.sort_spec is not None:
        plan_dict["sort_spec"] = request.sort_spec
    if request.notification is not None:
        plan_dict["notification"] = request.notification
    if request.members is not None:
        plan_dict["members"] = [
            {
                "strategy_key": m.strategy_key,
                "version_policy": m.version_policy,
                "strategy_version": m.strategy_version,
                "params": m.params,
                "conditions": [
                    {
                        "metric_key": c.metric_key,
                        "operator": c.operator,
                        "value": c.value,
                        **({"value2": c.value2} if c.value2 is not None else {}),
                    }
                    for c in m.conditions
                ],
                "enabled": m.enabled,
                **({"position": m.position} if m.position is not None else {}),
            }
            for m in request.members
        ]
    return plan_dict


async def _resolve_strategy_definition(
    db: AsyncSession, strategy_key: str
) -> StrategyDefinition:
    """根据 strategy_key 查询策略定义。

    Raises:
        HTTPException 404: 策略定义不存在
    """
    stmt = select(StrategyDefinition).where(
        StrategyDefinition.strategy_key == strategy_key
    )
    result = await db.execute(stmt)
    definition = result.scalar_one_or_none()
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略定义不存在: strategy_key={strategy_key}",
        )
    return definition


async def _resolve_strategy_version(
    db: AsyncSession,
    definition: StrategyDefinition,
    version_policy: str,
    strategy_version: str | None,
) -> uuid.UUID | None:
    """根据版本策略解析策略版本 ID。

    - PINNED: 必须提供 strategy_version，查询指定版本
    - STABLE_TRACK: 查询最新 released 版本

    Raises:
        HTTPException 400: PINNED 未提供版本号
        HTTPException 404: 指定版本不存在 / 无 released 版本
    """
    if version_policy == "PINNED":
        if strategy_version is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="version_policy=PINNED 时必须提供 strategy_version",
            )
        stmt = select(StrategyVersion).where(
            StrategyVersion.strategy_definition_id == definition.id,
            StrategyVersion.version == strategy_version,
        )
        result = await db.execute(stmt)
        version = result.scalar_one_or_none()
        if version is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"策略版本不存在: strategy_key={definition.strategy_key}, "
                f"version={strategy_version}",
            )
        return version.id

    # STABLE_TRACK: 查询最新 released 版本
    stmt = (
        select(StrategyVersion)
        .where(
            StrategyVersion.strategy_definition_id == definition.id,
            StrategyVersion.status == "released",
        )
        .order_by(StrategyVersion.released_at.desc().nullslast())
        .limit(1)
    )
    result = await db.execute(stmt)
    version = result.scalar_one_or_none()
    if version is None:
        # 无 released 版本时返回 None（运行时按 missing_member_policy 处理）
        logger.warning(
            "策略无 released 版本: strategy_key=%s, version_policy=STABLE_TRACK",
            definition.strategy_key,
        )
        return None
    return version.id


async def _create_revision_with_members(
    db: AsyncSession,
    plan: SelectionPlan,
    request: SelectionPlanCreateRequest | SelectionPlanUpdateRequest,
    user_id: uuid.UUID,
    revision_number: int,
) -> SelectionPlanRevision:
    """创建方案版本 + 成员 + 条件。

    流程：
    1. 校验方案符合 schema
    2. 创建 SelectionPlanRevision
    3. 对每个成员解析 strategy_key → definition_id + version_id
    4. 创建 SelectionPlanMember + SelectionMemberCondition

    Returns:
        创建的 SelectionPlanRevision（含 members + conditions 关系）
    """
    # 1. 校验方案
    plan_dict = _build_plan_dict_for_validation(request)
    try:
        validate_plan(plan_dict)
    except SelectionPlanValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e

    # 2. 创建 revision
    revision = SelectionPlanRevision(
        selection_plan_id=plan.id,
        revision=revision_number,
        operator=request.operator,  # type: ignore[arg-type]
        missing_member_policy=request.missing_member_policy or "FAIL_CLOSED",
        universe=request.universe or {},
        sort_spec=request.sort_spec or [],
        notification_config=request.notification or {},
        created_by=user_id,
    )
    db.add(revision)
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建方案版本失败: {exc}",
        ) from exc

    # 3. 创建成员 + 条件
    members_data = request.members or []
    for position, member_spec in enumerate(members_data):
        definition = await _resolve_strategy_definition(db, member_spec.strategy_key)
        version_id = await _resolve_strategy_version(
            db,
            definition,
            member_spec.version_policy,
            member_spec.strategy_version,
        )

        member = SelectionPlanMember(
            revision_id=revision.id,
            strategy_definition_id=definition.id,
            strategy_version_id=version_id,
            version_policy=member_spec.version_policy,
            position=member_spec.position if member_spec.position is not None else position,
            enabled=member_spec.enabled,
            params=member_spec.params,
        )
        db.add(member)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"创建方案成员失败 (position={position}): {exc}",
            ) from exc

        # 创建条件
        for cond_position, cond_spec in enumerate(member_spec.conditions):
            condition = SelectionMemberCondition(
                member_id=member.id,
                position=cond_position,
                metric_key=cond_spec.metric_key,
                operator=cond_spec.operator,
                value1=cond_spec.value,
                value2=cond_spec.value2,
            )
            db.add(condition)

    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建方案条件失败: {exc}",
        ) from exc

    return revision


async def _load_revision_with_relations(
    db: AsyncSession, revision_id: uuid.UUID
) -> SelectionPlanRevision | None:
    """加载版本及其成员、条件（使用 selectinload 避免 N+1）。"""
    stmt = (
        select(SelectionPlanRevision)
        .options(
            selectinload(SelectionPlanRevision.members).selectinload(
                SelectionPlanMember.conditions
            )
        )
        .where(SelectionPlanRevision.id == revision_id)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def _revision_to_response(
    revision: SelectionPlanRevision,
) -> SelectionPlanRevisionResponse:
    """将 revision ORM 转为响应模型（含 members + conditions）。"""
    members = []
    for member in sorted(revision.members, key=lambda m: m.position):
        conditions = [
            SelectionMemberConditionResponse.model_validate(c)
            for c in sorted(member.conditions, key=lambda c: c.position)
        ]
        members.append(
            SelectionPlanMemberResponse(
                id=member.id,
                revision_id=member.revision_id,
                strategy_definition_id=member.strategy_definition_id,
                strategy_version_id=member.strategy_version_id,
                version_policy=member.version_policy,
                position=member.position,
                enabled=member.enabled,
                params=member.params,
                conditions=conditions,
            )
        )
    return SelectionPlanRevisionResponse(
        id=revision.id,
        selection_plan_id=revision.selection_plan_id,
        revision=revision.revision,
        operator=revision.operator,
        missing_member_policy=revision.missing_member_policy,
        universe=revision.universe,
        sort_spec=revision.sort_spec,
        notification_config=revision.notification_config,
        created_by=revision.created_by,
        created_at=revision.created_at,
        members=members,
    )


async def _get_plan_owned_by_user(
    db: AsyncSession, plan_id: uuid.UUID, user_id: uuid.UUID
) -> SelectionPlan:
    """获取方案并校验归属（user_id 隔离）。

    Raises:
        HTTPException 404: 方案不存在或不属于该用户
    """
    stmt = select(SelectionPlan).where(
        SelectionPlan.id == plan_id,
        SelectionPlan.user_id == user_id,
    )
    result = await db.execute(stmt)
    plan = result.scalar_one_or_none()
    if plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"方案不存在或无权访问: plan_id={plan_id}",
        )
    return plan


# ============================================================
# C1 方案 CRUD 端点
# ============================================================


@router.get(
    "/selection-plans",
    response_model=SelectionPlanListResponse,
)
async def list_selection_plans(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanListResponse:
    """查询当前用户的选股方案列表。

    user_id 由认证上下文注入，不接受查询参数传入。
    """
    stmt = (
        select(SelectionPlan)
        .where(SelectionPlan.user_id == current_user.id)
        .order_by(SelectionPlan.updated_at.desc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()
    return SelectionPlanListResponse(
        items=[SelectionPlanResponse.model_validate(p) for p in items],
        total=len(items),
    )


@router.post(
    "/selection-plans",
    response_model=SelectionPlanDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_selection_plan(
    request: SelectionPlanCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanDetailResponse:
    """创建选股方案。

    user_id 由认证上下文注入（不接受 body 中的 user_id）。
    创建时生成 revision=1 的初始版本。
    """
    # 创建方案主表
    plan = SelectionPlan(
        user_id=current_user.id,
        name=request.name,
        description=request.description,
        status="draft",
        current_revision=1,
    )
    db.add(plan)
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"创建方案失败: {exc}",
        ) from exc

    # 创建 revision=1 + members + conditions
    revision = await _create_revision_with_members(
        db, plan, request, current_user.id, revision_number=1
    )

    await db.commit()
    await db.refresh(plan)

    # 重新加载 revision 含关系
    revision_loaded = await _load_revision_with_relations(db, revision.id)
    revision_resp = (
        _revision_to_response(revision_loaded) if revision_loaded else None
    )

    return SelectionPlanDetailResponse(
        id=plan.id,
        user_id=plan.user_id,
        name=plan.name,
        description=plan.description,
        status=plan.status,
        current_revision=plan.current_revision,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        current_revision_data=revision_resp,
    )


@router.get(
    "/selection-plans/{plan_id}",
    response_model=SelectionPlanDetailResponse,
)
async def get_selection_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanDetailResponse:
    """获取选股方案详情（含当前 revision + members + conditions）。"""
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 查询当前 revision
    stmt = select(SelectionPlanRevision).where(
        SelectionPlanRevision.selection_plan_id == plan.id,
        SelectionPlanRevision.revision == plan.current_revision,
    )
    result = await db.execute(stmt)
    revision = result.scalar_one_or_none()

    revision_resp = None
    if revision is not None:
        revision_loaded = await _load_revision_with_relations(db, revision.id)
        if revision_loaded is not None:
            revision_resp = _revision_to_response(revision_loaded)

    return SelectionPlanDetailResponse(
        id=plan.id,
        user_id=plan.user_id,
        name=plan.name,
        description=plan.description,
        status=plan.status,
        current_revision=plan.current_revision,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        current_revision_data=revision_resp,
    )


@router.put(
    "/selection-plans/{plan_id}",
    response_model=SelectionPlanDetailResponse,
)
async def update_selection_plan(
    plan_id: uuid.UUID,
    request: SelectionPlanUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanDetailResponse:
    """更新选股方案 - 创建新 revision（不可变快照）。

    更新时 current_revision 递增，旧 revision 保留（不可变）。
    若 members 为 None，则复制当前 revision 的成员到新 revision。
    """
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 更新主表字段
    if request.name is not None:
        plan.name = request.name
    if request.description is not None:
        plan.description = request.description
    plan.updated_at = datetime.now(UTC)

    # 创建新 revision
    new_revision_number = plan.current_revision + 1

    # 若 members 为 None，从当前 revision 复制（构造一个 Create 请求）
    if request.members is None:
        # 加载当前 revision 的成员
        current_revision = await _load_revision_with_relations(
            db,
            (await db.execute(
                select(SelectionPlanRevision.id).where(
                    SelectionPlanRevision.selection_plan_id == plan.id,
                    SelectionPlanRevision.revision == plan.current_revision,
                )
            )).scalar_one(),
        )
        if current_revision is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"当前版本不存在: revision={plan.current_revision}",
            )
        # 复制成员到新请求（使用当前 revision 的配置）
        copied_members = []
        for m in sorted(current_revision.members, key=lambda x: x.position):
            copied_members.append(MemberSpec(
                strategy_key="",  # 占位，后续通过 definition_id 反查
                version_policy=m.version_policy,
                strategy_version=None,
                params=m.params,
                conditions=[
                    ConditionSpec(
                        metric_key=c.metric_key,
                        operator=c.operator,
                        value=c.value1,
                        value2=c.value2,
                    )
                    for c in sorted(m.conditions, key=lambda x: x.position)
                ],
                enabled=m.enabled,
                position=m.position,
            ))
        # 直接复制成员（绕过 strategy_key 解析，使用 definition_id）
        new_revision = SelectionPlanRevision(
            selection_plan_id=plan.id,
            revision=new_revision_number,
            operator=request.operator or current_revision.operator,
            missing_member_policy=request.missing_member_policy or current_revision.missing_member_policy,
            universe=request.universe or current_revision.universe,
            sort_spec=request.sort_spec or current_revision.sort_spec,
            notification_config=request.notification or current_revision.notification_config,
            created_by=current_user.id,
        )
        db.add(new_revision)
        try:
            await db.flush()
        except Exception as exc:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"创建新版本失败: {exc}",
            ) from exc
        # 复制成员
        for m in sorted(current_revision.members, key=lambda x: x.position):
            new_member = SelectionPlanMember(
                revision_id=new_revision.id,
                strategy_definition_id=m.strategy_definition_id,
                strategy_version_id=m.strategy_version_id,
                version_policy=m.version_policy,
                position=m.position,
                enabled=m.enabled,
                params=m.params,
            )
            db.add(new_member)
            await db.flush()
            for c in sorted(m.conditions, key=lambda x: x.position):
                db.add(SelectionMemberCondition(
                    member_id=new_member.id,
                    position=c.position,
                    metric_key=c.metric_key,
                    operator=c.operator,
                    value1=c.value1,
                    value2=c.value2,
                ))
    else:
        # 提供了新 members，正常创建
        # 构造一个 Create 请求复用 _create_revision_with_members
        create_request = SelectionPlanCreateRequest(
            name=request.name or plan.name,
            description=request.description,
            operator=request.operator or "ALL",
            missing_member_policy=request.missing_member_policy or "FAIL_CLOSED",
            universe=request.universe or {},
            sort_spec=request.sort_spec or [],
            notification=request.notification or {},
            members=request.members,
        )
        new_revision = await _create_revision_with_members(
            db, plan, create_request, current_user.id, new_revision_number
        )

    plan.current_revision = new_revision_number
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"更新方案失败: {exc}",
        ) from exc

    await db.commit()
    await db.refresh(plan)

    # 重新加载新 revision
    revision_loaded = await _load_revision_with_relations(db, new_revision.id)
    revision_resp = (
        _revision_to_response(revision_loaded) if revision_loaded else None
    )

    return SelectionPlanDetailResponse(
        id=plan.id,
        user_id=plan.user_id,
        name=plan.name,
        description=plan.description,
        status=plan.status,
        current_revision=plan.current_revision,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        current_revision_data=revision_resp,
    )


@router.post(
    "/selection-plans/{plan_id}/clone",
    response_model=SelectionPlanDetailResponse,
    status_code=status.HTTP_201_CREATED,
)
async def clone_selection_plan(
    plan_id: uuid.UUID,
    request: SelectionPlanCloneRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanDetailResponse:
    """克隆选股方案 - 复制方案到新方案（含当前 revision 的成员与条件）。"""
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 加载当前 revision
    stmt = select(SelectionPlanRevision).where(
        SelectionPlanRevision.selection_plan_id == plan.id,
        SelectionPlanRevision.revision == plan.current_revision,
    )
    result = await db.execute(stmt)
    current_revision = result.scalar_one_or_none()
    if current_revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"当前版本不存在: revision={plan.current_revision}",
        )
    current_revision_loaded = await _load_revision_with_relations(
        db, current_revision.id
    )
    if current_revision_loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本加载失败: revision_id={current_revision.id}",
        )

    # 创建新方案
    new_plan = SelectionPlan(
        user_id=current_user.id,
        name=request.name,
        description=request.description or plan.description,
        status="draft",
        current_revision=1,
    )
    db.add(new_plan)
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"克隆方案主表失败: {exc}",
        ) from exc

    # 复制 revision + members + conditions
    new_revision = SelectionPlanRevision(
        selection_plan_id=new_plan.id,
        revision=1,
        operator=current_revision_loaded.operator,
        missing_member_policy=current_revision_loaded.missing_member_policy,
        universe=current_revision_loaded.universe,
        sort_spec=current_revision_loaded.sort_spec,
        notification_config=current_revision_loaded.notification_config,
        created_by=current_user.id,
    )
    db.add(new_revision)
    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"克隆版本失败: {exc}",
        ) from exc

    for m in sorted(current_revision_loaded.members, key=lambda x: x.position):
        new_member = SelectionPlanMember(
            revision_id=new_revision.id,
            strategy_definition_id=m.strategy_definition_id,
            strategy_version_id=m.strategy_version_id,
            version_policy=m.version_policy,
            position=m.position,
            enabled=m.enabled,
            params=m.params,
        )
        db.add(new_member)
        await db.flush()
        for c in sorted(m.conditions, key=lambda x: x.position):
            db.add(SelectionMemberCondition(
                member_id=new_member.id,
                position=c.position,
                metric_key=c.metric_key,
                operator=c.operator,
                value1=c.value1,
                value2=c.value2,
            ))

    try:
        await db.flush()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"克隆成员/条件失败: {exc}",
        ) from exc

    await db.commit()
    await db.refresh(new_plan)

    revision_loaded = await _load_revision_with_relations(db, new_revision.id)
    revision_resp = (
        _revision_to_response(revision_loaded) if revision_loaded else None
    )

    return SelectionPlanDetailResponse(
        id=new_plan.id,
        user_id=new_plan.user_id,
        name=new_plan.name,
        description=new_plan.description,
        status=new_plan.status,
        current_revision=new_plan.current_revision,
        created_at=new_plan.created_at,
        updated_at=new_plan.updated_at,
        current_revision_data=revision_resp,
    )


# ============================================================
# C4 运行/结果端点（validate/preview/run/results）
# ============================================================


@router.post(
    "/selection-plans/{plan_id}/validate",
    response_model=SelectionPlanValidateResponse,
)
async def validate_selection_plan(
    plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanValidateResponse:
    """验证选股方案 - 校验当前 revision 符合 schema + 语义规则。"""
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 加载当前 revision
    stmt = select(SelectionPlanRevision).where(
        SelectionPlanRevision.selection_plan_id == plan.id,
        SelectionPlanRevision.revision == plan.current_revision,
    )
    result = await db.execute(stmt)
    revision = result.scalar_one_or_none()
    if revision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"当前版本不存在: revision={plan.current_revision}",
        )
    revision_loaded = await _load_revision_with_relations(db, revision.id)
    if revision_loaded is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"版本加载失败: revision_id={revision.id}",
        )

    # 构建 plan_dict 用于校验
    plan_dict = _build_plan_dict_from_revision(plan, revision_loaded)
    try:
        validate_plan(plan_dict)
        return SelectionPlanValidateResponse(valid=True, errors=[])
    except SelectionPlanValidationError as e:
        return SelectionPlanValidateResponse(valid=False, errors=e.errors)


def _build_plan_dict_from_revision(
    plan: SelectionPlan, revision: SelectionPlanRevision
) -> dict:
    """从 ORM 对象构建 plan_dict 用于校验。"""
    members = []
    for m in sorted(revision.members, key=lambda x: x.position):
        # 反查 strategy_key（通过 definition_id）
        members.append({
            "strategy_key": "",  # 校验时 strategy_key 非空即可，实际值由 definition 决定
            "version_policy": m.version_policy,
            "strategy_version": None,
            "params": m.params,
            "conditions": [
                {
                    "metric_key": c.metric_key,
                    "operator": c.operator,
                    "value": c.value1,
                    **({"value2": c.value2} if c.value2 is not None else {}),
                }
                for c in sorted(m.conditions, key=lambda x: x.position)
            ],
            "enabled": m.enabled,
            "position": m.position,
        })
    return {
        "name": plan.name,
        "description": plan.description or "",
        "operator": revision.operator,
        "missing_member_policy": revision.missing_member_policy,
        "universe": revision.universe,
        "sort_spec": revision.sort_spec,
        "notification": revision.notification_config,
        "members": members,
    }


@router.post(
    "/selection-plans/{plan_id}/preview",
    response_model=SelectionPlanPreviewResponse,
)
async def preview_selection_plan(
    plan_id: uuid.UUID,
    request: SelectionPlanPreviewRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanPreviewResponse:
    """预览选股方案结果（不落库）。

    返回数量、样本（最多 20 条）和成员命中统计。
    """
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 延迟导入避免循环依赖
    from app.services.selection_run_service import preview_selection_plan as preview_svc

    try:
        return await preview_svc(
            db,
            plan_id=plan.id,
            trade_date=request.trade_date,
            revision_id=request.revision_id,
            user_id=current_user.id,
        )
    except SelectionPlanValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e


@router.post(
    "/selection-plans/{plan_id}/run",
    response_model=SelectionPlanRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def run_selection_plan(
    plan_id: uuid.UUID,
    request: SelectionPlanRunRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanRunResponse:
    """执行选股方案（幂等）。

    幂等键 = hash(revision_id + trade_date + trigger_kind + input_run_set_hash)。
    相同幂等键的运行不重复执行，直接返回已有运行记录。
    """
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    # 延迟导入避免循环依赖
    from app.services.selection_run_service import run_selection_plan as run_svc

    try:
        run = await run_svc(
            db,
            plan_id=plan.id,
            trade_date=request.trade_date,
            trigger_kind=request.trigger_kind,
            user_id=current_user.id,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e
    except SelectionPlanValidationError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        ) from e

    await db.commit()
    return SelectionPlanRunResponse.model_validate(run)


@router.get(
    "/selection-plans/{plan_id}/runs",
    response_model=SelectionPlanRunListResponse,
)
async def list_selection_plan_runs(
    plan_id: uuid.UUID,
    status_filter: str | None = Query(None, alias="status", description="运行状态过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanRunListResponse:
    """查询方案的运行历史。"""
    plan = await _get_plan_owned_by_user(db, plan_id, current_user.id)

    stmt = select(SelectionPlanRun).where(SelectionPlanRun.selection_plan_id == plan.id)
    count_stmt = select(SelectionPlanRun).where(
        SelectionPlanRun.selection_plan_id == plan.id
    )

    if status_filter is not None:
        stmt = stmt.where(SelectionPlanRun.status == status_filter)
        count_stmt = count_stmt.where(SelectionPlanRun.status == status_filter)

    from sqlalchemy import func as sa_func

    count_result = await db.execute(
        select(sa_func.count()).select_from(count_stmt.subquery())
    )
    total = int(count_result.scalar() or 0)

    stmt = stmt.order_by(SelectionPlanRun.started_at.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    items = result.scalars().all()

    return SelectionPlanRunListResponse(
        items=[SelectionPlanRunResponse.model_validate(r) for r in items],
        total=total,
    )


@router.get(
    "/selection-plan-runs/{run_id}/results",
    response_model=SelectionPlanResultListResponse,
)
async def list_run_results(
    run_id: uuid.UUID,
    matched_only: bool = Query(False, description="只返回 matched=True 的结果"),
    limit: int = Query(100, ge=1, le=500, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> SelectionPlanResultListResponse:
    """查询运行结果（分页）。

    支持按 matched 过滤，按 rank_value 排序。
    """
    # 校验运行存在且属于当前用户
    stmt = select(SelectionPlanRun).where(SelectionPlanRun.id == run_id)
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"运行不存在: run_id={run_id}",
        )
    if run.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"运行不存在或无权访问: run_id={run_id}",
        )

    # 查询结果
    result_stmt = select(SelectionPlanResult).where(
        SelectionPlanResult.plan_run_id == run_id
    )
    count_stmt = select(SelectionPlanResult).where(
        SelectionPlanResult.plan_run_id == run_id
    )

    if matched_only:
        result_stmt = result_stmt.where(SelectionPlanResult.matched.is_(True))
        count_stmt = count_stmt.where(SelectionPlanResult.matched.is_(True))

    from sqlalchemy import func as sa_func

    count_result = await db.execute(
        select(sa_func.count()).select_from(count_stmt.subquery())
    )
    total = int(count_result.scalar() or 0)

    # 按 rank_value 升序排序（nulls last）
    result_stmt = result_stmt.order_by(
        SelectionPlanResult.rank_value.asc().nullslast()
    ).limit(limit).offset(offset)
    result = await db.execute(result_stmt)
    items = result.scalars().all()

    page = offset // limit + 1 if limit > 0 else 1
    return SelectionPlanResultListResponse(
        items=[SelectionPlanResultResponse.model_validate(r) for r in items],
        total=total,
        page=page,
        page_size=limit,
    )


@router.get(
    "/selection-plan-runs/{run_id}/member-results/{member_id}",
    response_model=list[SelectionResultEvidenceResponse],
)
async def list_member_results(
    run_id: uuid.UUID,
    member_id: uuid.UUID,
    matched_only: bool = Query(False, description="只返回 matched=True 的证据"),
    limit: int = Query(100, ge=1, le=500, description="返回上限"),
    offset: int = Query(0, ge=0, description="偏移量"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[SelectionResultEvidenceResponse]:
    """查询成员级结果（证据链）- 指定成员在指定运行中的所有证据。

    返回该成员对该运行中每个标的的命中证据。
    """
    # 校验运行存在且属于当前用户
    stmt = select(SelectionPlanRun).where(SelectionPlanRun.id == run_id)
    result = await db.execute(stmt)
    run = result.scalar_one_or_none()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"运行不存在: run_id={run_id}",
        )
    if run.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"运行不存在或无权访问: run_id={run_id}",
        )

    # 查询该运行的所有结果 ID
    result_ids_stmt = select(SelectionPlanResult.id).where(
        SelectionPlanResult.plan_run_id == run_id
    )
    if matched_only:
        result_ids_stmt = result_ids_stmt.where(SelectionPlanResult.matched.is_(True))
    result_ids_result = await db.execute(result_ids_stmt)
    result_ids = [row[0] for row in result_ids_result.all()]

    if not result_ids:
        return []

    # 查询该成员在这些结果上的证据
    evidence_stmt = (
        select(SelectionResultEvidence)
        .where(
            SelectionResultEvidence.selection_result_id.in_(result_ids),
            SelectionResultEvidence.member_id == member_id,
        )
        .order_by(SelectionResultEvidence.selection_result_id)
        .limit(limit)
        .offset(offset)
    )
    evidence_result = await db.execute(evidence_stmt)
    evidences = evidence_result.scalars().all()

    return [SelectionResultEvidenceResponse.model_validate(e) for e in evidences]


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/selection-plans" in paths
    assert any("/selection-plans/{plan_id}" in p for p in paths)
    assert any("/clone" in p for p in paths)
    assert any("/validate" in p for p in paths)
    assert any("/preview" in p for p in paths)
    assert any("/run" in p for p in paths)
    assert any("/runs" in p for p in paths)
    assert any("/results" in p for p in paths)
    assert any("/member-results" in p for p in paths)
    print("OK")
