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
from uuid import UUID

from sqlalchemy import func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument
from app.schemas.export import ExportColumn
from app.schemas.market_stocks import MarketExportRequest
from app.services.distributed_lock import acquire_lock, generate_holder, release_lock
from app.services.excel_export_service import (
    MAX_EXPORT_ROWS,
    MarketXlsxWriter,
)
from app.services.market_stocks_service import (
    MarketQueryContext,
    _assemble_market_query,
)

# ===== 资源常量（硬约束）=====
# MAX_EXPORT_ROWS = 业务允许导出的最大行数（绝不再当 page_size / 一次性 materialization 数量）。
EXPORT_BATCH_SIZE = 250  # 任何一次 export DB fetch 行数上限（200~500 均合规）。
EXPORT_LOCK_KEY = "market_export"  # 全局导出租约（C1a 单 worker 最保守：global=1）。
EXPORT_LOCK_TTL = 600  # 秒；防止租约因请求中断而永久占用。


@dataclass
class MarketExportPlan:
    """归一化后的导出计划（仅 query 语义；输出列由服务端固定控制）。

    导出字段永远只有 Instrument.name + Instrument.symbol（见 MARKET_EXPORT_COLUMNS），
    因此 plan 不再承担 output source planner（needs_price/snapshot/boards/chip 全部删除）。
    筛选/排序语义完整保留，决定导出哪些股票与顺序。
    """

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


# 服务端固定导出列（顺序固定）：第一列股票名称，第二列股票代码。
# 客户端不得指定导出列；筛选/排序仅决定行集合与顺序。
MARKET_EXPORT_COLUMNS: tuple[ExportColumn, ...] = (
    ExportColumn(key="name", title="股票名称", data_type="text"),
    ExportColumn(key="symbol", title="股票代码", data_type="text"),
)


@dataclass(frozen=True)
class PreparedMarketExport:
    """导出准备产物：已落盘的临时 XLSX 文件 + 元数据。

    流式阶段（stream_prepared_market_export）只读取此对象，不再触达 DB / 锁 / count。
    """

    final_path: str
    tmp_dir: str
    rows: int
    columns: int
    batches: int
    max_batch: int
    bytes: int


def build_export_plan(request: MarketExportRequest) -> MarketExportPlan:
    """[S2-A-C1] 将请求归一化为 query-only 导出计划。

    导出列由服务端固定（MARKET_EXPORT_COLUMNS = 股票名称 + 股票代码），
    客户端不指定列；此函数只保留筛选/排序语义。
    """
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


def _build_export_batch_stmt(ctx: MarketQueryContext, batch_index: int):
    """[S2-A-C1a-C3a] 导出分批物理投影：只 SELECT name / symbol。

    复用 _assemble_market_query 装配的 FROM / JOIN / WHERE / ORDER BY，
    但通过 with_only_columns 把目标表达式收窄为 name + symbol，
    不在 DB 层为每只股票计算 id / market / is_watchlisted
    （这些只服务于 list read model，export 不需要）。
    maintain_column_froms=True 保证窄投影不会丢掉 JOIN / WHERE / ORDER BY，
    因此 fp_filter / fp_sort / industry-context 筛选 / watchlist join /
    keyword / state / price·change_pct 排序等 query semantics 全部保留。
    """
    return (
        ctx.base_stmt.with_only_columns(
            Instrument.name,
            Instrument.symbol,
            maintain_column_froms=True,
        )
        .limit(EXPORT_BATCH_SIZE)
        .offset(batch_index * EXPORT_BATCH_SIZE)
    )


async def _fetch_batch_rows(
    db: AsyncSession, ctx: MarketQueryContext, batch_index: int
) -> list[dict[str, str]]:
    """[S2-A-C1a-C3a] 轻量分批读取：仅执行已收窄为 name / symbol 的导出投影，最多 EXPORT_BATCH_SIZE 行。

    输出 dict 严格只有 name、symbol 两个 key；绝不随后再查询
    bars / snapshot / chip / boards。筛选/排序所需的 source JOIN 仍由
    _assemble_market_query 在 query 装配阶段处理，不在本函数做第二轮 enrichment。
    """
    stmt = _build_export_batch_stmt(ctx, batch_index)
    result = await db.execute(stmt)
    base_rows = result.all()
    return [
        {"name": row.name, "symbol": row.symbol}
        for row in base_rows
    ]


