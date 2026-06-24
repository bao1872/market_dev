"""用户自选股 API 路由（W1）。

端点：
- GET /watchlist: 当前用户自选列表（user_id 由认证上下文注入）
- POST /watchlist: 加入自选（instrument_id，user_id 由认证上下文注入）
- DELETE /watchlist/{instrument_id}: 移除自选（软删除：active=false + removed_at）
- GET /watchlist/monitor-status: 自选股+监控状态聚合查询

设计说明：
- user_id 由 get_current_active_user 注入，不接受请求体传入（V1.1 安全约束）
- 加入自选即参与当前启用的监控方案（universe_service 聚合 active=true 记录）
- 移除采用软删除（active=false + removed_at），保留历史，支持重新加入
- (user_id, instrument_id) 唯一约束：重复加入返回 409 Conflict
- 重新加入已软删除的记录：恢复 active=true 并清空 removed_at
- monitor-status 端点 JOIN Instrument + MonitorState(最新 released watchlist_monitor 版本)
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.deps import get_current_active_user
from app.db import get_db
from app.models.instrument import Instrument
from app.models.monitor_state import MonitorState
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.schemas.watchlist import (
    WatchlistAddRequest,
    WatchlistItemResponse,
    WatchlistListResponse,
    WatchlistMonitorStatusItem,
    WatchlistMonitorStatusResponse,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@router.get("", response_model=WatchlistListResponse)
async def list_watchlist(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchlistListResponse:
    """查询当前用户的自选列表（仅 active=true）。

    user_id 由认证上下文注入，不接受查询参数传入。
    """
    stmt = (
        select(UserWatchlistItem)
        .where(
            UserWatchlistItem.user_id == current_user.id,
            UserWatchlistItem.active.is_(True),
        )
        .order_by(UserWatchlistItem.created_at.desc())
    )
    result = await db.execute(stmt)
    items = result.scalars().all()
    return WatchlistListResponse(
        items=[WatchlistItemResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.post("", response_model=WatchlistItemResponse, status_code=status.HTTP_201_CREATED)
async def add_to_watchlist(
    payload: WatchlistAddRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchlistItemResponse:
    """加入自选。

    user_id 由认证上下文注入（不接受 body 中的 user_id）。
    若已存在软删除记录，则恢复 active=true 并清空 removed_at（重新加入）。
    若已存在 active 记录，返回 409 Conflict。
    """
    # 校验股票存在
    inst_stmt = select(Instrument).where(Instrument.id == payload.instrument_id)
    inst_result = await db.execute(inst_stmt)
    if inst_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 instrument_id={payload.instrument_id} 的股票",
        )

    # 查询是否已有记录（含软删除）
    stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == current_user.id,
        UserWatchlistItem.instrument_id == payload.instrument_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该股票已在自选列表中",
            )
        # 恢复软删除记录：重新加入
        existing.active = True
        existing.removed_at = None
        existing.source = payload.source
        await db.commit()
        await db.refresh(existing)
        return WatchlistItemResponse.model_validate(existing)

    # 新建自选记录
    item = UserWatchlistItem(
        user_id=current_user.id,
        instrument_id=payload.instrument_id,
        source=payload.source,
        active=True,
    )
    db.add(item)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        # 唯一约束冲突兜底（并发场景）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"加入自选失败（可能已存在）：{e}",
        ) from e
    await db.refresh(item)
    return WatchlistItemResponse.model_validate(item)


@router.get("/monitor-status", response_model=WatchlistMonitorStatusResponse)
async def get_watchlist_monitor_status(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> WatchlistMonitorStatusResponse:
    """查询当前用户自选股+监控状态聚合数据。

    返回当前用户所有 active 自选股，附带最新 released watchlist_monitor 版本的
    MonitorState。无监控状态时 has_monitor_state=false, monitor_state=null。
    """
    # 1. 查找 watchlist_monitor 策略的最新 released 版本 ID
    latest_version_stmt = (
        select(StrategyVersion.id)
        .join(StrategyDefinition, StrategyVersion.strategy_definition_id == StrategyDefinition.id)
        .where(
            StrategyDefinition.strategy_key == "watchlist_monitor",
            StrategyVersion.status == "released",
        )
        .order_by(StrategyVersion.released_at.desc())
        .limit(1)
    )
    ver_result = await db.execute(latest_version_stmt)
    monitor_version_id = ver_result.scalar_one_or_none()

    # 2. 查询用户 active 自选股 + Instrument 信息
    items_stmt = (
        select(UserWatchlistItem, Instrument)
        .join(Instrument, UserWatchlistItem.instrument_id == Instrument.id)
        .where(
            UserWatchlistItem.user_id == current_user.id,
            UserWatchlistItem.active.is_(True),
        )
        .order_by(UserWatchlistItem.created_at.desc())
    )
    items_result = await db.execute(items_stmt)
    rows = items_result.all()

    # 3. 若有 released 版本，批量查询所有相关 MonitorState
    monitor_states_map: dict[UUID, MonitorState] = {}
    if monitor_version_id is not None and rows:
        instrument_ids = [row[1].id for row in rows]
        states_stmt = (
            select(MonitorState)
            .where(
                MonitorState.strategy_version_id == monitor_version_id,
                MonitorState.instrument_id.in_(instrument_ids),
            )
        )
        states_result = await db.execute(states_stmt)
        for state in states_result.scalars():
            monitor_states_map[state.instrument_id] = state

    # 4. 组装响应
    response_items: list[WatchlistMonitorStatusItem] = []
    for _watchlist_item, instrument in rows:
        ms = monitor_states_map.get(instrument.id)
        if ms is not None:
            payload = ms.payload
            evaluation_status = payload.get("evaluation_status")
            evaluation_error = payload.get("evaluation_error")
            response_items.append(
                WatchlistMonitorStatusItem(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    name=instrument.name,
                    market=instrument.market,
                    has_monitor_state=True,
                    monitor_state=payload,
                    evaluation_status=evaluation_status,
                    evaluation_error=evaluation_error,
                    updated_at=ms.updated_at,
                )
            )
        else:
            response_items.append(
                WatchlistMonitorStatusItem(
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    name=instrument.name,
                    market=instrument.market,
                    has_monitor_state=False,
                    monitor_state=None,
                    evaluation_status=None,
                    evaluation_error=None,
                    updated_at=None,
                )
            )

    return WatchlistMonitorStatusResponse(items=response_items)


@router.delete("/{instrument_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_from_watchlist(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """移除自选（软删除：active=false + removed_at）。

    user_id 由认证上下文注入。
    不存在或已移除返回 404。
    """
    stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == current_user.id,
        UserWatchlistItem.instrument_id == instrument_id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(stmt)
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="自选记录不存在或已移除",
        )
    item.active = False
    item.removed_at = datetime.now(UTC)
    await db.commit()


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
