"""行情查询 API。

GET /api/v1/instruments/{instrument_id}/bars
    查询行情数据，支持多周期（15m/1h/1d/1w/1mo）、前复权/不复权、服务端分页。
    数据获取：DB 优先（PostgreSQL），仅 include_realtime=True 且交易时段内
    调用 Pytdx 补充最后一根 Bar（hybrid 模式）；DB 未命中时 Pytdx 兜底。

GET /api/v1/instruments/{instrument_id}/quote
    获取标的实时报价（pytdx 1 分钟线优先，DB 日线回退）。

GET /api/v1/bars/health
    行情系统健康检查，返回 DB/Redis 连通性与各周期数据新鲜度。

参数：
    timeframe: 1d | 15m | 1h | 1w | 1mo（默认 1d）
    adj: qfq | none（默认 none）
    start_date: 起始日期（YYYY-MM-DD），可选
    end_date: 结束日期（YYYY-MM-DD），可选
    page: 页码（1-based，默认 1）
    page_size: 每页大小（默认 100；15m 最大 4000，1h 最大 1200，其他最大 1000）
    include_realtime: 是否在交易时段内调用 Pytdx 补充最后一根 Bar（默认 true）

响应头：
    X-Data-Source: db | pytdx | hybrid
    X-Cache-Hit: true | false
    X-Total-Ms: <int>（总耗时毫秒）
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import INDICATOR_BARS
from app.core.deps import get_db, require_roles
from app.core.pytdx_adapter import get_pytdx_adapter
from app.core.redis_client import get_redis
from app.core.time import now_shanghai
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute, BarMonthly, BarWeekly
from app.schemas.bar import BarListResponse, BarResponse, QuoteResponse
from app.services.calendar_service import is_trading_day_async
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.market_status_service import (
    MARKET_SESSION_AFTERNOON,
    MARKET_SESSION_MORNING,
    compute_market_session,
)

logger = logging.getLogger("api.bars")

router = APIRouter(prefix="/api/v1", tags=["bars"])

# 支持的周期
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}

# 默认查询范围
_DEFAULT_DAILY_LOOKBACK_DAYS = 5000  # 日线/周线/月线默认回看 5000 天（覆盖约 13 年，确保周线 ≥250 条）
_DEFAULT_INTRADAY_LOOKBACK_DAYS = 180  # 15min/60min 默认回看 180 天（DB 实测 180 天 60min=460 根 > 320 根需求）
# [Chart] - page_size 上限与 Node Cluster 契约对齐：15m 需要 4000 根，1h 需要 1200 根
# 引用 indicator_contract 唯一真源，禁止散落硬编码 4000/1200
_DEFAULT_PAGE_SIZE_LIMIT = 1000
_PAGE_SIZE_LIMITS = {
    "15m": INDICATOR_BARS["15m"],
    "1h": INDICATOR_BARS["1h"],
}


# ===== /quote 实时行情可信化 helpers =====

# pytdx 模块级单例连接锁，防止多线程同时操作同一个同步 socket
_quote_adapter_lock = threading.Lock()
_quote_redis_cache_ttl_seconds = 10
_quote_redis_cache_prefix = "quote"


async def _is_quote_realtime_session(session: AsyncSession, now: datetime | None = None) -> bool:
    """判断当前是否应尝试 pytdx 实时行情（仅上午盘/下午盘）。

    使用 market_status_service.compute_market_session 统一午休口径，
    不再自行写 9:30-15:00 连续判断。
    """
    if now is None:
        now = now_shanghai()
    is_trading_day = await is_trading_day_async(session, now.date())
    session_name = compute_market_session(now, is_trading_day)
    return session_name in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON)


def _quote_cache_key(instrument_id: uuid.UUID) -> str:
    return f"{_quote_redis_cache_prefix}:{instrument_id}"


async def _quote_cache_get(instrument_id: uuid.UUID) -> dict[str, Any] | None:
    """从 Redis 读取 quote 短缓存（失败时静默降级，不阻塞请求）。"""
    try:
        redis_client = get_redis()
        raw = await redis_client.get(_quote_cache_key(instrument_id))
        if not raw:
            return None
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except Exception as exc:
        logger.debug("quote cache get 失败 instrument_id=%s: %s", instrument_id, exc)
        return None


async def _quote_cache_set(instrument_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """写入 Redis quote 短缓存（失败时静默降级）。"""
    try:
        redis_client = get_redis()
        await redis_client.set(
            _quote_cache_key(instrument_id),
            json.dumps(payload, default=str),
            ex=_quote_redis_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.debug("quote cache set 失败 instrument_id=%s: %s", instrument_id, exc)


async def _fetch_pytdx_quote(symbol: str) -> dict[str, Any] | None:
    """在线程锁保护下调用 pytdx 实时行情，10s 超时。

    失败时返回 None；由调用方决定降级策略，不在本函数伪装实时数据。
    """
    adapter = get_pytdx_adapter()

    def _call() -> dict[str, Any] | None:
        with _quote_adapter_lock:
            return adapter.get_realtime_quote(symbol)

    try:
        return await asyncio.wait_for(asyncio.to_thread(_call), timeout=10.0)
    except Exception as exc:
        logger.warning("pytdx 实时行情失败 symbol=%s: %s", symbol, exc)
        return None


def _quote_freshness_seconds(update_time_str: str) -> float:
    """根据 update_time 计算行情新鲜度（秒）。"""
    try:
        dt = datetime.fromisoformat(update_time_str)
    except (ValueError, TypeError):
        return 0.0

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    delta = now_shanghai() - dt
    return max(0.0, delta.total_seconds())


def _daily_fallback_as_of(trade_date: date) -> datetime:
    """日线 fallback 的默认时间戳为交易日收盘 15:00（上海时间）。"""
    return datetime.combine(trade_date, dt_time(15, 0), tzinfo=ZoneInfo("Asia/Shanghai"))


async def _build_daily_fallback_quote(
    session: AsyncSession,
    instrument,
) -> tuple[dict[str, Any], str | None] | None:
    """从 DB 最新 2 根日线构造 fallback quote。

    Returns:
        (quote_dict, degraded_reason) 或 None（无数据）
    """
    stmt = (
        select(BarDaily)
        .where(BarDaily.instrument_id == instrument.id)
        .order_by(BarDaily.trade_date.desc())
        .limit(2)
    )
    result = await session.execute(stmt)
    daily_bars = list(result.scalars().all())

    if not daily_bars:
        return None

    latest = daily_bars[0]
    current_price = float(latest.close or 0)
    prev_close = float(daily_bars[1].close or 0) if len(daily_bars) >= 2 and daily_bars[1].close else current_price
    if prev_close == 0:
        change_pct = 0.0
    else:
        change_pct = (current_price - prev_close) / prev_close * 100

    as_of = _daily_fallback_as_of(latest.trade_date)
    update_time = as_of.isoformat()

    return {
        "instrument_id": instrument.id,
        "symbol": instrument.symbol,
        "name": instrument.name,
        "current_price": round(current_price, 4),
        "open": round(float(latest.open or 0), 4),
        "high": round(float(latest.high or 0), 4),
        "low": round(float(latest.low or 0), 4),
        "close": round(current_price, 4),
        "volume": round(float(latest.volume or 0), 2),
        "prev_close": round(prev_close, 4),
        "change_pct": round(change_pct, 2),
        "update_time": update_time,
        "is_realtime": False,
        "source": "daily_fallback",
        "freshness_seconds": _quote_freshness_seconds(update_time),
        "degraded": False,
        "degraded_reason": None,
    }, None


def _build_pytdx_quote_response(
    instrument,
    pytdx_quote: dict[str, Any],
) -> dict[str, Any]:
    """将 pytdx 原始 quote 包装为统一响应字典。"""
    update_time = pytdx_quote.get("update_time")
    # [QuoteTrust] - 防御性校验：若上游仍返回 naive 时间，按 Asia/Shanghai 解释
    if isinstance(update_time, datetime) and update_time.tzinfo is None:
        update_time = update_time.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    elif isinstance(update_time, str) and update_time.endswith("+00:00"):
        # 防止被误标为 UTC：替换为 +08:00（pytdx 数据本质为上海时间）
        update_time = update_time[:-6] + "+08:00"
    return {
        "instrument_id": instrument.id,
        "symbol": instrument.symbol,
        "name": instrument.name,
        "current_price": pytdx_quote.get("current_price"),
        "open": pytdx_quote.get("open"),
        "high": pytdx_quote.get("high"),
        "low": pytdx_quote.get("low"),
        "close": pytdx_quote.get("close"),
        "volume": pytdx_quote.get("volume"),
        "prev_close": pytdx_quote.get("prev_close"),
        "change_pct": pytdx_quote.get("change_pct"),
        "update_time": update_time,
        "is_realtime": True,
        "source": "pytdx",
        "freshness_seconds": _quote_freshness_seconds(update_time) if update_time else 0.0,
        "degraded": False,
        "degraded_reason": None,
    }


# ===== /bars helpers =====


def _parse_date_range(
    timeframe: str,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, date] | tuple[datetime, datetime]:
    """解析日期范围，未提供时使用默认值。

    Args:
        timeframe: 1d | 15m | 1h | 1w | 1mo
        start_date: 起始日期（可选）
        end_date: 结束日期（可选）

    Returns:
        日线/周线/月线返回 (date, date)；15min/60min 返回 (datetime, datetime)
    """
    if timeframe in ("1d", "1w", "1mo"):
        end = end_date or date.today()
        start = start_date or (end - timedelta(days=_DEFAULT_DAILY_LOOKBACK_DAYS))
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_date 不能晚于 end_date",
            )
        return start, end
    # 15m / 1h（日内周期）
    # 默认回看 180 天，DB 实测 180 天 60min=460 根 > 320 根需求（策略计算窗口）
    end_dt = datetime.combine(end_date or date.today(), datetime.max.time())
    start_dt = datetime.combine(
        start_date or (end_dt.date() - timedelta(days=_DEFAULT_INTRADAY_LOOKBACK_DAYS)),
        datetime.min.time(),
    )
    if start_dt > end_dt:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="start_date 不能晚于 end_date",
        )
    return start_dt, end_dt


def _df_to_responses(
    df: pd.DataFrame,
    instrument_id: uuid.UUID,
    timeframe: str,
) -> list[BarResponse]:
    """将 DataFrame 转为 BarResponse 列表（向量化）。

    使用列级 fillna().astype(float).tolist() 替代 iterrows()，
    在 800 条数据时性能提升约 3-5 倍。

    Args:
        df: 行情数据，index 为 DatetimeIndex
        instrument_id: 标的 UUID
        timeframe: 1d | 15m | 1h | 1w | 1mo

    Returns:
        BarResponse 列表
    """
    if df.empty:
        return []

    # 日线/周线/月线使用 trade_date，15min/60min 使用 trade_time
    is_daily = timeframe in ("1d", "1w", "1mo")
    n = len(df)

    # 向量化提取时间戳
    timestamps = pd.to_datetime(df.index)

    # 向量化提取列值（fillna + astype 一次性完成，替代逐行 float() 转换）
    opens = df["open"].fillna(0.0).astype(float).tolist()
    highs = df["high"].fillna(0.0).astype(float).tolist()
    lows = df["low"].fillna(0.0).astype(float).tolist()
    closes = df["close"].fillna(0.0).astype(float).tolist()
    volumes = df["volume"].fillna(0.0).astype(float).tolist()
    amounts = df["amount"].fillna(0.0).astype(float).tolist()
    adj_factors = df["adj_factor"].fillna(1.0).astype(float).tolist()

    items: list[BarResponse] = []
    for i in range(n):
        ts = timestamps[i]
        if is_daily:
            trade_date = ts.date()
            trade_time = None
        else:
            trade_date = None
            trade_time = ts.to_pydatetime()

        items.append(BarResponse(
            instrument_id=instrument_id,
            trade_date=trade_date,
            trade_time=trade_time,
            open=opens[i],
            high=highs[i],
            low=lows[i],
            close=closes[i],
            volume=volumes[i],
            amount=amounts[i],
            adj_factor=adj_factors[i],
        ))
    return items


# ===== 路由 =====


@router.get(
    "/instruments/{instrument_id}/bars",
    response_model=BarListResponse,
    summary="查询行情数据",
)
async def get_bars(
    instrument_id: uuid.UUID,
    timeframe: str = Query("1d", description="周期: 1d | 15m | 1h | 1w | 1mo"),
    adj: str = Query("none", description="复权方式: qfq | none"),
    start_date: date | None = Query(None, description="起始日期 YYYY-MM-DD"),
    end_date: date | None = Query(None, description="结束日期 YYYY-MM-DD"),
    page: int = Query(1, ge=1, description="页码（1-based）"),
    page_size: int = Query(
        100, ge=1, le=max(_PAGE_SIZE_LIMITS.values()), description="每页大小（15m 最大 4000，1h 最大 1200，其他最大 1000）"
    ),
    include_realtime: bool = Query(True, description="是否在交易时段内调用 Pytdx 补充最后一根 Bar"),
    session: AsyncSession = Depends(get_db),
    response: Response = None,
) -> BarListResponse:
    """查询指定标的的行情数据。

    - 支持多周期：1d（日线）/ 15m / 1h / 1w（周线）/ 1mo（月线）
    - 统一委托 MarketDataAggregationService（行情聚合唯一事实源）处理：
      DB 优先、Pytdx 补尾/兜底、实时 1m 聚合、复权、去重、未完成 Bar 过滤、Redis 短缓存
    - 响应体与响应头返回数据源诊断字段

    响应头：
        X-Data-Source: db | pytdx | hybrid | degraded
        X-Cache-Hit: true | false
        X-Total-Ms: <int>（总耗时毫秒）
    """
    # 参数校验
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"timeframe 只支持 {sorted(_ALLOWED_TIMEFRAMES)}",
        )
    if adj not in ("qfq", "none"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="adj 只支持 qfq 或 none",
        )
    max_page_size = _PAGE_SIZE_LIMITS.get(timeframe, _DEFAULT_PAGE_SIZE_LIMIT)
    if page_size > max_page_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"timeframe={timeframe} 的 page_size 最大为 {max_page_size}",
        )

    logger.info(
        "查询行情 instrument_id=%s timeframe=%s adj=%s start=%s end=%s page=%d size=%d realtime=%s",
        instrument_id, timeframe, adj, start_date, end_date, page, page_size, include_realtime,
    )

    start_ms = time.time()

    # [行情聚合 SSOT] - 统一调用 MarketDataAggregationService 获取行情与诊断字段
    service = MarketDataAggregationService()
    try:
        result = await service.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=include_realtime,
            start_date=start_date,
            end_date=end_date,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.warning("行情聚合服务失败 instrument_id=%s: %s", instrument_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"查询行情失败: {exc}",
        ) from exc

    df = result.bars
    total_ms = int((time.time() - start_ms) * 1000)

    # --- 空数据处理 ---
    if df.empty:
        if response is not None:
            response.headers["X-Data-Source"] = result.data_source
            response.headers["X-Cache-Hit"] = "true" if result.cache_hit else "false"
            response.headers["X-Total-Ms"] = str(total_ms)
        return BarListResponse(
            items=[],
            total=0,
            page=page,
            page_size=page_size,
            timeframe=timeframe,
            adj=adj,
            data_source=result.data_source,
            as_of=result.as_of,
            is_partial=result.is_partial,
            last_persisted_bar_time=(
                result.last_persisted_bar_time.to_pydatetime()
                if result.last_persisted_bar_time is not None
                else None
            ),
            last_live_bar_time=(
                result.last_live_bar_time.to_pydatetime()
                if result.last_live_bar_time is not None
                else None
            ),
            freshness_seconds=result.freshness_seconds,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
        )

    # 服务端分页：返回最新的数据（page=1 返回最新 page_size 条）
    # df 按时间正序排列（最旧在前），从末尾取最新数据
    total = len(df)
    end_idx = total - (page - 1) * page_size
    start_idx = max(0, end_idx - page_size)
    page_df = df.iloc[start_idx:end_idx]

    items = _df_to_responses(page_df, instrument_id, timeframe)

    # --- 响应头 ---
    if response is not None:
        response.headers["X-Data-Source"] = result.data_source
        response.headers["X-Cache-Hit"] = "true" if result.cache_hit else "false"
        response.headers["X-Total-Ms"] = str(total_ms)

    return BarListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        timeframe=timeframe,
        adj=adj,
        data_source=result.data_source,
        as_of=result.as_of,
        is_partial=result.is_partial,
        last_persisted_bar_time=(
            result.last_persisted_bar_time.to_pydatetime()
            if result.last_persisted_bar_time is not None
            else None
        ),
        last_live_bar_time=(
            result.last_live_bar_time.to_pydatetime()
            if result.last_live_bar_time is not None
            else None
        ),
        freshness_seconds=result.freshness_seconds,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
    )




# ===== 实时行情 =====

@router.get(
    "/instruments/{instrument_id}/quote",
    response_model=QuoteResponse,
    summary="获取标的实时报价（可信来源与新鲜度）",
)
async def get_instrument_quote(
    instrument_id: uuid.UUID,
    session: AsyncSession = Depends(get_db),
) -> QuoteResponse:
    """获取标的实时报价，明确返回数据来源、实时性、新鲜度与降级状态。

    行为：
    - 仅上午盘/下午盘尝试 pytdx；pytdx 成功 -> source="pytdx", is_realtime=true。
    - 非交易时段或 pytdx 失败 -> source="daily_fallback", is_realtime=false。
    - 交易时段 pytdx 失败会标记 degraded=true 并记录原因；非交易时段 fallback 不算降级。
    - 使用 Redis 短缓存（10s）削峰，pytdx 单例连接 + 线程锁防止每请求建连。
    """
    from app.models.instrument import Instrument

    stmt = select(Instrument).where(Instrument.id == instrument_id)
    result = await session.execute(stmt)
    instrument = result.scalar_one_or_none()
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="标的不存在",
        )

    now = now_shanghai()
    realtime_session = await _is_quote_realtime_session(session, now)

    # 1. 尝试实时行情（仅在交易时段）
    if realtime_session:
        cached = await _quote_cache_get(instrument_id)
        if cached and cached.get("source") == "pytdx":
            logger.debug("quote cache hit instrument_id=%s", instrument_id)
            return QuoteResponse(**cached)

        pytdx_quote = await _fetch_pytdx_quote(instrument.symbol)
        if pytdx_quote is not None:
            payload = _build_pytdx_quote_response(instrument, pytdx_quote)
            await _quote_cache_set(instrument_id, payload)
            logger.info("pytdx quote 成功 instrument_id=%s symbol=%s", instrument_id, instrument.symbol)
            return QuoteResponse(**payload)

    # 2. 回退到 DB 日线
    fallback = await _build_daily_fallback_quote(session, instrument)
    if fallback is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="无行情数据",
        )

    payload, _ = fallback
    if realtime_session:
        # 交易时段 pytdx 失败才标记降级
        payload["degraded"] = True
        payload["degraded_reason"] = "pytdx 实时行情失败，已降级到日线 fallback"
        logger.warning(
            "pytdx quote 失败，降级到日线 fallback instrument_id=%s symbol=%s",
            instrument_id,
            instrument.symbol,
        )
    else:
        logger.debug(
            "非交易时段，使用日线 fallback instrument_id=%s symbol=%s",
            instrument_id,
            instrument.symbol,
        )

    return QuoteResponse(**payload)

# ===== 健康检查 =====

@router.get(
    "/bars/health",
    response_model=dict,
    summary="行情系统健康检查",
)
async def bars_health(session: AsyncSession = Depends(get_db)) -> dict:
    """返回行情系统健康状态。

    检查项：
    - DB 连通性：查询 bars_daily 最新日期
    - Redis 连通性：ping
    - 各周期数据新鲜度：查询各表最新日期/时间

    Returns:
        dict: 健康状态，包含 status（ok/degraded/down）、db、redis、freshness
    """
    health_status: dict = {
        "status": "ok",
        "db": {"connected": False, "latest_daily_date": None},
        "redis": {"connected": False},
        "freshness": {},
        "timestamp": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
    }

    # 1. 检查 DB 连通性
    try:
        result = await session.execute(
            select(func.max(BarDaily.trade_date))
        )
        latest_daily = result.scalar()
        health_status["db"] = {
            "connected": True,
            "latest_daily_date": latest_daily.isoformat() if latest_daily else None,
        }
    except Exception as exc:
        logger.warning("健康检查 DB 连接失败: %s", exc)
        health_status["db"] = {"connected": False, "error": str(exc)}
        health_status["status"] = "down"

    # 2. 检查 Redis 连通性
    try:
        redis_client = get_redis()
        await redis_client.ping()
        health_status["redis"] = {"connected": True}
    except Exception as exc:
        logger.warning("健康检查 Redis 连接失败: %s", exc)
        health_status["redis"] = {"connected": False, "error": str(exc)}
        # Redis 不可用不视为 down，仅 degraded
        if health_status["status"] == "ok":
            health_status["status"] = "degraded"

    # 3. 检查各周期数据新鲜度（查询最新日期/时间）
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    freshness_checks = [
        ("daily", BarDaily.trade_date, True),
        ("weekly", BarWeekly.trade_date, True),
        ("monthly", BarMonthly.trade_date, True),
        ("15min", Bar15Min.trade_time, False),
        ("60min", Bar60Min.trade_time, False),
        ("minute", BarMinute.trade_time, False),
    ]

    for name, column, is_daily in freshness_checks:
        try:
            result = await session.execute(select(func.max(column)))
            latest = result.scalar()
            if latest is None:
                health_status["freshness"][name] = {
                    "latest": None,
                    "age_seconds": None,
                }
            else:
                # 计算年龄
                if is_daily:
                    # 日线类：latest 为 date，收盘时间 15:00
                    if isinstance(latest, date) and not isinstance(latest, datetime):
                        latest_dt = datetime.combine(latest, datetime.min.time()).replace(hour=15)
                    else:
                        latest_dt = latest
                else:
                    # 分钟类：latest 为 datetime
                    latest_dt = latest
                    assert isinstance(latest_dt, datetime), f"分钟类 latest 应为 datetime， got {type(latest)}"
                    if latest_dt.tzinfo is not None:
                        latest_dt = latest_dt.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)

                age = (now - latest_dt).total_seconds()
                health_status["freshness"][name] = {
                    "latest": latest.isoformat() if hasattr(latest, "isoformat") else str(latest),
                    "age_seconds": round(age, 0),
                }
        except Exception as exc:
            logger.warning("健康检查 %s 新鲜度查询失败: %s", name, exc)
            health_status["freshness"][name] = {"error": str(exc)}
            if health_status["status"] == "ok":
                health_status["status"] = "degraded"

    return health_status


# ===== Admin 管理接口 =====

@router.post(
    "/admin/bars/refresh",
    response_model=dict,
    summary="手动触发全市场多周期行情更新（admin）",
)
async def trigger_bars_refresh(
    trade_date: date | None = Query(None, description="交易日期，默认今天"),
    _user=Depends(require_roles("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动触发全市场多周期行情更新（admin 权限）。

    使用 DAILY_COUNTS（小 count），耗时约 1.8 小时。
    用于测试调度任务或手动补充当日数据。
    """
    from datetime import date as date_cls

    from app.services.bars_scheduler_service import BarsSchedulerService

    target_date = trade_date or date_cls.today()
    logger.info("手动触发行情更新 trade_date=%s user=%s", target_date, _user)

    service = BarsSchedulerService()
    result = await service.refresh_all_instruments(target_date, db_session=db)

    return {
        "status": "ok",
        "trade_date": target_date.isoformat(),
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "failed_symbols": result.failed_symbols[:20],  # 最多返回 20 个失败股票
        "period_counts": result.period_counts,
    }


