"""策略指标实时计算 API。

GET /api/v1/instruments/{instrument_id}/indicators
    实时计算所有已注册策略的图表指标，供个股详情页面使用。
    返回所有策略的 chart_layers 定义 + 计算结果。

数据获取流程（DB 优先 + Redis 缓存）：
1. 查询 Redis 缓存 → 命中返回（X-Cache-Hit: true, X-Data-Source: redis）
2. 未命中 → 复用 MonitorEvaluation.metrics（X-Data-Source: monitor_evaluation）
3. 无 MonitorEvaluation → 实时计算（X-Data-Source: computed）
4. 计算结果写入 Redis 缓存（TTL 300s）

参数：
    timeframe: 1d | 15m | 1h | 1w | 1mo（默认 1d）
    adj: qfq | none（默认 qfq）
    bars: 返回最近 N 根 bar 的指标（默认 250，最大 500）

响应头：
    X-Data-Source: redis | monitor_evaluation | computed
    X-Cache-Hit: true | false
    X-Total-Ms: <int>（总耗时毫秒）
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.bar import BarDaily
from app.models.monitor_evaluation import MonitorEvaluation
from app.services import indicator_cache
from app.services.indicator_service import compute_all_indicators

logger = logging.getLogger("api.indicators")

router = APIRouter(prefix="/api/v1", tags=["indicators"])

# 支持的周期
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


async def _get_last_bar_time(
    db: AsyncSession,
    instrument_id: uuid.UUID,
) -> str | None:
    """[指标缓存] - 查询最新 bar 时间用于缓存键构造。

    优先从 MonitorEvaluation.source_bar_time 获取（监控已计算则复用），
    回退到 BarDaily.trade_date（最新日线）。

    Args:
        db: 异步 DB 会话
        instrument_id: 标的 UUID

    Returns:
        ISO 格式时间字符串，或 None（无数据时）
    """
    # [指标缓存] - 优先查询 MonitorEvaluation 最新 source_bar_time
    try:
        eval_stmt = (
            select(MonitorEvaluation.source_bar_time)
            .where(
                MonitorEvaluation.instrument_id == instrument_id,
                MonitorEvaluation.status == "SUCCEEDED",
            )
            .order_by(MonitorEvaluation.source_bar_time.desc())
            .limit(1)
        )
        eval_result = await db.execute(eval_stmt)
        eval_bar_time = eval_result.scalar_one_or_none()
        if eval_bar_time is not None:
            return eval_bar_time.isoformat()
    except Exception as exc:
        logger.warning("查询 MonitorEvaluation source_bar_time 失败: %s", exc)

    # [指标缓存] - 回退到 BarDaily 最新 trade_date
    try:
        bar_stmt = (
            select(BarDaily.trade_date)
            .where(BarDaily.instrument_id == instrument_id)
            .order_by(BarDaily.trade_date.desc())
            .limit(1)
        )
        bar_result = await db.execute(bar_stmt)
        bar_date = bar_result.scalar_one_or_none()
        if bar_date is not None:
            return bar_date.isoformat() if hasattr(bar_date, "isoformat") else str(bar_date)
    except Exception as exc:
        logger.warning("查询 BarDaily trade_date 失败: %s", exc)

    return None


async def _try_monitor_evaluation(
    db: AsyncSession,
    instrument_id: uuid.UUID,
) -> dict[str, Any] | None:
    """[指标缓存] - 查询最新 MonitorEvaluation.metrics（复用监控计算结果）。

    Args:
        db: 异步 DB 会话
        instrument_id: 标的 UUID

    Returns:
        metrics dict 或 None（无 SUCCEEDED 记录时）
    """
    try:
        stmt = (
            select(MonitorEvaluation.metrics)
            .where(
                MonitorEvaluation.instrument_id == instrument_id,
                MonitorEvaluation.status == "SUCCEEDED",
                MonitorEvaluation.metrics.isnot(None),
            )
            .order_by(MonitorEvaluation.source_bar_time.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        metrics = result.scalar_one_or_none()
        if metrics is not None and isinstance(metrics, dict):
            return metrics
    except Exception as exc:
        logger.warning("查询 MonitorEvaluation.metrics 失败: %s", exc)
    return None


@router.get("/instruments/{instrument_id}/indicators")
async def get_indicators(
    instrument_id: uuid.UUID,
    timeframe: str = Query("1d", description="K线周期: 1d | 15m | 1h | 1w | 1mo"),
    adj: str = Query("qfq", description="复权方式: qfq | none"),
    bars: int = Query(250, ge=50, le=500, description="返回最近 N 根 bar 的指标"),
    db: AsyncSession = Depends(get_db),
    response: Response = None,
) -> dict[str, Any]:
    """实时计算所有已注册策略的图表指标。

    数据获取流程：
    1. 查询 Redis 缓存 → 命中返回（X-Cache-Hit: true）
    2. 未命中 → 复用 MonitorEvaluation.metrics（X-Data-Source: monitor_evaluation）
    3. 无 MonitorEvaluation → 实时计算（X-Data-Source: computed）
    4. 结果写入 Redis 缓存（TTL 300s）

    响应头：
        X-Data-Source: redis | monitor_evaluation | computed
        X-Cache-Hit: true | false
        X-Total-Ms: <int>（总耗时毫秒）
    """
    if timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的周期: {timeframe}, 允许: {sorted(_ALLOWED_TIMEFRAMES)}",
        )
    if adj not in _ALLOWED_ADJ:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的复权方式: {adj}, 允许: {sorted(_ALLOWED_ADJ)}",
        )

    start_ms = time.time()

    # [指标缓存] - 获取 last_bar_time 用于缓存键
    last_bar_time = await _get_last_bar_time(db, instrument_id)

    # [指标缓存] - 1. 查询 Redis 缓存
    cached = await indicator_cache.get(instrument_id, timeframe, adj, last_bar_time)
    if cached is not None:
        total_ms = int((time.time() - start_ms) * 1000)
        if response is not None:
            response.headers["X-Data-Source"] = "redis"
            response.headers["X-Cache-Hit"] = "true"
            response.headers["X-Total-Ms"] = str(total_ms)
        logger.info(
            "指标缓存命中 instrument_id=%s timeframe=%s last_bar=%s",
            instrument_id, timeframe, last_bar_time,
        )
        return cached

    # [指标缓存] - 2. 缓存未命中：尝试复用 MonitorEvaluation.metrics
    data_source = "computed"
    try:
        eval_metrics = await _try_monitor_evaluation(db, instrument_id)
        if eval_metrics is not None:
            # 复用监控计算结果，写入缓存
            await indicator_cache.set(
                instrument_id, timeframe, adj, last_bar_time, eval_metrics,
            )
            total_ms = int((time.time() - start_ms) * 1000)
            if response is not None:
                response.headers["X-Data-Source"] = "monitor_evaluation"
                response.headers["X-Cache-Hit"] = "false"
                response.headers["X-Total-Ms"] = str(total_ms)
            logger.info(
                "复用 MonitorEvaluation.metrics instrument_id=%s last_bar=%s",
                instrument_id, last_bar_time,
            )
            return eval_metrics

        # [指标缓存] - 3. 无 MonitorEvaluation：实时计算
        result = await compute_all_indicators(
            session=db,
            instrument_id=instrument_id,
            timeframe=timeframe,
            adj=adj,
            bars=bars,
        )
        data_source = "computed"

        # [指标缓存] - 4. 写入 Redis 缓存
        await indicator_cache.set(
            instrument_id, timeframe, adj, last_bar_time, result,
        )

        total_ms = int((time.time() - start_ms) * 1000)
        if response is not None:
            response.headers["X-Data-Source"] = data_source
            response.headers["X-Cache-Hit"] = "false"
            response.headers["X-Total-Ms"] = str(total_ms)
        return result

    except Exception as e:
        logger.error("指标计算失败: instrument_id=%s, error=%s", instrument_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"指标计算失败: {e}",
        ) from e


if __name__ == "__main__":
    # 自测入口：验证模块加载和 router 定义
    import inspect

    # 1. 验证 router 存在
    assert router is not None, "router 应存在"
    print(f"router prefix={router.prefix} OK")

    # 2. 验证 get_indicators 函数
    sig = inspect.signature(get_indicators)
    params = list(sig.parameters.keys())
    assert "instrument_id" in params, "应有 instrument_id 参数"
    assert "timeframe" in params, "应有 timeframe 参数"
    assert "adj" in params, "应有 adj 参数"
    assert "bars" in params, "应有 bars 参数"
    assert "db" in params, "应有 db 参数"
    assert "response" in params, "应有 response 参数"
    print(f"get_indicators params={params} OK")

    # 3. 验证常量
    assert "1d" in _ALLOWED_TIMEFRAMES, "应支持 1d"
    assert "qfq" in _ALLOWED_ADJ, "应支持 qfq"
    print(f"_ALLOWED_TIMEFRAMES={sorted(_ALLOWED_TIMEFRAMES)} OK")
    print(f"_ALLOWED_ADJ={sorted(_ALLOWED_ADJ)} OK")

    # 4. 验证缓存模块导入
    assert indicator_cache is not None, "indicator_cache 模块应可导入"
    assert hasattr(indicator_cache, "get"), "应有 get 函数"
    assert hasattr(indicator_cache, "set"), "应有 set 函数"
    assert hasattr(indicator_cache, "invalidate"), "应有 invalidate 函数"
    print("indicator_cache 模块导入 OK")

    print("OK")
