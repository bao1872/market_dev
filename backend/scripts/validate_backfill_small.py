"""小批量回补验证：10 只股票 × 3 周期（日线/15min/60min）。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.validate_backfill_small

功能：
1. 查询 10 只 active 股票
2. 对每只股票执行日线（从 2023-01-01）、15min（count=15000）、60min（count=4000）回补
3. 验证回补后的数据完整性（日期范围、记录数）
4. 打印验证结果

设计说明：
- 串行处理（pytdx 不支持并发）
- 失败不中断，记录错误继续下一只
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, Bar60Min, BarDaily
from app.models.instrument import Instrument
from app.repositories.bar_repository import (
    refresh_15min_bars,
    refresh_60min_bars,
    refresh_daily_bars,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("validate_backfill_small")

# 验证阈值
DAILY_MIN_COUNT = 800
DAILY_EARLIEST_LIMIT = date(2023, 1, 4)
MIN15_MIN_COUNT = 14000
MIN60_MIN_COUNT = 3500

# 回补参数
DAILY_START_DATE = date(2023, 1, 1)
MIN15_COUNT = 15000
MIN60_COUNT = 4000

# 股票间延迟（秒），避免 pytdx 限流
STOCK_DELAY = 0.3


async def get_active_instruments(session: AsyncSession, limit: int = 10) -> list[Instrument]:
    """查询 active 个股（排除指数），使用已知个股代码。"""
    # 选择 10 只已知个股进行验证（排除指数）
    known_symbols = [
        "000063",  # 中兴通讯
        "000100",  # TCL科技
        "000333",  # 美的集团
        "000651",  # 格力电器
        "000858",  # 五粮液
        "002594",  # 比亚迪
        "300750",  # 宁德时代
        "600519",  # 贵州茅台
        "600036",  # 招商银行
        "601318",  # 中国平安
    ]
    stmt = (
        select(Instrument)
        .where(Instrument.status == "active")
        .where(Instrument.symbol.in_(known_symbols))
        .order_by(Instrument.symbol)
    )
    result = await session.execute(stmt)
    instruments = list(result.scalars().all())

    # 如果查询结果不足，补充其他个股
    if len(instruments) < limit:
        fallback_symbols = [
            "000001",  # 平安银行
            "000002",  # 万科A
            "600000",  # 浦发银行
            "600009",  # 上海机场
            "600016",  # 民生银行
            "600028",  # 中国石化
            "600030",  # 中信证券
            "600048",  # 保利发展
            "600050",  # 中国联通
            "600104",  # 上汽集团
        ]
        existing = {inst.symbol for inst in instruments}
        remaining = [s for s in fallback_symbols if s not in existing]
        if remaining:
            stmt2 = (
                select(Instrument)
                .where(Instrument.status == "active")
                .where(Instrument.symbol.in_(remaining))
                .order_by(Instrument.symbol)
            )
            result2 = await session.execute(stmt2)
            instruments.extend(result2.scalars().all())

    return instruments[:limit]


async def backfill_daily(
    session: AsyncSession,
    instrument_id: Any,
    adapter: PytdxAdapter,
) -> dict[str, Any]:
    """执行日线回补并验证。"""
    try:
        df = await refresh_daily_bars(
            session, instrument_id, DAILY_START_DATE, date.today(), adapter
        )
        backfill_rows = len(df)
    except Exception as exc:
        logger.error("日线回补失败 instrument_id=%s: %s", instrument_id, exc)
        backfill_rows = 0

    # 查询 DB 验证
    count_result = await session.execute(
        select(func.count()).select_from(BarDaily).where(BarDaily.instrument_id == instrument_id)
    )
    db_count = int(count_result.scalar() or 0)

    min_date_result = await session.execute(
        select(func.min(BarDaily.trade_date)).where(BarDaily.instrument_id == instrument_id)
    )
    earliest = min_date_result.scalar()

    date_ok = earliest is not None and earliest <= DAILY_EARLIEST_LIMIT
    count_ok = db_count >= DAILY_MIN_COUNT

    return {
        "backfill_rows": backfill_rows,
        "db_count": db_count,
        "earliest": str(earliest) if earliest else None,
        "date_ok": date_ok,
        "count_ok": count_ok,
        "pass": date_ok and count_ok,
    }


async def backfill_15min(
    session: AsyncSession,
    instrument_id: Any,
    adapter: PytdxAdapter,
) -> dict[str, Any]:
    """执行 15min 回补并验证。"""
    try:
        df = await refresh_15min_bars(session, instrument_id, count=MIN15_COUNT, adapter=adapter)
        backfill_rows = len(df)
    except Exception as exc:
        logger.error("15min 回补失败 instrument_id=%s: %s", instrument_id, exc)
        backfill_rows = 0

    count_result = await session.execute(
        select(func.count()).select_from(Bar15Min).where(Bar15Min.instrument_id == instrument_id)
    )
    db_count = int(count_result.scalar() or 0)

    count_ok = db_count >= MIN15_MIN_COUNT

    return {
        "backfill_rows": backfill_rows,
        "db_count": db_count,
        "count_ok": count_ok,
        "pass": count_ok,
    }


async def backfill_60min(
    session: AsyncSession,
    instrument_id: Any,
    adapter: PytdxAdapter,
) -> dict[str, Any]:
    """执行 60min 回补并验证。"""
    try:
        df = await refresh_60min_bars(session, instrument_id, count=MIN60_COUNT, adapter=adapter)
        backfill_rows = len(df)
    except Exception as exc:
        logger.error("60min 回补失败 instrument_id=%s: %s", instrument_id, exc)
        backfill_rows = 0

    count_result = await session.execute(
        select(func.count()).select_from(Bar60Min).where(Bar60Min.instrument_id == instrument_id)
    )
    db_count = int(count_result.scalar() or 0)

    count_ok = db_count >= MIN60_MIN_COUNT

    return {
        "backfill_rows": backfill_rows,
        "db_count": db_count,
        "count_ok": count_ok,
        "pass": count_ok,
    }


async def process_one_stock(
    session: AsyncSession,
    instrument: Instrument,
    adapter: PytdxAdapter,
) -> dict[str, Any]:
    """对单只股票执行 3 周期回补并验证。"""
    result: dict[str, Any] = {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "instrument_id": str(instrument.id),
        "daily": None,
        "15min": None,
        "60min": None,
        "error": None,
    }

    try:
        # 日线
        result["daily"] = await backfill_daily(session, instrument.id, adapter)

        # 15min
        result["15min"] = await backfill_15min(session, instrument.id, adapter)

        # 60min
        result["60min"] = await backfill_60min(session, instrument.id, adapter)

    except Exception as exc:
        logger.error("处理股票失败 symbol=%s: %s", instrument.symbol, exc)
        result["error"] = str(exc)

    return result


async def main() -> None:
    """主函数：小批量回补验证。"""
    print("=" * 70)
    print("小批量回补验证：10 只股票 × 3 周期（日线/15min/60min）")
    print("=" * 70)

    adapter = get_pytdx_adapter()

    async with AsyncSessionLocal() as session:
        # 1. 查询 10 只 active 股票
        instruments = await get_active_instruments(session, limit=10)
        if not instruments:
            print("[WARN] 未找到 active 股票，退出")
            return

        print(f"\n查询到 {len(instruments)} 只 active 股票：")
        for inst in instruments:
            print(f"  {inst.symbol} {inst.name} ({inst.market})")

        # 2. 串行回补
        print(f"\n--- 开始回补（串行，每只间隔 {STOCK_DELAY}s）---")
        results: list[dict[str, Any]] = []

        for i, inst in enumerate(instruments):
            print(f"\n[{i + 1}/{len(instruments)}] {inst.symbol} {inst.name}")
            result = await process_one_stock(session, inst, adapter)
            results.append(result)

            # 打印该股票结果
            d = result["daily"]
            m15 = result["15min"]
            m60 = result["60min"]

            if d:
                d_status = "PASS" if d["pass"] else "FAIL"
                print(f"  日线: [{d_status}] db={d['db_count']}, earliest={d['earliest']}, "
                      f"date_ok={d['date_ok']}, count_ok={d['count_ok']}")
            if m15:
                m15_status = "PASS" if m15["pass"] else "FAIL"
                print(f"  15min: [{m15_status}] db={m15['db_count']}, count_ok={m15['count_ok']}")
            if m60:
                m60_status = "PASS" if m60["pass"] else "FAIL"
                print(f"  60min: [{m60_status}] db={m60['db_count']}, count_ok={m60['count_ok']}")
            if result["error"]:
                print(f"  [ERROR] {result['error']}")

            # 股票间延迟
            if i < len(instruments) - 1:
                time.sleep(STOCK_DELAY)

    # 3. 汇总统计
    print("\n" + "=" * 70)
    print("汇总统计")
    print("=" * 70)

    total = len(results)
    daily_pass = sum(1 for r in results if r["daily"] and r["daily"]["pass"])
    min15_pass = sum(1 for r in results if r["15min"] and r["15min"]["pass"])
    min60_pass = sum(1 for r in results if r["60min"] and r["60min"]["pass"])
    errors = sum(1 for r in results if r["error"])

    daily_counts = [r["daily"]["db_count"] for r in results if r["daily"]]
    min15_counts = [r["15min"]["db_count"] for r in results if r["15min"]]
    min60_counts = [r["60min"]["db_count"] for r in results if r["60min"]]

    print(f"  股票总数: {total}")
    print(f"  日线通过: {daily_pass}/{total} (阈值: 最早≤{DAILY_EARLIEST_LIMIT}, 记录≥{DAILY_MIN_COUNT})")
    if daily_counts:
        print(f"  日线记录数: min={min(daily_counts)}, max={max(daily_counts)}, "
              f"avg={sum(daily_counts) / len(daily_counts):.0f}")
    print(f"  15min通过: {min15_pass}/{total} (阈值: 记录≥{MIN15_MIN_COUNT})")
    if min15_counts:
        print(f"  15min记录数: min={min(min15_counts)}, max={max(min15_counts)}, "
              f"avg={sum(min15_counts) / len(min15_counts):.0f}")
    print(f"  60min通过: {min60_pass}/{total} (阈值: 记录≥{MIN60_MIN_COUNT})")
    if min60_counts:
        print(f"  60min记录数: min={min(min60_counts)}, max={max(min60_counts)}, "
              f"avg={sum(min60_counts) / len(min60_counts):.0f}")
    print(f"  错误数: {errors}")

    # 4. 逐股明细表
    print(f"\n{'symbol':<8} {'name':<10} {'日线':<8} {'15min':<8} {'60min':<8} {'结果'}")
    print("-" * 60)
    for r in results:
        d_ok = "PASS" if r["daily"] and r["daily"]["pass"] else "FAIL"
        m15_ok = "PASS" if r["15min"] and r["15min"]["pass"] else "FAIL"
        m60_ok = "PASS" if r["60min"] and r["60min"]["pass"] else "FAIL"
        all_ok = "ALL PASS" if d_ok == "PASS" and m15_ok == "PASS" and m60_ok == "PASS" else "HAS FAIL"
        print(f"{r['symbol']:<8} {r['name']:<10} {d_ok:<8} {m15_ok:<8} {m60_ok:<8} {all_ok}")

    print("\n验证完成")


if __name__ == "__main__":
    asyncio.run(main())
