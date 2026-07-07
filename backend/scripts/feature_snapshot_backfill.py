"""特征快照历史回补脚本 - 为历史交易日批量生成 feature snapshots。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \
        --start 2026-06-01 --end latest --batch-size 20

参数：
    --start: 起始日期（YYYY-MM-DD，必填）
    --end: 结束日期（YYYY-MM-DD 或 "latest"，默认 "latest" 表示最新 bars_daily 日期）
    --batch-size: 每批 instrument 数（默认 20）
    --resume: 真正跳过已存在 snapshot 的 instrument（按完整唯一键过滤）
    --dry-run: 只打印计划与 missing 统计，不执行写入
    --failure-threshold: 失败比例阈值（默认 0.3，超过则该日 rollback）

Side Effects:
    写入 stock_feature_snapshots 表（upsert 幂等，可重复执行）
    不修改 bars / MonitorState / StrategyRun 等其他表

[Blocker2] 事务边界：
    - backfill_single_date 不内部 commit
    - main 按交易日控制事务：成功 commit，RuntimeError rollback
    - 失败日期不留半成品行

[Blocker3] --resume 真正跳过：
    - 按完整唯一键 (instrument_id, trade_date, tf, adj, schema_version) 查询已存在
    - 只对 missing instrument 调用 compute_for_trade_date
    - dry-run 输出 missing_instruments 数量

[Known Gap] 全量 instrument-first 优化未实现：
    - 当前仍是 date-first：每个交易日遍历全市场，每只股票重复 fetch 1d/15m bars
    - 全量回补 2026-01-01 到当前会非常重
    - 禁止全量生产 backfill，仅用于小范围 resume / dry-run

约束：
    - 不修改 DSA/BB/swing/temporal 数学公式
    - 复用 feature_snapshot_service.compute_for_trade_date
    - 单股失败记录到 degraded_reasons，不阻塞其他股票
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.services.feature_snapshot_service import (
    compute_for_trade_date,
    get_active_a_share_instruments,
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
    """[Blocker3] 查询某交易日已存在 snapshot 的 instrument_id 集合。

    按完整唯一键 (instrument_id, trade_date, primary_timeframe, secondary_timeframe,
    adj, schema_version) 过滤，不是"仍会 upsert 覆盖"。

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


