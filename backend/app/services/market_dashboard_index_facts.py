"""[PANJI-MARKET-OVERVIEW] 盘后事务外拉取三大指数 + 880006 涨跌停，exact-date merge。

冻结语义（已用真实 TDX 数据验证）：
- 880006（通达信「涨停板」统计指数）scale=1：
    close = 涨停家数, open = 跌停家数
- 000001 / 399001 / 399006 close = 指数收盘点位。

设计边界（Reviewer 合同）：
- 本模块是 pytdx 外部 I/O 的唯一 owner；调用方（rebuilding_market_dashboard）
  必须在 DB 事务 *外* 调用，且任意 series 获取失败必须抛异常 → caller fail-closed
  保留旧 projection（绝不 silent replace / yesterday fallback / Eastmoney fallback）。
- 解码后的 count 直接作为 SSOT；本模块不自行复现 880006 的 universe（即不要求
  我方逐股票 theoretical-limit 计算与 880006 逐日整数完全一致）。theoretical_limit
  仅保留为价格规则单元测试 + 抽样 sanity check。
- HTML/JSON/text 等富文本输出属于 dashboard 展示层，本数据服务不产出。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from app.core.pytdx_adapter import PytdxAdapter

# 通达信市场代码：1=上海, 0=深圳
_MARKET_SSE = 1
_MARKET_SZSE = 0

# (market, code, 输出字段键)
_INDEX_SERIES: tuple[tuple[int, str, str], ...] = (
    (_MARKET_SSE, "000001", "sse"),
    (_MARKET_SZSE, "399001", "szse"),
    (_MARKET_SZSE, "399006", "chinext"),
)
# 880006「涨停板」统计指数（上海统计指数）
_LIMIT_CODE = "880006"

# 取 >= 250 个真实交易日并留适量缓冲
_FETCH_LOOKBACK_DAYS = 420


@dataclass(frozen=True)
class MarketIndexFacts:
    """单交易日的三大指数收盘 + 通达信口径涨跌停家数。None = 该字段不可得。"""

    sse_close: float | None
    szse_close: float | None
    chinext_close: float | None
    limit_up_count: int | None
    limit_down_count: int | None


def _row_trade_date(row) -> date:
    dt = getattr(row, "datetime", None)
    if dt is None:
        raise ValueError("index bars row missing 'datetime' column")
    return pd.Timestamp(dt).date()


async def fetch_market_index_facts(end_date: date) -> dict[date, MarketIndexFacts]:
    """事务外拉取 000001/399001/399006/880006，按日期 exact-merge 为 facts 字典。

    Returns:
        {trade_date: MarketIndexFacts}；若某日仅部分 series 命中，缺字段为 None。
    Raises:
        任意 series 获取失败（网络/鉴权/解码异常）-> 原样抛出，caller 应 fail-closed。
    """
    start = end_date - timedelta(days=_FETCH_LOOKBACK_DAYS)
    merged: dict[date, dict[str, object]] = {}

    async with PytdxAdapter() as adapter:
        for market, code, key in _INDEX_SERIES:
            df = await asyncio.to_thread(
                adapter.get_index_daily_bars, market, code, start, end_date
            )
            for row in df.itertuples(index=False):
                d = _row_trade_date(row)
                merged.setdefault(d, {})[f"{key}_close"] = float(row.close)

        df6 = await asyncio.to_thread(
            adapter.get_index_daily_bars, _MARKET_SSE, _LIMIT_CODE, start, end_date
        )
        for row in df6.itertuples(index=False):
            d = _row_trade_date(row)
            bucket = merged.setdefault(d, {})
            # scale=1：解码值即为家数（已验证 scale=10 会解码出非整数）
            bucket["limit_up_count"] = int(round(float(row.close)))
            bucket["limit_down_count"] = int(round(float(row.open)))

    return {
        d: MarketIndexFacts(
            sse_close=v.get("sse_close"),
            szse_close=v.get("szse_close"),
            chinext_close=v.get("chinext_close"),
            limit_up_count=v.get("limit_up_count"),
            limit_down_count=v.get("limit_down_count"),
        )
        for d, v in merged.items()
    }
