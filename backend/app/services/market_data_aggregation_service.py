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
import json
import logging
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
from app.core.time import now_shanghai
from app.repositories.bar_repository import (
    _get_adj_factor_df,
    _get_symbol,
    _query_15min_bars,
    _query_60min_bars,
    _query_daily_bars,
    _query_minute_bars,
    apply_adj_factor_to_bars,
    convert_kline_frequency,
)
from app.services.calendar_service import is_trading_day_async

logger = logging.getLogger("services.market_data_aggregation_service")

# [mdas] - 描述: 支持的周期与复权方式
_ALLOWED_TIMEFRAMES: set[str] = {"1d", "15m", "1h", "1w", "1mo", "1m"}
_ALLOWED_ADJ: set[str] = {"qfq", "none"}

# [mdas] - 描述: 默认回看范围（与 bars.py / indicator_service.py 保持一致）
_DEFAULT_DAILY_LOOKBACK_DAYS: int = 5000
_DEFAULT_INTRADAY_LOOKBACK_DAYS: int = 180

# [mdas] - 描述: A 股收盘时间，日线 bar 完成边界
_DAILY_CLOSE_TIME: dt_time = dt_time(15, 0)

# [mdas] - 描述: Redis 短缓存 TTL 范围（秒）
_MIN_CACHE_TTL: int = 5
_MAX_CACHE_TTL: int = 15
_REDIS_CACHE_PREFIX: str = "mdas"

# [mdas] - 描述: 1m → 15m/1h 聚合频率映射
_TARGET_FREQ: dict[str, str] = {"15m": "15min", "1h": "60min"}

# [mdas] - 描述: 标准行情列
_BAR_COLUMNS: list[str] = ["open", "high", "low", "close", "volume", "amount", "adj_factor"]


@dataclass
class BarAggregationResult:
    """行情聚合结果，包含 bars DataFrame 与数据源诊断字段。"""

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


# ===== 交易时间判断 =====


def _is_trading_hours(now: datetime | None = None) -> bool:
    """判断当前是否在 A 股交易时段（周一至周五 9:30-15:00，上海时间）。"""
    if now is None:
        now = now_shanghai()
    if now.weekday() >= 5:
        return False
    return dt_time(9, 30) <= now.time() <= _DAILY_CLOSE_TIME


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
) -> tuple[date, date] | tuple[datetime, datetime]:
    """解析查询范围。"""
    today = date.today()
    if timeframe in ("1d", "1w", "1mo"):
        end = end_date if isinstance(end_date, date) else (end_date.date() if isinstance(end_date, datetime) else today)
        start = (
            start_date
            if isinstance(start_date, date)
            else (start_date.date() if isinstance(start_date, datetime) else end - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS))
        )
        return start, end

    # 15m / 1h
    if isinstance(end_date, datetime):
        end = end_date
    else:
        end = datetime.combine(end_date or today, datetime.max.time())
    if isinstance(start_date, datetime):
        start = start_date
    else:
        start = datetime.combine(
            start_date or (end.date() - timedelta(days=_DEFAULT_INTRADAY_LOOKBACK_DAYS)),
            datetime.min.time(),
        )
    return start, end


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


# ===== Redis 短缓存 =====


def _cache_key(
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    include_realtime: bool,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
) -> str:
    """构建缓存键，包含所有影响结果的参数。"""
    start = start_date.isoformat() if start_date is not None else "_"
    end = end_date.isoformat() if end_date is not None else "_"
    return (
        f"{_REDIS_CACHE_PREFIX}:"
        f"{instrument_id}:{timeframe}:{adj}:{include_realtime}:{start}:{end}"
    )


def _serialize_result(result: BarAggregationResult) -> str:
    """将结果序列化为 JSON 字符串。"""
    bars = result.bars
    if bars.empty:
        bars_payload: dict[str, Any] = {
            "index": [],
            "columns": list(bars.columns),
            "data": [],
        }
    else:
        bars_payload = bars.to_dict(orient="split")
        bars_payload["index"] = [idx.isoformat() for idx in bars.index]

    payload = {
        "bars": bars_payload,
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
    }
    return json.dumps(payload)


