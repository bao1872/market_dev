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

from datetime import UTC, datetime, time as dt_time
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import WATCHLIST_MONITOR
from app.core.deps import get_current_active_user
from app.db import get_db
from app.services.calendar_service import is_trading_day_async
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
    MonitorState。无监控状态时 monitor_status=WAITING_FIRST_RUN, metrics=null。

    monitor_status 枚举：
    - WAITING_FIRST_RUN: 无 MonitorState
    - SUCCEEDED: evaluation_status=SUCCEEDED
    - FAILED: evaluation_status=FAILED
    - STALE: 交易时段内 updated_at 超过 30 分钟
    - MARKET_CLOSED: 非交易时段
    """
    # 0. 判断当前是否交易时段
    from datetime import date as dt_date

    today = dt_date.today()
    is_trading_day = await is_trading_day_async(db, today)
    now_cst = datetime.now(ZoneInfo("Asia/Shanghai"))
    is_trading_hours = False
    if is_trading_day:
        current_time = now_cst.time()
        morning_session = dt_time(9, 30) <= current_time <= dt_time(11, 30)
        afternoon_session = dt_time(13, 0) <= current_time <= dt_time(15, 0)
        is_trading_hours = morning_session or afternoon_session

    # 1. 查找 watchlist_monitor 策略的最新 released 版本 ID
    latest_version_stmt = (
        select(StrategyVersion.id)
        .join(StrategyDefinition, StrategyVersion.strategy_definition_id == StrategyDefinition.id)
        .where(
            StrategyDefinition.strategy_key == WATCHLIST_MONITOR,
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
    STALE_THRESHOLD_MINUTES = 30
    response_items: list[WatchlistMonitorStatusItem] = []
    for watchlist_item, instrument in rows:
        ms = monitor_states_map.get(instrument.id)
        if ms is not None:
            payload = ms.payload
            evaluation_status = payload.get("evaluation_status")
            error_code = payload.get("evaluation_error") or payload.get("error_code")
            source_bar_time = ms.bar_time

            # 计算 monitor_status
            if not is_trading_hours:
                monitor_status = "MARKET_CLOSED"
            elif evaluation_status == "SUCCEEDED":
                # 检查是否 STALE
                if ms.updated_at:
                    age_minutes = (now_cst - ms.updated_at.astimezone(ZoneInfo("Asia/Shanghai"))).total_seconds() / 60
                    if age_minutes > STALE_THRESHOLD_MINUTES:
                        monitor_status = "STALE"
                    else:
                        monitor_status = "SUCCEEDED"
                else:
                    monitor_status = "SUCCEEDED"
            elif evaluation_status == "FAILED":
                monitor_status = "FAILED"
            else:
                # PENDING 或其他状态，按 STALE 逻辑判断
                if ms.updated_at:
                    age_minutes = (now_cst - ms.updated_at.astimezone(ZoneInfo("Asia/Shanghai"))).total_seconds() / 60
                    if age_minutes > STALE_THRESHOLD_MINUTES:
                        monitor_status = "STALE"
                    else:
                        monitor_status = "SUCCEEDED"
                else:
                    monitor_status = "SUCCEEDED"

            response_items.append(
                WatchlistMonitorStatusItem(
                    watchlist_item_id=watchlist_item.id,
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    name=instrument.name,
                    market=instrument.market,
                    watchlist_created_at=watchlist_item.created_at,
                    monitor_status=monitor_status,
                    evaluation_status=evaluation_status,
                    error_code=error_code,
                    source_bar_time=str(source_bar_time) if source_bar_time else None,
                    metrics=payload,
                    updated_at=ms.updated_at,
                )
            )
        else:
            # 无 MonitorState
            monitor_status = "MARKET_CLOSED" if not is_trading_hours else "WAITING_FIRST_RUN"
            response_items.append(
                WatchlistMonitorStatusItem(
                    watchlist_item_id=watchlist_item.id,
                    instrument_id=instrument.id,
                    symbol=instrument.symbol,
                    name=instrument.name,
                    market=instrument.market,
                    watchlist_created_at=watchlist_item.created_at,
                    monitor_status=monitor_status,
                    evaluation_status=None,
                    error_code=None,
                    source_bar_time=None,
                    metrics=None,
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
