"""复权因子事件方向一致性只读诊断（Round 3 补做实验）。

目的：验证 **DB 中已存储的 event factor 方向** 与 **provider（Eastmoney）事件日
preclose 推导出的 event factor 方向** 是否一致。

为什么需要它：
- 09-10 修复把 THS 作为 canonical raw 源，但 Eastmoney 仍作为交叉验证源。
- 若存储的 ``adj_factor`` 在除权事件日的「突变方向」与 Eastmoney 推导的方向相反，
  说明 event factor 的符号/方向在某一环节被反转，会导致全市场 qfq 错误。
- 本模块 **只读、不 UPDATE bars_daily.adj_factor**，仅报告差异分布。

canonical 约定（必须与 ``adjustment_factor_calculator`` 实际定义一致）：
- bar 在事件日**前** → ``adj_factor`` 含 ``event_factor``；
- 事件日及以后 → ``adj_factor`` 不含 ``event_factor``。
- 因此 ``stored_event_factor = prev_factor / event_day_factor``。

Eastmoney 侧：事件日 ``preclose``（provider_preclose）/ DB 事件日前收盘价（db_prev_close）
之比即 provider 视角的 event factor 方向：
``provider_event_factor = provider_preclose / db_prev_close``。

比较 ``diff = abs(provider_event_factor - stored_event_factor)``。

唯一副作用：只读 SELECT + 只读调用 Eastmoney kline。绝不写库、绝不改 adj_factor。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import httpx

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import PytdxSourceError  # noqa: F401  # 供调用方统一捕获源错误

logger = logging.getLogger(__name__)


@dataclass
class FactorEventSample:
    """单只标的的一次 adj_factor 事件样本（来自 DB 窗口查询）。"""

    instrument_id: object
    trade_date: date
    close: Decimal | None
    adj_factor: Decimal | None
    prev_close: Decimal | None
    prev_factor: Decimal | None
    symbol: str | None = None
    market: str | None = None


@dataclass
class CompatibilityStats:
    """事件方向一致性统计。"""

    sample_requested: int = 0
    fetch_success: int = 0
    comparable: int = 0
    p50_abs_diff: float = 0.0
    p95_abs_diff: float = 0.0
    p99_abs_diff: float = 0.0
    max_abs_diff: float = 0.0
    within_1e6: int = 0
    within_1e4: int = 0
    mismatch_samples: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.mismatch_samples is None:
            self.mismatch_samples = []


def stored_event_factor(
    prev_factor: Decimal | None,
    event_day_factor: Decimal | None,
) -> Decimal | None:
    """DB 侧 event factor = 事件日前 factor / 事件日 factor。

    事件日前 factor 含 event_factor，事件日 factor 不含，故比值即 event_factor。
    """
    if prev_factor is None or event_day_factor is None:
        return None
    if event_day_factor == 0:
        return None
    return prev_factor / event_day_factor


def provider_event_factor(
    provider_preclose: Decimal | None,
    db_prev_close: Decimal | None,
) -> Decimal | None:
    """Provider 侧 event factor = 事件日 preclose / 事件日前 DB 收盘价。"""
    if provider_preclose is None or db_prev_close is None:
        return None
    if db_prev_close == 0:
        return None
    return provider_preclose / db_prev_close


FactorEventKey = tuple[str, date]


def compare_factor_events(
    samples: list[FactorEventSample],
    preclose_map: dict[FactorEventKey, Decimal],
) -> CompatibilityStats:
    """比较 DB 存储 event factor 方向 vs provider preclose 推导方向。

    Args:
        samples: find_factor_events 返回的 DB 事件样本。
        preclose_map: {(symbol, event_date): provider 事件日 preclose}，
            缺失则该样本无法比较（不计入 fetch_success）。

    Returns:
        CompatibilityStats（含 p50/p95/p99/max 绝对差与阈值内计数）。
    """
    stats = CompatibilityStats(sample_requested=len(samples))
    diffs: list[float] = []
    for s in samples:
        if s.symbol is None:
            continue
        key = (s.symbol, s.trade_date)
        provider_preclose = preclose_map.get(key)
        if provider_preclose is None:
            # provider 未抓到该事件的 preclose：不计入成功抓取，亦不比较。
            continue
        stats.fetch_success += 1
        stored = stored_event_factor(s.prev_factor, s.adj_factor)
        provider = provider_event_factor(provider_preclose, s.prev_close)
        if stored is None or provider is None:
            continue
        stats.comparable += 1
        diff = float(abs(provider - stored))
        diffs.append(diff)
        if diff <= 1e-6:
            stats.within_1e6 += 1
        if diff <= 1e-4:
            stats.within_1e4 += 1
        if diff > 0.01:
            if len(stats.mismatch_samples) < 20:
                stats.mismatch_samples.append(
                    f"{s.symbol} trade_date={s.trade_date} "
                    f"stored={stored:.6f} provider={provider:.6f} diff={diff:.6f}"
                )

    if diffs:
        ordered = sorted(diffs)
        n = len(ordered)

        def _pct(q: float) -> float:
            if n == 1:
                return ordered[0]
            pos = q * (n - 1)
            lo = int(pos)
            hi = min(lo + 1, n - 1)
            frac = pos - lo
            return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

        stats.p50_abs_diff = _pct(0.50)
        stats.p95_abs_diff = _pct(0.95)
        stats.p99_abs_diff = _pct(0.99)
        stats.max_abs_diff = float(ordered[-1])
    return stats


async def find_factor_events(
    session: AsyncSession,
    limit: int = 100,
) -> list[FactorEventSample]:
    """从 ``bars_daily`` 中找出 adj_factor 发生变化的交易日作为事件样本。

    使用窗口函数 LAG 取相邻交易日的 close 与 adj_factor，过滤出
    ``ABS(adj_factor - prev_factor) > 1e-10`` 的事件日（>= 100 个）。

    只读 SELECT，不写库。
    """
    sql = text(
        """
        WITH x AS (
            SELECT
                b.instrument_id AS instrument_id,
                i.symbol AS symbol,
                i.market AS market,
                b.trade_date AS trade_date,
                b.close AS close,
                b.adj_factor AS adj_factor,
                LAG(b.close) OVER (
                    PARTITION BY b.instrument_id ORDER BY b.trade_date
                ) AS prev_close,
                LAG(b.adj_factor) OVER (
                    PARTITION BY b.instrument_id ORDER BY b.trade_date
                ) AS prev_factor
            FROM bars_daily b
            JOIN instruments i ON i.id = b.instrument_id
        )
        SELECT
            instrument_id,
            symbol,
            market,
            trade_date,
            close,
            adj_factor,
            prev_close,
            prev_factor
        FROM x
        WHERE prev_factor IS NOT NULL
          AND ABS(adj_factor - prev_factor) > 1e-10
        ORDER BY trade_date DESC
        LIMIT :lim
        """
    )
    rows = (await session.execute(sql, {"lim": limit})).all()
    samples: list[FactorEventSample] = []
    for row in rows:
        samples.append(
            FactorEventSample(
                instrument_id=row[0],
                trade_date=row[3],
                close=row[4],
                adj_factor=row[5],
                prev_close=row[6],
                prev_factor=row[7],
                symbol=row[1],
                market=row[2],
            )
        )
    return samples


async def fetch_eastmoney_event_preclose(
    client: httpx.AsyncClient,
    symbol: str,
    market: str,
    event_date: date,
) -> Decimal | None:
    """取 Eastmoney 事件日 preclose（不复权 close − 涨跌额）。

    只接受恰好等于 event_date 的那一根 K 线；缺失或不唯一则返回 None。
    """
    from app.services.eod_market_snapshot_provider import fetch_eastmoney_daily_kline

    rows = await fetch_eastmoney_daily_kline(
        client, symbol, market, event_date, event_date, extended=True,
    )
    exact = [row for row in rows if row.get("datetime") == event_date.isoformat()]
    if len(exact) != 1:
        return None
    close = Decimal(str(exact[0]["close"]))
    change_amount = Decimal(str(exact[0]["change_amount"]))
    return close - change_amount


async def run_factor_event_compatibility(
    session: AsyncSession,
    limit: int = 100,
    concurrency: int = 5,
) -> CompatibilityStats:
    """执行只读兼容性检查主流程（真实并行抓取 Eastmoney preclose）。

    默认实现使用共享 httpx.AsyncClient + 有界并发抓取 Eastmoney 事件日 preclose，
    不再依赖调用方注入 fetcher（避免「什么都不抓却声称兼容」）。

    Args:
        session: 异步 DB 会话（只读）。
        limit: 事件样本上限（默认 100）。
        concurrency: Eastmoney 抓取在途上限（有界并发）。

    返回 CompatibilityStats。本函数不写库、不改 adj_factor。
    """
    samples = await find_factor_events(session, limit=limit)
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(timeout=15.0) as client:
        async def fetch_one(
            sample: FactorEventSample,
        ) -> tuple[FactorEventSample, Decimal | None]:
            if sample.symbol is None or sample.market is None:
                return sample, None
            try:
                async with semaphore:
                    value = await fetch_eastmoney_event_preclose(
                        client, sample.symbol, sample.market, sample.trade_date,
                    )
            except Exception as exc:  # noqa: BLE001 - 诊断不因单股失败中断
                logger.warning(
                    "factor-compat: preclose 获取失败 %s/%s: %s",
                    sample.symbol, sample.trade_date, exc,
                )
                return sample, None
            return sample, value

        fetched = await asyncio.gather(*(fetch_one(s) for s in samples))

    preclose_map: dict[FactorEventKey, Decimal] = {
        (sample.symbol, sample.trade_date): value
        for sample, value in fetched
        if sample.symbol is not None and value is not None
    }
    return compare_factor_events(samples, preclose_map)
