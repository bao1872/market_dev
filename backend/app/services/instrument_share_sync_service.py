"""Instrument 股本同步服务 - 每日从 pytdx 同步总股本/流通股本。

CHANGE-20260713-010: 用于 quote 端点市值计算的数据源。
- 每日定时调用 pytdx get_finance_info 获取 zongguben/liutongguben/updated_date
- 写入 instruments 表 total_share/float_share/share_as_of 列
- 用户请求 quote 时只从 DB 读取，不调用 pytdx（禁止用户请求时第三方联网）
- pytdx 仅支持 SH/SZ，BJ 股票跳过（share 数据保持 null）

资源约束：
- 单进程、单 DB 连接、批次 500
- pytdx 同步调用通过 asyncio.to_thread 包装，不阻塞事件循环
- 单个股票失败不中断整体同步，记录到 failed_symbols

用法：
    from app.services.instrument_share_sync_service import sync_share_capitals
    result = await sync_share_capitals(db)
    # result = {"total": 5000, "succeeded": 4950, "failed": 50, "skipped_bj": 200, "failed_symbols": [...]}
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import PytdxAdapter
from app.models.instrument import Instrument
from app.services.instrument_maintenance_service import stock_symbol_sql_filter

logger = logging.getLogger("instrument_share_sync_service")

# 批量提交大小（每次 commit 的股票数量）
_BATCH_SIZE = 500


async def sync_share_capitals(
    db: AsyncSession,
    *,
    adapter: PytdxAdapter | None = None,
) -> dict[str, Any]:
    """同步全市场 SH/SZ active 股票的总股本/流通股本。

    Args:
        db: 异步数据库会话
        adapter: 可选的 PytdxAdapter（None 时内部创建独立连接）

    Returns:
        dict:
        - total: 查询到的 SH/SZ active 股票总数
        - succeeded: 成功同步数量
        - failed: 失败数量
        - skipped_bj: 跳过的 BJ 股票数（pytdx 不支持）
        - failed_symbols: 失败股票代码列表（前 100 条）
    """
    # 1. 查询所有 active SH/SZ 股票（pytdx 仅支持 SH/SZ）
    stmt = (
        select(Instrument.id, Instrument.symbol, Instrument.market)
        .where(Instrument.status == "active")
        .where(Instrument.market.in_(["SH", "SZ"]))
        .where(stock_symbol_sql_filter(Instrument))
    )
    result = await db.execute(stmt)
    rows = result.fetchall()

    # 同时查询 BJ 股票数（仅用于报告，不同步）
    bj_count_stmt = (
        select(Instrument.id)
        .where(Instrument.status == "active")
        .where(Instrument.market == "BJ")
    )
    bj_result = await db.execute(bj_count_stmt)
    skipped_bj = len(bj_result.fetchall())

    total = len(rows)
    logger.info(
        "[ShareSync] 开始同步: SH/SZ active=%d, BJ skipped=%d", total, skipped_bj,
    )

    if total == 0:
        return {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "skipped_bj": skipped_bj,
            "failed_symbols": [],
        }

    succeeded = 0
    failed = 0
    failed_symbols: list[str] = []
    pending_updates: list[tuple[Any, Decimal | None, Decimal | None, date | None]] = []

    # 2. 使用独立 PytdxAdapter 连接（不影响 bars scheduler 的单例）
    should_close_adapter = adapter is None
    if adapter is None:
        adapter = PytdxAdapter()

    try:
        if should_close_adapter:
            await asyncio.to_thread(adapter.connect)

        # 3. 逐股票拉取股本数据（pytdx 同步调用，通过 to_thread 不阻塞事件循环）
        for i, row in enumerate(rows):
            symbol = row.symbol
            try:
                finance = await asyncio.to_thread(adapter.get_finance_info, symbol)
                if finance is None or finance.get("total_share") is None:
                    failed += 1
                    failed_symbols.append(symbol)
                    continue

                total_share = Decimal(str(finance["total_share"]))
                float_share = (
                    Decimal(str(finance["float_share"]))
                    if finance.get("float_share") is not None
                    else None
                )
                share_as_of = finance.get("share_as_of")

                pending_updates.append((row.id, total_share, float_share, share_as_of))
                succeeded += 1

                # 批量提交
                if len(pending_updates) >= _BATCH_SIZE:
                    await _flush_updates(db, pending_updates)
                    pending_updates.clear()

                if (i + 1) % 500 == 0:
                    logger.info(
                        "[ShareSync] 进度: %d/%d (succeeded=%d, failed=%d)",
                        i + 1, total, succeeded, failed,
                    )

            except Exception as exc:
                failed += 1
                failed_symbols.append(symbol)
                logger.warning("[ShareSync] 失败 symbol=%s: %s", symbol, exc)

        # 4. 提交剩余更新
        if pending_updates:
            await _flush_updates(db, pending_updates)
            pending_updates.clear()

    finally:
        if should_close_adapter:
            adapter.disconnect()

    logger.info(
        "[ShareSync] 完成: total=%d succeeded=%d failed=%d skipped_bj=%d",
        total, succeeded, failed, skipped_bj,
    )

    return {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_bj": skipped_bj,
        "failed_symbols": failed_symbols[:100],
    }


async def _flush_updates(
    db: AsyncSession,
    updates: list[tuple[Any, Decimal | None, Decimal | None, date | None]],
) -> None:
    """批量更新 instruments 表的股本字段。

    Args:
        db: 异步数据库会话
        updates: [(instrument_id, total_share, float_share, share_as_of), ...]
    """
    for instrument_id, total_share, float_share, share_as_of in updates:
        await db.execute(
            update(Instrument)
            .where(Instrument.id == instrument_id)
            .values(
                total_share=total_share,
                float_share=float_share,
                share_as_of=share_as_of,
            )
        )
    await db.commit()
