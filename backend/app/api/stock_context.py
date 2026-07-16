"""StockContext API - 个股状态上下文只读接口（Atomic Fact Contract V1）。

核心契约：
- GET /api/v1/stocks/{symbol}/context
  用户侧只读接口，返回 Atomic Fact Contract V1 上下文
  （contractVersion/asOf/core/auxiliary/availability/recentChanges/dataQuality）。
  禁止请求时写数据（事实由盘后快照成功发布后异步生成，本接口只做只读查询）。
  需要 require_active_subscription 守卫（admin 豁免，member 需有效订阅）。
  as_of 直接声明 date | None，非法值由 FastAPI 返回 422。
  as_of 历史查询时严格 point-in-time（仅查 succeeded+published+full run），禁止返回未来快照或未来变化。
- GET /api/v1/admin/stocks/{symbol}/debug
  管理员调试接口，在用户响应基础上补充原始 payload + 原子事实可追溯信息。
  前后端统一使用 symbol（非 instrument_id）。
"""

from __future__ import annotations

import logging
from datetime import date
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
from app.schemas.atomic_fact_contract import (
    AdminStockDebugResponse,
    AtomicFactsContextResponse,
    PersistedAtomicFactsPayload,
)
from app.schemas.stock_state import (
    StockContextDataQuality,
)
from app.services.access_control_service import (
    AccessContext,
    require_active_subscription,
    require_admin,
)
from app.services.atomic_fact_contract_service import (
    AFC_PAYLOAD_VERSION,
    CONTRACT_VERSION,
    CORE_PUBLIC_KEY,
    PRESENTATION_VERSION,
    RESEARCH_FREEZE_VERSION,
    compute_atomic_fact_debug,
    compute_atomic_facts,
    compute_recent_changes,
)

logger = logging.getLogger("api.stock_context")

# 用户侧路由：/api/v1/stocks/{symbol}/context
stock_router = APIRouter(prefix="/api/v1/stocks", tags=["stock-context"])

# 管理员路由：/api/v1/admin/stocks/{symbol}/debug
admin_router = APIRouter(prefix="/api/v1/admin/stocks", tags=["admin-stock-debug"])

