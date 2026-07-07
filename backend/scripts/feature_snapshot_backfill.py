"""特征快照历史回补脚本 - instrument-first 优化版。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \\
        --start 2026-01-01 --end latest --batch-size 20

参数：
    --start: 起始日期（YYYY-MM-DD，必填）
    --end: 结束日期（YYYY-MM-DD 或 "latest"，默认 "latest" 表示最新 bars_daily 日期）
    --batch-size: 每批 instrument 数（默认 20，保守内存）
    --symbols: 只处理指定股票代码（逗号分隔，如 000100,603303）
    --limit-instruments: 限制处理的 instrument 数量（用于小样本验证）
    --resume: 跳过已存在 snapshot 且所属 trade_date 的 run.status='succeeded' 的行
    --dry-run: 只打印计划与 missing 统计，不执行写入
    --failure-threshold: 失败比例阈值（默认 0.3，超过则该 trade_date 标 run.status='failed'）

[instrument-first 优化]：
    旧 date-first 实现已废弃（每只 instrument 每个 trade_date 重复 fetch bars）。
    新 instrument-first：每只 instrument 只加载一次 1d/15m bars，遍历 trade_dates 在内存切片。

    for instrument_batch in active instruments:
        一次性加载该 batch 每只股票的 1d/15m bars
        for trade_date in trade_dates:
            compute_feature_snapshot_for_date(primary_bars=..., secondary_bars=...)
            upsert

[事务边界 + run gate]：
    - backfill_instrument_first 不内部 commit，由 main 控制
    - 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
    - 单股失败不阻塞其他股票
    - watchlist 只读取 run.status='succeeded' 的 snapshot（Phase 5 run gate）

约束：
    - 不修改 DSA/BB/swing/temporal 数学公式
    - 复用 feature_snapshot_service.compute_feature_snapshot_for_date
    - 单股失败记录到日志，不阻塞其他股票
    - 不并发，先保证稳定和低内存
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import (
    _fetch_bars_from_db,
    compute_feature_snapshot_for_date,
    create_snapshot_run,
    finish_snapshot_run,
    upsert_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("feature_snapshot_backfill")


# 默认参数（与 feature_snapshot_service 对齐）
_DEFAULT_PRIMARY_TF = "1d"
_DEFAULT_SECONDARY_TF = "15m"
_DEFAULT_ADJ = "qfq"
_DEFAULT_SCHEMA_VERSION = 1


async def get_trade_dates_from_bars(
    db: AsyncSession,
    start: date,
    end: date,
) -> list[date]:
    """从 bars_daily 表查询已有交易日期（确保数据存在再计算 snapshot）。

    Args:
        db: 异步会话
        start: 起始日期
        end: 结束日期

    Returns:
        已有 bars_daily 数据的交易日期列表（升序）
    """
    stmt = (
        select(func.distinct(BarDaily.trade_date))
        .where(BarDaily.trade_date >= start, BarDaily.trade_date <= end)
        .order_by(BarDaily.trade_date.asc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_latest_bar_date(db: AsyncSession) -> date | None:
    """查询 bars_daily 表中最新交易日。"""
    stmt = select(func.max(BarDaily.trade_date))
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_existing_instrument_ids(
    db: AsyncSession,
    trade_date: date,
    *,
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> set[uuid.UUID]:
    """查询某交易日已存在 snapshot 的 instrument_id 集合。

    按完整唯一键 (instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version) 过滤。

    Args:
        db: 异步会话
        trade_date: 业务交易日
        primary_timeframe: 主时间周期
        secondary_timeframe: 次时间周期
        adj: 复权方式
        schema_version: 快照 schema 版本

    Returns:
        已存在 snapshot 的 instrument_id 集合
    """
    stmt = select(StockFeatureSnapshot.instrument_id).where(
        StockFeatureSnapshot.trade_date == trade_date,
        StockFeatureSnapshot.primary_timeframe == primary_timeframe,
        StockFeatureSnapshot.secondary_timeframe == secondary_timeframe,
        StockFeatureSnapshot.adj == adj,
        StockFeatureSnapshot.schema_version == schema_version,
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def get_instruments_for_backfill(
    db: AsyncSession,
    *,
    symbols: list[str] | None = None,
    limit: int | None = None,
) -> list[uuid.UUID]:
    """获取待回补的 instrument 列表（支持 --symbols / --limit-instruments 过滤）。

    Args:
        db: 异步会话
        symbols: 股票代码过滤（如 ['000100', '603303']）；None 表示不过滤
        limit: 限制数量（如 20）；None 表示不限制

    Returns:
        instrument_id 列表
    """
    stmt = select(Instrument.id).where(Instrument.status == "active")

    if symbols is not None and len(symbols) > 0:
        # --symbols 优先于 6 位数字过滤
        stmt = stmt.where(Instrument.symbol.in_(symbols))
    else:
        # 默认只取 A 股（6 位数字 symbol），与 get_active_a_share_instruments 一致
        stmt = stmt.where(Instrument.symbol.op("~")(r"^\d{6}$"))

    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)

    result = await db.execute(stmt)
    return list(result.scalars().all())


async def load_instrument_bars(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """加载单个 instrument 的全部 1d + 15m bars（一次性，供后续内存切片复用）。

    通过复用 feature_snapshot_service._fetch_bars_from_db 实现：
    - 该函数返回 instrument 全部历史 bars（不按 trade_date 过滤）
    - backfill_instrument_first 在内存按 trade_date 切片

    Args:
        db: 异步会话
        instrument_id: 标的 UUID
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 次周期（默认 15m）
        adj: 复权方式（默认 qfq）

    Returns:
        (primary_bars, secondary_bars) 元组；失败时对应位置为 None
    """
    # trade_date 参数对 _fetch_bars_from_db 无实际过滤作用（返回全量历史）
    # 传 date.max 仅作占位符，未来若 _fetch_bars_from_db 改为按日期过滤时需调整
    placeholder_date = date.max
    primary_bars = await _fetch_bars_from_db(
        db, instrument_id, primary_timeframe, adj, placeholder_date,
    )
    secondary_bars = await _fetch_bars_from_db(
        db, instrument_id, secondary_timeframe, adj, placeholder_date,
    )
    return primary_bars, secondary_bars


async def _get_succeeded_trade_dates(
    db: AsyncSession,
    trade_dates: list[date],
    *,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> set[date]:
    """查询哪些 trade_date 已有 succeeded run（用于 --resume 判断）。

    --resume 语义：
    - 跳过 已存在 snapshot 且 所属 trade_date 的 run.status='succeeded' 的行
    - 如果 run.status='failed'，不跳过（允许重试）

    Args:
        db: 异步会话
        trade_dates: 待查询的交易日列表
        schema_version: 快照 schema 版本

    Returns:
        已 succeeded 的 trade_date 集合
    """
    if not trade_dates:
        return set()
    stmt = (
        select(StockFeatureSnapshotRun.trade_date)
        .where(
            StockFeatureSnapshotRun.trade_date.in_(trade_dates),
            StockFeatureSnapshotRun.schema_version == schema_version,
            StockFeatureSnapshotRun.status == STATUS_SUCCEEDED,
        )
        .distinct()
    )
    result = await db.execute(stmt)
    return set(result.scalars().all())


async def backfill_instrument_first(
    db: AsyncSession,
    trade_dates: list[date],
    instruments: list[uuid.UUID],
    *,
    batch_size: int = 20,
    failure_threshold: float = 0.3,
    resume: bool = False,
    dry_run: bool = False,
    run_type: str = "backfill",
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Instrument-first 回补：每只 instrument 加载 bars 一次，遍历 trade_dates 切片。

    [instrument-first 优化]
    - 外层循环按 instrument batch
    - 每只 instrument 只加载 1 次 1d/15m bars
    - 内层循环按 trade_date，在内存切片（compute_feature_snapshot_for_date 内部 _truncate_bars_to_trade_date）

    [事务边界 + run gate]
    - 本函数不内部 commit，由 caller（main）控制
    - 失败比例超阈值的 trade_date 标 run.status='failed'（不抛 RuntimeError）
    - 单股失败不阻塞其他股票
    - 每个 trade_date 创建独立 run 记录（status=running → succeeded/failed）

    dry-run 模式：
    - 只统计 trade_dates / instruments / expected_batches
    - 不调用 load_instrument_bars / compute_feature_snapshot_for_date / upsert_snapshot
    - 不创建 run 记录

    Args:
        db: 异步会话
        trade_dates: 交易日列表
        instruments: instrument_id 列表
        batch_size: 每批 instrument 数（默认 20）
        failure_threshold: 失败比例阈值（默认 0.3）
        resume: 跳过已存在 snapshot 且 succeeded run 的行
        dry_run: 只打印统计，不执行写入
        run_type: run 记录类型（默认 backfill）
        primary_timeframe: 主周期
        secondary_timeframe: 次周期
        adj: 复权方式
        schema_version: 快照 schema 版本

    Returns:
        统计信息 dict
    """
    total_instruments = len(instruments)
    total_dates = len(trade_dates)
    expected_batches = (total_instruments + batch_size - 1) // batch_size if batch_size > 0 else 0

    if dry_run:
        logger.info(
            "[backfill][dry-run] trade_dates=%d, instruments=%d, expected_batches=%d "
            "(batch_size=%d, resume=%s, failure_threshold=%s)",
            total_dates, total_instruments, expected_batches,
            batch_size, resume, failure_threshold,
        )
        return {
            "dry_run": True,
            "trade_dates": total_dates,
            "instruments": total_instruments,
            "expected_batches": expected_batches,
        }

    if total_instruments == 0 or total_dates == 0:
        logger.info(
            "[backfill] 无需处理: trade_dates=%d, instruments=%d",
            total_dates, total_instruments,
        )
        return {
            "dry_run": False,
            "trade_dates": total_dates,
            "instruments": total_instruments,
            "expected_batches": 0,
            "total_snapshots": 0,
            "total_failed": 0,
            "skipped_existing": 0,
        }

    # 为每个 trade_date 创建 running run（publish gate）
    run_records: dict[date, StockFeatureSnapshotRun] = {}
    for td in trade_dates:
        run = await create_snapshot_run(
            db, td, run_type,
            schema_version=schema_version,
            primary_timeframe=primary_timeframe,
            secondary_timeframe=secondary_timeframe,
            adj=adj,
            expected_count=total_instruments,
            metadata={"source": "backfill", "batch_size": batch_size},
        )
        run_records[td] = run

    # resume: 查询已 succeeded 的 trade_date + 已存在的 (instrument, date) snapshot
    succeeded_dates: set[date] = set()
    if resume:
        succeeded_dates = await _get_succeeded_trade_dates(db, trade_dates, schema_version=schema_version)

    # per-date 统计
    per_date_stats: dict[date, dict[str, int]] = {
        td: {"success": 0, "failed": 0, "skipped": 0} for td in trade_dates
    }

    # 预查询 resume 模式下每个 trade_date 的已存在 instrument_id 集合
    existing_per_date: dict[date, set[uuid.UUID]] = {}
    if resume:
        for td in trade_dates:
            if td in succeeded_dates:
                existing_per_date[td] = await get_existing_instrument_ids(
                    db, td,
                    primary_timeframe=primary_timeframe,
                    secondary_timeframe=secondary_timeframe,
                    adj=adj,
                    schema_version=schema_version,
                )
            else:
                # run 不是 succeeded，不跳过（允许重试）
                existing_per_date[td] = set()

    # instrument-first 主循环
    for i in range(0, total_instruments, batch_size):
        batch = instruments[i : i + batch_size]
        for instrument_id in batch:
            # 每只 instrument 只加载一次 bars
            primary_bars, secondary_bars = await load_instrument_bars(
                db, instrument_id,
                primary_timeframe=primary_timeframe,
                secondary_timeframe=secondary_timeframe,
                adj=adj,
            )

            for td in trade_dates:
                # resume 跳过：snapshot 已存在 AND run.status='succeeded'
                if resume and instrument_id in existing_per_date.get(td, set()):
                    per_date_stats[td]["skipped"] += 1
                    continue

                try:
                    snapshot = await compute_feature_snapshot_for_date(
                        db, instrument_id, td,
                        primary_timeframe=primary_timeframe,
                        secondary_timeframe=secondary_timeframe,
                        adj=adj,
                        primary_bars=primary_bars,
                        secondary_bars=secondary_bars,
                    )
                    await upsert_snapshot(db, snapshot)
                    per_date_stats[td]["success"] += 1
                except Exception as exc:
                    per_date_stats[td]["failed"] += 1
                    logger.error(
                        "[backfill] snapshot 计算失败 instrument_id=%s trade_date=%s: %s",
                        instrument_id, td, exc, exc_info=True,
                    )

        # 每个 batch 后 flush（不 commit，由 main 控制）
        await db.flush()
        logger.info(
            "[backfill] batch %d/%d 完成 (instruments %d-%d)",
            i // batch_size + 1, expected_batches,
            i + 1, min(i + batch_size, total_instruments),
        )

    # 为每个 trade_date finalize run（succeeded/failed）
    total_snapshots = 0
    total_failed = 0
    total_skipped = 0
    for td in trade_dates:
        stats = per_date_stats[td]
        snapshots = stats["success"]
        failed = stats["failed"]
        skipped = stats["skipped"]
        total_snapshots += snapshots
        total_failed += failed
        total_skipped += skipped

        processed = snapshots + failed
        failure_rate = failed / processed if processed > 0 else 0.0

        if failure_rate > failure_threshold:
            run_status = STATUS_FAILED
        else:
            run_status = STATUS_SUCCEEDED

        await finish_snapshot_run(
            db, run_records[td],
            status=run_status,
            snapshot_count=snapshots,
            failed_count=failed,
            skipped_count=skipped,
            expected_count=total_instruments,
            failure_rate=failure_rate,
            metadata={
                "source": "backfill",
                "batch_size": batch_size,
                "failure_threshold": failure_threshold,
            },
        )
        logger.info(
            "[backfill] trade_date=%s run finalized: status=%s, "
            "snapshots=%d, failed=%d, skipped=%d, failure_rate=%.2f",
            td, run_status, snapshots, failed, skipped, failure_rate,
        )

    logger.info(
        "[backfill] 全部完成: trade_dates=%d, instruments=%d, "
        "total_snapshots=%d, total_failed=%d, total_skipped=%d",
        total_dates, total_instruments,
        total_snapshots, total_failed, total_skipped,
    )

    return {
        "dry_run": False,
        "trade_dates": total_dates,
        "instruments": total_instruments,
        "expected_batches": expected_batches,
        "total_snapshots": total_snapshots,
        "total_failed": total_failed,
        "skipped_existing": total_skipped,
    }


