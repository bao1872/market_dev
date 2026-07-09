"""用户表格视图配置 API 路由 - /me/table-view-presets。

端点：
- GET /me/table-view-presets: 查询当前用户的 preset 列表（按 table_id + strategy_key 过滤）
- POST /me/table-view-presets: 创建 preset
- PATCH /me/table-view-presets/{id}: 更新 preset（name/config/is_default）
- DELETE /me/table-view-presets/{id}: 删除 preset

权限：
- require_active_subscription: 需有效订阅（admin 豁免）
- require_feature("trend_selection"): 需具备趋势选股功能（admin 豁免）
- 与 /strategies/{key}/published-runs 权限矩阵一致

业务规则：
- user_id 由 JWT 上下文注入，不接受 body 传入
- (user_id, table_id, strategy_key, name) 唯一约束
- 每 user+table_id+strategy_key 最多 20 个 preset（quota）
- is_default 同维度至多 1 个 true（设置新默认时旧默认自动取消）
- config 仅允许 keyword/sort/filters/hiddenColumns/pageSize（由 schema 强制）
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models.table_view_preset import UserTableViewPreset
from app.schemas.table_view_preset import (
    MAX_PRESETS_PER_SCOPE,
    TableViewPresetCreate,
    TableViewPresetListResponse,
    TableViewPresetPatch,
    TableViewPresetResponse,
)
from app.services.access_control_service import (
    AccessContext,
    require_active_subscription,
    require_feature,
)

router = APIRouter(tags=["me"])


@router.get(
    "/me/table-view-presets",
    response_model=TableViewPresetListResponse,
)
async def list_my_table_view_presets(
    table_id: str = Query(..., min_length=1, max_length=64, description="表格标识"),
    strategy_key: str | None = Query(
        default=None, max_length=64, description="策略 key（可选过滤）"
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
    _feat: AccessContext = Depends(require_feature("trend_selection")),
) -> TableViewPresetListResponse:
    """查询当前用户的 preset 列表（按 table_id + strategy_key 过滤）。

    权限：
    - require_active_subscription: 需有效订阅（admin 豁免）
    - require_feature("trend_selection"): 需具备趋势选股功能（admin 豁免）

    Args:
        table_id: 表格标识（必填）
        strategy_key: 策略 key（可选，传则按精确匹配过滤）
        db: 异步数据库会话
        ctx: 权限上下文（由 require_active_subscription 注入）

    Returns:
        preset 列表响应（按 created_at 升序）
    """
    user_id = uuid.UUID(ctx.user_id)
    stmt = (
        select(UserTableViewPreset)
        .where(
            UserTableViewPreset.user_id == user_id,
            UserTableViewPreset.table_id == table_id,
        )
        .order_by(UserTableViewPreset.created_at.asc())
    )
    if strategy_key is not None:
        # [PresetQuery] - 描述: strategy_key 精确匹配（NULL 不等于 NULL，需显式处理）
        if strategy_key == "":
            stmt = stmt.where(UserTableViewPreset.strategy_key.is_(None))
        else:
            stmt = stmt.where(UserTableViewPreset.strategy_key == strategy_key)

    result = await db.execute(stmt)
    presets = list(result.scalars().all())
    items = [TableViewPresetResponse.model_validate(p) for p in presets]
    return TableViewPresetListResponse(items=items, total=len(items))


@router.post(
    "/me/table-view-presets",
    response_model=TableViewPresetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_my_table_view_preset(
    payload: TableViewPresetCreate,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
    _feat: AccessContext = Depends(require_feature("trend_selection")),
) -> TableViewPresetResponse:
    """创建 preset。

    权限：
    - require_active_subscription + require_feature("trend_selection")（admin 豁免）

    业务规则：
    - user_id 由 JWT 上下文注入，body 中 user_id 字段被忽略
    - (user_id, table_id, strategy_key, name) 唯一约束，重复返回 409
    - 每 user+table_id+strategy_key 最多 20 个，超额返回 422
    - is_default=True 时自动取消同维度其他默认

    Args:
        payload: 创建请求（不含 user_id）
        db: 异步数据库会话
        ctx: 权限上下文

    Returns:
        创建的 preset 响应

    Raises:
        HTTPException 409: 名称重复
        HTTPException 422: 超出 quota 上限
    """
    user_id = uuid.UUID(ctx.user_id)

    # [QuotaCheck] - 描述: 检查同维度 preset 数量是否达到上限
    count_stmt = (
        select(func.count(UserTableViewPreset.id))
        .where(
            UserTableViewPreset.user_id == user_id,
            UserTableViewPreset.table_id == payload.table_id,
        )
    )
    if payload.strategy_key is None:
        count_stmt = count_stmt.where(UserTableViewPreset.strategy_key.is_(None))
    else:
        count_stmt = count_stmt.where(
            UserTableViewPreset.strategy_key == payload.strategy_key
        )

    count_result = await db.execute(count_stmt)
    current_count = count_result.scalar_one()
    if current_count >= MAX_PRESETS_PER_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"已达 preset 上限：每 user+table_id+strategy_key 最多 "
                f"{MAX_PRESETS_PER_SCOPE} 个"
            ),
        )

    # [DefaultMutex] - 描述: is_default=True 时先取消同维度旧默认
    if payload.is_default:
        await _unset_default_for_scope(
            db,
            user_id=user_id,
            table_id=payload.table_id,
            strategy_key=payload.strategy_key,
        )

    preset = UserTableViewPreset(
        user_id=user_id,
        table_id=payload.table_id,
        strategy_key=payload.strategy_key,
        name=payload.name,
        config=payload.config,
        is_default=payload.is_default,
    )
    db.add(preset)
    try:
        await db.flush()
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # [UniqueConstraint] - 描述: partial unique index 冲突（strategy_key NULL/非NULL 两个索引）
        err_str = str(e)
        if (
            "uq_user_table_view_preset_strategy_not_null" in err_str
            or "uq_user_table_view_preset_strategy_null" in err_str
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"同维度下已存在同名 preset: name={payload.name!r}, "
                    f"table_id={payload.table_id!r}, strategy_key={payload.strategy_key!r}"
                ),
            ) from e
        raise

    await db.refresh(preset)
    return TableViewPresetResponse.model_validate(preset)


@router.patch(
    "/me/table-view-presets/{preset_id}",
    response_model=TableViewPresetResponse,
)
async def update_my_table_view_preset(
    preset_id: uuid.UUID,
    payload: TableViewPresetPatch,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
    _feat: AccessContext = Depends(require_feature("trend_selection")),
) -> TableViewPresetResponse:
    """更新 preset（name/config/is_default，user_id/table_id/strategy_key 不可改）。

    权限：
    - require_active_subscription + require_feature("trend_selection")（admin 豁免）

    业务规则：
    - 只能操作自己的 preset，他人 preset 返回 404（避免泄露存在性）
    - 重命名时若新 name 与同维度其他 preset 冲突，返回 409
    - is_default=True 时自动取消同维度其他默认

    Args:
        preset_id: preset ID
        payload: 更新请求（至少一个字段）
        db: 异步数据库会话
        ctx: 权限上下文

    Returns:
        更新后的 preset 响应

    Raises:
        HTTPException 404: preset 不存在或不属于当前用户
        HTTPException 409: 重命名冲突
    """
    user_id = uuid.UUID(ctx.user_id)

    # 查询当前用户的 preset（user_id 隔离）
    stmt = select(UserTableViewPreset).where(
        UserTableViewPreset.id == preset_id,
        UserTableViewPreset.user_id == user_id,
    )
    result = await db.execute(stmt)
    preset = result.scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="preset 不存在或不属于当前用户",
        )

    # 更新字段
    if payload.name is not None:
        preset.name = payload.name
    if payload.config is not None:
        preset.config = payload.config
    if payload.is_default is not None:
        if payload.is_default:
            # [DefaultMutex] - 描述: 设置新默认前取消同维度旧默认（排除自身）
            await _unset_default_for_scope(
                db,
                user_id=user_id,
                table_id=preset.table_id,
                strategy_key=preset.strategy_key,
                exclude_id=preset.id,
            )
        preset.is_default = payload.is_default

    try:
        await db.flush()
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        # [UniqueConstraint] - 描述: partial unique index 冲突（strategy_key NULL/非NULL 两个索引）
        err_str = str(e)
        if (
            "uq_user_table_view_preset_strategy_not_null" in err_str
            or "uq_user_table_view_preset_strategy_null" in err_str
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"同维度下已存在同名 preset: name={payload.name!r}",
            ) from e
        raise

    await db.refresh(preset)
    return TableViewPresetResponse.model_validate(preset)


@router.delete(
    "/me/table-view-presets/{preset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_my_table_view_preset(
    preset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
    _feat: AccessContext = Depends(require_feature("trend_selection")),
) -> None:
    """删除 preset。

    权限：
    - require_active_subscription + require_feature("trend_selection")（admin 豁免）

    业务规则：
    - 只能删除自己的 preset，他人 preset 返回 404

    Args:
        preset_id: preset ID
        db: 异步数据库会话
        ctx: 权限上下文

    Raises:
        HTTPException 404: preset 不存在或不属于当前用户
    """
    user_id = uuid.UUID(ctx.user_id)

    stmt = select(UserTableViewPreset).where(
        UserTableViewPreset.id == preset_id,
        UserTableViewPreset.user_id == user_id,
    )
    result = await db.execute(stmt)
    preset = result.scalar_one_or_none()
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="preset 不存在或不属于当前用户",
        )

    try:
        await db.delete(preset)
        await db.flush()
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def _unset_default_for_scope(
    db: AsyncSession,
    user_id: uuid.UUID,
    table_id: str,
    strategy_key: str | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    """取消同维度其他 preset 的 is_default。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        table_id: 表格标识
        strategy_key: 策略 key（可空）
        exclude_id: 排除的 preset ID（更新场景下排除自身）
    """
    stmt = (
        update(UserTableViewPreset)
        .where(
            UserTableViewPreset.user_id == user_id,
            UserTableViewPreset.table_id == table_id,
            UserTableViewPreset.is_default.is_(True),
        )
        .values(is_default=False)
    )
    if strategy_key is None:
        stmt = stmt.where(UserTableViewPreset.strategy_key.is_(None))
    else:
        stmt = stmt.where(UserTableViewPreset.strategy_key == strategy_key)

    if exclude_id is not None:
        stmt = stmt.where(UserTableViewPreset.id != exclude_id)

    await db.execute(stmt)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [getattr(r, "path", None) for r in router.routes]
    paths = [p for p in paths if p is not None]
    print(f"router.routes={paths}")
    assert "/me/table-view-presets" in paths
    assert "/me/table-view-presets/{preset_id}" in paths
    methods = []
    for r in router.routes:
        if hasattr(r, "methods"):
            path = getattr(r, "path", "")
            methods.append((path, sorted(r.methods)))  # type: ignore[attr-defined]
    print(f"methods={methods}")
    print("OK")