async def backfill_single_date(
    db: AsyncSession,
    trade_date: date,
    *,
    batch_size: int,
    failure_threshold: float,
    resume: bool,
    dry_run: bool,
    primary_timeframe: str = _DEFAULT_PRIMARY_TF,
    secondary_timeframe: str = _DEFAULT_SECONDARY_TF,
    adj: str = _DEFAULT_ADJ,
    schema_version: int = _DEFAULT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """回补单个交易日的 feature snapshots（不内部 commit）。

    [Blocker2] 事务边界：
    - 本函数只负责 upsert（通过 compute_for_trade_date）+ 返回统计，不调用 session.commit()。
    - caller（main）负责：成功时 commit，RuntimeError 时 rollback。

    [Blocker3] --resume 真正跳过：
    - 查询已存在 snapshot 的 instrument_id 集合（按完整唯一键）
    - 从 active instrument 列表中过滤掉已存在的
    - 只对 missing instrument 调用 compute_for_trade_date
    - 不为已存在 row 重新计算

    dry-run 模式：
    - 只统计 missing_instruments 数量
    - 不调用 compute_for_trade_date
    - 返回 dry_run=True + 统计信息

    Returns:
        统计信息 dict
    """
    all_instrument_ids = await get_active_a_share_instruments(db)
    total = len(all_instrument_ids)

    existing_ids: set[uuid.UUID] = set()
    if resume:
        existing_ids = await get_existing_instrument_ids(
            db, trade_date,
            primary_timeframe=primary_timeframe,
            secondary_timeframe=secondary_timeframe,
            adj=adj,
            schema_version=schema_version,
        )

    missing_instrument_ids = [iid for iid in all_instrument_ids if iid not in existing_ids]
    missing_count = len(missing_instrument_ids)
    skipped_existing = len(existing_ids)

    if dry_run:
        logger.info(
            "[backfill][dry-run] trade_date=%s: total=%d, missing=%d, skipped_existing=%d",
            trade_date, total, missing_count, skipped_existing,
        )
        return {
            "trade_date": trade_date.isoformat(),
            "total_instruments": total,
            "missing_instruments": missing_count,
            "skipped_existing": skipped_existing,
            "dry_run": True,
        }

    # 全部已存在，无需计算
    if missing_count == 0:
        logger.info(
            "[backfill] trade_date=%s: 全部 %d instrument 已存在 snapshot，跳过计算",
            trade_date, skipped_existing,
        )
        return {
            "trade_date": trade_date.isoformat(),
            "snapshot_count": 0,
            "failed_count": 0,
            "skipped_existing": skipped_existing,
            "schema_version": schema_version,
        }

    # [Blocker2] 不内部 commit，由 main 控制
    result = await compute_for_trade_date(
        db,
        trade_date,
        missing_instrument_ids,
        batch_size=batch_size,
        failure_threshold=failure_threshold,
    )
    result["skipped_existing"] = skipped_existing
    logger.info(
        "[backfill] trade_date=%s 完成: snapshot_count=%d, failed_count=%d, skipped_existing=%d",
        trade_date,
        result.get("snapshot_count", 0),
        result.get("failed_count", 0),
        skipped_existing,
    )
    return result


async def main(args: argparse.Namespace) -> None:
    """主入口：解析参数，遍历交易日，批量回补。

    [Blocker2] 每个交易日独立事务：
    - 成功 → commit
    - RuntimeError → rollback 半成品 → 继续下一日
    - 其他异常 → rollback → 记录 error → 继续下一日
    """
    start_date = date.fromisoformat(args.start)

    # 解析 end 日期 + 获取交易日列表（使用临时 session）
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

        logger.info(
            "[backfill] 计划回补 %d 个交易日: %s ~ %s (batch_size=%d, "
            "resume=%s, dry_run=%s, failure_threshold=%s)",
            len(trade_dates),
            trade_dates[0],
            trade_dates[-1],
            args.batch_size,
            args.resume,
            args.dry_run,
            args.failure_threshold,
        )

        # [Blocker3] dry-run 阶段：先汇总 missing 统计
        if args.dry_run:
            total_missing = 0
            total_active = 0
            for td in trade_dates:
                all_instruments = await get_active_a_share_instruments(db)
                total_active = max(total_active, len(all_instruments))
                if args.resume:
                    existing = await get_existing_instrument_ids(db, td)
                    missing = len(all_instruments) - len(existing)
                else:
                    missing = len(all_instruments)
                total_missing += missing
                logger.info(
                    "[backfill][dry-run] trade_date=%s: active=%d, missing=%d",
                    td, len(all_instruments), missing,
                )
            logger.info(
                "[backfill][dry-run] 汇总: trade_dates=%d, active_instruments=%d, "
                "missing_snapshots=%d (估计 rows)",
                len(trade_dates), total_active, total_missing,
            )

    # [Blocker2] 逐交易日回补，每个交易日独立 session + 事务
    all_results: list[dict[str, Any]] = []
    for td in trade_dates:
        async with AsyncSessionLocal() as db:
            try:
                result = await backfill_single_date(
                    db,
                    td,
                    batch_size=args.batch_size,
                    failure_threshold=args.failure_threshold,
                    resume=args.resume,
                    dry_run=args.dry_run,
                )
                if not args.dry_run:
                    # [Blocker2] 成功 → commit
                    await db.commit()
                all_results.append(result)
            except Exception as exc:
                # [Blocker2] RuntimeError（失败比例超阈值）或其他异常：
                # rollback 该日所有半成品，避免 watchlist 读取
                if not args.dry_run:
                    await db.rollback()
                logger.error(
                    "[backfill] trade_date=%s 回补失败，已 rollback 半成品: %s",
                    td, exc, exc_info=True,
                )
                all_results.append({
                    "trade_date": td.isoformat(),
                    "error": str(exc),
                })

    # 汇总
    total_snapshots = sum(
        r.get("snapshot_count", 0) for r in all_results
    )
    total_failed = sum(
        r.get("failed_count", 0) for r in all_results
    )
    error_dates = [r["trade_date"] for r in all_results if "error" in r]

    logger.info(
        "[backfill] 全部完成: %d 个交易日, snapshot_count=%d, failed_count=%d, "
        "error_dates=%s",
        len(trade_dates),
        total_snapshots,
        total_failed,
        error_dates,
    )


def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="特征快照历史回补脚本",
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
        help="每批 instrument 数（默认 20）",
    )
    parser.add_argument(
        "--failure-threshold",
        type=float,
        default=0.3,
        help="失败比例阈值（默认 0.3）",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="跳过已有 snapshot 的 instrument（按完整唯一键过滤，不重新计算）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与 missing 统计，不执行写入",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
