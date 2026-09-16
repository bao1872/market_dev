"""[S2-A-C1] Market list export — 安全资源路径 owner。

设计原则（冻结）：
- 与 /market/stocks 共享同一套筛选/排序/scope/canonical CoreRun 语义
  （经由 market_stocks_service._assemble_market_query 单一真源），
  但物理 read model 不同：list 走完整 MarketStockRow 富集，
  export 走轻量分批读取 + 低内存增量 XLSX writer。
- 严禁回归：绝不直接 get_market_stocks(page_size=MAX_EXPORT_ROWS + 1)。
- DB 一次只处理有界 batch（EXPORT_BATCH_SIZE）。
- Python 不持有完整导出集的重型 read model。
- XLSX 的 XML/zip CPU 工作脱离 async event loop（asyncio.to_thread）。
- 临时 admin-only 熔断（C1a）：仅 require_admin 可触发；C1b 验证通过后恢复普通权限。
- 全局导出租约（并发=1，忙时 429），防止资源压力倍增。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.schemas.export import ExportColumn
from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION
from app.schemas.market_stocks import MarketExportRequest
from app.services.distributed_lock import acquire_lock, generate_holder, release_lock
from app.services.excel_export_service import (
    MAX_EXPORT_ROWS,
    MarketXlsxWriter,
    validate_export_columns,
)
from app.services.market_stocks_service import (
    FP_QUERY_FIELD_SPECS,
    MarketQueryContext,
    _assemble_market_query,
    _build_display_snapshot_query,
    _map_dsa_state,
    flatten_first_pyramid,
    get_instrument_boards_batch,
)

# ===== 资源常量（硬约束）=====
# MAX_EXPORT_ROWS = 业务允许导出的最大行数（绝不再当 page_size / 一次性 materialization 数量）。
EXPORT_BATCH_SIZE = 250  # 任何一次 export DB fetch 行数上限（200~500 均合规）。
EXPORT_LOCK_KEY = "market_export"  # 全局导出租约（C1a 单 worker 最保守：global=1）。
EXPORT_LOCK_TTL = 600  # 秒；防止租约因请求中断而永久占用。


@dataclass
class MarketExportPlan:
    """归一化后的导出计划（含 source planning）。"""

    scope: str
    query: str | None
    state: str | None
    industry: str | None
    concept: str | None
    fp_filter: str | None
    fp_sort: str | None
    sort: str | None
    stock_name: str | None
    stock_name_op: str | None
    columns: list[ExportColumn]
    needs_price: bool = False
    needs_snapshot: bool = False
    needs_boards: bool = False
    needs_chip: bool = False


def build_export_plan(request: MarketExportRequest) -> MarketExportPlan:
    """[S2-A-C1] 请求 fail-fast 校验 + source planning。

    在执行任何重 DB query 前完成；非法请求抛 HTTPException(422)。
    """
    validate_export_columns(request.visible_columns)
    keys = [c.key for c in request.visible_columns]

    needs_price = any(k in ("latest_price", "change_pct") for k in keys)
    needs_snapshot = any(
        (k in ("dsa_state", "structure_state"))
        or (
            k.startswith("fp_")
            and FP_QUERY_FIELD_SPECS.get(k, {}).get("source") in ("flat", "column", "computed")
        )
        for k in keys
    )
    needs_boards = (
        any(k in ("industry", "concepts") for k in keys)
        or bool(request.industry)
        or bool(request.concept)
    )
    needs_chip = any(
        (k.startswith("fp_") and FP_QUERY_FIELD_SPECS.get(k, {}).get("source") == "chip")
        or (
            k.startswith("fp_")
            and FP_QUERY_FIELD_SPECS.get(k, {}).get("computed_kind") == "chip_available"
        )
        or (k == "chip_status")
        for k in keys
    )

    scope = "watchlist" if request.scope == "watchlist" else "market"
    return MarketExportPlan(
        scope=scope,
        query=request.keyword,
        state=request.state,
        industry=request.industry,
        concept=request.concept,
        fp_filter=request.fp_filter,
        fp_sort=request.fp_sort,
        sort=request.sort,
        stock_name=request.stock_name,
        stock_name_op=request.stock_name_op,
        columns=request.visible_columns,
        needs_price=needs_price,
        needs_snapshot=needs_snapshot,
        needs_boards=needs_boards,
        needs_chip=needs_chip,
    )


def _stock_name_condition(op: str | None, value: str | None):
    """[S2-A-C1] 补齐历史 stock_name 筛选漂移：与列表 keyword/industry/concept/state/fp 同属 AND。"""
    if not value:
        return None
    op = op or "contains"
    if op == "eq":
        return Instrument.name == value
    if op == "not_contains":
        return ~Instrument.name.ilike(f"%{value}%")
    return Instrument.name.ilike(f"%{value}%")


def _cell_value(
    key: str,
    base: Any,
    price: tuple[float | None, float | None] | None,
    flat_fp: dict | None,
    payload: dict | None,
    boards: list[dict],
    chip_flat: dict | None,
) -> Any:
    if key == "stock":
        name = base.name or ""
        sym = base.symbol or ""
        return f"{name}({sym})" if name and sym else name or sym
    if key == "symbol":
        return base.symbol
    if key == "name":
        return base.name
    if key == "market":
        return base.market
    if key == "is_watchlisted":
        return base.is_watchlisted
    if key == "latest_price":
        return price[0] if price else None
    if key == "change_pct":
        return price[1] if price else None
    if key == "dsa_state":
        return _map_dsa_state(payload.get("daily_developing_swing_dir")) if payload else None
    if key == "structure_state":
        return payload.get("cost_position_zone") if payload else None
    if key == "industry":
        return next((b["name"] for b in boards if b.get("type") == "industry"), None)
    if key == "concepts":
        return [b["name"] for b in boards if b.get("type") == "concept"]
    if key == "chip_status":
        if chip_flat is not None:
            return "available" if chip_flat.get("chip_available") else "unavailable"
        return None
    if key.startswith("fp_"):
        spec = FP_QUERY_FIELD_SPECS.get(key)
        if spec and spec.get("source") == "chip":
            return chip_flat.get(key) if chip_flat else None
        return flat_fp.get(key) if flat_fp else None
    return None


async def _fetch_prices(db: AsyncSession, ids: list[UUID]) -> dict[UUID, tuple[float | None, float | None]]:
    if not ids:
        return {}
    bars_subq = (
        select(
            BarDaily.instrument_id,
            BarDaily.close,
            func.row_number()
            .over(partition_by=BarDaily.instrument_id, order_by=BarDaily.trade_date.desc())
            .label("rn"),
        )
        .where(BarDaily.instrument_id.in_(ids))
        .subquery()
    )
    bars_stmt = select(bars_subq).where(bars_subq.c.rn <= 2)
    res = await db.execute(bars_stmt)
    raw: dict[UUID, list[float | None]] = {}
    for bar in res:
        inst = bar.instrument_id
        close = float(bar.close) if bar.close is not None else None
        raw.setdefault(inst, [None, None])
        if raw[inst][0] is None:
            raw[inst][0] = close
        else:
            raw[inst][1] = close
    out: dict[UUID, tuple[float | None, float | None]] = {}
    for inst, (latest, prev) in raw.items():
        cp = None
        if latest is not None and prev is not None and prev != 0:
            cp = round((latest - prev) / prev * 100, 2)
        out[inst] = (latest, cp)
    return out


async def _fetch_snapshots(
    db: AsyncSession, ids: list[UUID], canonical_core_run_id: UUID | None
) -> tuple[dict, dict, dict]:
    if not ids:
        return {}, {}, {}
    stmt = _build_display_snapshot_query(ids, canonical_core_run_id=canonical_core_run_id)
    res = await db.execute(stmt)
    flat_map: dict[UUID, dict | None] = {}
    payload_map: dict[UUID, dict] = {}
    snap_meta: dict[UUID, tuple] = {}
    for row in res:
        payload = row.summary_payload or {}
        raw = payload.get("first_pyramid")
        flat_map[row.instrument_id] = flatten_first_pyramid(raw) if raw else None
        payload_map[row.instrument_id] = payload
        snap_meta[row.instrument_id] = (row.trade_date, row.source_run_id)
    return flat_map, payload_map, snap_meta


async def _fetch_chips(
    db: AsyncSession, snap_meta: dict[UUID, tuple]
) -> dict[UUID, dict]:
    pairs = [
        (iid, td, rid)
        for iid, (td, rid) in snap_meta.items()
        if td is not None and rid is not None
    ]
    if not pairs:
        return {}
    chip_stmt = select(
        StockChipConsensusSnapshot.instrument_id,
        StockChipConsensusSnapshot.chip_payload,
    ).where(
        StockChipConsensusSnapshot.algorithm_version == CHIP_CONSENSUS_ALGORITHM_VERSION,
        tuple_(
            StockChipConsensusSnapshot.instrument_id,
            StockChipConsensusSnapshot.trade_date,
            StockChipConsensusSnapshot.core_run_id,
        ).in_(pairs),
    )
    res = await db.execute(chip_stmt)
    out: dict[UUID, dict] = {}
    for row in res:
        payload = row.chip_payload if isinstance(row.chip_payload, dict) else {}
        chip_dim = payload.get("chip")
        available = bool(isinstance(chip_dim, dict) and chip_dim.get("available") is True)
        chip_flat = payload.get("chip_flat") or {}
        out[row.instrument_id] = {"chip_available": available, "chip_flat": chip_flat}
    return out


async def _fetch_batch_rows(
    db: AsyncSession, ctx: MarketQueryContext, plan: MarketExportPlan, batch_index: int
) -> list[dict]:
    stmt = ctx.base_stmt.limit(EXPORT_BATCH_SIZE).offset(batch_index * EXPORT_BATCH_SIZE)
    result = await db.execute(stmt)
    base_rows = result.all()
    ids = [r.id for r in base_rows]

    price_map: dict = await _fetch_prices(db, ids) if plan.needs_price else {}
    flat_map, payload_map, snap_meta = (
        await _fetch_snapshots(db, ids, ctx.canonical_core_run_id)
        if plan.needs_snapshot
        else ({}, {}, {})
    )
    boards_map: dict = await get_instrument_boards_batch(db, ids) if plan.needs_boards else {}
    chip_flat_map: dict = {}
    if plan.needs_chip:
        chip_flat_map = await _fetch_chips(db, snap_meta)

    out: list[dict] = []
    for r in base_rows:
        row: dict[str, Any] = {}
        for col in plan.columns:
            row[col.key] = _cell_value(
                col.key,
                r,
                price_map.get(r.id),
                flat_map.get(r.id),
                payload_map.get(r.id),
                boards_map.get(r.id, []),
                chip_flat_map.get(r.id),
            )
        out.append(row)
    return out


async def _build_export_file(
    db: AsyncSession, plan: MarketExportPlan, user_id: UUID
) -> tuple[str, dict]:
    """执行导出：filtered count → 分批读取 → 低内存 XLSX。返回 (final_path, stats)。

    调用方负责在流送结束后清理 tmp_dir。锁在生成完成后即释放（heavy-work 结束）。
    """
    ctx = await _assemble_market_query(
        db, user_id, plan.scope, plan.query, plan.state, plan.industry,
        plan.concept, plan.fp_filter, plan.fp_sort, plan.sort,
    )
    sn_cond = _stock_name_condition(plan.stock_name_op, plan.stock_name)
    if sn_cond is not None:
        ctx.base_stmt = ctx.base_stmt.where(sn_cond)

    # filtered COUNT 必须先于任何重 fetch（与导出语义同源，但先于批次）
    total = await db.scalar(select(func.count()).select_from(ctx.base_stmt.subquery())) or 0
    if total > MAX_EXPORT_ROWS:
        raise _over_limit_error()

    holder = await acquire_lock(EXPORT_LOCK_KEY, EXPORT_LOCK_TTL, generate_holder())
    if holder is None:
        raise _busy_error()

    tmp_dir = tempfile.mkdtemp(prefix="panji-export-")
    try:
        writer = MarketXlsxWriter(plan.columns, tmp_dir)
        batches = (total + EXPORT_BATCH_SIZE - 1) // EXPORT_BATCH_SIZE if total else 0
        max_batch = 0
        for b in range(batches):
            rows = await _fetch_batch_rows(db, ctx, plan, b)
            if rows:
                max_batch = max(max_batch, len(rows))
            # CPU 密集的 XML 写入脱离 event loop
            await asyncio.to_thread(writer.add_rows, rows)
        writer.finalize()
        final_path = os.path.join(tmp_dir, "export.xlsx")
        # 最终 zip 压缩脱离 event loop
        await asyncio.to_thread(writer.build_zip, final_path)
        size = os.path.getsize(final_path)
        await release_lock(EXPORT_LOCK_KEY, holder)
        return final_path, {
            "rows": total,
            "columns": len(plan.columns),
            "batches": batches,
            "max_batch": max_batch,
            "bytes": size,
            "tmp_dir": tmp_dir,
        }
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await release_lock(EXPORT_LOCK_KEY, holder)
        raise


def _over_limit_error():
    from fastapi import HTTPException

    return HTTPException(
        status_code=422,
        detail=f"导出行数超过上限 {MAX_EXPORT_ROWS}，请缩小筛选范围后再导出",
    )


def _busy_error():
    from fastapi import HTTPException

    return HTTPException(status_code=429, detail="导出任务进行中，请稍后再试")


async def stream_market_export(
    db: AsyncSession,
    request: MarketExportRequest,
    user_id: UUID,
    plan: MarketExportPlan,
) -> AsyncIterator[bytes]:
    """[S2-A-C1] 导出流式响应生成器（admin-only 已由端点守卫）。"""
    final_path, stats = await _build_export_file(db, plan, user_id)
    try:
        with open(final_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk
    finally:
        shutil.rmtree(stats["tmp_dir"], ignore_errors=True)
