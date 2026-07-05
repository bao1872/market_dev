"""结构状态因子 API。

GET /api/v1/instruments/{instrument_id}/structural-factors
    返回双周期 (1d + 15m) 结构状态因子，供个股详情页右侧面板使用。

参数：
    primary_timeframe: 主周期（默认 1d）
    secondary_timeframe: 副周期（默认 15m）
    adj: 复权方式（默认 qfq）
    as_of: 截止时间（默认 latest）

响应结构：
    {
      "primary": { "<timeframe>": {5 factor groups} },
      "secondary": { "<timeframe>": {5 factor groups} },
      "relation": { trend_alignment, momentum_alignment, notes },
      "meta": { as_of, lookback_bars, degraded_reasons, warmup_notes }
    }

V1 范围：
- 只按需计算单只股票最新已完成 bar
- 不做全市场预计算
- Node/POC 失败时返回 null + degraded_reasons，不阻塞页面
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.services.structural_factor_service import compute_structural_factors

logger = logging.getLogger("api.structural_factors")

router = APIRouter(prefix="/api/v1/instruments", tags=["structural-factors"])

_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


@router.get("/{instrument_id}/structural-factors")
async def get_structural_factors(
    instrument_id: uuid.UUID,
    primary_timeframe: str = Query(
        "1d", description="主周期: 1d | 15m | 1h | 1w | 1mo"
    ),
    secondary_timeframe: str = Query(
        "15m", description="副周期: 1d | 15m | 1h | 1w | 1mo"
    ),
    adj: str = Query("qfq", description="复权方式: qfq | none"),
    as_of: str = Query("latest", description="截止时间（默认 latest）"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """获取双周期结构状态因子。

    返回 1d 和 15m 两组结构因子，包含 5 张卡的数据：
    1. DSA 段质量
    2. Swing 结构位置
    3. 成本/节点
    4. 动量/波动 (BB + SQZMOM)
    5. 成交参与

    Node/POC 失败时返回 null + degraded_reasons，不阻塞页面。
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

    result = await compute_structural_factors(
        session=db,
        instrument_id=instrument_id,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        as_of=as_of,
    )
    return result
