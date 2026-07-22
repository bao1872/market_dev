"""行情聚合服务 - 统一 OHLCV bar 聚合唯一事实源。

用法:
    service = MarketDataAggregationService()
    result = await service.get_bars(session, instrument_id, timeframe="1d", adj="qfq")
    result.bars  # DataFrame
    result.data_source  # db | hybrid | pytdx | degraded

职责:
- 日线: DB 优先 → Pytdx 补尾 → 复权 → 过滤未完成 bar → 排序去重
- 周线/月线: 从日线动态合成
- 日内(15m/1h): DB 优先 → 交易时段拉 1m 聚合为 partial bar → 复权 → 合并
- 数据源诊断: data_source / as_of / is_partial / last_persisted_bar_time /
  last_live_bar_time / freshness_seconds / degraded / degraded_reason
- Redis 短缓存: TTL 5–15 秒，缓存键含所有影响结果的参数
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pytdx_adapter import get_pytdx_adapter
from app.core.redis_client import get_sync_redis
from app.core.time import SHANGHAI_TZ, now_shanghai, shanghai_business_date
from app.repositories.bar_repository import (
    _get_symbol,
    _query_15min_bars,
    _query_60min_bars,
    _query_daily_bars,
    _query_minute_bars,
)
from app.services.adjustment_factor_service import AdjustmentFactorService
from app.services.calendar_service import is_trading_day_async
from app.services.chart_bars_service import compute_source_bar_hash
from app.services.kline_aggregator import aggregate as aggregate_kline
from app.services.market_status_service import (
    MARKET_SESSION_AFTERNOON,
    MARKET_SESSION_MORNING,
    compute_market_session,
)

logger = logging.getLogger("services.market_data_aggregation_service")

# [mdas] - 描述: 支持的周期与复权方式
_ALLOWED_TIMEFRAMES: set[str] = {"1d", "15m", "1h", "1w", "1mo", "1m"}
_ALLOWED_ADJ: set[str] = {"qfq", "none"}

# [mdas] - 描述: 默认回看范围（与 bars.py / indicator_service.py 保持一致）
_DEFAULT_DAILY_LOOKBACK_DAYS: int = 5000
_DEFAULT_INTRADAY_LOOKBACK_DAYS: int = 180

# [CP-V3-A] - 描述: 日内周期每交易日 bar 根数（用于 limit 驱动的回看天数计算）
# A 股交易日 4 小时：15m=16 根，1h=4 根，1m=240 根
_BARS_PER_DAY: dict[str, int] = {"15m": 16, "1h": 4, "1m": 240}

# [CP-V3-A] - 描述: 日内回看安全边界（最大回看天数，约 20 年，防止无限扩大查询）
_MAX_INTRADAY_LOOKBACK_DAYS: int = 5000

# [CP-V3-A] - 描述: limit 驱动回看的额外 buffer 天数（确保交易日充足）
_LIMIT_LOOKBACK_BUFFER_DAYS: int = 10

# [mdas] - 描述: A 股收盘时间，日线 bar 完成边界
_DAILY_CLOSE_TIME: dt_time = dt_time(15, 0)

# [mdas] - 描述: Redis 短缓存 TTL 范围（秒）
_MIN_CACHE_TTL: int = 5
_MAX_CACHE_TTL: int = 15
_REDIS_CACHE_PREFIX: str = "mdas"

# [CP-V3-A] - 描述: 行情数据契约版本 v3（count-aware：含 requested_count/actual_count/
#   coverage_start/coverage_end/history_exhausted，支持 availability 三态状态机）
# [CP-V3-A2] - 描述: v4 — 迭代回补（backfill_rounds/coverage_reason）+ history_exhausted
#   语义修正（intraday: 基于 _fetch_intraday_with_backfill 真实 earliest bar 判定，
#   不再"单次查询 < limit 即 True"）。bump v3→v4 自动隔离旧缓存（旧 v3 缓存的
#   history_exhausted 语义不准确，必须失效）
_MARKET_DATA_CONTRACT_VERSION: str = "v4"

# [mdas] - 描述: 1m → 15m/1h 聚合频率映射
_TARGET_FREQ: dict[str, str] = {"15m": "15min", "1h": "60min"}

# [mdas] - 描述: 标准行情列
_BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]


@dataclass
class BarAggregationResult:
    """行情聚合结果，包含 bars DataFrame 与数据源诊断字段。

    CHANGE-20260717-002 扩展（v2 契约）：
    - warmup_bars_full: 含 warmup 的完整计算集（warmup_bars=0 时为 None）
    - market_data_contract_version: 契约版本常量 "v2"
    - source_bar_hash: bars 的 OHLCV SHA256 前 16 字符（跨调用方一致性校验）
    - adj_factor_hash: 因子序列 SHA256 前 16 字符（adj=none 时为空串）
    - adjustment_as_of: 回显复权锚点（None=最新）
    - completed_through: 最新已完成 bar 时间（不含 partial/realtime）

    [CP-V3-A] v3 契约扩展（count-aware，支持 availability 三态状态机）：
    - requested_count: 调用方请求的 limit 值（None=未指定）
    - actual_count: 实际返回的 bars 数量
    - coverage_start: bars 最早时间（None=空数据）
    - coverage_end: bars 最晚时间（None=空数据）
    - history_exhausted: DB/上游历史是否不足（True=真实历史不够；
      False=历史足够但可能因系统回看窗口未取满——后者为 INPUT_CONTRACT_VIOLATION）

    [CP-V3-A2] v4 契约扩展（迭代回补，修正 history_exhausted 语义）：
    - backfill_rounds: 日内迭代回补的实际查询轮数（1=单次满足；>1=多轮扩展；
      日线=0，日线不需要迭代回补）
    - coverage_reason: 覆盖率原因诊断：
      * "no_limit" — 未指定 limit，单次查询
      * "met_after_N_rounds" — N 轮后满足 required_count
      * "history_exhausted_empty_query" — 查询返回空，已到 listing date 之前
      * "history_exhausted_no_progress" — 连续扩展无新数据，DB 真实历史不足
      * "max_rounds_reached" — 达到最大轮数仍不足（INPUT_CONTRACT_VIOLATION 风险）
      * "daily_no_backfill" — 日线路径不经过迭代回补
    - history_exhausted 语义修正（intraday）：基于 _fetch_intraday_with_backfill
      真实 earliest bar 判定，不再"单次查询 < limit 即 True"
    """

    bars: pd.DataFrame
    data_source: str
    as_of: datetime
    is_partial: bool
    last_persisted_bar_time: pd.Timestamp | None
    last_live_bar_time: pd.Timestamp | None
    freshness_seconds: float
    degraded: bool
    degraded_reason: str | None
    cache_hit: bool = False
    warmup_bars_full: pd.DataFrame | None = None
    market_data_contract_version: str = _MARKET_DATA_CONTRACT_VERSION
    source_bar_hash: str = ""
    adj_factor_hash: str = ""
    adjustment_as_of: date | None = None
    completed_through: pd.Timestamp | None = None
    # [CP-V3-A] count-aware 字段
    requested_count: int | None = None
    actual_count: int = 0
    coverage_start: pd.Timestamp | None = None
    coverage_end: pd.Timestamp | None = None
    history_exhausted: bool = False
    # [CP-V3-A2] 迭代回补诊断字段
    backfill_rounds: int = 0
    coverage_reason: str = ""


# ===== 交易时间判断 =====


def _is_trading_hours(now: datetime | None = None) -> bool:
    """判断当前是否在 A 股实时交易时段（上午盘/下午盘，午休不算）。

    复用 market_status_service.compute_market_session，与 /market/status 口径一致，
    不再自行写 9:30-15:00 连续判断。
    """
    if now is None:
        now = now_shanghai()
    # 这里只做 weekday 快速判断；节假日场景由调用方按需使用 is_trading_day_async
    is_trading_day = now.weekday() < 5
    session_name = compute_market_session(now, is_trading_day)
    return session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON)


async def _is_trading_hours_async(now: datetime | None = None) -> bool:
    """异步包装（生产代码使用），支持同步/异步两种 patch 形态。"""
    result = _is_trading_hours(now)
    return result


# ===== 日线最后一个已完成 bar 边界 =====


async def _expected_last_completed_daily_bar(
    session: AsyncSession,
    now: datetime | None = None,
) -> date:
    """计算当前最后一个已完成日线的交易日。

    规则:
    - 今天是交易日且已过收盘时间 -> 今天
    - 否则往前找最近一个交易日
    """
    if now is None:
        now = now_shanghai()
    today = now.date()
    if await is_trading_day_async(session, today) and now.time() >= _DAILY_CLOSE_TIME:
        return today

    prev = today - timedelta(days=1)
    for _ in range(90):
        if await is_trading_day_async(session, prev):
            return prev
        prev -= timedelta(days=1)
    return prev


async def _call_expected_last_completed_daily_bar(
    session: AsyncSession,
    now: datetime,
) -> date:
    """调用 _expected_last_completed_daily_bar，兼容同步/异步 patch。"""
    fn = _expected_last_completed_daily_bar
    if asyncio.iscoroutinefunction(fn):
        return await fn(session, now)
    return fn(session, now)  # type: ignore[return-value]


# ===== 日期范围解析 =====


def _resolve_date_range(
    timeframe: str,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
    *,
    limit: int | None = None,
) -> tuple[date, date] | tuple[datetime, datetime]:
    """解析查询范围。

    [CP-V3-A] count-aware 回补：当 limit 指定且 timeframe 为日内周期时，
    自动根据 limit 和每交易日 bar 根数计算所需最小回看天数，与
    _DEFAULT_INTRADAY_LOOKBACK_DAYS 取较大值，确保 actual_count 达到 limit。
    安全边界：最大不超过 _MAX_INTRADAY_LOOKBACK_DAYS（约 20 年）。
    """
    # [mdas] - 描述: 统一使用上海业务日期，避免服务器本地时区跨日误判
    today = shanghai_business_date()
    if timeframe in ("1d", "1w", "1mo"):
        if isinstance(end_date, date):
            end = end_date
        elif isinstance(end_date, datetime):
            end = end_date.date()
        else:
            end = today
        if isinstance(start_date, date):
            start = start_date
        elif isinstance(start_date, datetime):
            start = start_date.date()
        else:
            start = end - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS)
        return start, end

    # 15m / 1h / 1m
    if isinstance(end_date, datetime):
        end = end_date
    else:
        end = datetime.combine(end_date or today, datetime.max.time())
    if isinstance(start_date, datetime):
        start = start_date
    else:
        # [CP-V3-A] count-aware：limit 驱动的回看天数计算
        lookback_days = _DEFAULT_INTRADAY_LOOKBACK_DAYS
        if start_date is None and limit is not None:
            bars_per_day = _BARS_PER_DAY.get(timeframe, 16)
            min_days_needed = (
                math.ceil(limit / bars_per_day) + _LIMIT_LOOKBACK_BUFFER_DAYS
            )
            lookback_days = min(
                max(_DEFAULT_INTRADAY_LOOKBACK_DAYS, min_days_needed),
                _MAX_INTRADAY_LOOKBACK_DAYS,
            )
        start = datetime.combine(
            start_date or (end.date() - timedelta(days=lookback_days)),
            datetime.min.time(),
        )
    return start, end


# [CP-V3-A2] - 描述: 日内迭代回补参数
# 迭代回补循环：查询→不足→向前扩展→满足 required_count 或确认 history_exhausted
_MAX_BACKFILL_ROUNDS: int = 10  # 最大查询轮数（防止死循环）
_BACKFILL_EXPAND_DAYS: int = 90  # 每轮向前扩展的天数（约 60 交易日）
_NO_PROGRESS_LIMIT: int = 2  # 连续 N 轮无新数据 → 真实历史耗尽


async def _fetch_intraday_with_backfill(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    initial_start: datetime,
    end: datetime,
    query_fn: Any,
    *,
    required_count: int | None = None,
) -> tuple[pd.DataFrame, int, bool, str]:
    """[CP-V3-A2] 日内迭代回补查询。

    正确算法（PRD V3.3 §1.2 + DEVELOP §1.2）：
      query range → 去重排序 → actual >= required_count：结束
      → 已到 instrument first_bar/listing date：history_exhausted=True
      → 否则按固定步长向前扩展 → 达到 max_rounds：coverage_reason=max_rounds_reached

    与 CP-V3-A 的区别：
      CP-V3-A 只按 limit 估算一次回看日期 → 节假日/停牌/数据缺口会导致返回 < limit
      CP-V3-A2 实现受控循环，持续扩展直到满足或确认真实历史不足

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        timeframe:日内周期（15m/1h/1m）
        initial_start: 初始查询起始时间（来自 _resolve_date_range 估算）
        end: 查询结束时间
        query_fn: 查询函数（_query_15min_bars / _query_60min_bars / _query_minute_bars）
        required_count: 需要的最小 bar 数（None=无 limit 要求，单次查询）

    Returns:
        (bars_df, backfill_rounds, history_exhausted, coverage_reason)
        - bars_df: 合并去重后的 DataFrame（可能 > required_count，由调用方 tail(limit)）
        - backfill_rounds: 实际查询轮数
        - history_exhausted: True=真实历史不足；False=历史足够或未指定 limit
        - coverage_reason: 诊断原因字符串
    """
    # 无 limit 要求 → 单次查询
    if required_count is None:
        bars_df = await query_fn(session, instrument_id, initial_start, end)
        return bars_df, 1, False, "no_limit"

    bars_df = pd.DataFrame()
    current_start = initial_start
    rounds = 0
    no_progress_count = 0
    last_count = 0

    while rounds < _MAX_BACKFILL_ROUNDS:
        rounds += 1
        new_bars = await query_fn(session, instrument_id, current_start, end)

        if not new_bars.empty:
            if bars_df.empty:
                bars_df = new_bars
            else:
                bars_df = _merge_bars(bars_df, new_bars)

        current_count = len(bars_df)

        # 满足要求 → 返回（history_exhausted=False）
        if current_count >= required_count:
            return bars_df, rounds, False, f"met_after_{rounds}_rounds"

        # 查询返回空 → 已到 listing date 之前（真实历史不足）
        if new_bars.empty and rounds > 1:
            # 第 1 轮可能因 start 过晚返回空，第 2 轮仍空 → 真实历史不足
            return bars_df, rounds, True, "history_exhausted_empty_query"

        # 连续无进展检测（扩展后没有新数据 → DB 中没有更早的 bar）
        if current_count == last_count:
            no_progress_count += 1
            if no_progress_count >= _NO_PROGRESS_LIMIT:
                return bars_df, rounds, True, "history_exhausted_no_progress"
        else:
            no_progress_count = 0

        last_count = current_count

        # 向前扩展 start
        new_start = current_start - timedelta(days=_BACKFILL_EXPAND_DAYS)
        if new_start >= current_start:
            # 无法继续扩展（datetime 下限）
            return bars_df, rounds, True, "history_exhausted_cannot_expand"
        current_start = new_start

    # 达到 max_rounds 仍不足
    # 此时 history_exhausted=False（可能是查询窗口仍不足 → INPUT_CONTRACT_VIOLATION）
    return bars_df, rounds, False, f"max_rounds_reached_{rounds}"


# ===== 实时源拉取（Pytdx 直调，不走 DB） =====


async def fetch_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    """从 Pytdx 拉取日线数据（DB 有缺口时补尾）。"""
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_daily_bars, symbol, start_date, end_date)
    except Exception as exc:
        logger.warning("Pytdx 拉取日线失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    # [mdas-dedup] - pytdx 日线 datetime 为 15:00（收盘时刻），DB trade_date 为 00:00（午夜）。
    # 规范化到午夜，使 _merge_bars 的 index.duplicated() 能按交易日正确去重，
    # 避免"同日 00:00 和 15:00 两根错误日线"（CHANGE-20260717-002 验收发现）。
    raw_df.index = raw_df.index.normalize()
    raw_df.index.name = "trade_date"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


async def fetch_minute_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    start_time: datetime,
    end_time: datetime,
) -> pd.DataFrame:
    """从 Pytdx 拉取 1 分钟线数据（实时聚合用，不写库）。"""
    symbol = await _get_symbol(session, instrument_id)
    if symbol is None:
        logger.warning("instrument 不存在 instrument_id=%s", instrument_id)
        return pd.DataFrame()

    adapter = get_pytdx_adapter()
    try:
        raw_df = await asyncio.to_thread(adapter.get_minute_bars, symbol, start_time, end_time)
    except Exception as exc:
        logger.warning("Pytdx 拉取 1m 失败 instrument_id=%s: %s", instrument_id, exc)
        raise

    if raw_df.empty:
        return raw_df

    raw_df = raw_df.copy()
    raw_df = raw_df.set_index("datetime")
    raw_df.index.name = "trade_time"
    if "adj_factor" not in raw_df.columns:
        raw_df["adj_factor"] = 1.0
    return raw_df


# ===== 数据合并与聚合 =====


def _merge_bars(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并两个 DataFrame，去重保留最新，按时间排序。"""
    if existing.empty:
        return new.copy()
    if new.empty:
        return existing.copy()

    merged = pd.concat([existing, new])
    merged = merged[~merged.index.duplicated(keep="last")]
    merged = merged.sort_index()
    return merged


