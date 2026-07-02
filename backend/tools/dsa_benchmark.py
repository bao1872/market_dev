#!/usr/bin/env python3
"""DSA 选股计算性能基准测试脚本。

用法：
    APP_ENV=production DATABASE_URL=postgresql+psycopg://... python backend/tools/dsa_benchmark.py
    APP_ENV=development python backend/tools/dsa_benchmark.py  # 读取 backend/app/config.local.py

输出：
    至少 300 只代表性股票的 DSA 计算耗时统计（p50/p90/p95/p99/max），
    区分冷启动与热启动。失败/跳过股票单独列出。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import AsyncSessionLocal
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.strategy import StrategyVersion
from app.repositories.bar_repository import get_bars
from app.services.instrument_maintenance_service import stock_symbol_sql_filter
from app.services.strategy_service import list_versions
from app.strategy.runtime import MarketDataContext, StrategyLoader


# [DSABenchmark] - 默认样本数与回看天数
_DEFAULT_SAMPLE_SIZE = 350
_DEFAULT_LOOKBACK_DAYS = 1000


def _safe_percentile(sorted_values: list[float], p: float) -> float:
    """计算已排序数值列表的百分位数（线性插值）。"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


async def _load_dsa_runtime(db: AsyncSession) -> tuple[Any, StrategyVersion]:
    """加载 dsa_selector 最新 released 版本的 runtime。"""
    versions = await list_versions(db, "dsa_selector")
    if not versions:
        raise RuntimeError("dsa_selector 无可用版本")
    released = [v for v in versions if v.status == "released"]
    version = released[-1] if released else versions[-1]
    runtime = await StrategyLoader.load(version)
    return runtime, version


async def _pick_trade_date(db: AsyncSession) -> date:
    """选择用于基准测试的交易日：bars_daily 中最新 trade_date。"""
    result = await db.execute(select(func.max(BarDaily.trade_date)))
    latest = result.scalar()
    if latest is None:
        raise RuntimeError("bars_daily 表无数据，无法进行基准测试")
    if isinstance(latest, datetime):
        return latest.date()
    return latest