async def prepare_market_export(
    db: AsyncSession, plan: MarketExportPlan, user_id: UUID
) -> PreparedMarketExport:
    """[S2-A-C1] 导出准备阶段：全局租约 → 组装查询 → filtered count → 有界分批 → 低内存 XLSX。

    硬合同：422（超限）/ 429（忙）/ query 异常 / writer 异常 **全部发生在此函数内**，
    即 `StreamingResponse` 创建之前。流式阶段（stream_prepared_market_export）只负责
    读已落盘文件并清理临时目录，不得访问 DB / 取锁 / 做 count / 抛业务 HTTPException。
    """
    # 1) 全局导出租约必须先于任何重 DB 工作（从 count 起即受 lease 保护）
    holder = await acquire_lock(EXPORT_LOCK_KEY, EXPORT_LOCK_TTL, generate_holder())
    if holder is None:
        raise _busy_error()

    tmp_dir = tempfile.mkdtemp(prefix="panji-export-")
    try:
        ctx = await _assemble_market_query(
            db, user_id, plan.scope, plan.query, plan.state, plan.industry,
            plan.concept, plan.fp_filter, plan.fp_sort, plan.sort,
        )
        sn_cond = _stock_name_condition(plan.stock_name_op, plan.stock_name)
        if sn_cond is not None:
            ctx.base_stmt = ctx.base_stmt.where(sn_cond)

        # filtered COUNT：通过 with_only_columns(literal(1), maintain_column_froms=True)
        # 只保留 FROM / JOIN / WHERE，不计算 is_watchlisted / market / name / symbol
        # 等无关 target expression；并去除与导出无关的 ORDER BY。
        count_source = (
            ctx.base_stmt.with_only_columns(
                literal(1), maintain_column_froms=True
            )
            .order_by(None)
            .subquery()
        )
        total = await db.scalar(select(func.count()).select_from(count_source)) or 0
        if total > MAX_EXPORT_ROWS:
            raise _over_limit_error()

        writer = MarketXlsxWriter(MARKET_EXPORT_COLUMNS, tmp_dir)
        batches = (total + EXPORT_BATCH_SIZE - 1) // EXPORT_BATCH_SIZE if total else 0
        max_batch = 0
        for b in range(batches):
            rows = await _fetch_batch_rows(db, ctx, b)
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
        return PreparedMarketExport(
            final_path=final_path,
            tmp_dir=tmp_dir,
            rows=total,
            columns=len(MARKET_EXPORT_COLUMNS),
            batches=batches,
            max_batch=max_batch,
            bytes=size,
        )
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await release_lock(EXPORT_LOCK_KEY, holder)
        raise


async def stream_prepared_market_export(
    prepared: PreparedMarketExport,
) -> AsyncIterator[bytes]:
    """[S2-A-C1] 仅负责把已落盘 XLSX 流式送出并清理临时目录。

    硬合同：不得访问 DB / 不得获取 Redis lock / 不得做 filtered count /
    不得抛业务 HTTPException(422/429)。只允许 open + 分块 read + yield + finally cleanup。
    """
    try:
        with open(prepared.final_path, "rb") as f:
            while True:
                chunk = await asyncio.to_thread(f.read, 65536)
                if not chunk:
                    break
                yield chunk
    finally:
        shutil.rmtree(prepared.tmp_dir, ignore_errors=True)


def _over_limit_error():
    from fastapi import HTTPException

    return HTTPException(
        status_code=422,
        detail=f"导出行数超过上限 {MAX_EXPORT_ROWS}，请缩小筛选范围后再导出",
    )


def _busy_error():
    from fastapi import HTTPException

    return HTTPException(status_code=429, detail="导出任务进行中，请稍后再试")
