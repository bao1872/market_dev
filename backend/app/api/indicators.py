"""策略指标实时计算 API。

GET /api/v1/instruments/{instrument_id}/indicators
    实时计算所有已注册策略的图表指标，供个股详情页面使用。
    返回所有策略的 chart_layers 定义 + 计算结果。

参数：
    timeframe: 1d | 15m | 1h | 1w | 1mo（默认 1d）
    adj: qfq | none（默认 qfq）
    bars: 返回最近 N 根 bar 的指标（默认 250，最大 500）
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.services.indicator_service import compute_all_indicators

logger = logging.getLogger("api.indicators")

router = APIRouter(prefix="/api/v1", tags=["indicators"])

# 支持的周期
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


@router.get("/instruments/{instrument_id}/indicators")
async def get_indicators(
    instrument_id: uuid.UUID,
    timeframe: str = Query("1d", description="K线周期: 1d | 15m | 1h | 1w | 1mo"),
    adj: str = Query("qfq", description="复权方式: qfq | none"),
    bars: int = Query(250, ge=50, le=500, description="返回最近 N 根 bar 的指标"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """实时计算所有已注册策略的图表指标。

    返回结构：
    {
        "layers": [
            {
                "strategy_id": "dsa_selector",
                "strategy_name": "DSA 方向稳定性选股",
                "layer_id": "dsa_vwap",
                "layer_name": "DSA VWAP",
                "renderer": "line",
                "pane": "price",
                "color": "#ff1744",
                "direction_colored": true,
                "fields": ["dsa_vwap", "dsa_dir"],
                "hover_fields": ["dsa_dir", "dsa_vwap"],
            },
            ...
        ],
        "data": {
            "dsa_selector": {
                "dsa_vwap": [12.34, 12.35, ...],
                "dsa_dir": [1, 1, -1, ...],
            },
            "volume_node_monitor": {
                "upper_node": [...],
                "lower_node": [...],
                ...
            },
        },
        "errors": {
            "strategy_id": "error message",
        },
    }
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

    try:
        result = await compute_all_indicators(
            session=db,
            instrument_id=instrument_id,
            timeframe=timeframe,
            adj=adj,
            bars=bars,
        )
        return result
    except Exception as e:
        logger.error("指标计算失败: instrument_id=%s, error=%s", instrument_id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"指标计算失败: {e}",
        ) from e


if __name__ == "__main__":
    # 自测：验证模块加载和 router 定义
    import inspect

    # 1. 验证 router 存在
    assert router is not None, "router 应存在"
    print(f"router prefix={router.prefix} ✓")

    # 2. 验证 get_indicators 函数
    sig = inspect.signature(get_indicators)
    params = list(sig.parameters.keys())
    assert "instrument_id" in params, "应有 instrument_id 参数"
    assert "timeframe" in params, "应有 timeframe 参数"
    assert "adj" in params, "应有 adj 参数"
    assert "bars" in params, "应有 bars 参数"
    assert "db" in params, "应有 db 参数"
    print(f"get_indicators params={params} ✓")

    # 3. 验证常量
    assert "1d" in _ALLOWED_TIMEFRAMES, "应支持 1d"
    assert "qfq" in _ALLOWED_ADJ, "应支持 qfq"
    print(f"_ALLOWED_TIMEFRAMES={sorted(_ALLOWED_TIMEFRAMES)} ✓")
    print(f"_ALLOWED_ADJ={sorted(_ALLOWED_ADJ)} ✓")

    print("OK")
