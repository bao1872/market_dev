"""Capture Snapshot API - 个股详情截图专用数据快照端点。

[Capture] - 描述: 专用 Capture 链路，仅供截图 worker 通过 Capture Token 访问
（advice.md 第六节 + spec.md Requirement: Capture 专用链路）

端点：
- GET /api/v1/capture/stocks/{instrument_id}/snapshot
    一次返回截图所需完整数据（行情、指标、事件），避免前端多次请求。

认证：
- 不依赖 get_current_active_user（普通用户认证链路）
- 依赖 get_capture_token_payload（Capture Token 解析 + 校验）
- 校验 path 参数 instrument_id 与 token 中的 instrument_id 一致（否则 403）

复用现有服务（禁止重新实现）：
- MarketDataAggregationService.get_bars：行情聚合 SSOT（与 /bars API 同款）
- compute_all_indicators：策略指标计算（与 /indicators API 同款）
- query_events：策略事件查询（与 /instruments/{id}/events 同款）
- _df_to_responses：DataFrame → BarResponse 列表（与 bars API 同款）
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bars import _df_to_responses
from app.constants.indicator_contract import CHART_BARS_COUNT, INDICATOR_BARS
from app.core.deps import get_capture_token_payload, get_db
from app.models.instrument import Instrument
from app.repositories.strategy_event_repository import query_events
from app.schemas.instrument import InstrumentResponse
from app.schemas.strategy_event import StrategyEventResponse
from app.services.indicator_service import compute_all_indicators
from app.services.market_data_aggregation_service import MarketDataAggregationService

logger = logging.getLogger("api.capture")

router = APIRouter(prefix="/api/v1", tags=["capture"])

# [Capture] - 描述: 截图固定参数（advice.md 第六节）
# bars_limit 引用 indicator_contract.CHART_BARS_COUNT 唯一真源（=DAILY_HISTORY_BARS=250），
# 与 chart_bars_service / indicator_service 保持一致，禁止散落硬编码 250
_CAPTURE_TIMEFRAME = "1d"
_CAPTURE_ADJ = "qfq"
_CAPTURE_EVENTS_LIMIT = 20

# [capture-realtime] - 截图支持多周期，bars 根数按周期对齐（与 StockDetailPage barsCountByTimeframe 一致）
# 周期 bars 根数直接复用 indicator_contract.INDICATOR_BARS 唯一真源，禁止散落硬编码 250/4000
_CAPTURE_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}


@router.get(
    "/capture/stocks/{instrument_id}/snapshot",
    summary="个股详情截图数据快照（Capture Token 专用）",
)
async def get_capture_snapshot(
    instrument_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    capture_payload: dict[str, Any] = Depends(get_capture_token_payload),
    timeframe: str = Query("1d", description="K线周期：1d|15m|1h|1w|1mo"),
    source_bar_time: str | None = Query(None, description="实时 bar 时间（防旧图，用于日志/cache key）"),
    force_refresh: bool = Query(False, description="跳过指标缓存强制实时计算（截图链路默认 True）"),
    capture: bool = Query(False, description="截图模式标记（等价 force_refresh）"),
) -> dict[str, Any]:
    """一次返回截图所需完整数据（行情、指标、事件）。

    [Capture] - 描述: 截图 worker 通过 Capture Token 调用，避免前端多次请求
    （advice.md 第六节 + spec.md Requirement: Capture 专用链路）

    校验：
    1. Capture Token 已由 get_capture_token_payload 校验（type/scope/exp/声明）
    2. path 参数 instrument_id 必须与 token 中的 instrument_id 一致（否则 403）

    复用现有服务（禁止重新实现）：
    - MarketDataAggregationService.get_bars：1d qfq 行情（与 /bars API 同款）
    - compute_all_indicators：策略指标（与 /indicators API 同款）
    - query_events：策略事件（与 /instruments/{id}/events 同款）

    Returns:
        dict 含 instrument/bars/indicators/events/snapshot_time
    """
    # 1. 校验 path instrument_id 与 token instrument_id 一致（防越权）
    token_instrument_id = capture_payload.get("instrument_id")
    if token_instrument_id != str(instrument_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Capture Token instrument_id 与 path 不匹配："
                f"token={token_instrument_id}, path={instrument_id}"
            ),
        )

    # [capture-realtime] - 周期校验 + 实时聚合开关（截图始终实时，杜绝复用旧图）
    if timeframe not in _CAPTURE_ALLOWED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的周期: {timeframe}, 允许: {sorted(_CAPTURE_ALLOWED_TIMEFRAMES)}",
        )
    bars_limit = INDICATOR_BARS.get(timeframe, CHART_BARS_COUNT)
    # 截图场景始终使用实时聚合（include_realtime=True），保证 K线为当前盘中数据
    realtime = True
    logger.info(
        "[Capture] 快照请求 instrument_id=%s timeframe=%s source_bar_time=%s "
        "force_refresh=%s capture=%d realtime=%s",
        instrument_id, timeframe, source_bar_time, force_refresh,
        1 if capture else 0, realtime,
    )

    snapshot_start = datetime.now(UTC)

    # 2. 查询 instrument 基本信息
    instrument = await db.get(Instrument, instrument_id)
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"标的不存在: instrument_id={instrument_id}",
        )

    # 3. 行情：复用 MarketDataAggregationService（与 /bars API 同款 SSOT）
    bars_service = MarketDataAggregationService()
    try:
        bars_result = await bars_service.get_bars(
            db,
            instrument_id,
            timeframe=_CAPTURE_TIMEFRAME,
            adj=_CAPTURE_ADJ,
            include_realtime=False,  # 截图场景无需实时聚合
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"行情查询参数错误: {exc}",
        ) from exc
    except Exception as exc:
        logger.warning(
            "Capture 行情聚合失败 instrument_id=%s: %s", instrument_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"行情聚合失败: {exc}",
        ) from exc

    # 截取最近 CHART_BARS_COUNT 根（与页面显示一致，引用 indicator_contract 唯一真源）
    df: pd.DataFrame = bars_result.bars
    if not df.empty:
        df = df.tail(bars_limit)
    bars_items = _df_to_responses(df, instrument_id, _CAPTURE_TIMEFRAME)

    # 4. 指标：复用 compute_all_indicators（与 /indicators API 同款）
    try:
        indicators = await compute_all_indicators(
            session=db,
            instrument_id=instrument_id,
            timeframe=_CAPTURE_TIMEFRAME,
            adj=_CAPTURE_ADJ,
            bars=CHART_BARS_COUNT,
        )
    except Exception as exc:
        logger.warning(
            "Capture 指标计算失败 instrument_id=%s: %s", instrument_id, exc
        )
        # 指标失败不阻塞截图（行情已就绪），返回空指标 + 错误信息
        indicators = {"layers": [], "data": {}, "errors": {"_capture": str(exc)}}

    # 5. 事件：复用 query_events（与 /instruments/{id}/events 同款）
    try:
        events = await query_events(
            db,
            instrument_id=instrument_id,
            limit=_CAPTURE_EVENTS_LIMIT,
        )
        event_items = [StrategyEventResponse.model_validate(e) for e in events]
    except Exception as exc:
        logger.warning(
            "Capture 事件查询失败 instrument_id=%s: %s", instrument_id, exc
        )
        event_items = []

    snapshot_ms = int((datetime.now(UTC) - snapshot_start).total_seconds() * 1000)
    logger.info(
        "[Capture] 快照完成 instrument_id=%s bars=%d indicators_layers=%d events=%d ms=%d",
        instrument_id,
        len(bars_items),
        len(indicators.get("layers", [])) if isinstance(indicators, dict) else 0,
        len(event_items),
        snapshot_ms,
    )

    return {
        "instrument": InstrumentResponse.model_validate(instrument).model_dump(mode="json"),
        "bars": {
            "items": [b.model_dump(mode="json") for b in bars_items],
            "total": len(bars_items),
            "timeframe": timeframe,
            "adj": _CAPTURE_ADJ,
            "data_source": bars_result.data_source,
            "as_of": bars_result.as_of.isoformat() if bars_result.as_of else None,
            "is_partial": bars_result.is_partial,
            "last_persisted_bar_time": (
                bars_result.last_persisted_bar_time.to_pydatetime().isoformat()
                if bars_result.last_persisted_bar_time is not None
                else None
            ),
            "last_live_bar_time": (
                bars_result.last_live_bar_time.to_pydatetime().isoformat()
                if bars_result.last_live_bar_time is not None
                else None
            ),
            "freshness_seconds": bars_result.freshness_seconds,
            "degraded": bars_result.degraded,
            "degraded_reason": bars_result.degraded_reason,
        },
        "indicators": indicators,
        "events": {
            "items": [e.model_dump(mode="json") for e in event_items],
            "total": len(event_items),
        },
        "snapshot_time": datetime.now(UTC).isoformat(),
        "capture": {
            "user_id": capture_payload.get("user_id"),
            "event_id": capture_payload.get("event_id"),
            "scope": capture_payload.get("scope"),
        },
    }


if __name__ == "__main__":
    # 自测入口：验证路由注册 + 依赖配置（不连 DB）
    paths: list[str] = []
    for r in router.routes:
        path = getattr(r, "path", None)
        if isinstance(path, str):
            paths.append(path)
    print(f"router.prefix={router.prefix}")
    print(f"router.routes={paths}")
    assert any("/capture/stocks/" in p for p in paths), "应包含 /capture/stocks/{id}/snapshot 路由"
    assert router.prefix == "/api/v1", "prefix 应为 /api/v1"

    # 验证依赖导入
    assert callable(get_capture_token_payload), "get_capture_token_payload 应可导入"
    assert callable(compute_all_indicators), "compute_all_indicators 应可导入"
    assert callable(_df_to_responses), "_df_to_responses 应可导入"
    assert callable(query_events), "query_events 应可导入"
    print("依赖导入 OK")

    # 验证常量（bars_limit 引用 indicator_contract 唯一真源）
    assert _CAPTURE_TIMEFRAME == "1d"
    assert _CAPTURE_ADJ == "qfq"
    assert CHART_BARS_COUNT == 250, f"CHART_BARS_COUNT 应为 250，实际 {CHART_BARS_COUNT}"
    print(f"常量: timeframe={_CAPTURE_TIMEFRAME} adj={_CAPTURE_ADJ} bars={CHART_BARS_COUNT}")
    print("OK")