def _aggregate_minute_to_target(minute_df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """将 1 分钟线聚合为 15m/1h。"""
    freq = _TARGET_FREQ[timeframe]
    agg = minute_df.resample(freq, closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
        "amount": "sum",
        "adj_factor": "last",
    })
    agg = agg.dropna(subset=["close"])
    return agg


def _aggregate_minute_to_daily(
    minute_df: pd.DataFrame,
    adj_factor: float | None = None,
) -> pd.DataFrame:
    """将当日 1 分钟线聚合成一根 partial daily bar。

    规则：
    - open = 第一根 1m open
    - high = max(high)
    - low = min(low)
    - close = 最后一根 1m close
    - volume/amount = sum
    - adj_factor = 最后一根 adj_factor（或传入的复权因子）
    - index = 最后一根 1m 时间（表示 last_live_bar_time）

    调用方需保证 minute_df 只含已完成 1m bar。

    Args:
        minute_df: 已完成 1m bar DataFrame
        adj_factor: 若指定，对 open/high/low/close 应用该复权因子；
                    用于 qfq 场景，保证 partial daily bar 与复权后的历史日线连续。
    """
    if minute_df.empty:
        return minute_df
    # [mdas] - partial daily 的索引使用 pd.Timestamp(date)，与 DB 日线表（trade_date）保持一致，
    # 同时保证索引类型为 DatetimeIndex，避免与 date 对象混合导致 sort_index 失败。
    trade_date = minute_df.index[-1].date() if hasattr(minute_df.index[-1], "date") else pd.Timestamp(minute_df.index[-1]).date()
    factor = adj_factor if adj_factor is not None else float(minute_df["adj_factor"].iloc[-1])
    partial = pd.DataFrame({
        "open": [float(minute_df["open"].iloc[0]) * factor],
        "high": [float(minute_df["high"].max()) * factor],
        "low": [float(minute_df["low"].min()) * factor],
        "close": [float(minute_df["close"].iloc[-1]) * factor],
        "volume": [float(minute_df["volume"].sum())],
        "amount": [float(minute_df["amount"].sum())],
        "adj_factor": [factor],
    }, index=[pd.Timestamp(trade_date)])
    partial.index.name = "trade_date"
    return partial


