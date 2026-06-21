"""Task 16.4: 批量回补全市场个股的 15min/60min 行情数据。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.backfill_15min_60min_all

功能：
1. 查询全市场有日线数据的 active 个股（预期约 6166 只）
2. 串行调用 refresh_15min_bars（count=15000）+ refresh_60min_bars（count=4000）
3. 支持断点续传：跳过已有 15min/60min 数据的 instrument
4. tqdm 进度条（position=0 固定底部），失败信息走 logger 避免刷屏
5. 统计成功/失败数量并打印失败明细

设计说明：
- pytdx 串行拉取，不支持并发（使用共享 adapter）
- count=15000（15min）约回补 2 年，count=4000（60min）约回补 2 年
- pytdx 服务端对 15min/60min 历史数据有返回上限（约 8000 条），非代码问题
- 股票间延迟 0.3s 避免 pytdx 限流
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

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, Bar60Min, BarDaily
from app.models.instrument import Instrument
from app.repositories.bar_repository import refresh_15min_bars, refresh_60min_bars

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill_15min_60min_all")

# 回补参数（与 BACKFILL_COUNTS 一致）
COUNT_15MIN = 15000  # 15min 回补到 2023-01-01 约需 13264 条
COUNT_60MIN = 4000  # 60min 回补到 2023-01-01 约需 3316 条
STOCK_DELAY = 0.3  # 股票间延迟（秒），避免 pytdx 限流


@dataclass
class BackfillResult:
    """单只 instrument 的回补结果。"""

    instrument_id: uuid.UUID
    symbol: str
    name: str
    min15_rows: int = 0
    min60_rows: int = 0
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


async def query_target_instruments(
    db: AsyncSession,
) -> list[tuple[uuid.UUID, str, str]]:
    """查询需要回补 15min/60min 的 instrument 列表。

    断点续传：已有 15min 且已有 60min 的 instrument 跳过。
    取有日线但（无 15min 或 无 60min）的 instrument 并集。

    Args:
        db: 异步会话

    Returns:
        list of (instrument_id, symbol, name)，按 symbol 排序
    """
    # 查询三张表的 distinct instrument_id
    daily_ids = {
        row[0] for row in (await db.execute(select(BarDaily.instrument_id).distinct())).all()
    }
    min15_ids = {
        row[0] for row in (await db.execute(select(Bar15Min.instrument_id).distinct())).all()
    }
    min60_ids = {
        row[0] for row in (await db.execute(select(Bar60Min.instrument_id).distinct())).all()
    }

    # 差集并集：有日线但（无 15min 或 无 60min）
    missing_ids = (daily_ids - min15_ids) | (daily_ids - min60_ids)
    logger.info(
        "差集统计: daily=%d 15min=%d 60min=%d 待补=%d",
        len(daily_ids), len(min15_ids), len(min60_ids), len(missing_ids),
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
    adapter: PytdxAdapter,
) -> BackfillResult:
    """回补单只 instrument 的 15min + 60min。

    单只失败不吞异常：捕获后记录 error 返回，由上层决定是否继续。

    Args:
        db: 异步会话
        instrument_id: 标的 UUID
        symbol: 股票代码
        name: 股票名称
        adapter: pytdx 适配器（共享实例）

    Returns:
        BackfillResult: 回补结果
    """
    result = BackfillResult(instrument_id=instrument_id, symbol=symbol, name=name)
    try:
        # 15min 回补
        df15 = await refresh_15min_bars(db, instrument_id, count=COUNT_15MIN, adapter=adapter)
        result.min15_rows = 0 if df15.empty else len(df15)

        # 60min 回补
        df60 = await refresh_60min_bars(db, instrument_id, count=COUNT_60MIN, adapter=adapter)
        result.min60_rows = 0 if df60.empty else len(df60)

        result.success = True
    except Exception as exc:
        # 不吞异常：记录原始异常信息供排查
        logger.error("回补失败 symbol=%s name=%s: %s", symbol, name, exc)
        result.error = str(exc)
    return result


async def backfill_batch(
    instruments: list[tuple[uuid.UUID, str, str]],
) -> BatchStats:
    """串行批量回补 15min/60min。

    使用共享 session + 共享 pytdx adapter（串行，无并发）。
    tqdm 进度条 position=0 固定底部，失败信息走 logger 避免刷屏。

    Args:
        instruments: list of (instrument_id, symbol, name)

    Returns:
        BatchStats: 批量执行统计
    """
    stats = BatchStats(total=len(instruments))
    adapter = get_pytdx_adapter()

    async with AsyncSessionLocal() as db:
        pbar = tqdm(
            instruments,
            desc="回补 15min/60min 全市场",
            unit="stock",
            position=0,
            leave=True,
        )
        for instrument_id, symbol, name in pbar:
            pbar.set_postfix_str(f"{symbol}", refresh=False)
            res = await backfill_one(db, instrument_id, symbol, name, adapter)
            if res.success:
                stats.success += 1
            else:
                stats.failed += 1
                stats.failures.append(res)
            await asyncio.sleep(STOCK_DELAY)
        pbar.close()

    return stats


async def main() -> None:
    """主入口：查询差集 -> 批量回补 -> 打印统计。"""
    print("=" * 60)
    print("Task 16.4: 批量回补全市场 15min/60min 行情数据")
    print("=" * 60)

    # 1. 查询差集（断点续传：已回补的自动跳过）
    async with AsyncSessionLocal() as db:
        instruments = await query_target_instruments(db)

    if not instruments:
        print("\n[INFO] 无需回补：15min/60min 已覆盖所有有日线的 instrument")
        return

    print(f"\n待回补: {len(instruments)} 只")
    print(f"参数: 15min_count={COUNT_15MIN}, 60min_count={COUNT_60MIN}, delay={STOCK_DELAY}s")
    print("-" * 60)

    # 2. 批量回补
    stats = await backfill_batch(instruments)

    # 3. 打印统计
    print("\n" + "=" * 60)
    print("执行统计")
    print("=" * 60)
    print(f"  总数: {stats.total}")
    print(f"  成功: {stats.success}")
    print(f"  失败: {stats.failed}")
    if stats.failures:
        print("\n--- 失败明细（前 20 只）---")
        for f in stats.failures[:20]:
            print(f"  {f.symbol} {f.name}: {f.error}")
        if len(stats.failures) > 20:
            print(f"  ... 共 {len(stats.failures)} 只失败")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
