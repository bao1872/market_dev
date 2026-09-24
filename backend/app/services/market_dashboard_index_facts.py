"""[PANJI-MARKET-OVERVIEW] 盘后事务外拉取三大指数 + 880006 涨跌停，exact-date merge。

冻结语义（已用真实 TDX 数据验证）：
- 880006（通达信「涨停板」统计指数）scale=1：
    close = 涨停家数, open = 跌停家数
- 000001 / 399001 / 399006 close = 指数收盘点位。

设计边界（Reviewer 合同）：
- 本模块是 pytdx 外部 I/O 的唯一 owner；调用方（rebuilding_market_dashboard）
  必须在 DB 事务 *外* 调用，且任意 series 获取/解码失败必须抛异常 → caller fail-closed
  保留旧 projection（绝不 silent replace / yesterday fallback / Eastmoney fallback）。
- 解码后的 count 直接作为 SSOT；本模块不自行复现 880006 的 universe（即不要求
  我方逐股票 theoretical-limit 计算与 880006 逐日整数完全一致）。theoretical_limit
  仅保留为价格规则单元测试 + 抽样 sanity check。
- HTML/JSON/text 等富文本输出属于 dashboard 展示层，本数据服务不产出。

执行契约（P1 修正）：
- ``PytdxAdapter`` 是**同步**上下文管理器；本模块把整段同步 pytdx 会话包进
  ``_fetch_all_market_indices_sync``，再由 ``fetch_market_index_facts`` 经
  ``asyncio.to_thread`` 跑**一次**（只进一次线程、只开一次 pytdx session、
  不阻塞 event loop、不新增 connection/retry framework）。
- ``get_index_daily_bars`` 是 keyword-only；所有调用必须显式
  ``market=... code=... start=... end=...``。
- 解码严格性：指数收盘须 finite 且 > 0；880006 家数须 finite、>= 0、且为
  整数值（不得用 ``round`` 把 51.4 / 负数 / NaN 修成看似合法的计数）——不合法
  即视为数据源漂移，直接 raise（provider contract failure）。
- 880006 scale 恒为 1；但 TDX 对“零家数”使用最小价格刻度 0.01 编码
  （而非 0.00）。因此唯一允许的浮点表示即精确的 0.01，由
  ``_decode_880006_count`` 归一化为整数 0。任何其他非整数（0.02 / 1.01 /
  NaN / 负数等）一律视为 provider 漂移，raise 让 caller fail-closed 保留旧
  projection。不得把 ``value < 阈值 -> 0`` 这类宽松规则写进 decoder。
"""
from __future__ import annotations

import asyncio
import math
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


def _require_finite_positive(value, field: str) -> float:
    """指数收盘点位：finite 且 > 0。"""
    fv = float(value)
    if not math.isfinite(fv) or fv <= 0:
        raise ValueError(f"market index fact {field} must be finite positive, got {value!r}")
    return fv


def _decode_880006_count(value, field: str) -> int:
    """880006 涨跌停家数解码（TDX provider-specific）。

    冻结语义：880006 scale=1，close=涨停家数、open=跌停家数。
    TDX 对“零家数”使用最小价格刻度 0.01 编码（而非 0.00），因此唯一允许的
    浮点表示即精确的 0.01，归一化为整数 0。任何其他非整数（0.02 / 1.01 /
    NaN / 负数等）一律视为 provider 漂移，raise 让 caller fail-closed 保留旧
    projection。绝不使用 ``value < 阈值 -> 0`` / round / floor / ceil / clamp
    这类会吞掉真实漂移的宽松规则。
    """
    fv = float(value)
    if not math.isfinite(fv) or fv < 0:
        raise ValueError(f"market index fact {field} must be finite non-negative, got {value!r}")
    # TDX 零家数最小刻度编码：仅精确 0.01 归一化为 0
    if math.isclose(fv, 0.01, rel_tol=0.0, abs_tol=1e-9):
        return 0
    if abs(fv - round(fv)) > 1e-9:
        raise ValueError(f"market index fact {field} must be integer-valued, got {value!r}")
    return int(round(fv))


def _fetch_all_market_indices_sync(end_date: date) -> dict[date, MarketIndexFacts]:
    """同步整段 pytdx 会话：只开一次 ``PytdxAdapter``，顺序取四条 series 并严格解码。

    - ``with PytdxAdapter() as adapter``：单次 sync context manager（非 async）。
    - 任意 series 获取失败 / 解码不合法 → 原样抛出，caller 应 fail-closed。
    - 解码后缺字段在该日事实中为 None（NULL 合同由 caller 处理）。
    """
    start = end_date - timedelta(days=_FETCH_LOOKBACK_DAYS)
    merged: dict[date, dict[str, object]] = {}

    with PytdxAdapter() as adapter:
        for market, code, key in _INDEX_SERIES:
            df = adapter.get_index_daily_bars(market=market, code=code, start=start, end=end_date)
            for row in df.itertuples(index=False):
                d = _row_trade_date(row)
                merged.setdefault(d, {})[f"{key}_close"] = _require_finite_positive(
                    row.close, f"{key}_close"
                )

        df6 = adapter.get_index_daily_bars(market=_MARKET_SSE, code=_LIMIT_CODE, start=start, end=end_date)
        for row in df6.itertuples(index=False):
            d = _row_trade_date(row)
            bucket = merged.setdefault(d, {})
            # scale=1：解码值即为家数；TDX 对零家数用最小刻度 0.01 编码，归一化为 0；
            # 其余非整数/负数/NaN 一律视为 provider 漂移，拒绝（fail-closed）
            bucket["limit_up_count"] = _decode_880006_count(row.close, "limit_up_count")
            bucket["limit_down_count"] = _decode_880006_count(row.open, "limit_down_count")

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


async def fetch_market_index_facts(end_date: date) -> dict[date, MarketIndexFacts]:
    """事务外拉取 000001/399001/399006/880006，按日期 exact-merge 为 facts 字典。

    整段同步 pytdx 会话经 ``asyncio.to_thread`` 跑一次（不阻塞 event loop、
    不新增 framework）。

    Returns:
        {trade_date: MarketIndexFacts}；若某日仅部分 series 命中，缺字段为 None。
    Raises:
        任意 series 获取失败（网络/鉴权/解码异常）-> 原样抛出，caller 应 fail-closed。
    """
    return await asyncio.to_thread(_fetch_all_market_indices_sync, end_date)