def _filter_unfinished_daily_bars(
    df: pd.DataFrame,
    now: datetime | None = None,
) -> pd.DataFrame:
    """过滤当日未完成日线 Bar。"""
    if df.empty:
        return df
    if now is None:
        now = now_shanghai()
    today = now.date()
    latest_date = df.index[-1].date()
    if latest_date == today and now.time() < _DAILY_CLOSE_TIME:
        df = df[df.index.date < today]
    return df


def _finalize_bars(
    df: pd.DataFrame,
    timeframe: str,
    now: datetime | None = None,
) -> pd.DataFrame:
    """排序、去重、过滤未完成 bar。"""
    if df.empty:
        return df
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if timeframe == "1d":
        df = _filter_unfinished_daily_bars(df, now)
    return df


# ===== 复权因子哈希（跨调用方一致性校验） =====


def _compute_adj_factor_hash(factor_df: pd.DataFrame) -> str:
    """计算复权因子序列哈希（trade_date|adj_factor 拼接的 SHA256 前 16 字符）。

    用于跨调用方（bars API / indicator / feature snapshot）的因子一致性校验。
    与 compute_source_bar_hash 配对：source_bar_hash 校验 OHLCV，adj_factor_hash 校验因子。

    Args:
        factor_df: 复权因子 DataFrame，columns=[trade_date, adj_factor]

    Returns:
        SHA256 hexdigest 前 16 字符；空 DataFrame 返回空字符串
    """
    if factor_df is None or factor_df.empty:
        return ""
    parts: list[str] = []
    for _, row in factor_df.iterrows():
        td = row["trade_date"]
        td_str = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td)
        parts.append(f"{td_str}|{row['adj_factor']}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ===== Redis 短缓存 =====


def _cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    include_realtime: bool,
    completed_only: bool,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
    limit: int | None,
    warmup_bars: int,
    adjustment_as_of: date | None,
) -> str:
    """构建缓存键，包含所有影响结果的参数 + 契约版本（自动隔离新旧缓存）。"""
    start = start_date.isoformat() if start_date is not None else "_"
    end = end_date.isoformat() if end_date is not None else "_"
    as_of_str = adjustment_as_of.isoformat() if adjustment_as_of is not None else "_"
    limit_str = str(limit) if limit is not None else "_"
    return (
        f"{_REDIS_CACHE_PREFIX}:"
        f"{instrument_id}:{timeframe}:{adj}:{include_realtime}:{completed_only}:"
        f"{start}:{end}:{limit_str}:{warmup_bars}:{as_of_str}:"
        f"{_MARKET_DATA_CONTRACT_VERSION}"
    )