_SCHEMA_VERSION = 1
# P0-3: 使用 Asia/Shanghai 时区计算 as_of 截止边界（非 UTC）
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


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
    """按 as_of 截止日期查找 succeeded+published+full run。

    as_of 为截止日期语义（非当天精确匹配）：
    - 查 `trade_date <= as_of`，按 trade_date DESC, published_at DESC, finished_at DESC
      取最新 1 条；
    - 周末/节假日/无批次日期返回该日期之前最近一次已发布状态（而非空态）。
    """
    stmt = (
        select(StockFeatureSnapshotRun)
        .where(
            StockFeatureSnapshotRun.trade_date <= trade_date,
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


async def _get_snapshot_for_instrument(
    session: AsyncSession,
    instrument_id: UUID,
    run: StockFeatureSnapshotRun,
) -> tuple[StockFeatureSnapshot | None, str | None]:
    """获取指定 instrument + run 对应的快照。

    P0-1: 先按 source_run_id 精确查询；失败后按唯一约束字段 legacy 回退查询。
    返回 (snapshot, reasonCode):
    - (snapshot, None): 精确匹配成功
    - (snapshot, "snapshot_run_not_linked"): legacy 匹配，source_run_id=NULL 需修复
    - (snapshot, "legacy_snapshot_ambiguous"): legacy 匹配但 source_run_id 指向其他 run
    - (None, None): 未找到任何快照
    """
    # 1. 精确查询：source_run_id == run.id
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
    snapshot = result.scalar_one_or_none()
    if snapshot is not None:
        return snapshot, None

    # 2. Legacy 回退：按唯一约束字段查询（不含 source_run_id）
    legacy_stmt = (
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
    legacy_result = await session.execute(legacy_stmt)
    legacy_snapshot = legacy_result.scalar_one_or_none()
    if legacy_snapshot is None:
        return None, None

    # 3. 判断 legacy 快照的归属状态
    if legacy_snapshot.source_run_id is None:
        return legacy_snapshot, "snapshot_run_not_linked"
    # source_run_id 指向其他 run（数据不一致）
    return legacy_snapshot, "legacy_snapshot_ambiguous"


def _build_data_quality(
    instrument: Instrument,
    run: StockFeatureSnapshotRun | None,
    snapshot: StockFeatureSnapshot | None,
    reason_code: str | None = None,
) -> StockContextDataQuality:
    """构建数据质量信息（含 reasonCode / degradedReasons）。

    降级原因处理：
    - 无 snapshot：reasonCode 保留传入的 reason_code（如 snapshot_missing）；
    - snapshot 存在但 legacy/ambiguous（snapshot_run_not_linked /
      legacy_snapshot_ambiguous）：不清除 reason，加入 degradedReasons；
    - 精确匹配（reason_code=None）：无降级原因。
    """
    degraded: list[str] = list(snapshot.degraded_reasons) if snapshot else []
    if snapshot is not None and reason_code is not None:
        # legacy/ambiguous 快照存在但归属状态异常：加入 degradedReasons（不清除）
        if reason_code not in degraded:
            degraded.append(reason_code)
        effective_reason: str | None = reason_code
    else:
        # 无 snapshot：保留传入的 reason_code；精确匹配：None
        effective_reason = None if snapshot is not None else reason_code
    return StockContextDataQuality(
        hasSucceededRun=run is not None,
        hasSnapshot=snapshot is not None,
        reasonCode=effective_reason,
        degradedReasons=degraded,
        runTradeDate=run.trade_date.isoformat() if run else None,
        runPublishedAt=run.published_at.isoformat() if run and run.published_at else None,
        instrumentStatus=instrument.status,
    )


def _is_valid_stored_afc(stored: Any) -> bool:
    """严格校验 summary_payload.atomic_fact_contract_v1 是否为当前持久化结构。

    委托 PersistedAtomicFactsPayload Pydantic Schema 严格校验：
    - 四版本字段完全匹配；
    - core 键恰好 trend/momentum/structure/volume；
    - 每一项均通过 PublicAtomicFactItem；
    - publicKey 属于正确维度且无重复/未知；
    - T3/T6/V1 不存在；
    - availability 与实际数组及固定分母 14 一致；
    - 不含 debug（extra=forbid）。
    任一不满足 → 返回 False，触发纯函数 fallback 重算（不回写旧快照），不得 500。
    """
    if not isinstance(stored, dict):
        return False
    try:
        PersistedAtomicFactsPayload.model_validate(stored)
        return True
    except Exception:  # noqa: BLE001 - 任意校验异常均触发 fallback
        return False


# 管理员 debug 由 compute_atomic_fact_debug 按需即时生成（见下方 include_raw 分支）。


def _afc_meta() -> dict[str, str]:
    """公共响应 meta：三版本字段（前端禁止硬编码 V4.13）。"""
    return {
        "payloadVersion": AFC_PAYLOAD_VERSION,
        "researchFreezeVersion": RESEARCH_FREEZE_VERSION,
        "presentationVersion": PRESENTATION_VERSION,
    }


def _empty_atomic_response(
    instrument: Instrument,
    reason_code: str | None,
) -> dict[str, Any]:
    """无 run / 无快照时的空态响应（Core 分母仍固定 14，全部缺失）。"""
    return {
        "contractVersion": CONTRACT_VERSION,
        "meta": _afc_meta(),
        "asOf": None,
        "core": {dim: [] for dim in ("trend", "momentum", "structure", "volume")},
        "auxiliary": [],
        "availability": {
            "coreDenominator": len(CORE_PUBLIC_KEY),
            "corePresent": 0,
            "coreMissing": list(CORE_PUBLIC_KEY.values()),
            "auxiliaryAvailable": [],
            "auxiliaryHidden": [],
            "v1Present": False,
            "rejectedPresent": False,
        },
        "recentChanges": [],
        "dataQuality": _build_data_quality(
            instrument, None, None, reason_code=reason_code,
        ),
    }


async def _find_recent_published_snapshots(
    session: AsyncSession,
    instrument_id: UUID,
    limit: int = 10,
    as_of: date | None = None,
) -> list[dict[str, Any]]:
    """一次查询读取最近 ≤limit 个已发布 full scope 快照（升序），供近期变化计算。

    只读查询，不写 stock_state_events。
    as_of 给定时：SQL 直接加 `trade_date <= as_of` 过滤（再 DESC LIMIT，最后升序），
    禁止先取最新 10 条再在内存过滤（PROMPT 一.8）。
    """
    from app.models.stock_feature_snapshot import StockFeatureSnapshot

    conditions = [
        StockFeatureSnapshot.instrument_id == instrument_id,
        StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
        StockFeatureSnapshotRun.published_at.is_not(None),
        StockFeatureSnapshotRun.metadata_["scope"].astext == "full",
        StockFeatureSnapshotRun.schema_version == _SCHEMA_VERSION,
    ]
    if as_of is not None:
        conditions.append(StockFeatureSnapshotRun.trade_date <= as_of)

    stmt = (
        select(StockFeatureSnapshot)
        .join(
            StockFeatureSnapshotRun,
            StockFeatureSnapshot.source_run_id == StockFeatureSnapshotRun.id,
        )
        .where(*conditions)
        .order_by(desc(StockFeatureSnapshot.trade_date))
        .limit(limit)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()
    # 升序（compute_recent_changes 要求按 trade_date 升序）
    rows_sorted = sorted(rows, key=lambda s: s.trade_date)
    return [
        {
            "trade_date": s.trade_date.isoformat(),
            "structural_payload": s.structural_payload or {},
            "temporal_payload": s.temporal_payload or {},
        }
        for s in rows_sorted
    ]


async def _build_stock_context(
    session: AsyncSession,
    symbol: str,
    as_of: date | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """构建 Atomic Fact Contract V1 上下文响应（共享逻辑，只读查询）。

    普通用户响应替换旧 state/events 为：
      contractVersion / asOf / core / auxiliary / availability / recentChanges / dataQuality
    as_of 严格 point-in-time：仅查 succeeded+published+full run，禁止返回未来快照或未来变化。
    """
    instrument = await _get_instrument_by_symbol(session, symbol)

    # 查找 run（as_of 历史回看 or 最新）
    if as_of is not None:
        run = await _find_run_by_trade_date(session, as_of)
    else:
        run = await _find_latest_succeeded_run(session)

    if run is None:
        return _empty_atomic_response(instrument, reason_code="no_published_full_run")

    snapshot, reason_code = await _get_snapshot_for_instrument(
        session, instrument.id, run,
    )
    if snapshot is None:
        return _empty_atomic_response(instrument, reason_code="snapshot_missing")

    # 优先读取已持久化的原子事实（新快照写入 summary_payload.atomic_fact_contract_v1）；
    # 缺失或版本/结构不匹配 → 同一纯函数 fallback 重算（不回写旧快照）。
    _summary = snapshot.summary_payload
    stored = _summary.get("atomic_fact_contract_v1") if isinstance(_summary, dict) else None
    if _is_valid_stored_afc(stored) and isinstance(stored, dict):
        facts = stored
    else:
        facts = compute_atomic_facts(snapshot.structural_payload, snapshot.temporal_payload)

    # 近期变化：一次查询 ≤10 个已发布兼容快照（as_of 时 SQL 直接过滤），升序只读计算
    recent_snaps = await _find_recent_published_snapshots(
        session, instrument.id, limit=10, as_of=as_of,
    )
    recent_changes = compute_recent_changes(recent_snaps)

    data_quality = _build_data_quality(instrument, run, snapshot, reason_code)
    # 数据质量异常（如 M5 双 true）并入 degradedReasons
    _warnings = (
        facts.get("availability", {}).get("warnings", [])
        if isinstance(facts.get("availability"), dict)
        else []
    )
    if "m5_inconsistent" in _warnings:
        _cur = list(data_quality.degradedReasons or [])
        if "m5_inconsistent" not in _cur:
            _cur.append("m5_inconsistent")
            data_quality.degradedReasons = _cur

    response: dict[str, Any] = {
        "contractVersion": CONTRACT_VERSION,
        "meta": _afc_meta(),
        "asOf": run.trade_date.isoformat(),
        "core": facts["core"],
        "auxiliary": facts["auxiliary"],
        "availability": facts["availability"],
        "recentChanges": recent_changes,
        "dataQuality": data_quality,
    }

    if include_raw:
        # 管理员调试：返回原始 payload + 原子事实可追溯信息
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
        response["atomicFactsDebug"] = compute_atomic_fact_debug(snapshot.structural_payload, snapshot.temporal_payload)

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
) -> AtomicFactsContextResponse:
    """获取个股原子事实上下文（只读，需登录 + 有效订阅）。

    Atomic Fact Contract V1 核心契约：
    - 替换旧 state/events 为 contractVersion/asOf/core/auxiliary/availability/recentChanges/dataQuality
    - 禁止请求时写事件（事实由盘后快照成功发布后异步生成）
    - as_of 历史查询时事件 occurred_at <= as_of 当日结束，禁止返回未来信息
    - 无数据时返回 core 全缺失 + dataQuality 说明

    权限：
    - active admin 允许（豁免订阅）
    - active member 且订阅有效允许
    - 过期/无订阅拒绝
    - Capture token 不可访问
    """
    # ctx 仅用于权限守卫，不直接使用
    _ = ctx
    result = await _build_stock_context(db, symbol, as_of, include_raw=False)
    return AtomicFactsContextResponse(**result)


# =============================================================================
# 管理员调试接口：GET /api/v1/admin/stocks/{symbol}/debug
# =============================================================================


@admin_router.get("/{symbol}/debug")
async def get_admin_stock_debug(
    symbol: str,
    as_of: date | None = Query(None, description="截止日期 ISO，默认最新"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_admin),
) -> AdminStockDebugResponse:
    """管理员个股调试接口（前后端统一使用 symbol）。

    在用户响应基础上补充原始 payload（structural/temporal/summary）+ 原子事实可追溯信息
    （Fact ID / 真实路径 / raw value / 阈值来源 / feature flag）。
    仅管理员可访问。
    """
    _ = ctx
    result = await _build_stock_context(db, symbol, as_of, include_raw=True)
    return AdminStockDebugResponse(**result)
