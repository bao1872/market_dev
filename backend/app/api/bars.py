"""行情查询 API。

GET /api/v1/instruments/{instrument_id}/bars
    查询行情数据，支持多周期（15m/1h/1d/1w/1mo）、前复权/不复权、服务端分页。

GET /api/v1/bars/health
    行情系统健康检查，返回 DB/Redis 连通性与各周期数据新鲜度。

参数：
    timeframe: 1d | 15m | 1h | 1w | 1mo（默认 1d）
    adj: qfq | none（默认 none）
    start_date: 起始日期（YYYY-MM-DD），可选
    end_date: 结束日期（YYYY-MM-DD），可选
    page: 页码（1-based，默认 1）
    page_size: 每页大小（默认 100，最大 1000）
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.core.redis_client import get_redis
from app.models.bar import Bar15Min, Bar60Min, BarDaily, BarMinute, BarMonthly, BarWeekly
from app.repositories.bar_repository import (
    _get_adj_factor_df,
    apply_adj_factor_to_bars,
    fetch_15min_bars,
    fetch_60min_bars,
    fetch_daily_bars,
    fetch_monthly_bars,
    fetch_weekly_bars,
)
from app.schemas.bar import BarListResponse, BarResponse

logger = logging.getLogger("api.bars")

router = APIRouter(prefix="/api/v1", tags=["bars"])

# 支持的周期
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}

# 默认查询范围
_DEFAULT_DAILY_LOOKBACK_DAYS = 180  # 日线/周线/月线默认回看 180 天
_DEFAULT_INTRADAY_LOOKBACK_DAYS = 5  # 15min/60min 默认回看 5 天（覆盖一周交易）
_MAX_PAGE_SIZE = 1000


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
    # 默认回看 5 天，覆盖一周的交易（避免周末/非交易日时查不到数据）
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
    page_size: int = Query(100, ge=1, le=_MAX_PAGE_SIZE, description="每页大小"),
    session: AsyncSession = Depends(get_db),
) -> BarListResponse:
    """查询指定标的的行情数据。

    - 支持多周期：1d（日线）/ 15m / 1h / 1w（周线）/ 1mo（月线）
    - DB 优先：先查 DB，DB 无数据则从 pytdx 拉取并入库
    - 前复权：adj=qfq 时对 OHLC 应用前复权（volume 不变）
    - 分页：服务端分页，返回 total/page/page_size
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

    logger.info(
        "查询行情 instrument_id=%s timeframe=%s adj=%s start=%s end=%s page=%d size=%d",
        instrument_id, timeframe, adj, start_date, end_date, page, page_size,
    )

    try:
        start, end = _parse_date_range(timeframe, start_date, end_date)
        # 按周期分发到对应的 fetch 函数
        if timeframe == "1d":
            df = await fetch_daily_bars(session, instrument_id, start, end)
        elif timeframe == "1w":
            df = await fetch_weekly_bars(session, instrument_id, start, end)
        elif timeframe == "1mo":
            df = await fetch_monthly_bars(session, instrument_id, start, end)
        elif timeframe == "15m":
            df = await fetch_15min_bars(session, instrument_id, start, end)
        else:  # 1h
            df = await fetch_60min_bars(session, instrument_id, start, end)
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("查询行情失败 instrument_id=%s: %s", instrument_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"查询行情失败: {exc}",
        ) from exc

    if df.empty:
        return BarListResponse(
            items=[],
            total=0,
            page=page,
            page_size=page_size,
            timeframe=timeframe,
            adj=adj,
        )

    # 前复权处理
    if adj == "qfq":
        try:
            adj_factor_df = await _get_adj_factor_df(session, instrument_id)
            # 15min/60min 使用日内前复权，其他使用日线前复权
            intraday = timeframe in ("15m", "1h")
            df = apply_adj_factor_to_bars(df, adj_factor_df, intraday=intraday)
        except Exception as exc:
            logger.warning("前复权失败 instrument_id=%s: %s", instrument_id, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"前复权计算失败: {exc}",
            ) from exc

    # 服务端分页
    total = len(df)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_df = df.iloc[start_idx:end_idx]

    items = _df_to_responses(page_df, instrument_id, timeframe)

    return BarListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        timeframe=timeframe,
        adj=adj,
    )


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
        "timestamp": datetime.now().isoformat(),
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
    now = datetime.now()
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
                    if latest_dt.tzinfo is not None:
                        latest_dt = latest_dt.replace(tzinfo=None)

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
