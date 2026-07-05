"""时序特征 API。

GET /api/v1/instruments/{instrument_id}/temporal-features
    返回双周期 (1d + 15m) 时序特征，补充 V1.8 结构状态因子的变化量/持续度/派生关系。

参数：
    primary_timeframe: 主周期（默认 1d）
    secondary_timeframe: 副周期（默认 15m）
    adj: 复权方式（默认 qfq）
    as_of: 截止时间（V1 只支持 latest）

响应结构：
    {
      "daily_context": {9 字段},
      "m15_response": {9 字段},
      "derived_relation": {3 字段},
      "meta": {as_of, timeframes, degraded_reasons, warmup_notes}
    }

V1 范围：
- 只按需计算单只股票最新已完成 bar
- 只支持 as_of=latest
- 不新增 worker/大表/全市场
- 字段无法计算返回 null，不影响整体返回
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.services.temporal_feature_service import compute_temporal_features

logger = logging.getLogger("api.temporal_features")

router = APIRouter(prefix="/api/v1/instruments", tags=["temporal-features"])

_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


@router.get("/{instrument_id}/temporal-features")
async def get_temporal_features(
    instrument_id: uuid.UUID,
    primary_timeframe: str = Query(
        "1d", description="主周期: 1d | 15m | 1h | 1w | 1mo"
    ),
    secondary_timeframe: str = Query(
        "15m", description="副周期: 1d | 15m | 1h | 1w | 1mo"
    ),
    adj: str = Query("qfq", description="复权方式: qfq | none"),
    as_of: str = Query("latest", description="截止时间（V1 只支持 latest）"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取双周期时序特征。

    返回 daily_context / m15_response / derived_relation / meta：
    - daily_context: 日线长周期结构背景（DSA 方向/持续度/斜率/效率/Swing 位置/SQZMOM 变化/成交量变化）
    - m15_response: 15m 短周期 swing/动量/波动/成交响应（anchor 变化量）
    - derived_relation: 只由 daily + m15 派生的对齐/强度关系
    - meta: as_of/timeframes/degraded_reasons/warmup_notes

    字段无法计算返回 null + warmup_notes，不阻塞页面。
    """
    if primary_timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 primary_timeframe: {primary_timeframe}, "
            f"允许: {sorted(_ALLOWED_TIMEFRAMES)}",
        )
    if secondary_timeframe not in _ALLOWED_TIMEFRAMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 secondary_timeframe: {secondary_timeframe}, "
            f"允许: {sorted(_ALLOWED_TIMEFRAMES)}",
        )
    if adj not in _ALLOWED_ADJ:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的 adj: {adj}, 允许: {sorted(_ALLOWED_ADJ)}",
        )

    result = await compute_temporal_features(
        session=db,
        instrument_id=instrument_id,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        as_of=as_of,
    )
    return result
