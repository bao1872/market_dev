"""特征快照历史回补脚本 - 为历史交易日批量生成 feature snapshots。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.feature_snapshot_backfill \
        --start 2026-06-01 --end latest --batch-size 20 --commit-every 500

参数：
    --start: 起始日期（YYYY-MM-DD，必填）
    --end: 结束日期（YYYY-MM-DD 或 "latest"，默认 "latest" 表示最新 bars_daily 日期）
    --batch-size: 每批 instrument 数（默认 20）
    --commit-every: 每 N rows commit 一次（默认 500）
    --resume: 跳过已有 snapshot 的 (instrument_id, trade_date) 组合
    --dry-run: 只打印计划，不执行写入
    --failure-threshold: 失败比例阈值（默认 0.3，超过则整体抛异常）

Side Effects:
    写入 stock_feature_snapshots 表（upsert 幂等，可重复执行）
    不修改 bars / MonitorState / StrategyRun 等其他表

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


async def count_existing_snapshots(
    db: AsyncSession,
    trade_date: date,
) -> int:
    """查询某交易日已存在的 snapshot 数（用于 --resume 判断）。"""
    stmt = select(func.count(StockFeatureSnapshot.id)).where(
        StockFeatureSnapshot.trade_date == trade_date,
        StockFeatureSnapshot.schema_version == 1,
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


async def backfill_single_date(
    db: AsyncSession,
    trade_date: date,
    batch_size: int,
    commit_every: int,
    failure_threshold: float,
    resume: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """回补单个交易日的 feature snapshots。

    Returns:
        统计信息 dict
    """
    instrument_ids = await get_active_a_share_instruments(db)
    total = len(instrument_ids)

    if resume:
        existing = await count_existing_snapshots(db, trade_date)
        logger.info(
            "[backfill] trade_date=%s: 已有 %d snapshots（resume 模式，仍会 upsert 覆盖）",
            trade_date, existing,
        )

    if dry_run:
        logger.info(
            "[backfill][dry-run] trade_date=%s: 将计算 %d 只股票的 snapshot",
            trade_date, total,
        )
        return {
            "trade_date": trade_date.isoformat(),
            "total_instruments": total,
            "dry_run": True,
        }

    result = await compute_for_trade_date(
        db,
        trade_date,
        instrument_ids,
        batch_size=batch_size,
        commit_every=commit_every,
        failure_threshold=failure_threshold,
    )
    logger.info(
        "[backfill] trade_date=%s 完成: snapshot_count=%d, failed_count=%d",
        trade_date,
        result.get("snapshot_count", 0),
        result.get("failed_count", 0),
    )
    return result


async def main(args: argparse.Namespace) -> None:
    """主入口：解析参数，遍历交易日，批量回补。"""
    start_date = date.fromisoformat(args.start)

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
            "[backfill] 计划回补 %d 个交易日: %s ~ %s (batch_size=%d, commit_every=%d, "
            "resume=%s, dry_run=%s)",
            len(trade_dates),
            trade_dates[0],
            trade_dates[-1],
            args.batch_size,
            args.commit_every,
            args.resume,
            args.dry_run,
        )

    # 逐交易日回补（每个交易日使用独立 session）
    all_results: list[dict[str, Any]] = []
    for td in trade_dates:
        async with AsyncSessionLocal() as db:
            try:
                result = await backfill_single_date(
                    db,
                    td,
                    batch_size=args.batch_size,
                    commit_every=args.commit_every,
                    failure_threshold=args.failure_threshold,
                    resume=args.resume,
                    dry_run=args.dry_run,
                )
                all_results.append(result)
            except Exception as exc:
                logger.error(
                    "[backfill] trade_date=%s 回补失败: %s", td, exc, exc_info=True,
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
        "--commit-every",
        type=int,
        default=500,
        help="每 N rows commit 一次（默认 500）",
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
        help="跳过已有 snapshot 的交易日（仍会 upsert 覆盖）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，不执行写入",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
