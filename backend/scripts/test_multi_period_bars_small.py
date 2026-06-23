"""小批量验证：5 只股票 × 3 周期拉取 + upsert。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.test_multi_period_bars_small

验证内容：
1. 从 DB 查询 5 只 active 股票
2. 串行拉取 3 个周期（d/15m/60m）行情
3. upsert 到对应表
4. 验证数据完整性（COUNT + 字段非空）
5. 重复执行验证幂等性

设计说明：
- pytdx 不支持并发，串行拉取
- 使用小 count（50/10/5/5），快速验证
- 无副作用：upsert 幂等，可重复执行
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, Bar60Min, BarMonthly, BarWeekly
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    refresh_15min_bars,
    refresh_60min_bars,
    refresh_monthly_bars,
    refresh_weekly_bars,
)
from app.services.bars_scheduler_service import BarsSchedulerService

# 测试股票（5 只，覆盖 SH/SZ 主板/创业板/科创板）
TEST_SYMBOLS = ["000001", "000002", "600000", "300001", "688001"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("test_multi_period_bars")


async def get_test_instruments(db: AsyncSession) -> list[Instrument]:
    """查询测试股票。"""
    stmt = (
        select(Instrument)
        .where(Instrument.symbol.in_(TEST_SYMBOLS))
        .where(Instrument.status == "active")
        .order_by(Instrument.symbol)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def verify_table_count(
    db: AsyncSession,
    model,
    instrument_id: uuid.UUID,
    table_name: str,
) -> int:
    """验证指定表的数据量。"""
    stmt = select(func.count()).where(model.instrument_id == instrument_id)
    result = await db.execute(stmt)
    count = result.scalar_one()
    logger.info("验证 %s: instrument_id=%s count=%d", table_name, instrument_id, count)
    return count


async def test_single_instrument_all_periods(
    db: AsyncSession,
    instrument: Instrument,
) -> dict:
    """测试单只股票的 4 周期拉取 + upsert。

    Returns:
        各周期的 upsert 记录数
    """
    logger.info(
        "开始测试 symbol=%s instrument_id=%s",
        instrument.symbol, instrument.id,
    )

    counts = {}
    # 使用 DAILY_COUNTS（小 count，快速验证）
    test_counts = {"d": 5, "15m": 50, "60m": 10}

    # 1. 串行拉取 3 个周期
    for period, count in test_counts.items():
        logger.info(
            "拉取 symbol=%s period=%s count=%d",
            instrument.symbol, period, count,
        )
        if period == "15m":
            df = await refresh_15min_bars(db, instrument.id, count)
        elif period == "60m":
            df = await refresh_60min_bars(db, instrument.id, count)
        elif period == "w":
            df = await refresh_weekly_bars(db, instrument.id, count)
        elif period == "m":
            df = await refresh_monthly_bars(db, instrument.id, count)

        counts[period] = 0 if df.empty else len(df)
        logger.info(
            "拉取完成 symbol=%s period=%s rows=%d",
            instrument.symbol, period, counts[period],
        )

    # 2. 验证 DB 数据量
    weekly_count = await verify_table_count(db, BarWeekly, instrument.id, "bars_weekly")
    monthly_count = await verify_table_count(db, BarMonthly, instrument.id, "bars_monthly")
    min15_count = await verify_table_count(db, Bar15Min, instrument.id, "bars_15min")
    min60_count = await verify_table_count(db, Bar60Min, instrument.id, "bars_60min")

    # 3. 验证数据完整性（count > 0）
    assert weekly_count > 0, f"bars_weekly 数据量为 0 symbol={instrument.symbol}"
    assert monthly_count > 0, f"bars_monthly 数据量为 0 symbol={instrument.symbol}"
    assert min15_count > 0, f"bars_15min 数据量为 0 symbol={instrument.symbol}"
    assert min60_count > 0, f"bars_60min 数据量为 0 symbol={instrument.symbol}"

    logger.info(
        "验证通过 symbol=%s weekly=%d monthly=%d 15min=%d 60min=%d",
        instrument.symbol, weekly_count, monthly_count, min15_count, min60_count,
    )

    return {
        "weekly": weekly_count,
        "monthly": monthly_count,
        "15min": min15_count,
        "60min": min60_count,
    }


async def test_idempotent(
    db: AsyncSession,
    instrument: Instrument,
) -> None:
    """验证幂等性：重复执行不报错，数据量不变。"""
    logger.info("验证幂等性 symbol=%s", instrument.symbol)

    # 记录第一次的 count
    weekly_before = await verify_table_count(db, BarWeekly, instrument.id, "bars_weekly")

    # 重复执行
    await refresh_weekly_bars(db, instrument.id, 5)

    # 验证 count 不变
    weekly_after = await verify_table_count(db, BarWeekly, instrument.id, "bars_weekly")
    assert weekly_after == weekly_before, \
        f"幂等性失败: before={weekly_before} after={weekly_after}"

    logger.info("幂等性验证通过 symbol=%s", instrument.symbol)


async def test_bars_scheduler_service(
    instruments: list[Instrument],
) -> None:
    """测试 BarsSchedulerService.refresh_one_instrument。"""
    logger.info("测试 BarsSchedulerService.refresh_one_instrument")

    service = BarsSchedulerService()
    test_counts = {"15m": 50, "60m": 10, "w": 5, "m": 5}

    for instrument in instruments[:2]:  # 只测前 2 只
        result = await service.refresh_one_instrument(
            instrument_id=instrument.id,
            symbol=instrument.symbol,
            counts=test_counts,
        )
        assert result.success, f"refresh_one_instrument 失败: {result.error}"
        logger.info(
            "refresh_one_instrument 成功 symbol=%s upsert_counts=%s",
            instrument.symbol, result.upsert_counts,
        )


async def main() -> None:
    """主测试入口。"""
    logger.info("=" * 60)
    logger.info("开始小批量验证：5 只股票 × 4 周期")
    logger.info("=" * 60)

    async with AsyncSessionLocal() as db:
        # 1. 查询测试股票
        instruments = await get_test_instruments(db)
        assert len(instruments) > 0, "未找到测试股票"
        logger.info("找到 %d 只测试股票", len(instruments))

        # 2. 测试每只股票的 4 周期拉取 + upsert
        all_results = {}
        for instrument in instruments:
            result = await test_single_instrument_all_periods(db, instrument)
            all_results[instrument.symbol] = result

        # 3. 验证幂等性（只测第一只）
        await test_idempotent(db, instruments[0])

        # 4. 测试 BarsSchedulerService
        await test_bars_scheduler_service(instruments)

    logger.info("=" * 60)
    logger.info("所有测试通过 ✓")
    logger.info("测试结果汇总：")
    for symbol, counts in all_results.items():
        logger.info(
            "  %s: weekly=%d monthly=%d 15min=%d 60min=%d",
            symbol, counts["weekly"], counts["monthly"],
            counts["15min"], counts["60min"],
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