def _deserialize_result(raw: str) -> BarAggregationResult | None:
    """从 JSON 字符串反序列化结果。"""
    try:
        payload = json.loads(raw)
        bars_payload = payload["bars"]
        index = pd.to_datetime(bars_payload["index"])
        bars = pd.DataFrame(
            bars_payload["data"],
            index=index,
            columns=bars_payload["columns"],
        )
        for col in bars.columns:
            if col in _BAR_COLUMNS:
                bars[col] = pd.to_numeric(bars[col], errors="coerce")

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
        start_date: date | datetime | None = None,
        end_date: date | datetime | None = None,
    ) -> BarAggregationResult:
        """获取行情聚合结果。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            timeframe: 1d | 15m | 1h | 1w | 1mo | 1m
            adj: qfq | none
            include_realtime: 交易时段是否补充实时 1m 数据
            start_date: 起始日期/时间（可选）
            end_date: 结束日期/时间（可选）

        Returns:
            BarAggregationResult
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

        # [mdas] - 描述: 先查 Redis 短缓存
        cache_key = _cache_key(
            instrument_id, timeframe, adj, include_realtime, start_date, end_date
        )
        cached = _cache_get(cache_key)
        if cached is not None:
            cached.cache_hit = True
            cached.freshness_seconds = (now - cached.as_of).total_seconds()
            return cached

        start, end = _resolve_date_range(timeframe, start_date, end_date)

        bars_df = pd.DataFrame()
        data_source = "db"
        is_partial = False
        last_persisted_bar_time: pd.Timestamp | None = None
        last_live_bar_time: pd.Timestamp | None = None
        degraded = False
        degraded_reason: str | None = None

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

            # [mdas] - 描述: 周线/月线从日线合成
            if timeframe == "1w":
                bars_df = convert_kline_frequency(daily_df, "w") if not daily_df.empty else daily_df
            elif timeframe == "1mo":
                bars_df = convert_kline_frequency(daily_df, "m") if not daily_df.empty else daily_df
            else:
                bars_df = daily_df

            # [mdas] - 描述: 日线复权在合成前应用（周线/月线更准确）
            if adj == "qfq" and not bars_df.empty:
                try:
                    adj_factor_df = await _get_adj_factor_df(session, instrument_id)
                    if not adj_factor_df.empty:
                        daily_df = apply_adj_factor_to_bars(
                            daily_df, adj_factor_df, intraday=False
                        )
                        if timeframe == "1w":
                            bars_df = convert_kline_frequency(daily_df, "w")
                        elif timeframe == "1mo":
                            bars_df = convert_kline_frequency(daily_df, "m")
                        else:
                            bars_df = daily_df
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"qfq failed: {exc}"
                    data_source = "degraded"

        # ============================================================
        # 日内周期（含 1m 原始分钟线）
        # ============================================================
        else:
            if timeframe == "15m":
                bars_df = await _query_15min_bars(session, instrument_id, start, end)  # type: ignore[arg-type]
            elif timeframe == "1h":
                bars_df = await _query_60min_bars(session, instrument_id, start, end)  # type: ignore[arg-type]
            else:  # 1m
                bars_df = await _query_minute_bars(session, instrument_id, start, end)  # type: ignore[arg-type]

            if not bars_df.empty:
                last_persisted_bar_time = pd.Timestamp(bars_df.index[-1])

            if include_realtime and _is_trading_hours(now):
                live_start = datetime.combine(now.date(), dt_time(9, 30))
                live_end = now
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

            if adj == "qfq" and not bars_df.empty:
                try:
                    adj_factor_df = await _get_adj_factor_df(session, instrument_id)
                    if not adj_factor_df.empty:
                        bars_df = apply_adj_factor_to_bars(
                            bars_df, adj_factor_df, intraday=True
                        )
                except Exception as exc:
                    degraded = True
                    degraded_reason = f"qfq failed: {exc}"
                    data_source = "degraded"

        # [mdas] - 描述: 排序、去重、过滤未完成 bar
        bars_df = _finalize_bars(bars_df, timeframe, now)

        # [mdas] - 描述: 若 Pytdx 数据被过滤掉，同步 last_live_bar_time
        if last_live_bar_time is not None and not bars_df.empty:
            if last_live_bar_time not in bars_df.index:
                last_live_bar_time = None
        elif bars_df.empty:
            last_live_bar_time = None

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

    # 5. 验证 get_bars 签名
    sig = inspect.signature(MarketDataAggregationService.get_bars)
    params = list(sig.parameters.keys())
    assert params == [
        "self", "session", "instrument_id", "timeframe", "adj",
        "include_realtime", "start_date", "end_date",
    ], f"get_bars 参数不匹配: {params}"
    print(f"get_bars params={params} ✓")

    print("OK")