async def _sample_instruments(
    db: AsyncSession,
    trade_date: date,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
) -> list[Instrument]:
    """分层选取当日有 K 线的活跃 A 股股票样本。

    分层逻辑：按历史数据长度（bars 数量）分 5 层，每层按等步长采样，
    保证长/中/短历史股票均被覆盖。
    """
    # 子查询：统计每只活跃 A 股股票在 trade_date 之前的日线数量
    count_stmt = (
        select(
            Instrument.id,
            func.count(BarDaily.trade_date).label("bar_count"),
        )
        .join(BarDaily, Instrument.id == BarDaily.instrument_id)
        .where(Instrument.status == "active")
        .where(stock_symbol_sql_filter(Instrument))
        .where(BarDaily.trade_date <= trade_date)
        .group_by(Instrument.id)
        .having(func.count(BarDaily.trade_date) >= 60)
        .subquery()
    )

    stmt = (
        select(Instrument, count_stmt.c.bar_count)
        .join(count_stmt, Instrument.id == count_stmt.c.id)
        .order_by(count_stmt.c.bar_count)
    )
    result = await db.execute(stmt)
    rows = result.all()
    if len(rows) < sample_size:
        raise RuntimeError(
            f"满足条件的活跃股票不足 {sample_size} 只（实际 {len(rows)}）"
        )

    # 按 bar_count 分 5 层等距采样
    n = len(rows)
    layers = 5
    per_layer = sample_size // layers
    sampled: list[Instrument] = []
    for layer in range(layers):
        start = n * layer // layers
        end = n * (layer + 1) // layers
        step = max(1, (end - start) // per_layer)
        layer_rows = rows[start:end:step]
        sampled.extend([r[0] for r in layer_rows[:per_layer]])

    # 若分层采样不足，从剩余列表补齐
    if len(sampled) < sample_size:
        existing_ids = {inst.id for inst in sampled}
        for r in rows:
            if r[0].id not in existing_ids:
                sampled.append(r[0])
            if len(sampled) >= sample_size:
                break

    return sampled[:sample_size]


async def _load_bars(
    db: AsyncSession,
    instrument_id: Any,
    symbol: str,
    trade_date: date,
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS,
) -> pd.DataFrame | None:
    """加载单只股票的日线行情（前复权）。"""
    start_date = trade_date - timedelta(days=lookback_days)
    try:
        bars_result = await get_bars(
            db,
            instrument_id,
            timeframe="1d",
            start_date=start_date,
            end_date=trade_date,
            adjustment="qfq",
        )
    except Exception as exc:
        raise RuntimeError(f"加载 {symbol} 行情失败: {exc}") from exc
    bars = bars_result.bars
    if bars is None or bars.empty or len(bars) < 60:
        return None
    return bars


async def _run_benchmark(
    db: AsyncSession,
    runtime: Any,
    instruments: list[Instrument],
    trade_date: date,
) -> dict[str, Any]:
    """执行 DSA 计算基准测试，返回冷/热耗时分布与错误信息。"""
    cold_times: list[float] = []
    warm_times: list[float] = []
    errors: list[tuple[str, str]] = []
    skipped: list[str] = []

    total = len(instruments)
    for idx, inst in enumerate(instruments, 1):
        bars = await _load_bars(db, inst.id, inst.symbol, trade_date)
        if bars is None:
            skipped.append(inst.symbol)
            continue

        ctx = MarketDataContext(
            instrument_id=inst.id,
            symbol=inst.symbol,
            bars_daily=bars,
            trade_date=trade_date,
        )

        # 冷启动：首次执行（features 模块/缓存初始化）
        try:
            t0 = time.perf_counter()
            await runtime.execute(ctx)
            t1 = time.perf_counter()
            cold_times.append(t1 - t0)
        except Exception as exc:
            errors.append((inst.symbol, f"cold: {exc}"))
            continue

        # 热启动：同一上下文再次执行
        try:
            t0 = time.perf_counter()
            await runtime.execute(ctx)
            t1 = time.perf_counter()
            warm_times.append(t1 - t0)
        except Exception as exc:
            errors.append((inst.symbol, f"warm: {exc}"))

        if idx % 50 == 0:
            print(f"  进度: {idx}/{total} 冷启动中位数={_safe_percentile(sorted(cold_times), 0.5)*1000:.2f}ms")

    return {
        "cold_times": cold_times,
        "warm_times": warm_times,
        "errors": errors,
        "skipped": skipped,
    }


def _print_report(
    trade_date: date,
    sample_size: int,
    result: dict[str, Any],
) -> None:
    """输出基准测试报告。"""
    cold = sorted(result["cold_times"])
    warm = sorted(result["warm_times"])
    errors = result["errors"]
    skipped = result["skipped"]

    print("\n" + "=" * 60)
    print("DSA 选股计算性能基准测试报告")
    print("=" * 60)
    print(f"交易日:     {trade_date}")
    print(f"样本总数:   {sample_size}")
    print(f"有效冷启动: {len(cold)}")
    print(f"有效热启动: {len(warm)}")
    print(f"跳过(数据不足): {len(skipped)}")
    print(f"失败:       {len(errors)}")

    def line(label: str, times: list[float]) -> None:
        if not times:
            print(f"{label}: 无数据")
            return
        print(
            f"{label}: p50={_safe_percentile(times, 0.50)*1000:6.2f}ms  "
            f"p90={_safe_percentile(times, 0.90)*1000:6.2f}ms  "
            f"p95={_safe_percentile(times, 0.95)*1000:6.2f}ms  "
            f"p99={_safe_percentile(times, 0.99)*1000:6.2f}ms  "
            f"max={times[-1]*1000:6.2f}ms  "
            f"avg={statistics.mean(times)*1000:6.2f}ms"
        )

    print("-" * 60)
    line("冷启动", cold)
    line("热启动", warm)

    if skipped:
        print("-" * 60)
        print("数据不足跳过的股票（前 20）:")
        for symbol in skipped[:20]:
            print(f"  - {symbol}")

    if errors:
        print("-" * 60)
        print("计算失败的股票（前 20）:")
        for symbol, msg in errors[:20]:
            print(f"  - {symbol}: {msg}")

    print("=" * 60)


async def main() -> None:
    """脚本入口。"""
    settings = get_settings()
    safe_url = settings.database_url
    # 简单脱敏：隐藏密码
    if "://" in safe_url:
        parts = safe_url.split("@")
        if len(parts) == 2:
            prefix = parts[0].rsplit(":", 1)[0] + ":***"
            safe_url = f"{prefix}@{parts[1]}"
    print(f"APP_ENV={settings.app_env}")
    print(f"DATABASE_URL={safe_url}")

    async with AsyncSessionLocal() as db:
        runtime, version = await _load_dsa_runtime(db)
        print(f"DSA version: {version.version} (status={version.status})")

        trade_date = await _pick_trade_date(db)
        print(f"基准交易日: {trade_date}")

        instruments = await _sample_instruments(db, trade_date, _DEFAULT_SAMPLE_SIZE)
        print(f"选中 {len(instruments)} 只代表性活跃股票")

        start_all = time.perf_counter()
        result = await _run_benchmark(db, runtime, instruments, trade_date)
        elapsed_all = time.perf_counter() - start_all

    _print_report(trade_date, len(instruments), result)
    print(f"总耗时: {elapsed_all:.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