def _serialize_result(result: BarAggregationResult) -> str:
    """将结果序列化为 JSON 字符串。"""
    def _df_to_payload(df: pd.DataFrame) -> dict[str, Any]:
        if df.empty:
            return {"index": [], "columns": list(df.columns), "data": []}
        payload = df.to_dict(orient="split")
        payload["index"] = [idx.isoformat() for idx in df.index]
        return payload

    payload = {
        "bars": _df_to_payload(result.bars),
        "warmup_bars_full": (
            _df_to_payload(result.warmup_bars_full)
            if result.warmup_bars_full is not None
            else None
        ),
        "data_source": result.data_source,
        "as_of": result.as_of.isoformat(),
        "is_partial": result.is_partial,
        "last_persisted_bar_time": (
            result.last_persisted_bar_time.isoformat()
            if result.last_persisted_bar_time is not None
            else None
        ),
        "last_live_bar_time": (
            result.last_live_bar_time.isoformat()
            if result.last_live_bar_time is not None
            else None
        ),
        "freshness_seconds": result.freshness_seconds,
        "degraded": result.degraded,
        "degraded_reason": result.degraded_reason,
        "market_data_contract_version": result.market_data_contract_version,
        "source_bar_hash": result.source_bar_hash,
        "adj_factor_hash": result.adj_factor_hash,
        "adjustment_as_of": (
            result.adjustment_as_of.isoformat()
            if result.adjustment_as_of is not None
            else None
        ),
        "completed_through": (
            result.completed_through.isoformat()
            if result.completed_through is not None
            else None
        ),
        # [CP-V3-A] count-aware 字段
        "requested_count": result.requested_count,
        "actual_count": result.actual_count,
        "coverage_start": (
            result.coverage_start.isoformat()
            if result.coverage_start is not None
            else None
        ),
        "coverage_end": (
            result.coverage_end.isoformat()
            if result.coverage_end is not None
            else None
        ),
        "history_exhausted": result.history_exhausted,
        # [CP-V3-A2] 迭代回补诊断字段
        "backfill_rounds": result.backfill_rounds,
        "coverage_reason": result.coverage_reason,
    }
    return json.dumps(payload)


