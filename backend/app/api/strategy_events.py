"""StrategyEvent API 路由 - 策略事件查询（M4）。

端点：
- GET /instruments/{id}/events: 查询某股票的事件
- GET /strategies/{key}/events: 查询某策略的事件（支持 version 过滤）
- GET /strategy-events/{event_id}: 事件详情（含 snapshot）

设计说明：
- /strategies/{key} 路径需将 strategy_key 解析为 strategy_version_id 列表。
- 列表查询返回不含 snapshot（减少负载），详情查询返回含 snapshot。
- 支持 event_type / start_time / end_time 过滤。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.core.route_utils import get_route_paths
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.repositories.strategy_event_repository import get_event, query_events
from app.schemas.strategy_event import (
    StrategyEventDetailResponse,
    StrategyEventListResponse,
    StrategyEventResponse,
)

router = APIRouter(tags=["strategy-events"])


async def _resolve_strategy_version_ids(
    db: AsyncSession,
    strategy_key: str,
    version: str | None = None,
) -> list[UUID]:
    """将 strategy_key 解析为 strategy_version_id 列表。

    Args:
        db: 异步会话
        strategy_key: 策略唯一标识
        version: 可选版本号过滤

    Returns:
        strategy_version_id 列表

    Raises:
        HTTPException 404: 策略不存在
    """
    stmt_def = select(StrategyDefinition).where(
        StrategyDefinition.strategy_key == strategy_key
    )
    result_def = await db.execute(stmt_def)
    definition = result_def.scalar_one_or_none()
    if definition is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略不存在: strategy_key={strategy_key}",
        )

    stmt_ver = select(StrategyVersion.id).where(
        StrategyVersion.strategy_definition_id == definition.id
    )
    if version is not None:
        stmt_ver = stmt_ver.where(StrategyVersion.version == version)
    result_ver = await db.execute(stmt_ver)
    version_ids = [row[0] for row in result_ver.all()]

    if version is not None and not version_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略版本不存在: strategy_key={strategy_key}, version={version}",
        )
    return version_ids


@router.get(
    "/instruments/{instrument_id}/events",
    response_model=StrategyEventListResponse,
)
async def get_instrument_events(
    instrument_id: UUID,
    event_type: str | None = Query(None, description="按事件类型过滤"),
    start_time: datetime | None = Query(None, description="事件时间 >= start_time"),
    end_time: datetime | None = Query(None, description="事件时间 <= end_time"),
    limit: int = Query(100, ge=1, le=500, description="最大返回数"),
    db: AsyncSession = Depends(get_db),
) -> StrategyEventListResponse:
    """查询某股票的策略事件。"""
    events = await query_events(
        db,
        instrument_id=instrument_id,
        event_type=event_type,
        start_time=start_time,
        end_time=end_time,
        limit=limit,
    )
    items = [StrategyEventResponse.model_validate(e) for e in events]
    return StrategyEventListResponse(items=items, total=len(items))


@router.get(
    "/strategies/{strategy_key}/events",
    response_model=StrategyEventListResponse,
)
async def get_strategy_events(
    strategy_key: str,
    version: str | None = Query(None, description="按版本号过滤"),
    event_type: str | None = Query(None, description="按事件类型过滤"),
    start_time: datetime | None = Query(None, description="事件时间 >= start_time"),
    end_time: datetime | None = Query(None, description="事件时间 <= end_time"),
    limit: int = Query(100, ge=1, le=500, description="最大返回数"),
    db: AsyncSession = Depends(get_db),
) -> StrategyEventListResponse:
    """查询某策略的事件（支持 version/event_type/时间范围过滤）。"""
    version_ids = await _resolve_strategy_version_ids(db, strategy_key, version)

    items: list[StrategyEventResponse] = []
    for vid in version_ids:
        events = await query_events(
            db,
            strategy_version_id=vid,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        items.extend(StrategyEventResponse.model_validate(e) for e in events)
    return StrategyEventListResponse(items=items, total=len(items))


@router.get(
    "/strategy-events/{event_id}",
    response_model=StrategyEventDetailResponse,
)
async def get_strategy_event_detail(
    event_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> StrategyEventDetailResponse:
    """查询事件详情（含 snapshot 快照）。"""
    event = await get_event(db, event_id)
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"事件不存在: event_id={event_id}",
        )
    return StrategyEventDetailResponse.model_validate(event)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = get_route_paths(router.routes)
    print(f"router.routes={paths}")
    assert any("/events" in p for p in paths)
    assert any("/strategy-events/" in p for p in paths)
    print("OK")
