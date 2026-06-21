"""Task 15.1: 查询有日线但无周线/月线的 instrument_id 列表。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.query_missing_weekly_monthly

功能：
1. 查询 bars_daily 表中有数据的 instrument_id 集合
2. 查询 bars_weekly / bars_monthly 表中有数据的 instrument_id 集合
3. 计算差集（有日线但无周线/月线的 instrument_id）
4. 打印差集数量和前 10 个示例（symbol + instrument_id）

设计说明：
- 仅查询，无副作用（不写库表）
- 使用 AsyncSessionLocal 异步会话
- 输出周线差集与月线差集，并打印两者的交集（同时缺周线和月线）
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily, BarMonthly, BarWeekly
from app.models.instrument import Instrument

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("query_missing")


async def query_distinct_instrument_ids(
    db: AsyncSession,
    model,
    table_name: str,
) -> set[uuid.UUID]:
    """查询指定表中有数据的 distinct instrument_id 集合。

    Args:
        db: 异步会话
        model: ORM 模型类（BarDaily/BarWeekly/BarMonthly）
        table_name: 表名（仅用于日志）

    Returns:
        instrument_id 的集合
    """
    stmt = select(model.instrument_id).distinct()
    result = await db.execute(stmt)
    ids = {row[0] for row in result.all()}
    logger.info("表 %s distinct instrument_id 数量=%d", table_name, len(ids))
    return ids


async def fetch_symbol_map(
    db: AsyncSession,
    instrument_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[str, str, str]]:
    """查询 instrument_id -> (symbol, name, market) 映射。

    Args:
        db: 异步会话
        instrument_ids: 需要查询的 instrument_id 列表

    Returns:
        dict: instrument_id -> (symbol, name, market)
    """
    if not instrument_ids:
        return {}
    stmt = (
        select(Instrument.id, Instrument.symbol, Instrument.name, Instrument.market)
        .where(Instrument.id.in_(instrument_ids))
    )
    result = await db.execute(stmt)
    return {
        row[0]: (row[1], row[2], row[3]) for row in result.all()
    }


async def main() -> None:
    """主入口：查询差集并打印示例。"""
    print("=" * 60)
    print("Task 15.1: 查询有日线但无周线/月线的 instrument_id 列表")
    print("=" * 60)

    async with AsyncSessionLocal() as db:
        # 1. 查询三张表的 distinct instrument_id
        daily_ids = await query_distinct_instrument_ids(db, BarDaily, "bars_daily")
        weekly_ids = await query_distinct_instrument_ids(db, BarWeekly, "bars_weekly")
        monthly_ids = await query_distinct_instrument_ids(db, BarMonthly, "bars_monthly")

        # 2. 计算差集
        missing_weekly = daily_ids - weekly_ids
        missing_monthly = daily_ids - monthly_ids
        missing_both = missing_weekly & missing_monthly

        print("\n--- 差集统计 ---")
        print(f"  bars_daily 总数: {len(daily_ids)}")
        print(f"  bars_weekly 总数: {len(weekly_ids)}")
        print(f"  bars_monthly 总数: {len(monthly_ids)}")
        print(f"  有日线但无周线: {len(missing_weekly)}")
        print(f"  有日线但无月线: {len(missing_monthly)}")
        print(f"  同时缺周线和月线: {len(missing_both)}")

        # 3. 打印前 10 个示例（同时缺周线和月线的）
        print("\n--- 前 10 个示例（同时缺周线和月线）---")
        sample_ids = sorted(missing_both)[:10]
        symbol_map = await fetch_symbol_map(db, sample_ids)
        for iid in sample_ids:
            info = symbol_map.get(iid, ("(unknown)", "(unknown)", "(unknown)"))
            print(f"  symbol={info[0]:<10s} name={info[1]:<20s} market={info[2]:<4s} instrument_id={iid}")

        # 4. 统计市场分布（同时缺周线和月线的）
        print("\n--- 市场分布（同时缺周线和月线）---")
        all_missing_list = sorted(missing_both)
        all_symbol_map = await fetch_symbol_map(db, all_missing_list)
        market_count: dict[str, int] = {}
        for iid in all_missing_list:
            info = all_symbol_map.get(iid)
            if info:
                m = info[2]
                market_count[m] = market_count.get(m, 0) + 1
        for m, c in sorted(market_count.items()):
            print(f"  {m}: {c}")

    print("\n" + "=" * 60)
    print("查询完成")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