def _deserialize_result(raw: str) -> BarAggregationResult | None:
    """从 JSON 字符串反序列化结果。"""
    def _payload_to_df(payload_df: dict[str, Any] | None) -> pd.DataFrame:
        if payload_df is None:
            return pd.DataFrame()
        index = pd.to_datetime(payload_df["index"])
        df = pd.DataFrame(
            payload_df["data"],
            index=index,
            columns=payload_df["columns"],
        )
        for col in df.columns:
            if col in _BAR_COLUMNS:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        return df

    try:
        payload = json.loads(raw)
        bars = _payload_to_df(payload["bars"])
        warmup = (
            _payload_to_df(payload.get("warmup_bars_full"))
            if payload.get("warmup_bars_full") is not None
            else None
        )

        return BarAggregationResult(
            bars=bars,
            data_source=payload["data_source"],
            as_of=datetime.fromisoformat(payload["as_of"]),
            is_partial=payload["is_partial"],
            last_persisted_bar_time=(
                pd.Timestamp(payload["last_persisted_bar_time"])
                if payload["last_persisted_bar_time"] is not None
                else None
            ),
            last_live_bar_time=(
                pd.Timestamp(payload["last_live_bar_time"])
                if payload["last_live_bar_time"] is not None
                else None
            ),
            freshness_seconds=payload["freshness_seconds"],
            degraded=payload["degraded"],
            degraded_reason=payload["degraded_reason"],
            cache_hit=payload.get("cache_hit", False),
            warmup_bars_full=warmup,
            market_data_contract_version=payload.get(
                "market_data_contract_version", _MARKET_DATA_CONTRACT_VERSION
            ),
            source_bar_hash=payload.get("source_bar_hash", ""),
            adj_factor_hash=payload.get("adj_factor_hash", ""),
            adjustment_as_of=(
                date.fromisoformat(payload["adjustment_as_of"])
                if payload.get("adjustment_as_of") is not None
                else None
            ),
            completed_through=(
                pd.Timestamp(payload["completed_through"])
                if payload.get("completed_through") is not None
                else None
            ),
            # [CP-V3-A] count-aware 字段（向后兼容：旧缓存无这些字段时用默认值）
            requested_count=payload.get("requested_count"),
            actual_count=payload.get("actual_count", len(bars)),
            coverage_start=(
                pd.Timestamp(payload["coverage_start"])
                if payload.get("coverage_start") is not None
                else None
            ),
            coverage_end=(
                pd.Timestamp(payload["coverage_end"])
                if payload.get("coverage_end") is not None
                else None
            ),
            history_exhausted=payload.get("history_exhausted", False),
            # [CP-V3-A2] 迭代回补诊断字段（向后兼容：旧缓存无这些字段时用默认值）
            backfill_rounds=payload.get("backfill_rounds", 0),
            coverage_reason=payload.get("coverage_reason", ""),
        )
    except Exception as exc:
        logger.warning("MDAS 缓存反序列化失败: %s", exc)
        return None