@router.post(
    "/admin/bars/backfill",
    response_model=dict,
    summary="手动触发全市场历史数据回补（admin）",
)
async def trigger_bars_backfill(
    start_date: date = Query(date(2023, 1, 1), description="回补起始日期，默认 2023-01-01"),
    _user=Depends(require_roles("admin")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """手动触发全市场历史数据回补（admin 权限）。

    使用 BACKFILL_COUNTS（大 count），耗时约 11.1 小时。
    用于首次部署或需要补充历史数据时。
    """
    from app.services.bars_scheduler_service import BarsSchedulerService

    logger.info("手动触发历史回补 start_date=%s user=%s", start_date, _user)

    service = BarsSchedulerService()
    result = await service.backfill_all_instruments(start_date, db_session=db)

    return {
        "status": "ok",
        "start_date": start_date.isoformat(),
        "total": result.total,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "failed_symbols": result.failed_symbols[:20],
        "period_counts": result.period_counts,
    }


if __name__ == "__main__":
    # 自测入口：验证路由注册（无副作用）
    print(f"router.routes={[r.path for r in router.routes]}")
    assert any("/instruments/" in r.path for r in router.routes), "应包含 instruments bars 路由"
    assert any("/admin/bars/refresh" in r.path for r in router.routes), "应包含 admin refresh 路由"
    assert any("/admin/bars/backfill" in r.path for r in router.routes), "应包含 admin backfill 路由"
    assert any("/bars/health" in r.path for r in router.routes), "应包含 bars health 路由"
    print("所有路由注册验证 ✓（含 /bars/health）")
    print("OK")
