"""平安银行行情数据验证：DB vs akshare 逐 bar 对比。

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.verify_bars_akshare

验证内容：
1. 日线：DB vs akshare（腾讯接口 stock_zh_a_daily）
2. 周线：DB vs akshare（东方财富 stock_zh_a_hist，带重试）
3. 月线：DB vs akshare（东方财富 stock_zh_a_hist，带重试）
4. 15min：DB vs akshare（东方财富 stock_zh_a_hist_min_em，带重试）
5. 60min：DB vs akshare（东方财富 stock_zh_a_hist_min_em，带重试）

输出：
- 每个 bar 的 OHLCV 对比
- 差异超过阈值时标记 ❌
- 汇总统计
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta

import akshare as ak
import pandas as pd
from sqlalchemy import select

from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMonthly, BarWeekly
from app.models.instrument import Instrument

# 验证阈值
PRICE_THRESHOLD = 0.001  # 价格差异阈值 0.1%
VOLUME_THRESHOLD = 0.01  # 成交量差异阈值 1%

SYMBOL = "000001"
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒


def ak_call_with_retry(func, *args, **kwargs):
    """带重试的 akshare 调用。"""
    for attempt in range(MAX_RETRIES):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"  [重试 {attempt+1}/{MAX_RETRIES}] {func.__name__} 失败: {e}")
                time.sleep(RETRY_DELAY)
            else:
                raise


def compare_bar(
    label: str,
    db_bar: dict | None,
    ak_bar: dict | None,
    ak_volume_unit: str = "手",
) -> dict:
    """对比单个 bar 的 OHLCV。

    Args:
        label: 标签（如 "2026-06-18"）
        db_bar: DB 数据 {open, high, low, close, volume}
        ak_bar: akshare 数据 {open, high, low, close, volume}
        ak_volume_unit: akshare 成交量单位（"手" 或 "股"）

    Returns:
        差异结果
    """
    if db_bar is None and ak_bar is None:
        return {"label": label, "status": "both_empty"}
    if db_bar is None:
        return {"label": label, "status": "db_missing", "ak": ak_bar}
    if ak_bar is None:
        return {"label": label, "status": "ak_missing", "db": db_bar}

    # akshare volume 转换为股
    ak_volume = ak_bar["volume"]
    if ak_volume_unit == "手":
        ak_volume = ak_volume * 100

    diffs = {}
    for field in ["open", "high", "low", "close"]:
        db_val = float(db_bar[field])
        ak_val = float(ak_bar[field])
        if abs(db_val) > 0:
            diff_pct = abs(db_val - ak_val) / abs(db_val)
        else:
            diff_pct = 0 if ak_val == 0 else 1
        if diff_pct > PRICE_THRESHOLD:
            diffs[field] = {"db": db_val, "ak": ak_val, "diff_pct": diff_pct}

    # volume 对比
    db_vol = float(db_bar["volume"])
    ak_vol = float(ak_volume)
    if db_vol > 0:
        vol_diff_pct = abs(db_vol - ak_vol) / db_vol
    else:
        vol_diff_pct = 0 if ak_vol == 0 else 1
    if vol_diff_pct > VOLUME_THRESHOLD:
        diffs["volume"] = {"db": db_vol, "ak": ak_vol, "diff_pct": vol_diff_pct}

    status = "❌" if diffs else "✓"
    return {
        "label": label,
        "status": status,
        "diffs": diffs,
        "db": db_bar,
        "ak": ak_bar,
        "ak_volume_converted": ak_volume,
    }


async def verify_daily() -> None:
    """验证日线数据。"""
    print("\n" + "=" * 80)
    print("=== 日线验证：DB vs akshare ===")
    print("=" * 80)

    async with AsyncSessionLocal() as db:
        # 查 DB 数据
        stmt = select(Instrument).where(Instrument.symbol == SYMBOL)
        result = await db.execute(stmt)
        inst = result.scalar_one()

        stmt = (
            select(BarDaily)
            .where(BarDaily.instrument_id == inst.id)
            .order_by(BarDaily.trade_date.desc())
            .limit(20)
        )
        result = await db.execute(stmt)
        db_rows = result.scalars().all()
        db_map = {r.trade_date.isoformat(): {
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        } for r in db_rows}

    # 查 akshare 数据（腾讯接口，volume 单位是"股"）
    ak_df = ak_call_with_retry(
        ak.stock_zh_a_daily,
        symbol="sz000001", start_date="20260101", end_date="20260620", adjust="",
    )
    ak_map = {}
    for _, row in ak_df.iterrows():
        d = str(row["date"].date()) if hasattr(row["date"], "date") else str(row["date"])[:10]
        ak_map[d] = {
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"],
        }

    # 逐 bar 对比
    all_dates = sorted(set(list(db_map.keys()) + list(ak_map.keys())), reverse=True)[:20]
    print(f"DB 数据: {len(db_rows)} 条, akshare 数据: {len(ak_df)} 条")
    print(f"{'日期':<12} {'状态':<4} {'DB O/C/V':<30} {'AK O/C/V':<30} {'差异'}")
    print("-" * 120)

    diff_count = 0
    for d in all_dates:
        db_bar = db_map.get(d)
        ak_bar = ak_map.get(d)
        # 腾讯接口 volume 单位是"股"，不需要转换
        result = compare_bar(d, db_bar, ak_bar, ak_volume_unit="股")

        if result["status"] == "✓":
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume'])
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            print(f"{d:<12} ✓    {db_str:<30} {ak_str:<30}")
        elif result["status"] == "db_missing":
            print(f"{d:<12} ❌   DB缺失                        AK={ak_bar}")
            diff_count += 1
        elif result["status"] == "ak_missing":
            print(f"{d:<12} ❌   DB={db_bar}           AK缺失")
            diff_count += 1
        else:
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume'])
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            diff_str = str(result["diffs"])
            print(f"{d:<12} ❌   {db_str:<30} {ak_str:<30} {diff_str}")
            diff_count += 1

    print(f"\n日线验证结果: {len(all_dates) - diff_count}/{len(all_dates)} 一致, {diff_count} 差异")


async def verify_weekly() -> None:
    """验证周线数据：用 akshare 日线（腾讯）聚合计算周线来对比。"""
    print("\n" + "=" * 80)
    print("=== 周线验证：DB vs akshare（日线聚合）===")
    print("=" * 80)

    async with AsyncSessionLocal() as db:
        stmt = select(Instrument).where(Instrument.symbol == SYMBOL)
        result = await db.execute(stmt)
        inst = result.scalar_one()

        stmt = (
            select(BarWeekly)
            .where(BarWeekly.instrument_id == inst.id)
            .order_by(BarWeekly.trade_date.desc())
            .limit(10)
        )
        result = await db.execute(stmt)
        db_rows = result.scalars().all()
        db_map = {r.trade_date.isoformat(): {
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        } for r in db_rows}

    # 用腾讯日线数据手动按周聚合（标签为该周最后一个交易日，与 pytdx 对齐）
    ak_df = ak_call_with_retry(
        ak.stock_zh_a_daily,
        symbol="sz000001", start_date="20260101", end_date="20260620", adjust="",
    )
    ak_df["date"] = pd.to_datetime(ak_df["date"])
    ak_df = ak_df.set_index("date").sort_index()

    # 按周分组：W-FRI 表示周五为周末，但标签用该周最后一个交易日
    ak_df["week"] = ak_df.index.to_period("W-FRI")
    weekly = ak_df.groupby("week").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    # 标签改为该周最后一个交易日（与 pytdx 对齐）
    weekly.index = ak_df.groupby("week").apply(lambda x: x.index[-1].date(), include_groups=False)
    weekly = weekly.dropna()

    ak_map = {}
    for idx, row in weekly.iterrows():
        # idx 可能是 date 或 datetime
        d = idx.isoformat() if isinstance(idx, (date,)) else str(idx)[:10]
        ak_map[d] = {
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"],
        }

    all_dates = sorted(set(list(db_map.keys()) + list(ak_map.keys())), reverse=True)[:10]
    print(f"DB 数据: {len(db_rows)} 条, akshare 聚合周线: {len(weekly)} 条")
    print(f"{'日期':<12} {'状态':<4} {'DB O/C/V':<35} {'AK O/C/V':<35} {'差异'}")
    print("-" * 130)

    diff_count = 0
    for d in all_dates:
        db_bar = db_map.get(d)
        ak_bar = ak_map.get(d)
        # 腾讯日线 volume 单位是"股"，聚合后仍是"股"
        result = compare_bar(d, db_bar, ak_bar, ak_volume_unit="股")

        if result["status"] == "✓":
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={float(ak_bar['volume']):.0f}"
            print(f"{d:<12} ✓    {db_str:<35} {ak_str:<35}")
        elif result["status"] == "db_missing":
            print(f"{d:<12} ❌   DB缺失                             AK={ak_bar}")
            diff_count += 1
        elif result["status"] == "ak_missing":
            print(f"{d:<12} ❌   DB={db_bar}              AK缺失")
            diff_count += 1
        else:
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={float(ak_bar['volume']):.0f}"
            diff_str = str(result["diffs"])
            print(f"{d:<12} ❌   {db_str:<35} {ak_str:<35} {diff_str}")
            diff_count += 1

    print(f"\n周线验证结果: {len(all_dates) - diff_count}/{len(all_dates)} 一致, {diff_count} 差异")


async def verify_monthly() -> None:
    """验证月线数据：用 akshare 日线（腾讯）聚合计算月线来对比。"""
    print("\n" + "=" * 80)
    print("=== 月线验证：DB vs akshare（日线聚合）===")
    print("=" * 80)

    async with AsyncSessionLocal() as db:
        stmt = select(Instrument).where(Instrument.symbol == SYMBOL)
        result = await db.execute(stmt)
        inst = result.scalar_one()

        stmt = (
            select(BarMonthly)
            .where(BarMonthly.instrument_id == inst.id)
            .order_by(BarMonthly.trade_date.desc())
            .limit(6)
        )
        result = await db.execute(stmt)
        db_rows = result.scalars().all()
        db_map = {r.trade_date.isoformat(): {
            "open": r.open, "high": r.high, "low": r.low, "close": r.close,
            "volume": r.volume,
        } for r in db_rows}

    # 用腾讯日线数据手动按月聚合（标签为该月最后一个交易日，与 pytdx 对齐）
    ak_df = ak_call_with_retry(
        ak.stock_zh_a_daily,
        symbol="sz000001", start_date="20260101", end_date="20260620", adjust="",
    )
    ak_df["date"] = pd.to_datetime(ak_df["date"])
    ak_df = ak_df.set_index("date").sort_index()

    # 按月分组：标签用该月最后一个交易日
    ak_df["month"] = ak_df.index.to_period("M")
    monthly = ak_df.groupby("month").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    # 标签改为该月最后一个交易日（与 pytdx 对齐）
    monthly.index = ak_df.groupby("month").apply(lambda x: x.index[-1].date(), include_groups=False)
    monthly = monthly.dropna()

    ak_map = {}
    for idx, row in monthly.iterrows():
        d = idx.isoformat() if isinstance(idx, (date,)) else str(idx)[:10]
        ak_map[d] = {
            "open": row["open"], "high": row["high"], "low": row["low"],
            "close": row["close"], "volume": row["volume"],
        }

    all_dates = sorted(set(list(db_map.keys()) + list(ak_map.keys())), reverse=True)[:6]
    print(f"DB 数据: {len(db_rows)} 条, akshare 聚合月线: {len(monthly)} 条")
    print(f"{'日期':<12} {'状态':<4} {'DB O/C/V':<35} {'AK O/C/V':<35} {'差异'}")
    print("-" * 130)

    diff_count = 0
    for d in all_dates:
        db_bar = db_map.get(d)
        ak_bar = ak_map.get(d)
        result = compare_bar(d, db_bar, ak_bar, ak_volume_unit="股")

        if result["status"] == "✓":
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={float(ak_bar['volume']):.0f}"
            print(f"{d:<12} ✓    {db_str:<35} {ak_str:<35}")
        elif result["status"] == "db_missing":
            print(f"{d:<12} ❌   DB缺失                             AK={ak_bar}")
            diff_count += 1
        elif result["status"] == "ak_missing":
            print(f"{d:<12} ❌   DB={db_bar}              AK缺失")
            diff_count += 1
        else:
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={float(ak_bar['volume']):.0f}"
            diff_str = str(result["diffs"])
            print(f"{d:<12} ❌   {db_str:<35} {ak_str:<35} {diff_str}")
            diff_count += 1

    print(f"\n月线验证结果: {len(all_dates) - diff_count}/{len(all_dates)} 一致, {diff_count} 差异")


async def verify_15min() -> None:
    """验证 15min 数据。"""
    print("\n" + "=" * 80)
    print("=== 15min 验证：DB vs akshare ===")
    print("=" * 80)

    async with AsyncSessionLocal() as db:
        stmt = select(Instrument).where(Instrument.symbol == SYMBOL)
        result = await db.execute(stmt)
        inst = result.scalar_one()

        stmt = (
            select(Bar15Min)
            .where(Bar15Min.instrument_id == inst.id)
            .order_by(Bar15Min.trade_time.desc())
            .limit(20)
        )
        result = await db.execute(stmt)
        db_rows = result.scalars().all()
        # DB 时间是 UTC，转换为北京时间（+8h）用于对比
        db_map = {}
        for r in db_rows:
            # UTC -> CST
            cst_time = r.trade_time + timedelta(hours=8)
            key = cst_time.strftime("%Y-%m-%d %H:%M")
            db_map[key] = {
                "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                "volume": r.volume,
            }

    # akshare 15min
    ak_df = ak_call_with_retry(
        ak.stock_zh_a_hist_min_em,
        symbol=SYMBOL, period="15",
        start_date="2026-06-16 09:30:00", end_date="2026-06-18 15:00:00",
        adjust="",
    )
    ak_map = {}
    for _, row in ak_df.iterrows():
        t = str(row["时间"])
        # 格式: "2026-06-18 15:00:00"
        key = t[:16]  # "2026-06-18 15:00"
        ak_map[key] = {
            "open": row["开盘"], "high": row["最高"], "low": row["最低"],
            "close": row["收盘"], "volume": row["成交量"],
        }

    all_times = sorted(set(list(db_map.keys()) + list(ak_map.keys())), reverse=True)[:20]
    print(f"DB 数据: {len(db_rows)} 条, akshare 数据: {len(ak_df)} 条")
    print(f"{'时间':<20} {'状态':<4} {'DB O/C/V':<40} {'AK O/C/V':<40} {'差异'}")
    print("-" * 150)

    diff_count = 0
    for t in all_times:
        db_bar = db_map.get(t)
        ak_bar = ak_map.get(t)
        result = compare_bar(t, db_bar, ak_bar, ak_volume_unit="手")

        if result["status"] == "✓":
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume']) * 100
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            print(f"{t:<20} ✓    {db_str:<40} {ak_str:<40}")
        elif result["status"] == "db_missing":
            print(f"{t:<20} ❌   DB缺失                                  AK={ak_bar}")
            diff_count += 1
        elif result["status"] == "ak_missing":
            print(f"{t:<20} ❌   DB={db_bar}                   AK缺失")
            diff_count += 1
        else:
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume']) * 100
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            diff_str = str(result["diffs"])
            print(f"{t:<20} ❌   {db_str:<40} {ak_str:<40} {diff_str}")
            diff_count += 1

    print(f"\n15min 验证结果: {len(all_times) - diff_count}/{len(all_times)} 一致, {diff_count} 差异")


async def verify_60min() -> None:
    """验证 60min 数据。"""
    print("\n" + "=" * 80)
    print("=== 60min 验证：DB vs akshare ===")
    print("=" * 80)

    async with AsyncSessionLocal() as db:
        stmt = select(Instrument).where(Instrument.symbol == SYMBOL)
        result = await db.execute(stmt)
        inst = result.scalar_one()

        stmt = (
            select(Bar60Min)
            .where(Bar60Min.instrument_id == inst.id)
            .order_by(Bar60Min.trade_time.desc())
            .limit(10)
        )
        result = await db.execute(stmt)
        db_rows = result.scalars().all()
        db_map = {}
        for r in db_rows:
            cst_time = r.trade_time + timedelta(hours=8)
            key = cst_time.strftime("%Y-%m-%d %H:%M")
            db_map[key] = {
                "open": r.open, "high": r.high, "low": r.low, "close": r.close,
                "volume": r.volume,
            }

    # akshare 60min
    ak_df = ak_call_with_retry(
        ak.stock_zh_a_hist_min_em,
        symbol=SYMBOL, period="60",
        start_date="2026-06-16 09:30:00", end_date="2026-06-18 15:00:00",
        adjust="",
    )
    ak_map = {}
    for _, row in ak_df.iterrows():
        t = str(row["时间"])
        key = t[:16]
        ak_map[key] = {
            "open": row["开盘"], "high": row["最高"], "low": row["最低"],
            "close": row["收盘"], "volume": row["成交量"],
        }

    all_times = sorted(set(list(db_map.keys()) + list(ak_map.keys())), reverse=True)[:10]
    print(f"DB 数据: {len(db_rows)} 条, akshare 数据: {len(ak_df)} 条")
    print(f"{'时间':<20} {'状态':<4} {'DB O/C/V':<40} {'AK O/C/V':<40} {'差异'}")
    print("-" * 150)

    diff_count = 0
    for t in all_times:
        db_bar = db_map.get(t)
        ak_bar = ak_map.get(t)
        result = compare_bar(t, db_bar, ak_bar, ak_volume_unit="手")

        if result["status"] == "✓":
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume']) * 100
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            print(f"{t:<20} ✓    {db_str:<40} {ak_str:<40}")
        elif result["status"] == "db_missing":
            print(f"{t:<20} ❌   DB缺失                                  AK={ak_bar}")
            diff_count += 1
        elif result["status"] == "ak_missing":
            print(f"{t:<20} ❌   DB={db_bar}                   AK缺失")
            diff_count += 1
        else:
            db_str = f"O={float(db_bar['open']):.2f} C={float(db_bar['close']):.2f} V={float(db_bar['volume']):.0f}"
            ak_vol = float(ak_bar['volume']) * 100
            ak_str = f"O={float(ak_bar['open']):.2f} C={float(ak_bar['close']):.2f} V={ak_vol:.0f}"
            diff_str = str(result["diffs"])
            print(f"{t:<20} ❌   {db_str:<40} {ak_str:<40} {diff_str}")
            diff_count += 1

    print(f"\n60min 验证结果: {len(all_times) - diff_count}/{len(all_times)} 一致, {diff_count} 差异")


async def main() -> None:
    """主验证入口。"""
    print("=" * 80)
    print(f"平安银行 ({SYMBOL}) 行情数据验证：DB vs akshare")
    print(f"验证时间: {datetime.now()}")
    print(f"价格差异阈值: {PRICE_THRESHOLD*100}%, 成交量差异阈值: {VOLUME_THRESHOLD*100}%")
    print("=" * 80)

    results = {}
    for name, func in [
        ("日线", verify_daily),
        ("周线", verify_weekly),
        ("月线", verify_monthly),
        ("15min", verify_15min),
        ("60min", verify_60min),
    ]:
        try:
            await func()
            results[name] = "完成"
        except Exception as e:
            print(f"\n⚠️ {name} 验证失败（跳过）: {e}")
            results[name] = f"失败: {e}"

    print("\n" + "=" * 80)
    print("验证汇总:")
    for name, status in results.items():
        print(f"  {name}: {status}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
