"""StockContext API - 个股状态上下文只读接口。

PRD V1.1 §7.3 核心契约：
- GET /api/v1/stocks/{symbol}/context
  用户侧只读接口，返回 StockState + 最近事件 + 数据质量。
  禁止请求时写事件（事件由盘后快照成功发布后异步生成）。
  需要 require_active_subscription 守卫（admin 豁免，member 需有效订阅）。
  as_of 直接声明 date | None，非法值由 FastAPI 返回 422。
  as_of 历史查询时，事件 occurred_at < as_of 次日 00:00（exclusive），禁止返回未来事件。
- GET /api/v1/admin/stocks/{symbol}/debug
  管理员调试接口，返回 StockState + 事件 + 原始 payload。
  前后端统一使用 symbol（非 instrument_id）。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.schemas.stock_state import (
    Evidence,
    StateEventDTO,
    StockContextResponse,
    build_stock_state,
    strip_internal_fields_for_user,
)
from app.services.access_control_service import (
    AccessContext,
    require_active_subscription,
    require_admin,
)
from app.services.state_event_service import get_recent_events_for_instrument

logger = logging.getLogger("api.stock_context")

# 用户侧路由：/api/v1/stocks/{symbol}/context
stock_router = APIRouter(prefix="/api/v1/stocks", tags=["stock-context"])

# 管理员路由：/api/v1/admin/stocks/{symbol}/debug
admin_router = APIRouter(prefix="/api/v1/admin/stocks", tags=["admin-stock-debug"])

_SCHEMA_VERSION = 1
# P0-3: 使用 Asia/Shanghai 时区计算 as_of 截止边界（非 UTC）
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 事件证据 field → 用户可读文案白名单（与 state_event_service._FIELD_LABELS 对齐）
_EVIDENCE_FIELD_LABELS: dict[str, str] = {
    "structure.price": "价格位置",
    "structure.consensusRelation": "成交密集区关系",
    "momentum.macd": "MACD 动量",
    "momentum.sqzmom": "SQZMOM 动量",
    "momentum.temporal.daily_dsa_dir": "日线 DSA 方向",
    "momentum.temporal.trend_alignment": "趋势对齐",
    "volatility.bollPosition": "布林位置",
}


async def _get_instrument_by_symbol(
    session: AsyncSession,
    symbol: str,
) -> Instrument:
    """按 symbol 查询 Instrument（前后端统一使用 symbol）。"""
    from fastapi import HTTPException, status

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

    P0-3: 确定性排序 — trade_date DESC, published_at DESC, finished_at DESC
    确保同日多 run 时选择最新发布的批次。
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .order_by(
            desc(StockFeatureSnapshotRun.trade_date),
            desc(StockFeatureSnapshotRun.published_at),
            desc(StockFeatureSnapshotRun.finished_at),
        )
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _find_run_by_trade_date(
    session: AsyncSession,
    trade_date: date,
    schema_version: int = _SCHEMA_VERSION,
) -> StockFeatureSnapshotRun | None:
    """按 trade_date 查找 succeeded run（as_of 历史回看）。

    P0-3: 确定性排序 — published_at DESC, finished_at DESC
    确保同日多 run 时选择最新发布的批次。
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
            StockFeatureSnapshotRun.published_at.is_not(None),
            StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        )
        .order_by(
            desc(StockFeatureSnapshotRun.published_at),
            desc(StockFeatureSnapshotRun.finished_at),
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
    """获取指定 instrument + run 对应的快照（按 source_run_id 精确查询）。"""
    stmt = (
        select(StockFeatureSnapshot)
        .where(
            and_(
                StockFeatureSnapshot.instrument_id == instrument_id,
                StockFeatureSnapshot.source_run_id == run.id,
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


def _format_evidence_value(code: Any, value: Any) -> str | None:
    """格式化证据值为用户可读字符串。

    优先返回 code（稳定状态码），code 为 None 时格式化 numeric value。
    """
    if code is not None:
        return str(code)
    if value is not None:
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value)
    return None


def _map_event_evidence_to_dto(
    raw_evidence: list[dict[str, Any]] | None,
) -> list[Evidence]:
    """将 ORM event.evidence (list[dict]) 映射为用户 Evidence DTO 列表。

    DB evidence 结构：
    - field: 稳定字段路径（如 "momentum.sqzmom"）
    - prevCode/currCode: 前后状态 code
    - prevValue/currValue: 前后原始数值
    - unit: 单位
    - timeframe: 来源周期

    API Evidence DTO：
    - fieldName: 用户可读指标名（白名单映射）
    - code: 稳定字段路径
    - currentValue/previousValue: 前后值（优先 code，其次 numeric value）
    - unit/timeframe
    """
    if not raw_evidence:
        return []
    result: list[Evidence] = []
    for item in raw_evidence:
        field = item.get("field", "")
        result.append(Evidence(
            fieldName=_EVIDENCE_FIELD_LABELS.get(field, field.split(".")[-1]),
            code=field,
            currentValue=_format_evidence_value(
                item.get("currCode"), item.get("currValue")
            ),
            previousValue=_format_evidence_value(
                item.get("prevCode"), item.get("prevValue")
            ),
            unit=item.get("unit"),
            timeframe=item.get("timeframe") or "",
        ))
    return result


def _event_to_dto(event: Any) -> StateEventDTO:
    """将 StockStateEvent ORM 转为 StateEventDTO。

    P0-3: 映射 event.evidence (list[dict]) 为 Evidence DTO 列表。
    """
    return StateEventDTO(
        id=str(event.id),
        symbol=event.symbol,
        occurredAt=event.occurred_at.isoformat() if event.occurred_at else "",
        eventType=event.event_type,
        title=event.title,
        description=event.description,
        evidence=_map_event_evidence_to_dto(event.evidence),
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
    """构建 StockContext 响应（共享逻辑，只读查询）。

    P0-4: as_of 历史查询时，事件 occurred_at <= as_of 当日结束，禁止返回未来事件。
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

    # P0-3: as_of 历史查询时，事件截止到 as_of 次日 00:00 (exclusive)
    # 使用 Asia/Shanghai 时区，禁止 max.time()+1day-1sec 的旧写法
    occurred_at_lte: datetime | None = None
    if as_of is not None:
        next_day = as_of + timedelta(days=1)
        occurred_at_lte = datetime.combine(
            next_day, datetime.min.time(), tzinfo=_SHANGHAI_TZ,
        )

    # 获取最近事件（只读查询）
    recent_events = await get_recent_events_for_instrument(
        session, instrument.id, limit=10, occurred_at_lte=occurred_at_lte,
    )
    event_dtos = [_event_to_dto(e) for e in recent_events]

    data_quality = _build_data_quality(instrument, run, snapshot)

    # PRD V1.1: 用户接口完全排除 sourceField/idempotencyKey（不是 null，是字段不存在）
    response_state: Any
    response_events: Any
    if not include_raw:
        response_state, response_events = strip_internal_fields_for_user(
            stock_state, event_dtos
        )
    else:
        response_state = stock_state
        response_events = event_dtos

    response: dict[str, Any] = {
        "state": response_state,
        "events": response_events,
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
    as_of: date | None = Query(None, description="截止日期 ISO（如 2026-07-10），默认最新"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_active_subscription),
) -> StockContextResponse:
    """获取个股状态上下文（只读，需登录 + 有效订阅）。

    V1.1 核心契约：
    - 返回 StockState + 最近事件 + 数据质量
    - 禁止请求时写事件
    - as_of 历史查询时事件 occurred_at <= as_of 当日结束
    - 无数据时返回 state=null + dataQuality 说明

    权限：
    - active admin 允许（豁免订阅）
    - active member 且订阅有效允许
    - 过期/无订阅拒绝
    - Capture token 不可访问
    """
    # ctx 仅用于权限守卫，不直接使用
    _ = ctx
    result = await _build_stock_context(db, symbol, as_of, include_raw=False)
    return StockContextResponse(**result)


# =============================================================================
# 管理员调试接口：GET /api/v1/admin/stocks/{symbol}/debug
# =============================================================================


@admin_router.get("/{symbol}/debug")
async def get_admin_stock_debug(
    symbol: str,
    as_of: date | None = Query(None, description="截止日期 ISO，默认最新"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> dict[str, Any]:
    """管理员个股调试接口（前后端统一使用 symbol）。

    返回 StockState + 事件 + 原始 payload（structural/temporal/summary）。
    仅管理员可访问。
    """
    _ = ctx
    result = await _build_stock_context(db, symbol, as_of, include_raw=True)
    return result
