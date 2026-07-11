"""StockContext API - 个股状态上下文只读接口。

PRD V1.1 §7.3 核心契约：
- GET /api/v1/stocks/{symbol}/context
  用户侧只读接口，返回 StockState + 最近事件 + 数据质量。
  禁止请求时写事件（事件由盘后快照成功发布后异步生成）。
- GET /api/v1/admin/stocks/{symbol}/debug
  管理员调试接口，返回 StockState + 事件 + 原始 payload（structural/temporal）。
  前后端统一使用 symbol（非 instrument_id）。

设计原则：
- 只读查询，无副作用
- symbol → instrument_id → snapshot → StockState
- as_of 参数支持历史回看（按 trade_date 查询快照）
- 无数据时返回 state=null + dataQuality 说明，不抛 404

用法：
    from app.api.stock_context import router as stock_context_router
    app.include_router(stock_context_router)
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.schemas.stock_state import (
    StateEventDTO,
    StockContextResponse,
    build_stock_state,
)
from app.services.state_event_service import get_recent_events_for_instrument

logger = logging.getLogger("api.stock_context")

# 用户侧路由：/api/v1/stocks/{symbol}/context
stock_router = APIRouter(prefix="/api/v1/stocks", tags=["stock-context"])

# 管理员路由：/api/v1/admin/stocks/{symbol}/debug
admin_router = APIRouter(prefix="/api/v1/admin/stocks", tags=["admin-stock-debug"])

_SCHEMA_VERSION = 1
_DEFAULT_PRIMARY_TIMEFRAME = "1d"
_DEFAULT_SECONDARY_TIMEFRAME = "15m"
_DEFAULT_ADJ = "qfq"


async def _get_instrument_by_symbol(
    session: AsyncSession,
    symbol: str,
) -> Instrument:
    """按 symbol 查询 Instrument（前后端统一使用 symbol）。"""
    stmt = select(Instrument).where(Instrument.symbol == symbol)
    result = await session.execute(stmt)
    instrument = result.scalar_one_or_none()
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"股票代码不存在: {symbol}",
        )
    return instrument


async def _find_latest_succeeded_run(
    session: AsyncSession,
    schema_version: int = _SCHEMA_VERSION,
) -> StockFeatureSnapshotRun | None:
    """查找最新的 succeeded + published + full scope 的 snapshot run。

    publish gate 与 has_succeeded_snapshot_run 一致：
    - status='succeeded'
    - published_at IS NOT NULL
    - metadata_['scope']='full'
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .order_by(desc(StockFeatureSnapshotRun.trade_date))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_run_by_trade_date(
    session: AsyncSession,
    trade_date: date,
    schema_version: int = _SCHEMA_VERSION,
) -> StockFeatureSnapshotRun | None:
    """按 trade_date 查找 succeeded run（as_of 历史回看）。"""
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _get_snapshot_for_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    run: StockFeatureSnapshotRun,
) -> StockFeatureSnapshot | None:
    """获取指定 instrument + run 对应的快照。"""
    stmt = (
        select(StockFeatureSnapshot)
        .where(
            and_(
                StockFeatureSnapshot.instrument_id == instrument_id,
                StockFeatureSnapshot.trade_date == run.trade_date,
                StockFeatureSnapshot.schema_version == run.schema_version,
                StockFeatureSnapshot.primary_timeframe == run.primary_timeframe,
                StockFeatureSnapshot.secondary_timeframe == run.secondary_timeframe,
                StockFeatureSnapshot.adj == run.adj,
            )
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


def _build_data_quality(
    instrument: Instrument,
    run: StockFeatureSnapshotRun | None,
    snapshot: StockFeatureSnapshot | None,
) -> dict[str, Any]:
    """构建数据质量信息。"""
    return {
        "hasSucceededRun": run is not None,
        "hasSnapshot": snapshot is not None,
        "degradedReasons": snapshot.degraded_reasons if snapshot else [],
        "runTradeDate": run.trade_date.isoformat() if run else None,
        "runPublishedAt": run.published_at.isoformat() if run and run.published_at else None,
        "instrumentStatus": instrument.status,
    }


def _event_to_dto(event: Any) -> StateEventDTO:
    """将 StockStateEvent ORM 转为 StateEventDTO。"""
    return StateEventDTO(
        id=str(event.id),
        symbol=event.symbol,
        occurredAt=event.occurred_at.isoformat() if event.occurred_at else "",
        eventType=event.event_type,
        title=event.title,
        description=event.description,
        changedFields=event.changed_fields or [],
        previousAsOf=event.previous_as_of.isoformat() if event.previous_as_of else None,
        currentAsOf=event.current_as_of.isoformat() if event.current_as_of else "",
        idempotencyKey=event.idempotency_key,
    )


async def _build_stock_context(
    session: AsyncSession,
    symbol: str,
    as_of: date | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """构建 StockContext 响应（共享逻辑）。

    Args:
        session: 异步 DB 会话
        symbol: 股票代码
        as_of: 截止日期（None 表示最新）
        include_raw: 是否包含原始 payload（管理员调试）
    """
    instrument = await _get_instrument_by_symbol(session, symbol)

    # 查找 run（as_of 历史回看 or 最新）
    if as_of is not None:
        run = await _find_run_by_trade_date(session, as_of)
    else:
        run = await _find_latest_succeeded_run(session)

    if run is None:
        return {
            "state": None,
            "events": [],
            "dataQuality": _build_data_quality(instrument, None, None),
        }

    snapshot = await _get_snapshot_for_instrument(session, instrument.id, run)
    if snapshot is None:
        return {
            "state": None,
            "events": [],
            "dataQuality": _build_data_quality(instrument, run, None),
        }

    # 构建 StockState（纯函数，无副作用）
    stock_state = build_stock_state(snapshot, run, symbol)

    # 获取最近事件（只读查询）
    recent_events = await get_recent_events_for_instrument(
        session, instrument.id, limit=10
    )
    event_dtos = [_event_to_dto(e) for e in recent_events]

    data_quality = _build_data_quality(instrument, run, snapshot)

    response: dict[str, Any] = {
        "state": stock_state,
        "events": event_dtos,
        "dataQuality": data_quality,
    }

    if include_raw:
        # 管理员调试：返回原始 payload
        response["rawDebug"] = {
            "structuralPayload": snapshot.structural_payload,
            "temporalPayload": snapshot.temporal_payload,
            "summaryPayload": snapshot.summary_payload,
            "sourcePrimaryBarTime": (
                snapshot.source_primary_bar_time.isoformat()
                if snapshot.source_primary_bar_time else None
            ),
            "sourceSecondaryBarTime": (
                snapshot.source_secondary_bar_time.isoformat()
                if snapshot.source_secondary_bar_time else None
            ),
            "runId": str(run.id),
            "runType": run.run_type,
            "runStartedAt": run.started_at.isoformat() if run.started_at else None,
            "runFinishedAt": run.finished_at.isoformat() if run.finished_at else None,
        }

    return response


# =============================================================================
# 用户侧接口：GET /api/v1/stocks/{symbol}/context
# =============================================================================


@stock_router.get("/{symbol}/context")
async def get_stock_context(
    symbol: str,
    as_of: str | None = Query(None, description="截止日期 ISO（如 2026-07-10），默认最新"),
    db: AsyncSession = Depends(get_db),
) -> StockContextResponse:
    """获取个股状态上下文（只读）。

    V1.1 核心契约：
    - 返回 StockState + 最近事件 + 数据质量
    - 禁止请求时写事件（事件由盘后快照成功发布后异步生成）
    - 无数据时返回 state=null + dataQuality 说明，不抛 404

    Args:
        symbol: 股票代码（如 000001）
        as_of: 截止日期（ISO 格式，如 2026-07-10），默认最新
    """
    parsed_as_of: date | None = None
    if as_of is not None:
        try:
            parsed_as_of = date.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"as_of 格式无效，应为 ISO 日期（如 2026-07-10）: {as_of}",
            ) from exc

    result = await _build_stock_context(db, symbol, parsed_as_of, include_raw=False)
    return StockContextResponse(**result)


# =============================================================================
# 管理员调试接口：GET /api/v1/admin/stocks/{symbol}/debug
# =============================================================================


@admin_router.get("/{symbol}/debug")
async def get_admin_stock_debug(
    symbol: str,
    as_of: str | None = Query(None, description="截止日期 ISO，默认最新"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """管理员个股调试接口（前后端统一使用 symbol）。

    返回 StockState + 事件 + 原始 payload（structural/temporal/summary）。
    仅管理员可访问。

    Args:
        symbol: 股票代码（如 000001）
        as_of: 截止日期（ISO 格式），默认最新
    """
    parsed_as_of: date | None = None
    if as_of is not None:
        try:
            parsed_as_of = date.fromisoformat(as_of)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"as_of 格式无效: {as_of}",
            ) from exc

    result = await _build_stock_context(db, symbol, parsed_as_of, include_raw=True)
    return result


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    print("stock_context API 自测...")

    # 验证路由前缀
    assert stock_router.prefix == "/api/v1/stocks", f"用户路由前缀错误: {stock_router.prefix}"
    assert admin_router.prefix == "/api/v1/admin/stocks", f"管理员路由前缀错误: {admin_router.prefix}"

    # 验证路由数量
    stock_routes = list(stock_router.routes)
    admin_routes = list(admin_router.routes)
    assert len(stock_routes) == 1, f"用户路由应只有 1 个，实际: {len(stock_routes)}"
    assert len(admin_routes) == 1, f"管理员路由应只有 1 个，实际: {len(admin_routes)}"

    # 验证路径
    assert stock_routes[0].path == "/api/v1/stocks/{symbol}/context"
    assert admin_routes[0].path == "/api/v1/admin/stocks/{symbol}/debug"

    print(f"stock_router: {stock_router.prefix}, routes: {[r.path for r in stock_routes]}")
    print(f"admin_router: {admin_router.prefix}, routes: {[r.path for r in admin_routes]}")
    print("OK: stock_context API 自测通过")
