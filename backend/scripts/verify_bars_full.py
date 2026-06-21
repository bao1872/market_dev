"""Task 17: 清理后全量验证 - 数据完整性 + adj_factor 正确性 + volume 单位。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.verify_bars_full

功能：
1. 验证 6 张 bar 表的数据完整性（记录数、股票数、日期范围、无重复）
2. 验证 adj_factor 正确性（抽样 10 只股票，15min/60min adj_factor 与日线一致）
3. 验证 volume 单位正确性（周线 volume = 日线 volume 之和）

前置条件：
- Task 14 完成（旧数据清理）
- Task 15 完成（ETF/基金补合并）
- Task 16 完成（15min/60min 回补）
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import timedelta

# 确保可以 import app.*
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.bar import (
    Bar15Min,
    Bar60Min,
    BarDaily,
    BarMonthly,
    BarWeekly,
)
from app.models.instrument import Instrument

# 抽样股票（覆盖 SH/SZ 主板/创业板，均有除权除息事件）
SAMPLE_SYMBOLS = [
    "000001",  # 平安银行
    "000063",  # 中兴通讯
    "000651",  # 格力电器
    "000858",  # 五粮液
    "002415",  # 海康威视
    "002594",  # 比亚迪
    "300001",  # 特锐德
    "300750",  # 宁德时代
    "600036",  # 招商银行
    "601318",  # 中国平安
]


async def verify_completeness(db: AsyncSession) -> dict:
    """验证 6 张 bar 表的数据完整性。"""
    tables = {
        "daily": BarDaily,
        "weekly": BarWeekly,
        "monthly": BarMonthly,
        "15min": Bar15Min,
        "60min": Bar60Min,
    }

    results = {}
    for name, model in tables.items():
        # 记录数 + 股票数
        r = await db.execute(
            select(
                func.count(),
                func.count(model.instrument_id.distinct()),
            )
        )
        total_rows, total_stocks = r.one()

        # 日期范围
        if name == "daily":
            date_col = model.trade_date
        else:
            date_col = model.trade_time

        r2 = await db.execute(
            select(func.min(date_col), func.max(date_col))
        )
        min_date, max_date = r2.one()

        # 重复检查（同 instrument_id + 同 trade_date/trade_time 只有一条）
        if name == "daily":
            dup_sql = text(
                "SELECT COUNT(*) FROM ("
                "  SELECT instrument_id, trade_date, COUNT(*) AS c "
                "  FROM bars_daily GROUP BY instrument_id, trade_date HAVING COUNT(*) > 1"
                ") t"
            )
        elif name == "weekly":
            dup_sql = text(
                "SELECT COUNT(*) FROM ("
                "  SELECT instrument_id, trade_date, COUNT(*) AS c "
                "  FROM bars_weekly GROUP BY instrument_id, trade_date HAVING COUNT(*) > 1"
                ") t"
            )
        elif name == "monthly":
            dup_sql = text(
                "SELECT COUNT(*) FROM ("
                "  SELECT instrument_id, trade_date, COUNT(*) AS c "
                "  FROM bars_monthly GROUP BY instrument_id, trade_date HAVING COUNT(*) > 1"
                ") t"
            )
        else:
            tbl = f"bars_{name}"
            dup_sql = text(
                f"SELECT COUNT(*) FROM ("
                f"  SELECT instrument_id, trade_time, COUNT(*) AS c "
                f"  FROM {tbl} GROUP BY instrument_id, trade_time HAVING COUNT(*) > 1"
                f") t"
            )

        r3 = await db.execute(dup_sql)
        dup_count = r3.scalar() or 0

        results[name] = {
            "total_rows": total_rows,
            "total_stocks": total_stocks,
            "min_date": str(min_date) if min_date else "N/A",
            "max_date": str(max_date) if max_date else "N/A",
            "dup_groups": dup_count,
        }

    return results


async def verify_adj_factor(db: AsyncSession) -> dict:
    """验证 adj_factor 正确性：抽样股票的 15min/60min adj_factor 与日线一致。"""
    # 查询抽样股票
    r = await db.execute(
        select(Instrument).where(Instrument.symbol.in_(SAMPLE_SYMBOLS))
    )
    instruments = list(r.scalars().all())

    results = {}
    for inst in instruments:
        # 查日线 adj_factor 映射
        r_daily = await db.execute(
            select(BarDaily.trade_date, BarDaily.adj_factor)
            .where(BarDaily.instrument_id == inst.id)
            .where(BarDaily.adj_factor.isnot(None))
        )
        daily_map = {row[0]: float(row[1]) for row in r_daily.all() if row[1] is not None}

        # 15min adj_factor 匹配率
        r15 = await db.execute(
            select(Bar15Min.trade_time, Bar15Min.adj_factor)
            .where(Bar15Min.instrument_id == inst.id)
        )
        total_15 = 0
        match_15 = 0
        for tt, af in r15.all():
            tt_local = tt
            if getattr(tt, "tzinfo", None) is not None:
                tt_local = tt.astimezone(tz=None).replace(tzinfo=None)
            bar_date = tt_local.date()
            expected = daily_map.get(bar_date, 1.0)
            actual = float(af) if af is not None else 1.0
            total_15 += 1
            if abs(actual - expected) < 1e-9:
                match_15 += 1

        # 60min adj_factor 匹配率
        r60 = await db.execute(
            select(Bar60Min.trade_time, Bar60Min.adj_factor)
            .where(Bar60Min.instrument_id == inst.id)
        )
        total_60 = 0
        match_60 = 0
        for tt, af in r60.all():
            tt_local = tt
            if getattr(tt, "tzinfo", None) is not None:
                tt_local = tt.astimezone(tz=None).replace(tzinfo=None)
            bar_date = tt_local.date()
            expected = daily_map.get(bar_date, 1.0)
            actual = float(af) if af is not None else 1.0
            total_60 += 1
            if abs(actual - expected) < 1e-9:
                match_60 += 1

        results[inst.symbol] = {
            "15min_total": total_15,
            "15min_match": match_15,
            "15min_rate": f"{match_15}/{total_15} = {match_15/total_15*100:.2f}%" if total_15 else "N/A",
            "60min_total": total_60,
            "60min_match": match_60,
            "60min_rate": f"{match_60}/{total_60} = {match_60/total_60*100:.2f}%" if total_60 else "N/A",
        }

    return results


async def verify_volume(db: AsyncSession) -> dict:
    """验证 volume 单位正确性：周线 volume = 日线 volume 之和（抽样 000001）。"""
    r = await db.execute(
        select(Instrument).where(Instrument.symbol == "000001")
    )
    inst = r.scalar_one_or_none()
    if inst is None:
        return {"error": "000001 not found"}

    # 查最近 4 周的周线 volume
    r_weekly = await db.execute(
        select(BarWeekly.trade_date, BarWeekly.volume)
        .where(BarWeekly.instrument_id == inst.id)
        .order_by(BarWeekly.trade_date.desc())
        .limit(4)
    )
    weekly_rows = r_weekly.all()

    results = []
    for trade_date, weekly_vol in weekly_rows:
        # 查该周的日线 volume 之和
        # 周线 trade_date 是前对齐（周期内第一个交易日），所以日线范围是 [trade_date, trade_date+7)
        r_daily = await db.execute(
            select(func.sum(BarDaily.volume))
            .where(BarDaily.instrument_id == inst.id)
            .where(BarDaily.trade_date >= trade_date)
            .where(BarDaily.trade_date < trade_date + timedelta(days=7))
        )
        daily_sum = r_daily.scalar() or 0

        weekly_vol_f = float(weekly_vol) if weekly_vol else 0
        daily_sum_f = float(daily_sum) if daily_sum else 0
        match = abs(weekly_vol_f - daily_sum_f) < 1e-2

        results.append({
            "trade_date": str(trade_date),
            "weekly_volume": weekly_vol_f,
            "daily_sum": daily_sum_f,
            "match": match,
        })

    return {"symbol": "000001", "results": results}


async def main() -> None:
    """主入口：全量验证。"""
    print("=" * 70)
    print("Task 17: 清理后全量验证")
    print("=" * 70)

    async with AsyncSessionLocal() as db:
        # ===== Step 1: 数据完整性验证 =====
        print("\n--- Step 1: 数据完整性验证 ---")
        completeness = await verify_completeness(db)
        print(f"\n{'table':<10}{'rows':<12}{'stocks':<8}{'min_date':<14}{'max_date':<14}{'dup_groups':<12}")
        all_ok = True
        for name, r in completeness.items():
            dup_status = "OK" if r["dup_groups"] == 0 else f"BAD({r['dup_groups']})"
            if r["dup_groups"] > 0:
                all_ok = False
            print(
                f"{name:<10}{r['total_rows']:<12}{r['total_stocks']:<8}"
                f"{r['min_date']:<14}{r['max_date']:<14}{dup_status:<12}"
            )

        # 验证股票数一致性
        daily_stocks = completeness["daily"]["total_stocks"]
        for name in ["weekly", "monthly", "15min", "60min"]:
            stocks = completeness[name]["total_stocks"]
            if stocks != daily_stocks:
                print(f"[WARN] {name} 股票数 {stocks} != daily 股票数 {daily_stocks}")
                all_ok = False

        print(f"\n数据完整性: {'全部通过' if all_ok else '存在问题（见上）'}")

        # ===== Step 2: adj_factor 正确性验证 =====
        print("\n--- Step 2: adj_factor 正确性验证（抽样 10 只）---")
        adj_results = await verify_adj_factor(db)
        print(f"\n{'symbol':<10}{'15min_rate':<20}{'60min_rate':<20}")
        all_adj_ok = True
        for symbol, r in adj_results.items():
            print(f"{symbol:<10}{r['15min_rate']:<20}{r['60min_rate']:<20}")
            # 检查匹配率是否 100%
            if r["15min_total"] > 0 and r["15min_match"] != r["15min_total"]:
                all_adj_ok = False
            if r["60min_total"] > 0 and r["60min_match"] != r["60min_total"]:
                all_adj_ok = False

        print(f"\nadj_factor 正确性: {'全部通过' if all_adj_ok else '存在问题（见上）'}")

        # ===== Step 3: volume 单位正确性验证 =====
        print("\n--- Step 3: volume 单位正确性验证（抽样 000001 最近 4 周）---")
        vol_results = await verify_volume(db)
        if "error" in vol_results:
            print(f"  {vol_results['error']}")
        else:
            print(f"  symbol: {vol_results['symbol']}")
            all_vol_ok = True
            for r in vol_results["results"]:
                status = "OK" if r["match"] else "MISMATCH"
                if not r["match"]:
                    all_vol_ok = False
                print(
                    f"  trade_date={r['trade_date']} "
                    f"weekly_vol={r['weekly_volume']:.0f} "
                    f"daily_sum={r['daily_sum']:.0f} "
                    f"{status}"
                )
            print(f"\nvolume 单位正确性: {'全部通过' if all_vol_ok else '存在问题（见上）'}")

    # ===== 汇总 =====
    print("\n" + "=" * 70)
    print("验证汇总")
    print("=" * 70)
    print(f"1. 数据完整性: {'通过' if all_ok else '存在问题'}")
    print(f"2. adj_factor 正确性: {'通过' if all_adj_ok else '存在问题'}")
    if "error" not in vol_results:
        print(f"3. volume 单位正确性: {'通过' if all_vol_ok else '存在问题'}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