def _cache_get(cache_key: str) -> BarAggregationResult | None:
    """从 Redis 读取缓存结果。"""
    from app.config import get_settings

    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return None
    try:
        client = get_sync_redis()
        raw = client.get(cache_key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return _deserialize_result(raw)
    except Exception as exc:
        logger.warning("MDAS 缓存读取失败: %s", exc)
        return None


def _cache_set(
    cache_key: str,
    result: BarAggregationResult,
    ttl: int | None = None,
) -> None:
    """写入 Redis 缓存。"""
    from app.config import get_settings

    settings = get_settings()
    if not settings.bars_redis_cache_enabled:
        return
    if ttl is None:
        ttl = random.randint(_MIN_CACHE_TTL, _MAX_CACHE_TTL)
    try:
        client = get_sync_redis()
        client.set(cache_key, _serialize_result(result), ex=ttl)
    except Exception as exc:
        logger.warning("MDAS 缓存写入失败: %s", exc)


# ===== 主服务 =====


class MarketDataAggregationService:
    """行情聚合统一入口。"""

    async def get_bars(
        self,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        timeframe: str = "1d",
        adj: str = "none",
        include_realtime: bool = True,
        completed_only: bool = False,
        start_date: date | datetime | None = None,
        end_date: date | datetime | None = None,
        limit: int | None = None,
        warmup_bars: int = 0,
        adjustment_as_of: date | None = None,
    ) -> BarAggregationResult:
        """获取行情聚合结果（v2 契约，CHANGE-20260717-002）。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            timeframe: 1d | 15m | 1h | 1w | 1mo | 1m
            adj: qfq | none
            include_realtime: 交易时段是否补充实时 1m 数据
            completed_only: 只返回已完成 bar（True 时强制 include_realtime=False）
            start_date: 起始日期/时间（可选）
            end_date: 结束日期/时间（可选）
            limit: 返回最近 N 根（服务端截取，保证 source_bar_hash 稳定）
            warmup_bars: 额外预热根数（>0 时返回 warmup_bars_full 含完整计算集）
            adjustment_as_of: 复权锚点（None=最新；date=point-in-time，禁止未来除权事件泄漏）

        Returns:
            BarAggregationResult（含 bars、warmup_bars_full、hash、contract_version 等诊断字段）
        """
        now = now_shanghai()
        as_of = now
        timeframe = timeframe.lower()
        if timeframe not in _ALLOWED_TIMEFRAMES:
            raise ValueError(
                f"timeframe 只支持 {sorted(_ALLOWED_TIMEFRAMES)}, got {timeframe!r}"
            )
        if adj not in _ALLOWED_ADJ:
            raise ValueError(f"adj 只支持 qfq/none, got {adj!r}")

        # [mdas] - completed_only 与 include_realtime 互斥：completed_only 强制不含实时
        if completed_only:
            include_realtime = False

        # [mdas] - 先查 Redis 短缓存（11 参数 + 契约版本）
        cache_key = _cache_key(
            instrument_id, timeframe, adj, include_realtime, completed_only,
            start_date, end_date, limit, warmup_bars, adjustment_as_of,
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            cached.cache_hit = True
            cached.freshness_seconds = (now - cached.as_of).total_seconds()
            return cached

        start, end = _resolve_date_range(timeframe, start_date, end_date, limit=limit)

        bars_df = pd.DataFrame()
        data_source = "db"
        is_partial = False
        last_persisted_bar_time: pd.Timestamp | None = None
        last_live_bar_time: pd.Timestamp | None = None
        degraded = False
        degraded_reason: str | None = None

        # [mdas] - 获取复权因子序列（在数据查询前，因子序列用于 qfq 和 adj_factor_hash）
        # include_realtime=True 时取全量因子（含今日，用于 partial daily qfq）
        # include_realtime=False 时按 as_of 过滤（历史可复现，无未来泄漏）
        _adj_service = AdjustmentFactorService()
        factor_df = pd.DataFrame()
        if adj == "qfq":
            fetch_as_of = None if include_realtime else adjustment_as_of
            try:
                factor_df = await _adj_service.get_factor_series(
                    session, instrument_id, as_of=fetch_as_of
                )
            except Exception as exc:
                degraded = True
                degraded_reason = f"adj_factor_unavailable: {exc}"
                data_source = "degraded"
        adj_factor_hash = _compute_adj_factor_hash(factor_df)

        # [CP-V3-A2] 迭代回补诊断字段默认值（日线/周线/月线路径不经过 backfill）。
        # intraday 路径会通过 _fetch_intraday_with_backfill 覆盖这些值；
        # daily 路径保持默认值（backfill_rounds=0, coverage_reason="daily_no_backfill"）。
        backfill_rounds = 0
        intraday_history_exhausted = False
        coverage_reason = "daily_no_backfill"

        # ============================================================
        # 日线 / 周线 / 月线
        # ============================================================
        if timeframe in ("1d", "1w", "1mo"):
            daily_df = await _query_daily_bars(session, instrument_id, start, end)  # type: ignore[arg-type]
            if not daily_df.empty:
                last_persisted_bar_time = pd.Timestamp(daily_df.index[-1])

            expected = await _call_expected_last_completed_daily_bar(session, now)
            need_tail = daily_df.empty or daily_df.index[-1].date() < expected

            if need_tail:
                try:
                    tail_df = await fetch_daily_bars(
                        session, instrument_id, start, end  # type: ignore[arg-type]
                    )
                    if not tail_df.empty:
                        daily_df = _merge_bars(daily_df, tail_df)
                        data_source = "hybrid"
                        last_live_bar_time = pd.Timestamp(tail_df.index[-1])
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"pytdx daily fallback failed: {exc}"
                    data_source = "degraded"

            # [mdas] - qfq 应用在合成前（"日线完成复权后再聚合"）
            if adj == "qfq" and not daily_df.empty and not factor_df.empty:
                try:
                    daily_df = _adj_service.apply_qfq(
                        daily_df, factor_df, as_of=adjustment_as_of, intraday=False
                    )
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"qfq failed: {exc}"
                    data_source = "degraded"

            # [mdas] - 周线/月线从已复权日线合成（委托 kline_aggregator）
            if timeframe == "1w":
                bars_df = aggregate_kline(daily_df, "1w") if not daily_df.empty else daily_df
            elif timeframe == "1mo":
                bars_df = aggregate_kline(daily_df, "1mo") if not daily_df.empty else daily_df
            else:
                bars_df = daily_df

        # ============================================================
        # 日内周期（含 1m 原始分钟线）
        # ============================================================
        else:
            # [CP-V3-A2] 日内迭代回补：受控循环查询直到满足 required_count 或确认
            # history_exhausted。修正 CP-V3-A "只按 limit 估算一次日期"的缺陷：
            # 节假日/停牌/数据缺口下 CP-V3-A 会误判 history_exhausted=True。
            # mypy: _query_15min_bars/_query_minute_bars 有可选 limit 参数，
            # _query_60min_bars 没有，三者签名不完全一致，统一用 Any
            _intraday_query_fn: Any
            if timeframe == "15m":
                _intraday_query_fn = _query_15min_bars
            elif timeframe == "1h":
                _intraday_query_fn = _query_60min_bars
            else:  # 1m
                _intraday_query_fn = _query_minute_bars

            (
                bars_df,
                backfill_rounds,
                intraday_history_exhausted,
                coverage_reason,
            ) = await _fetch_intraday_with_backfill(
                session,
                instrument_id,
                timeframe,
                start,  # type: ignore[arg-type]
                end,  # type: ignore[arg-type]
                _intraday_query_fn,
                required_count=limit,
            )

            if not bars_df.empty:
                last_persisted_bar_time = pd.Timestamp(bars_df.index[-1])

            if include_realtime and _is_trading_hours(now):
                # [mdas-timezone] - live_start/live_end 必须同为 Asia/Shanghai aware datetime
                now_cst = now if now.tzinfo else now.replace(tzinfo=SHANGHAI_TZ)
                live_start = now_cst.replace(hour=9, minute=30, second=0, microsecond=0)
                live_end = now_cst
                try:
                    live_1m = await fetch_minute_bars(
                        session, instrument_id, live_start, live_end
                    )
                    if not live_1m.empty:
                        if timeframe == "1m":
                            live_agg = live_1m
                        else:
                            live_agg = _aggregate_minute_to_target(live_1m, timeframe)
                        if not live_agg.empty:
                            bars_df = _merge_bars(bars_df, live_agg)
                            if data_source == "db":
                                data_source = "hybrid"
                            is_partial = True
                            last_live_bar_time = pd.Timestamp(live_agg.index[-1])
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"pytdx realtime fallback failed: {exc}"
                    data_source = "degraded"

            # [mdas] - qfq 应用（日内按交易日映射同一权威日线因子）
            if adj == "qfq" and not bars_df.empty and not factor_df.empty:
                try:
                    bars_df = _adj_service.apply_qfq(
                        bars_df, factor_df, as_of=adjustment_as_of, intraday=True
                    )
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"qfq failed: {exc}"
                    data_source = "degraded"

        # [mdas] - 排序、去重、过滤未完成 bar
        bars_df = _finalize_bars(bars_df, timeframe, now)

        # [mdas] - 若 Pytdx 数据被过滤掉，同步 last_live_bar_time
        if last_live_bar_time is not None and not bars_df.empty:
            if last_live_bar_time not in bars_df.index:
                last_live_bar_time = None
        elif bars_df.empty:
            last_live_bar_time = None

        # [mdas] - 1d 交易时段合成今日 partial daily bar（不写库，仅响应）
        # 放在 _finalize_bars 之后，避免被过滤未完成日线逻辑误删
        if timeframe == "1d" and include_realtime:
            try:
                is_trading_day = await is_trading_day_async(session, now.date())
                session_name = compute_market_session(now, is_trading_day)
                if session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON):
                    # [mdas-timezone] - live_start/live_end 必须同为 Asia/Shanghai aware datetime
                    now_cst = now if now.tzinfo else now.replace(tzinfo=SHANGHAI_TZ)
                    live_start = now_cst.replace(hour=9, minute=30, second=0, microsecond=0)
                    live_end = now_cst
                    live_1m = await fetch_minute_bars(
                        session, instrument_id, live_start, live_end
                    )
                    if not live_1m.empty:
                        # 只使用已完成 1m bar：剔除最后一根可能未完成的 bar
                        if len(live_1m) > 1:
                            live_1m = live_1m.iloc[:-1]
                        # [mdas] - partial daily 先合成 raw（factor=1.0），再统一走 apply_qfq
                        # 保证 partial bar 与复权后的历史日线连续（"复权一次"原则）
                        partial_daily = _aggregate_minute_to_daily(live_1m, 1.0)
                        if not partial_daily.empty:
                            if adj == "qfq" and not factor_df.empty:
                                try:
                                    partial_daily = _adj_service.apply_qfq(
                                        partial_daily, factor_df,
                                        as_of=adjustment_as_of, intraday=False,
                                    )
                                except Exception as exc:
                                    logger.warning(
                                        "partial daily qfq 失败 instrument_id=%s: %s",
                                        instrument_id, exc,
                                    )
                            bars_df = _merge_bars(bars_df, partial_daily)
                            if data_source == "db":
                                data_source = "hybrid"
                            is_partial = True
                            # last_live_bar_time 保留完整 datetime，便于前端展示精确到分钟
                            last_live_bar_time = pd.Timestamp(live_1m.index[-1])
            except Exception as exc:
                logger.warning(
                    "1d partial daily 合成失败 instrument_id=%s: %s",
                    instrument_id, exc,
                )
                degraded = True
                degraded_reason = f"pytdx partial daily failed: {exc}"
                data_source = "degraded"

        # [mdas] - completed_through = 最新已完成 DB bar 时间（不含 partial/realtime）
        completed_through = last_persisted_bar_time

        # [CP-V3-A] 记录 limit 截取前的原始数量（用于 history_exhausted 判定）
        pre_limit_count = len(bars_df) if not bars_df.empty else 0

        # [mdas] - limit / warmup 截取（在 hash 计算前，保证相同 limit 下 hash 稳定）
        warmup_bars_full: pd.DataFrame | None = None
        if warmup_bars > 0 and not bars_df.empty:
            full_count = (limit or 0) + warmup_bars
            warmup_bars_full = (
                bars_df.tail(full_count) if full_count <= len(bars_df) else bars_df
            )
        if limit is not None and not bars_df.empty:
            bars_df = bars_df.tail(limit)

        # [mdas] - source_bar_hash 在 limit 截取后计算（跨调用方一致性校验）
        source_bar_hash = (
            compute_source_bar_hash(bars_df, timeframe) if not bars_df.empty else ""
        )

        # [CP-V3-A] count-aware 诊断字段
        actual_count = len(bars_df) if not bars_df.empty else 0
        coverage_start: pd.Timestamp | None = (
            pd.Timestamp(bars_df.index[0]) if not bars_df.empty else None
        )
        coverage_end: pd.Timestamp | None = (
            pd.Timestamp(bars_df.index[-1]) if not bars_df.empty else None
        )
        # [CP-V3-A2] history_exhausted 语义修正：
        # - intraday 路径：使用 _fetch_intraday_with_backfill 的判定（基于真实 earliest
        #   bar / empty query / no_progress，而非"单次查询 < limit"）。旧逻辑
        #   (CP-V3-A) 会在节假日/停牌/查询窗口不足时误判 True，把
        #   INPUT_CONTRACT_VIOLATION（系统回看窗口不足）当成真实历史不足（degraded）。
        # - daily 路径：保持 "pre_limit_count < limit" 判定（日线不经过迭代回补，
        #   DB 历史不足即真实历史不足）。
        if timeframe in ("1d", "1w", "1mo"):
            history_exhausted = (
                limit is not None and pre_limit_count < limit
            )
        else:
            history_exhausted = intraday_history_exhausted

        result = BarAggregationResult(
            bars=bars_df,
            data_source=data_source,
            as_of=as_of,
            is_partial=is_partial,
            last_persisted_bar_time=last_persisted_bar_time,
            last_live_bar_time=last_live_bar_time,
            freshness_seconds=0.0,
            degraded=degraded,
            degraded_reason=degraded_reason,
            warmup_bars_full=warmup_bars_full,
            source_bar_hash=source_bar_hash,
            adj_factor_hash=adj_factor_hash,
            adjustment_as_of=adjustment_as_of,
            completed_through=completed_through,
            # [CP-V3-A] count-aware 字段
            requested_count=limit,
            actual_count=actual_count,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            history_exhausted=history_exhausted,
            # [CP-V3-A2] 迭代回补诊断字段
            backfill_rounds=backfill_rounds,
            coverage_reason=coverage_reason,
        )

        _cache_set(cache_key, result)
        return result


if __name__ == "__main__":
    # 自测入口：验证数据结构、缓存序列化、合并逻辑（不连 DB/网络）
    import inspect

    logging.basicConfig(level=logging.INFO)

    # 1. 验证 BarAggregationResult 字段
    sample_df = pd.DataFrame({
        "open": [10.0],
        "high": [10.5],
        "low": [9.8],
        "close": [10.2],
        "volume": [100000.0],
        "amount": [1000000.0],
        "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-18"]))
    sample_df.index.name = "trade_date"

    result = BarAggregationResult(
        bars=sample_df,
        data_source="db",
        as_of=now_shanghai(),
        is_partial=False,
        last_persisted_bar_time=pd.Timestamp("2026-06-18"),
        last_live_bar_time=None,
        freshness_seconds=0.0,
        degraded=False,
        degraded_reason=None,
    )
    assert result.data_source == "db"
    assert not result.degraded
    print("BarAggregationResult 构造 ✓")

    # 2. 验证缓存序列化/反序列化
    serialized = _serialize_result(result)
    restored = _deserialize_result(serialized)
    assert restored is not None
    assert restored.data_source == result.data_source
    assert len(restored.bars) == len(result.bars)
    assert restored.bars.index[0] == result.bars.index[0]
    print("缓存序列化/反序列化 ✓")

    # 3. 验证 _merge_bars
    df1 = pd.DataFrame({
        "open": [10.0], "high": [10.5], "low": [9.8],
        "close": [10.2], "volume": [100.0], "amount": [1000.0], "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-17"]))
    df2 = pd.DataFrame({
        "open": [11.0], "high": [11.5], "low": [10.8],
        "close": [11.2], "volume": [200.0], "amount": [2000.0], "adj_factor": [1.0],
    }, index=pd.to_datetime(["2026-06-18"]))
    merged = _merge_bars(df1, df2)
    assert len(merged) == 2
    assert merged.index[-1] == pd.Timestamp("2026-06-18")
    print("_merge_bars ✓")

    # 4. 验证 1m -> 15m 聚合
    minute_df = pd.DataFrame({
        "open": [10.0, 10.02, 10.03, 10.04, 10.05],
        "high": [10.02, 10.03, 10.04, 10.05, 10.06],
        "low": [9.99, 10.01, 10.02, 10.03, 10.04],
        "close": [10.02, 10.03, 10.04, 10.05, 10.06],
        "volume": [100.0, 100.0, 100.0, 100.0, 100.0],
        "amount": [1000.0, 1000.0, 1000.0, 1000.0, 1000.0],
        "adj_factor": [1.0] * 5,
    }, index=pd.date_range("2026-06-18 09:45:00", periods=5, freq="1min"))
    minute_df.index.name = "trade_time"
    agg15 = _aggregate_minute_to_target(minute_df, "15m")
    assert len(agg15) == 1
    assert agg15.index[0] == pd.Timestamp("2026-06-18 09:45:00")
    assert agg15.iloc[0]["close"] == 10.06
    print("1m -> 15m 聚合 ✓")

    # 5. 验证 get_bars 签名（v2 契约：11 参数 + self）
    sig = inspect.signature(MarketDataAggregationService.get_bars)
    params = list(sig.parameters.keys())
    expected_params = [
        "self", "session", "instrument_id", "timeframe", "adj",
        "include_realtime", "completed_only", "start_date", "end_date",
        "limit", "warmup_bars", "adjustment_as_of",
    ]
    assert params == expected_params, f"get_bars 参数不匹配: {params}"
    print(f"get_bars params={params} ✓")

    # 6. 验证 v2 契约新字段默认值
    assert result.market_data_contract_version == "v2", \
        f"contract_version 应为 v2, got {result.market_data_contract_version}"
    assert result.source_bar_hash == "", \
        f"source_bar_hash 默认应为空串, got {result.source_bar_hash!r}"
    assert result.adj_factor_hash == "", \
        f"adj_factor_hash 默认应为空串, got {result.adj_factor_hash!r}"
    assert result.adjustment_as_of is None, \
        f"adjustment_as_of 默认应为 None, got {result.adjustment_as_of!r}"
    assert result.completed_through is None, \
        f"completed_through 默认应为 None, got {result.completed_through!r}"
    assert result.warmup_bars_full is None, \
        f"warmup_bars_full 默认应为 None, got {result.warmup_bars_full!r}"
    print("v2 契约新字段默认值 ✓")

    # 7. 验证 _compute_adj_factor_hash
    factor_df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-06-16", "2026-06-17"]),
        "adj_factor": [0.5, 1.0],
    })
    h = _compute_adj_factor_hash(factor_df)
    assert len(h) == 16, f"adj_factor_hash 应为 16 字符, got {len(h)}"
    assert _compute_adj_factor_hash(pd.DataFrame()) == "", "空因子 hash 应为空串"
    print(f"_compute_adj_factor_hash ✓ (hash={h})")

    # 8. 验证 _cache_key 含契约版本（11 参数）
    ck = _cache_key(
        uuid.UUID("00000000-0000-0000-0000-000000000001"), "1d", "qfq", True, False,
        None, None, 4000, 1000, None,
    )
    assert ":v2" in ck, f"缓存键应含契约版本后缀, got {ck}"
    print("_cache_key 含 v2 契约版本 ✓")

    print("OK")