async def main(args: argparse.Namespace) -> None:
    """主入口：解析参数，加载 trade_dates / instruments，调用 instrument-first 回补。

    事务边界：
    - 单 session 控制整个回补
    - backfill_instrument_first 不内部 commit
    - 成功 → commit；异常 → rollback
    """
    start_date = date.fromisoformat(args.start)

    # 解析 end 日期 + 获取交易日列表 + 获取 instruments（使用临时 session）
    async with AsyncSessionLocal() as db:
        if args.end == "latest":
            end_date = await get_latest_bar_date(db)
            if end_date is None:
                logger.error("[backfill] bars_daily 表无数据，无法确定 end 日期")
                sys.exit(1)
            logger.info("[backfill] end=latest → 使用 bars_daily 最新日期: %s", end_date)
        else:
            end_date = date.fromisoformat(args.end)

        if start_date > end_date:
            logger.error(
                "[backfill] start (%s) > end (%s)", start_date, end_date,
            )
            sys.exit(1)

        # 从 bars_daily 获取实际有数据的交易日
        trade_dates = await get_trade_dates_from_bars(db, start_date, end_date)
        if not trade_dates:
            logger.error(
                "[backfill] bars_daily 表在 %s ~ %s 之间无数据",
                start_date, end_date,
            )
            sys.exit(1)

        # 获取 instruments（支持 --symbols / --limit-instruments）
        instruments = await get_instruments_for_backfill(
            db,
            symbols=args.symbols,
            limit=args.limit_instruments,
        )

        logger.info(
            "[backfill] 计划回补: trade_dates=%d (%s ~ %s), instruments=%d "
            "(batch_size=%d, resume=%s, dry_run=%s, failure_threshold=%s, "
            "symbols=%s, limit_instruments=%s)",
            len(trade_dates),
            trade_dates[0], trade_dates[-1],
            len(instruments),
            args.batch_size,
            args.resume, args.dry_run, args.failure_threshold,
            args.symbols, args.limit_instruments,
        )

        # dry-run 在临时 session 内完成统计（不写库）
        if args.dry_run:
            await backfill_instrument_first(
                db,
                trade_dates=trade_dates,
                instruments=instruments,
                batch_size=args.batch_size,
                failure_threshold=args.failure_threshold,
                resume=args.resume,
                dry_run=True,
            )
            return

    # 非干跑模式：新开 session 执行回补
    if not args.dry_run:
        async with AsyncSessionLocal() as db:
            try:
                result = await backfill_instrument_first(
                    db,
                    trade_dates=trade_dates,
                    instruments=instruments,
                    batch_size=args.batch_size,
                    failure_threshold=args.failure_threshold,
                    resume=args.resume,
                    dry_run=False,
                )
                await db.commit()
                logger.info(
                    "[backfill] 提交完成: total_snapshots=%d, total_failed=%d, "
                    "skipped_existing=%d",
                    result.get("total_snapshots", 0),
                    result.get("total_failed", 0),
                    result.get("skipped_existing", 0),
                )
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "[backfill] 回补失败，已 rollback: %s", exc, exc_info=True,
                )
                raise


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="特征快照历史回补脚本（instrument-first）",
    )
    parser.add_argument(
        "--start",
        required=True,
        help="起始日期（YYYY-MM-DD）",
    )
    parser.add_argument(
        "--end",
        default="latest",
        help='结束日期（YYYY-MM-DD 或 "latest"，默认 latest）',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=20,
        help="每批 instrument 数（默认 20，保守内存）",
    )
    parser.add_argument(
        "--failure-threshold",
        type=float,
        default=0.3,
        help="失败比例阈值（默认 0.3，超过则该 trade_date 标 run.status='failed'）",
    )
    parser.add_argument(
        "--symbols",
        default=None,
        help="只处理指定股票代码（逗号分隔，如 000100,603303）",
    )
    parser.add_argument(
        "--limit-instruments",
        type=int,
        default=None,
        help="限制处理的 instrument 数量（用于小样本验证）",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="跳过已存在 snapshot 且所属 trade_date 的 run.status='succeeded' 的行",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与统计，不执行写入",
    )
    args = parser.parse_args()
    # 解析 --symbols 为 list[str]
    if args.symbols is not None:
        args.symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    return args


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
