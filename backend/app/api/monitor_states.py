"""MonitorState API 路由 - 监控状态查询（M3）。

端点：
- GET /instruments/{id}/monitor-states: 查询某股票的所有监控策略状态
- GET /strategies/{key}/monitor-states: 查询某策略的所有股票状态（支持 version 过滤）

设计说明：
- /strategies/{key} 路径需将 strategy_key 解析为 strategy_version_id 列表
  （一个 strategy_key 对应一个 StrategyDefinition，下挂多个 StrategyVersion）。
- 支持 ?version= 过滤特定版本。
- 禁异常吞没：仓储异常由 FastAPI 异常处理器捕获。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.repositories.monitor_state_repository import (
    list_states_by_instrument,
    list_states_by_strategy_version,
)
from app.schemas.monitor_state import (
    MonitorStateListResponse,
    MonitorStateResponse,
)

router = APIRouter(tags=["monitor-states"])


async def _resolve_strategy_version_ids(
    db: AsyncSession,
    strategy_key: str,
    version: str | None = None,
) -> list[UUID]:
    """将 strategy_key 解析为 strategy_version_id 列表（仅 released 版本）。

    仅返回 released 状态的版本，且同一股票不重复（按 released_at 降序取最新）。

    Args:
        db: 异步会话
        strategy_key: 策略唯一标识
        version: 可选版本号过滤

    Returns:
        strategy_version_id 列表（仅 released 版本）

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
        StrategyVersion.strategy_definition_id == definition.id,
        StrategyVersion.status == "released",
    )
    if version is not None:
        stmt_ver = stmt_ver.where(StrategyVersion.version == version)
    # [监控状态] - 仅返回最新 released 版本，避免同一股票跨版本重复
    stmt_ver = stmt_ver.order_by(StrategyVersion.released_at.desc()).limit(1)
    result_ver = await db.execute(stmt_ver)
    version_ids = [row[0] for row in result_ver.all()]

    if version is not None and not version_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"策略版本不存在或未发布: strategy_key={strategy_key}, version={version}",
        )
    return version_ids


@router.get(
    "/instruments/{instrument_id}/monitor-states",
    response_model=MonitorStateListResponse,
)
async def get_instrument_monitor_states(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> MonitorStateListResponse:
    """查询某股票的所有监控策略状态。"""
    states = await list_states_by_instrument(db, instrument_id)
    items = [MonitorStateResponse.model_validate(s) for s in states]
    return MonitorStateListResponse(items=items, total=len(items))


@router.get(
    "/strategies/{strategy_key}/monitor-states",
    response_model=MonitorStateListResponse,
)
async def get_strategy_monitor_states(
    strategy_key: str,
    version: str | None = Query(None, description="按版本号过滤"),
    db: AsyncSession = Depends(get_db),
) -> MonitorStateListResponse:
    """查询某策略的所有股票状态（支持 version 过滤）。"""
    version_ids = await _resolve_strategy_version_ids(db, strategy_key, version)

    items: list[MonitorStateResponse] = []
    for vid in version_ids:
        states = await list_states_by_strategy_version(db, vid)
        items.extend(MonitorStateResponse.model_validate(s) for s in states)
    return MonitorStateListResponse(items=items, total=len(items))


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert any("/monitor-states" in p for p in paths)
    print("OK")
