"""Task 15.2: 批量补合并 ETF/基金的周线/月线数据（从日线合并）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.backfill_etf_weekly_monthly

功能：
1. 查询有日线但无周线/月线的 instrument_id 列表（差集）
2. 串行调用 refresh_weekly_bars / refresh_monthly_bars 从日线合并并 upsert
3. 支持断点续传：每次启动重新查询差集，已合并的自动跳过
4. tqdm 进度条（position=0 固定底部），失败信息走 logger 避免刷屏
5. 统计成功/失败数量并打印失败明细

设计说明：
- refresh_weekly_bars / refresh_monthly_bars 从 DB 日线合并，不涉及 pytdx
- adapter 参数传 None（保留兼容性但不再使用）
- count 参数：周线 200、月线 50（默认值，仅用于估算日线回溯天数）
- upsert 幂等，可重复执行
- 单只失败不中断整体流程，最后汇总失败列表供人工排查
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field

# 确保可以 import app.*（兼容 -m scripts.xxx 与直接 python scripts/xxx.py 两种调用方式）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tqdm import tqdm

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily, BarMonthly, BarWeekly
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    refresh_monthly_bars,
    refresh_weekly_bars,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill_etf_weekly_monthly")

# 周线/月线合并参数（与 refresh_*_bars 默认值一致）
WEEKLY_COUNT = 200
MONTHLY_COUNT = 50


@dataclass
class BackfillResult:
    """单只 instrument 的合并结果。"""

    instrument_id: uuid.UUID
    symbol: str
    name: str
    weekly_rows: int = 0
    monthly_rows: int = 0
    success: bool = False
    error: str | None = None


@dataclass
class BatchStats:
    """批量执行统计。"""

    total: int = 0
    success: int = 0
    failed: int = 0
    skipped: int = 0
    failures: list[BackfillResult] = field(default_factory=list)


async def query_missing_instruments(
    db: AsyncSession,
) -> list[tuple[uuid.UUID, str, str]]:
    """查询有日线但无周线（或无月线）的 instrument_id 列表。

    断点续传：每次启动重新查询差集，已合并的自动跳过。
    取周线差集与月线差集的并集（任一缺失即需处理）。

    Args:
        db: 异步会话

    Returns:
        list of (instrument_id, symbol, name)，按 symbol 排序
    """
    # 查询三张表的 distinct instrument_id
    daily_ids = {row[0] for row in (await db.execute(select(BarDaily.instrument_id).distinct())).all()}
    weekly_ids = {row[0] for row in (await db.execute(select(BarWeekly.instrument_id).distinct())).all()}
    monthly_ids = {row[0] for row in (await db.execute(select(BarMonthly.instrument_id).distinct())).all()}

    # 差集并集：有日线但（无周线 或 无月线）
    missing_ids = (daily_ids - weekly_ids) | (daily_ids - monthly_ids)
    logger.info(
        "差集统计: daily=%d weekly=%d monthly=%d 待补=%d",
        len(daily_ids), len(weekly_ids), len(monthly_ids), len(missing_ids),
    )

    if not missing_ids:
        return []

    # 查询 instrument 详情（symbol + name），按 symbol 排序
    stmt = (
        select(Instrument.id, Instrument.symbol, Instrument.name)
        .where(Instrument.id.in_(sorted(missing_ids)))
        .order_by(Instrument.symbol)
    )
    result = await db.execute(stmt)
    return [(row[0], row[1], row[2]) for row in result.all()]


async def backfill_one(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
    name: str,
) -> BackfillResult:
    """合并单只 instrument 的周线 + 月线。

    单只失败不吞异常：捕获后记录 error 返回，由上层决定是否继续。

    Args:
        db: 异步会话
        instrument_id: 标的 UUID
        symbol: 股票代码
        name: 股票名称

    Returns:
        BackfillResult: 合并结果
    """
    result = BackfillResult(instrument_id=instrument_id, symbol=symbol, name=name)
    try:
        # 合并周线（从 DB 日线合并，不涉及 pytdx）
        weekly_df = await refresh_weekly_bars(db, instrument_id, count=WEEKLY_COUNT, adapter=None)
        result.weekly_rows = 0 if weekly_df.empty else len(weekly_df)

        # 合并月线（从 DB 日线合并，不涉及 pytdx）
        monthly_df = await refresh_monthly_bars(db, instrument_id, count=MONTHLY_COUNT, adapter=None)
        result.monthly_rows = 0 if monthly_df.empty else len(monthly_df)

        result.success = True
    except Exception as exc:
        # 不吞异常：记录原始异常信息供排查，re-raise 由上层统计
        logger.error("合并失败 symbol=%s name=%s: %s", symbol, name, exc)
        result.error = str(exc)
    return result


async def backfill_batch(
    instruments: list[tuple[uuid.UUID, str, str]],
) -> BatchStats:
    """串行批量合并周线/月线。

    使用共享 session（串行，无并发），tqdm 进度条 position=0 固定底部。
    失败信息走 logger（不刷新进度条 postfix），避免刷屏。

    Args:
        instruments: list of (instrument_id, symbol, name)

    Returns:
        BatchStats: 批量执行统计
    """
    stats = BatchStats(total=len(instruments))

    async with AsyncSessionLocal() as db:
        pbar = tqdm(
            instruments,
            desc="补合并 ETF 周线/月线",
            unit="inst",
            position=0,
            leave=True,
        )
        for instrument_id, symbol, name in pbar:
            res = await backfill_one(db, instrument_id, symbol, name)
            if res.success:
                stats.success += 1
            else:
                stats.failed += 1
                stats.failures.append(res)
        pbar.close()

    return stats


async def main() -> None:
    """主入口：查询差集 -> 批量合并 -> 打印统计。"""
    print("=" * 60)
    print("Task 15.2: 批量补合并 ETF/基金的周线/月线数据")
    print("=" * 60)

    # 1. 查询差集（断点续传：已合并的自动跳过）
    async with AsyncSessionLocal() as db:
        instruments = await query_missing_instruments(db)

    if not instruments:
        print("\n[INFO] 无需补合并：周线/月线已覆盖所有有日线的 instrument")
        return

    print(f"\n待补合并: {len(instruments)} 只")
    print(f"参数: weekly_count={WEEKLY_COUNT}, monthly_count={MONTHLY_COUNT}")
    print("-" * 60)

    # 2. 批量合并
    stats = await backfill_batch(instruments)

    # 3. 打印统计
    print("\n" + "=" * 60)
    print("执行统计")
    print("=" * 60)
    print(f"  总数: {stats.total}")
    print(f"  成功: {stats.success}")
    print(f"  失败: {stats.failed}")
    if stats.failures:
        print(f"\n--- 失败明细（{len(stats.failures)} 只）---")
        for f in stats.failures:
            print(f"  {f.symbol} {f.name}: {f.error}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
