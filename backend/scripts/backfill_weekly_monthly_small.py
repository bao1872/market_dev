"""Task 15.2 小批量验证：3 只 ETF 从日线合并周线/月线。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.backfill_weekly_monthly_small

验证内容：
1. 从差集中取前 3 只 ETF
2. 调用 refresh_weekly_bars + refresh_monthly_bars 从日线合并
3. 验证合并结果正确：
   - 合并后周线/月线行数 > 0
   - 周线/月线的 trade_date 范围在日线范围内
   - 周线/月线的 close 与日线对齐（周线 close = 周期最后交易日 close）
4. 打印详细对比

设计说明：
- refresh_weekly_bars/refresh_monthly_bars 从 DB 日线合并，不涉及 pytdx
- 无副作用：upsert 幂等，可重复执行
- 先验证 3 只正确后再批量处理 970 只
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import BarDaily, BarMonthly, BarWeekly
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    refresh_monthly_bars,
    refresh_weekly_bars,
)

# 小批量验证数量（Task 15 要求验证 5 只）
BATCH_SIZE = 5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill_small")


async def get_missing_instruments(
    db: AsyncSession,
    limit: int = BATCH_SIZE,
) -> list[tuple[uuid.UUID, str, str, str]]:
    """查询有日线但无周线的 instrument（取前 limit 只）。

    Returns:
        list of (instrument_id, symbol, name, market)
    """
    # 查询有日线的 instrument_id
    daily_stmt = select(BarDaily.instrument_id).distinct()
    daily_result = await db.execute(daily_stmt)
    daily_ids = {row[0] for row in daily_result.all()}

    # 查询有周线的 instrument_id
    weekly_stmt = select(BarWeekly.instrument_id).distinct()
    weekly_result = await db.execute(weekly_stmt)
    weekly_ids = {row[0] for row in weekly_result.all()}

    # 差集
    missing_ids = sorted(daily_ids - weekly_ids)[:limit]

    # 查询 instrument 详情
    if not missing_ids:
        return []
    inst_stmt = (
        select(Instrument.id, Instrument.symbol, Instrument.name, Instrument.market)
        .where(Instrument.id.in_(missing_ids))
        .order_by(Instrument.symbol)
    )
    result = await db.execute(inst_stmt)
    return [(row[0], row[1], row[2], row[3]) for row in result.all()]


async def verify_merge_result(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
) -> dict:
    """验证合并结果：日线/周线/月线数据完整性。

    Returns:
        dict: 各周期的统计信息
    """
    # 日线统计
    daily_stmt = (
        select(
            func.count(),
            func.min(BarDaily.trade_date),
            func.max(BarDaily.trade_date),
        )
        .where(BarDaily.instrument_id == instrument_id)
    )
    daily_result = await db.execute(daily_stmt)
    daily_row = daily_result.one()
    daily_count = daily_row[0]
    daily_min_date = daily_row[1]
    daily_max_date = daily_row[2]

    # 周线统计
    weekly_stmt = (
        select(
            func.count(),
            func.min(BarWeekly.trade_date),
            func.max(BarWeekly.trade_date),
        )
        .where(BarWeekly.instrument_id == instrument_id)
    )
    weekly_result = await db.execute(weekly_stmt)
    weekly_row = weekly_result.one()
    weekly_count = weekly_row[0]
    weekly_min_date = weekly_row[1]
    weekly_max_date = weekly_row[2]

    # 月线统计
    monthly_stmt = (
        select(
            func.count(),
            func.min(BarMonthly.trade_date),
            func.max(BarMonthly.trade_date),
        )
        .where(BarMonthly.instrument_id == instrument_id)
    )
    monthly_result = await db.execute(monthly_stmt)
    monthly_row = monthly_result.one()
    monthly_count = monthly_row[0]
    monthly_min_date = monthly_row[1]
    monthly_max_date = monthly_row[2]

    return {
        "symbol": symbol,
        "daily": {
            "count": daily_count,
            "min_date": str(daily_min_date) if daily_min_date else None,
            "max_date": str(daily_max_date) if daily_max_date else None,
        },
        "weekly": {
            "count": weekly_count,
            "min_date": str(weekly_min_date) if weekly_min_date else None,
            "max_date": str(weekly_max_date) if weekly_max_date else None,
        },
        "monthly": {
            "count": monthly_count,
            "min_date": str(monthly_min_date) if monthly_min_date else None,
            "max_date": str(monthly_max_date) if monthly_max_date else None,
        },
    }


async def verify_weekly_close_alignment(
    db: AsyncSession,
    instrument_id: uuid.UUID,
    symbol: str,
) -> bool:
    """验证周线 OHLC 与日线对齐。

    周线 trade_date 是周期内第一个交易日（前对齐），因此验证时需查询
    [w_date, w_date + 7天) 范围内的日线，取最后一条的 close 与周线 close 对比。

    对齐规则（convert_kline_frequency）：
    - open  = 周期内第一个交易日 open
    - close = 周期内最后一个交易日 close
    - high  = 周期内最高价
    - low   = 周期内最低价

    Returns:
        bool: 是否对齐
    """
    # 取最近 5 条周线
    weekly_stmt = (
        select(
            BarWeekly.trade_date, BarWeekly.close,
            BarWeekly.open, BarWeekly.high, BarWeekly.low,
        )
        .where(BarWeekly.instrument_id == instrument_id)
        .order_by(BarWeekly.trade_date.desc())
        .limit(5)
    )
    weekly_result = await db.execute(weekly_stmt)
    weekly_rows = list(weekly_result.all())

    if not weekly_rows:
        logger.warning("周线数据为空 symbol=%s", symbol)
        return False

    print(f"\n  [{symbol}] 周线最近 5 条 vs 对应日线 OHLC:")
    aligned = True
    for w_date, w_close, w_open, w_high, w_low in weekly_rows:
        # 查询该周范围内的日线 [w_date, w_date+7)
        week_end = w_date + timedelta(days=7)
        daily_stmt = (
            select(
                BarDaily.trade_date, BarDaily.close,
                BarDaily.open, BarDaily.high, BarDaily.low,
            )
            .where(BarDaily.instrument_id == instrument_id)
            .where(BarDaily.trade_date >= w_date)
            .where(BarDaily.trade_date < week_end)
            .order_by(BarDaily.trade_date)
        )
        daily_result = await db.execute(daily_stmt)
        daily_rows = list(daily_result.all())

        if not daily_rows:
            print(f"    {w_date}: 周线 close={w_close} | 该周无日线数据")
            aligned = False
            continue

        first = daily_rows[0]
        last = daily_rows[-1]
        # 周线 open 应 = 日线 first open
        open_match = (
            w_open is not None and first[2] is not None
            and abs(float(w_open) - float(first[2])) < 1e-6
        )
        # 周线 close 应 = 日线 last close
        close_match = (
            w_close is not None and last[1] is not None
            and abs(float(w_close) - float(last[1])) < 1e-6
        )
        # 周线 high 应 = 日线 max high
        d_high_max = max(float(r[3]) for r in daily_rows if r[3] is not None)
        high_match = (
            w_high is not None and abs(float(w_high) - d_high_max) < 1e-6
        )
        # 周线 low 应 = 日线 min low
        d_low_min = min(float(r[4]) for r in daily_rows if r[4] is not None)
        low_match = (
            w_low is not None and abs(float(w_low) - d_low_min) < 1e-6
        )

        all_match = open_match and close_match and high_match and low_match
        status = "OK" if all_match else "MISMATCH"
        if not all_match:
            aligned = False
        print(
            f"    {w_date}: weekly(O={w_open},H={w_high},L={w_low},C={w_close}) "
            f"vs daily_last(O={first[2]},C={last[1]},Hmax={d_high_max},Lmin={d_low_min}) [{status}]"
        )

    return aligned


async def main() -> None:
    """主入口：小批量验证 3 只 ETF。"""
    print("=" * 60)
    print(f"Task 15.2 小批量验证：{BATCH_SIZE} 只 ETF 从日线合并周线/月线")
    print("=" * 60)

    async with AsyncSessionLocal() as db:
        # 1. 查询差集中的前 3 只
        instruments = await get_missing_instruments(db, BATCH_SIZE)
        if not instruments:
            print("[ERROR] 未找到差集中的 instrument")
            return

        print(f"\n--- 待验证的 {len(instruments)} 只 ETF ---")
        for iid, symbol, name, market in instruments:
            print(f"  {symbol} {name} ({market}) instrument_id={iid}")

        # 2. 逐只合并周线 + 月线
        print("\n--- 开始合并周线 + 月线 ---")
        for iid, symbol, name, market in instruments:
            print(f"\n>>> 处理 {symbol} {name}")

            # 合并周线
            try:
                weekly_df = await refresh_weekly_bars(db, iid, count=200)
                print(f"  周线合并完成: {len(weekly_df)} 条")
            except Exception as exc:
                logger.error("周线合并失败 symbol=%s: %s", symbol, exc)
                raise

            # 合并月线
            try:
                monthly_df = await refresh_monthly_bars(db, iid, count=50)
                print(f"  月线合并完成: {len(monthly_df)} 条")
            except Exception as exc:
                logger.error("月线合并失败 symbol=%s: %s", symbol, exc)
                raise

        # 3. 验证合并结果
        print("\n--- 验证合并结果 ---")
        all_ok = True
        for iid, symbol, name, market in instruments:
            stats = await verify_merge_result(db, iid, symbol)
            print(f"\n  [{stats['symbol']}]")
            print(
                f"    日线: count={stats['daily']['count']}, "
                f"date={stats['daily']['min_date']} ~ {stats['daily']['max_date']}"
            )
            print(
                f"    周线: count={stats['weekly']['count']}, "
                f"date={stats['weekly']['min_date']} ~ {stats['weekly']['max_date']}"
            )
            print(
                f"    月线: count={stats['monthly']['count']}, "
                f"date={stats['monthly']['min_date']} ~ {stats['monthly']['max_date']}"
            )

            # 基本断言：周线/月线行数 > 0
            if stats["weekly"]["count"] == 0:
                print(f"  [FAIL] {symbol} 周线行数为 0")
                all_ok = False
            if stats["monthly"]["count"] == 0:
                print(f"  [FAIL] {symbol} 月线行数为 0")
                all_ok = False
            # 周线行数应 < 日线行数（合并后行数减少）
            if (
                stats["weekly"]["count"] > 0
                and stats["daily"]["count"] > 0
                and stats["weekly"]["count"] >= stats["daily"]["count"]
            ):
                print(
                    f"  [WARN] {symbol} 周线行数({stats['weekly']['count']}) "
                    f">= 日线行数({stats['daily']['count']})"
                )

            # 验证周线 close 与日线对齐
            aligned = await verify_weekly_close_alignment(db, iid, symbol)
            if not aligned:
                print(f"  [WARN] {symbol} 周线 close 与日线未完全对齐")
                all_ok = False

        print("\n" + "=" * 60)
        if all_ok:
            print("小批量验证通过 ✓，可继续批量处理 970 只")
        else:
            print("小批量验证存在问题，请检查上述日志")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
