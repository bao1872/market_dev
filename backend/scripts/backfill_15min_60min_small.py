"""Task 16.3: 15min/60min 小批量验证（10 只股票）- 回补 + 完整性验证。

验证内容：
1. 对 10 只股票串行回补 15min（count=15000）+ 60min（count=4000）
2. 验证数据完整性（记录数、日期范围、每日 16 条 15min / 4 条 60min）
3. 验证 adj_factor 映射正确性（与 bars_daily 对应交易日一致）
4. 验证 OHLC 合理性（high >= max(open,close), low <= min(open,close), volume > 0）
5. 验证 15min 与 60min 一致性（4 根 15min 合并 = 1 根 60min）

用法：
    cd /root/web_dev/backend && .venv/bin/python -m scripts.backfill_15min_60min_small

Side Effects:
    写入 DB（upsert 幂等，可重复执行）
    pytdx 串行拉取，10 只股票约需 5-10 分钟
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from tqdm import tqdm

from app.core.pytdx_adapter import PytdxAdapter, get_pytdx_adapter
from app.db import AsyncSessionLocal
from app.models.bar import Bar15Min, Bar60Min, BarDaily
from app.models.instrument import Instrument
from app.repositories.bar_repository import refresh_15min_bars, refresh_60min_bars

# 测试股票（10 只，覆盖 SH/SZ 主板/创业板，均有除权除息事件 adj_factor_variety >= 2）
TEST_SYMBOLS = [
    "000001",  # 平安银行 adj_var=6
    "000063",  # 中兴通讯 adj_var=3
    "000651",  # 格力电器 adj_var=5
    "000858",  # 五粮液 adj_var=5
    "002415",  # 海康威视 adj_var=5
    "002594",  # 比亚迪 adj_var=3
    "300001",  # 特锐德 adj_var=4
    "300750",  # 宁德时代 adj_var=6
    "600036",  # 招商银行 adj_var=4
    "601318",  # 中国平安 adj_var=6
]

# 回补参数
COUNT_15MIN = 15000  # 15min 回补到 2023-01-01 约需 13264 条
COUNT_60MIN = 4000  # 60min 回补到 2023-01-01 约需 3316 条
STOCK_DELAY = 0.5  # 股票间延迟（秒），避免 pytdx 限流

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("backfill_15min_60min")


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


async def backfill_one_stock(
    db: AsyncSession,
    instrument: Instrument,
    adapter: PytdxAdapter,
) -> dict:
    """对单只股票回补 15min + 60min。

    Returns:
        dict: symbol, name, 15min_rows, 60min_rows, success, error
    """
    result = {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "15min_rows": 0,
        "60min_rows": 0,
        "success": False,
        "error": None,
    }
    try:
        # 15min 回补
        df15 = await refresh_15min_bars(db, instrument.id, count=COUNT_15MIN, adapter=adapter)
        result["15min_rows"] = 0 if df15.empty else len(df15)
        logger.info(
            "15min 回补完成 symbol=%s rows=%d",
            instrument.symbol, result["15min_rows"],
        )

        # 60min 回补
        df60 = await refresh_60min_bars(db, instrument.id, count=COUNT_60MIN, adapter=adapter)
        result["60min_rows"] = 0 if df60.empty else len(df60)
        logger.info(
            "60min 回补完成 symbol=%s rows=%d",
            instrument.symbol, result["60min_rows"],
        )

        result["success"] = True
    except Exception as exc:
        logger.error("回补失败 symbol=%s: %s", instrument.symbol, exc)
        result["error"] = str(exc)
    return result


async def backfill_all(instruments: list[Instrument]) -> list[dict]:
    """串行回补所有股票的 15min + 60min。

    使用共享的 pytdx adapter（串行拉取，不并发）。
    """
    adapter = get_pytdx_adapter()
    results: list[dict] = []

    async with AsyncSessionLocal() as db:
        pbar = tqdm(instruments, desc="回补 15min/60min", unit="stock")
        for inst in pbar:
            pbar.set_postfix_str(f"{inst.symbol} {inst.name}")
            res = await backfill_one_stock(db, inst, adapter)
            results.append(res)
            await asyncio.sleep(STOCK_DELAY)
        pbar.close()

    return results


# ===== 验证函数 =====


async def verify_completeness(db: AsyncSession, instruments: list[Instrument]) -> list[dict]:
    """验证数据完整性：记录数、日期范围、每日 16 条 15min / 4 条 60min。"""
    results = []
    for inst in instruments:
        # 15min 记录数与日期范围
        r15 = await db.execute(
            select(
                func.count(),
                func.min(Bar15Min.trade_time),
                func.max(Bar15Min.trade_time),
            ).where(Bar15Min.instrument_id == inst.id)
        )
        cnt15, min15, max15 = r15.one()

        # 60min 记录数与日期范围
        r60 = await db.execute(
            select(
                func.count(),
                func.min(Bar60Min.trade_time),
                func.max(Bar60Min.trade_time),
            ).where(Bar60Min.instrument_id == inst.id)
        )
        cnt60, min60, max60 = r60.one()

        # 每日 15min 条数分布（应为 16）
        daily15 = await db.execute(
            text(
                "SELECT (trade_time AT TIME ZONE 'Asia/Shanghai')::date AS d, "
                "COUNT(*) AS c "
                "FROM bars_15min WHERE instrument_id = :iid "
                "GROUP BY d ORDER BY c"
            ),
            {"iid": inst.id},
        )
        rows15 = daily15.all()
        cnt_dist_15 = dict.fromkeys([16], 0)
        abnormal_15 = []
        for d, c in rows15:
            if c in cnt_dist_15:
                cnt_dist_15[c] += 1
            else:
                abnormal_15.append((str(d), c))

        # 每日 60min 条数分布（应为 4）
        daily60 = await db.execute(
            text(
                "SELECT (trade_time AT TIME ZONE 'Asia/Shanghai')::date AS d, "
                "COUNT(*) AS c "
                "FROM bars_60min WHERE instrument_id = :iid "
                "GROUP BY d ORDER BY c"
            ),
            {"iid": inst.id},
        )
        rows60 = daily60.all()
        cnt_dist_60 = dict.fromkeys([4], 0)
        abnormal_60 = []
        for d, c in rows60:
            if c in cnt_dist_60:
                cnt_dist_60[c] += 1
            else:
                abnormal_60.append((str(d), c))

        results.append({
            "symbol": inst.symbol,
            "name": inst.name,
            "cnt_15min": cnt15,
            "min_15": str(min15),
            "max_15": str(max15),
            "cnt_60min": cnt60,
            "min_60": str(min60),
            "max_60": str(max60),
            "days_15_16": cnt_dist_15[16],
            "abnormal_15": abnormal_15[:5],
            "days_60_4": cnt_dist_60[4],
            "abnormal_60": abnormal_60[:5],
        })
    return results


async def verify_adj_factor_mapping(db: AsyncSession, symbol: str) -> dict:
    """验证 adj_factor 映射正确性：抽样股票的 15min/60min adj_factor 与 bars_daily 一致。

    特别验证除权除息日前后的 adj_factor 变化。
    """
    # 查 instrument
    inst = await db.execute(
        select(Instrument).where(Instrument.symbol == symbol)
    )
    instrument = inst.scalar_one_or_none()
    if instrument is None:
        return {"symbol": symbol, "error": "instrument not found"}

    # 查 15min 中 adj_factor 有变化的日期（除权除息日附近）
    # 取 bars_daily 中 adj_factor distinct 的日期
    daily_adj = await db.execute(
        select(BarDaily.trade_date, BarDaily.adj_factor)
        .where(BarDaily.instrument_id == instrument.id)
        .where(BarDaily.adj_factor.isnot(None))
        .order_by(BarDaily.trade_date)
    )
    daily_rows = daily_adj.all()
    daily_map = {r[0]: float(r[1]) for r in daily_rows if r[1] is not None}

    # 找出 adj_factor 发生变化的日期（除权除息日）
    adj_change_dates = []
    prev_adj = None
    for d, adj in sorted(daily_map.items()):
        if prev_adj is not None and abs(adj - prev_adj) > 1e-9:
            adj_change_dates.append((d, prev_adj, adj))
        prev_adj = adj

    # 抽样验证 15min：取除权除息日前后的 15min 记录
    sample_15 = []
    if adj_change_dates:
        # 取第一个除权除息日
        target_date, prev_adj, new_adj = adj_change_dates[0]
        # 查该日期及前一天的 15min 记录
        prev_day = target_date - timedelta(days=5)  # 往前找 5 天确保有交易日
        r = await db.execute(
            select(
                Bar15Min.trade_time,
                Bar15Min.adj_factor,
            )
            .where(Bar15Min.instrument_id == instrument.id)
            .where(Bar15Min.trade_time >= datetime.combine(prev_day, datetime.min.time()))
            .where(Bar15Min.trade_time <= datetime.combine(target_date + timedelta(days=1), datetime.min.time()))
            .order_by(Bar15Min.trade_time)
        )
        for tt, af in r.all():
            tt_local = tt
            if getattr(tt, "tzinfo", None) is not None:
                tt_local = tt.astimezone(tz=None).replace(tzinfo=None)
            bar_date = tt_local.date()
            expected = daily_map.get(bar_date, 1.0)
            sample_15.append({
                "trade_time": str(tt_local),
                "bar_date": str(bar_date),
                "adj_factor_15min": float(af) if af is not None else None,
                "adj_factor_daily": expected,
                "match": abs((float(af) if af else 0) - expected) < 1e-9,
            })

    # 抽样验证 60min：同样取除权除息日附近
    sample_60 = []
    if adj_change_dates:
        target_date, prev_adj, new_adj = adj_change_dates[0]
        prev_day = target_date - timedelta(days=5)
        r = await db.execute(
            select(
                Bar60Min.trade_time,
                Bar60Min.adj_factor,
            )
            .where(Bar60Min.instrument_id == instrument.id)
            .where(Bar60Min.trade_time >= datetime.combine(prev_day, datetime.min.time()))
            .where(Bar60Min.trade_time <= datetime.combine(target_date + timedelta(days=1), datetime.min.time()))
            .order_by(Bar60Min.trade_time)
        )
        for tt, af in r.all():
            tt_local = tt
            if getattr(tt, "tzinfo", None) is not None:
                tt_local = tt.astimezone(tz=None).replace(tzinfo=None)
            bar_date = tt_local.date()
            expected = daily_map.get(bar_date, 1.0)
            sample_60.append({
                "trade_time": str(tt_local),
                "bar_date": str(bar_date),
                "adj_factor_60min": float(af) if af is not None else None,
                "adj_factor_daily": expected,
                "match": abs((float(af) if af else 0) - expected) < 1e-9,
            })

    # 统计 15min 全量 adj_factor 匹配率
    total_15 = 0
    match_15 = 0
    mismatch_15_samples = []
    r15 = await db.execute(
        select(Bar15Min.trade_time, Bar15Min.adj_factor)
        .where(Bar15Min.instrument_id == instrument.id)
        .order_by(Bar15Min.trade_time)
    )
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
        elif len(mismatch_15_samples) < 5:
            mismatch_15_samples.append({
                "trade_time": str(tt_local),
                "bar_date": str(bar_date),
                "adj_factor_15min": actual,
                "adj_factor_daily": expected,
            })

    # 统计 60min 全量 adj_factor 匹配率
    total_60 = 0
    match_60 = 0
    mismatch_60_samples = []
    r60 = await db.execute(
        select(Bar60Min.trade_time, Bar60Min.adj_factor)
        .where(Bar60Min.instrument_id == instrument.id)
        .order_by(Bar60Min.trade_time)
    )
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
        elif len(mismatch_60_samples) < 5:
            mismatch_60_samples.append({
                "trade_time": str(tt_local),
                "bar_date": str(bar_date),
                "adj_factor_60min": actual,
                "adj_factor_daily": expected,
            })

    return {
        "symbol": symbol,
        "adj_change_dates": [(str(d), a, b) for d, a, b in adj_change_dates],
        "total_15min": total_15,
        "match_15min": match_15,
        "match_rate_15min": f"{match_15}/{total_15} = {match_15/total_15*100:.2f}%" if total_15 else "N/A",
        "mismatch_15min_samples": mismatch_15_samples,
        "total_60min": total_60,
        "match_60min": match_60,
        "match_rate_60min": f"{match_60}/{total_60} = {match_60/total_60*100:.2f}%" if total_60 else "N/A",
        "mismatch_60min_samples": mismatch_60_samples,
        "sample_15_around_ex_div": sample_15[:8],
        "sample_60_around_ex_div": sample_60[:8],
    }


async def verify_ohlc_reasonableness(db: AsyncSession, instruments: list[Instrument]) -> list[dict]:
    """验证 OHLC 合理性：high >= max(open,close), low <= min(open,close), volume > 0。"""
    results = []
    for inst in instruments:
        # 15min OHLC 校验
        r15 = await db.execute(
            select(
                Bar15Min.trade_time,
                Bar15Min.open,
                Bar15Min.high,
                Bar15Min.low,
                Bar15Min.close,
                Bar15Min.volume,
            ).where(Bar15Min.instrument_id == inst.id)
        )
        rows15 = r15.all()
        total_15 = len(rows15)
        bad_ohlc_15 = 0
        bad_vol_15 = 0
        bad_samples_15 = []
        for tt, o, h, l, c, v in rows15:
            o_f = float(o) if o else 0
            h_f = float(h) if h else 0
            l_f = float(l) if l else 0
            c_f = float(c) if c else 0
            v_f = float(v) if v else 0
            ohlc_bad = (h_f < max(o_f, c_f) - 1e-9) or (l_f > min(o_f, c_f) + 1e-9)
            vol_bad = v_f <= 0
            if ohlc_bad:
                bad_ohlc_15 += 1
                if len(bad_samples_15) < 3:
                    bad_samples_15.append((str(tt), o_f, h_f, l_f, c_f, v_f))
            if vol_bad:
                bad_vol_15 += 1

        # 60min OHLC 校验
        r60 = await db.execute(
            select(
                Bar60Min.trade_time,
                Bar60Min.open,
                Bar60Min.high,
                Bar60Min.low,
                Bar60Min.close,
                Bar60Min.volume,
            ).where(Bar60Min.instrument_id == inst.id)
        )
        rows60 = r60.all()
        total_60 = len(rows60)
        bad_ohlc_60 = 0
        bad_vol_60 = 0
        bad_samples_60 = []
        for tt, o, h, l, c, v in rows60:
            o_f = float(o) if o else 0
            h_f = float(h) if h else 0
            l_f = float(l) if l else 0
            c_f = float(c) if c else 0
            v_f = float(v) if v else 0
            ohlc_bad = (h_f < max(o_f, c_f) - 1e-9) or (l_f > min(o_f, c_f) + 1e-9)
            vol_bad = v_f <= 0
            if ohlc_bad:
                bad_ohlc_60 += 1
                if len(bad_samples_60) < 3:
                    bad_samples_60.append((str(tt), o_f, h_f, l_f, c_f, v_f))
            if vol_bad:
                bad_vol_60 += 1

        results.append({
            "symbol": inst.symbol,
            "name": inst.name,
            "total_15min": total_15,
            "bad_ohlc_15min": bad_ohlc_15,
            "bad_vol_15min": bad_vol_15,
            "bad_samples_15min": bad_samples_15,
            "total_60min": total_60,
            "bad_ohlc_60min": bad_ohlc_60,
            "bad_vol_60min": bad_vol_60,
            "bad_samples_60min": bad_samples_60,
        })
    return results


async def verify_15min_60min_consistency(
    db: AsyncSession, symbol: str
) -> dict:
    """验证 15min 与 60min 一致性：4 根 15min 合并 = 1 根 60min。

    抽样最近 5 个交易日验证。
    """
    inst = await db.execute(select(Instrument).where(Instrument.symbol == symbol))
    instrument = inst.scalar_one_or_none()
    if instrument is None:
        return {"symbol": symbol, "error": "instrument not found"}

    # 取最近 5 个交易日的 60min 数据
    r60 = await db.execute(
        select(
            Bar60Min.trade_time,
            Bar60Min.open,
            Bar60Min.high,
            Bar60Min.low,
            Bar60Min.close,
            Bar60Min.volume,
        )
        .where(Bar60Min.instrument_id == instrument.id)
        .order_by(Bar60Min.trade_time.desc())
        .limit(20)  # 最近 5 天 × 4 根
    )
    bars60 = r60.all()
    if not bars60:
        return {"symbol": symbol, "error": "no 60min data"}

    # 按交易日分组，取最近 5 天
    bars60_sorted = sorted(bars60, key=lambda x: x[0])
    # 转为本地时间
    bars60_local = []
    for tt, o, h, l, c, v in bars60_sorted:
        tt_local = tt
        if getattr(tt, "tzinfo", None) is not None:
            tt_local = tt.astimezone(tz=None).replace(tzinfo=None)
        bars60_local.append((tt_local, o, h, l, c, v))

    # 按日期分组
    from collections import defaultdict
    day_groups: dict = defaultdict(list)
    for tt, o, h, l, c, v in bars60_local:
        day_groups[tt.date()].append((tt, o, h, l, c, v))

    # 取最近 5 天
    recent_days = sorted(day_groups.keys())[-5:]

    results = []
    for day in recent_days:
        bars60_day = day_groups[day]
        for tt60, o60, h60, l60, c60, v60 in bars60_day:
            # 查该 60min 时段内的 4 根 15min
            start = tt60
            end = tt60 + timedelta(minutes=60)
            r15 = await db.execute(
                select(
                    Bar15Min.trade_time,
                    Bar15Min.open,
                    Bar15Min.high,
                    Bar15Min.low,
                    Bar15Min.close,
                    Bar15Min.volume,
                )
                .where(Bar15Min.instrument_id == instrument.id)
                .where(Bar15Min.trade_time >= start)
                .where(Bar15Min.trade_time < end)
                .order_by(Bar15Min.trade_time)
            )
            bars15 = r15.all()
            if len(bars15) != 4:
                results.append({
                    "bar60_time": str(tt60),
                    "bars15_count": len(bars15),
                    "status": f"BAD: 期望 4 根 15min，实际 {len(bars15)} 根",
                })
                continue

            # 合并 4 根 15min
            opens = [float(b[1]) for b in bars15 if b[1] is not None]
            highs = [float(b[2]) for b in bars15 if b[2] is not None]
            lows = [float(b[3]) for b in bars15 if b[3] is not None]
            closes = [float(b[4]) for b in bars15 if b[4] is not None]
            vols = [float(b[5]) for b in bars15 if b[5] is not None]

            merged_open = opens[0]
            merged_close = closes[-1]
            merged_high = max(highs)
            merged_low = min(lows)
            merged_vol = sum(vols)

            o60_f = float(o60) if o60 else 0
            h60_f = float(h60) if h60 else 0
            l60_f = float(l60) if l60 else 0
            c60_f = float(c60) if c60 else 0
            v60_f = float(v60) if v60 else 0

            checks = {
                "open_match": abs(merged_open - o60_f) < 1e-4,
                "close_match": abs(merged_close - c60_f) < 1e-4,
                "high_match": abs(merged_high - h60_f) < 1e-4,
                "low_match": abs(merged_low - l60_f) < 1e-4,
                "volume_match": abs(merged_vol - v60_f) < 1e-2,
            }
            all_match = all(checks.values())

            results.append({
                "bar60_time": str(tt60),
                "bars15_count": len(bars15),
                "merged_open": merged_open,
                "bar60_open": o60_f,
                "merged_close": merged_close,
                "bar60_close": c60_f,
                "merged_high": merged_high,
                "bar60_high": h60_f,
                "merged_low": merged_low,
                "bar60_low": l60_f,
                "merged_vol": merged_vol,
                "bar60_vol": v60_f,
                "checks": checks,
                "all_match": all_match,
                "status": "OK" if all_match else "MISMATCH",
            })

    total = len(results)
    ok = sum(1 for r in results if r.get("all_match"))
    return {
        "symbol": symbol,
        "total_checked": total,
        "ok_count": ok,
        "mismatch_count": total - ok,
        "details": results,
    }


async def main() -> None:
    """主函数：小批量回补 + 验证。"""
    print("=" * 70)
    print("Task 16.3: 15min/60min 小批量验证（10 只股票）")
    print("=" * 70)

    # ===== Step 1: 查询测试股票 =====
    print("\n--- Step 1: 查询测试股票 ---")
    async with AsyncSessionLocal() as db:
        instruments = await get_test_instruments(db)
    print(f"找到 {len(instruments)} 只测试股票:")
    for inst in instruments:
        print(f"  {inst.symbol} {inst.name} ({inst.market})")
    assert len(instruments) == 10, f"期望 10 只股票，实际 {len(instruments)}"

    # ===== Step 2: 回补 15min/60min =====
    print("\n--- Step 2: 回补 15min/60min ---")
    t0 = time.time()
    backfill_results = await backfill_all(instruments)
    elapsed = time.time() - t0
    print(f"\n回补完成，耗时 {elapsed:.1f}s")
    print(f"{'symbol':<10}{'name':<12}{'15min':<8}{'60min':<8}{'status':<8}")
    for r in backfill_results:
        status = "OK" if r["success"] else "FAIL"
        print(f"{r['symbol']:<10}{r['name']:<12}{r['15min_rows']:<8}{r['60min_rows']:<8}{status:<8}")

    failed = [r for r in backfill_results if not r["success"]]
    if failed:
        print(f"\n[WARN] {len(failed)} 只股票回补失败:")
        for r in failed:
            print(f"  {r['symbol']}: {r['error']}")

    # ===== Step 3: 验证数据完整性 =====
    print("\n--- Step 3: 验证数据完整性 ---")
    async with AsyncSessionLocal() as db:
        completeness = await verify_completeness(db, instruments)
    print(f"\n{'symbol':<10}{'15min':<8}{'60min':<8}{'min_15':<22}{'max_15':<22}{'days16':<8}{'days4':<8}{'abn15':<6}{'abn60':<6}")
    for r in completeness:
        print(
            f"{r['symbol']:<10}{r['cnt_15min']:<8}{r['cnt_60min']:<8}"
            f"{r['min_15']:<22}{r['max_15']:<22}"
            f"{r['days_15_16']:<8}{r['days_60_4']:<8}"
            f"{len(r['abnormal_15']):<6}{len(r['abnormal_60']):<6}"
        )
        if r["abnormal_15"]:
            print(f"    abnormal_15min samples: {r['abnormal_15']}")
        if r["abnormal_60"]:
            print(f"    abnormal_60min samples: {r['abnormal_60']}")

    # ===== Step 4: 验证 adj_factor 映射正确性（抽样 000001） =====
    print("\n--- Step 4: 验证 adj_factor 映射正确性（抽样 000001 平安银行）---")
    async with AsyncSessionLocal() as db:
        adj_result = await verify_adj_factor_mapping(db, "000001")
    print(f"  symbol: {adj_result['symbol']}")
    print(f"  除权除息日（adj_factor 变化）: {adj_result['adj_change_dates']}")
    print(f"  15min adj_factor 匹配率: {adj_result['match_rate_15min']}")
    print(f"  60min adj_factor 匹配率: {adj_result['match_rate_60min']}")
    if adj_result["mismatch_15min_samples"]:
        print(f"  15min 不匹配样本: {adj_result['mismatch_15min_samples']}")
    if adj_result["mismatch_60min_samples"]:
        print(f"  60min 不匹配样本: {adj_result['mismatch_60min_samples']}")
    if adj_result["sample_15_around_ex_div"]:
        print("  15min 除权除息日附近样本（前 8 条）:")
        for s in adj_result["sample_15_around_ex_div"]:
            print(f"    {s}")
    if adj_result["sample_60_around_ex_div"]:
        print("  60min 除权除息日附近样本（前 8 条）:")
        for s in adj_result["sample_60_around_ex_div"]:
            print(f"    {s}")

    # ===== Step 5: 验证 OHLC 合理性 =====
    print("\n--- Step 5: 验证 OHLC 合理性 ---")
    async with AsyncSessionLocal() as db:
        ohlc_results = await verify_ohlc_reasonableness(db, instruments)
    print(f"\n{'symbol':<10}{'15min':<8}{'bad_ohlc':<10}{'bad_vol':<10}{'60min':<8}{'bad_ohlc':<10}{'bad_vol':<10}")
    all_ohlc_ok = True
    for r in ohlc_results:
        print(
            f"{r['symbol']:<10}{r['total_15min']:<8}{r['bad_ohlc_15min']:<10}{r['bad_vol_15min']:<10}"
            f"{r['total_60min']:<8}{r['bad_ohlc_60min']:<10}{r['bad_vol_60min']:<10}"
        )
        if r["bad_ohlc_15min"] > 0 or r["bad_ohlc_60min"] > 0:
            all_ohlc_ok = False
            if r["bad_samples_15min"]:
                print(f"    15min bad samples: {r['bad_samples_15min']}")
            if r["bad_samples_60min"]:
                print(f"    60min bad samples: {r['bad_samples_60min']}")

    # ===== Step 6: 验证 15min 与 60min 一致性（抽样 000001） =====
    print("\n--- Step 6: 验证 15min 与 60min 一致性（抽样 000001 平安银行，最近 5 天）---")
    async with AsyncSessionLocal() as db:
        consistency = await verify_15min_60min_consistency(db, "000001")
    print(f"  symbol: {consistency['symbol']}")
    print(f"  检查总数: {consistency['total_checked']}")
    print(f"  一致: {consistency['ok_count']}")
    print(f"  不一致: {consistency['mismatch_count']}")
    if consistency["mismatch_count"] > 0:
        print("  不一致详情:")
        for d in consistency["details"]:
            if not d.get("all_match", True):
                print(f"    {d}")

    # ===== 汇总 =====
    print("\n" + "=" * 70)
    print("验证汇总")
    print("=" * 70)
    success_count = sum(1 for r in backfill_results if r["success"])
    print(f"1. 回补成功: {success_count}/10 只股票")
    print(f"2. adj_factor 匹配率: 15min={adj_result['match_rate_15min']}, 60min={adj_result['match_rate_60min']}")
    print(f"3. OHLC 合理性: {'全部通过' if all_ohlc_ok else '存在问题（见上）'}")
    print(f"4. 15min/60min 一致性: {consistency['ok_count']}/{consistency['total_checked']} 一致")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
