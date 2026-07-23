"""Atomic Chart Snapshot API - 个股详情页原子图表快照端点。

[PRD V2.0 §4.2 SNAP-01] - 一次 MDAS DataFrame 同时生成 bars + indicators +
completed_frame + live_revision + diagnostics，禁止详情页 Bars/Indicators 两次
独立实时请求。

端点：
- GET /api/v1/instruments/{instrument_id}/chart-snapshot
    一次返回个股详情页图表所需的完整数据（bars + indicators + display_frame +
    render_frame + snapshot_time），替代详情页独立 useBars + useIndicators 两次请求。

原子性保证（[CP-16] 真正单输入 — 不再依赖 Redis 缓存间接同步）：
1. 端点调用 MarketDataAggregationService.get_bars() 获取展示窗口 DataFrame
   （仅此一次 MDAS 行情读取；Redis 只缓存最终 Snapshot 响应，不作为同请求
   内部两次调用的同步手段）。
2. 端点用同一 DataFrame 构建 bars response（items + display_frame + 诊断字段）。
3. 端点将同一 BarAggregationResult 通过 preloaded_display_bars 参数传给
   compute_all_indicators()，指标计算直接接收预加载 DataFrame，不再第二次
   调用 MDAS get_bars 获取展示周期行情。
4. 端点用 is_display_frame_match() 校验 bars vs indicators display_frame，
   返回 render_frame.matched。前端 mismatch 时可重试。

Node Cluster 输入隔离：
- compute_all_indicators 内部 _load_node_cluster_inputs 仍独立查询 completed qfq
  日线/15m（合同常量 250/4000，与页面 include_realtime/completed_only/bars 隔离）。
- 这不算"第二次行情读取"——Node 输入是不同参数（completed_only=True）的独立查询，
  保证 Node 计算不受展示窗口 partial bar 污染。

认证：
- 依赖 get_db（标准 AsyncSession），与 /bars 和 /indicators 端点一致。
- 权限/限流由网关层统一处理（与 /bars、/indicators 同款）。

复用现有服务（禁止重新实现）：
- MarketDataAggregationService.get_bars：行情聚合 SSOT（与 /bars API 同款）
- compute_all_indicators：策略指标计算（与 /indicators API 同款，新增 preloaded_display_bars 参数）
- _df_to_responses：DataFrame → BarResponse 列表（与 bars API 同款）
- build_display_frame：展示帧构建（与 bars/indicators API 同款）
- is_display_frame_match：展示帧匹配校验（与 capture API 同款）

[PROMPT.md §二 V2 DisplayWindowSpec] bars/indicators/capture 必须基于同一 Spec
和同一最终展示 DataFrame 生成 frame。本端点是详情页的原子入口，保证同一展示窗口
产生同一 display_hash，消除"1d 周期永久 mismatch、指标图层被屏蔽"问题。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import date
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bars import _df_to_responses
from app.core.deps import get_db
from app.core.time import now_shanghai
from app.schemas.bar import BarListResponse
from app.services.chart_snapshot_service import ChartSnapshotService

logger = logging.getLogger("api.chart_snapshot")

router = APIRouter(prefix="/api/v1", tags=["chart-snapshot"])

# 支持的周期与复权方式（与 bars/indicators API 对齐）
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


@router.get(
    "/instruments/{instrument_id}/chart-snapshot",
    summary="个股详情页原子图表快照（一次返回 bars + indicators + display_frame）",
)
async def get_chart_snapshot(
    instrument_id: uuid.UUID,
    timeframe: str = Query("1d", description="K线周期: 1d | 15m | 1h | 1w | 1mo"),
    adj: str = Query("qfq", description="复权方式: qfq | none"),
    bars: int = Query(
        250,
        ge=50,
        le=4000,
        description="返回最近 N 根 bar 的指标和行情（最大 4000，与 Node Cluster 15m 契约对齐）",
    ),
    include_smc: bool = Query(
        False,
        description="是否计算 SMC 指标（默认 False，前端通过 IndicatorToolbar 显式开启）",
    ),
    include_realtime: bool = Query(
        True,
        description="是否包含实时 partial bar（默认 True，与 bars API 默认对齐）",
    ),
    completed_only: bool = Query(
        False,
        description="只返回已完成 bar（True 时强制 include_realtime=False）",
    ),
    adjustment_as_of: date | None = Query(
        None,
        description="复权锚点 YYYY-MM-DD（None=最新；历史回算传业务日）",
    ),
    db: AsyncSession = Depends(get_db),
    *,
    response: Response,
) -> dict[str, Any]:
    """原子图表快照 - 一次 MDAS DataFrame 同时生成 bars + indicators + display_frame。

    [PRD V2.0 §4.2 SNAP-01] 详情页必须使用本端点，禁止独立调用 /bars 和 /indicators。
    本端点保证 bars 和 indicators 基于同一 MDAS DataFrame 生成 display_frame，
    display_hash 必然一致（render_frame.matched=true）。

    [CP-16] 原子性实现（真正单输入，不依赖 Redis 缓存间接同步）：
    1. MDAS get_bars() 获取展示窗口 DataFrame（仅此一次行情读取）
    2. 用同一 DataFrame 构建 bars response（items + display_frame + 诊断字段）
    3. 将同一 BarAggregationResult 通过 preloaded_display_bars 传给
       compute_all_indicators()，指标计算直接接收预加载 DataFrame
    4. is_display_frame_match() 校验 bars vs indicators display_frame，返回 render_frame

    响应头：
        X-Data-Source: db | hybrid | pytdx | degraded（来自 MDAS）
        X-Cache-Hit: true | false（MDAS Redis 缓存命中）
        X-Render-Matched: true | false（bars vs indicators display_frame 匹配）
        X-Total-Ms: <int>（总耗时毫秒）

    Returns:
        dict 含：
        - bars: BarListResponse 形状 dict（items + 分页 + 诊断 + display_frame）
        - indicators: compute_all_indicators 返回的 dict（layers + data + display_frame + 诊断）
        - snapshot_time: ISO 8601 时间戳
        - render_frame: {matched, bars_hash, indicators_hash, ...} display_frame 匹配结果
        - timeframe: 周期 echo（供前端周期切换乱序丢弃检查）
    """
    # 1. 参数校验（与 bars/indicators API 一致）
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
    logger.info(
        "[ChartSnapshot] 请求 instrument_id=%s timeframe=%s adj=%s bars=%d "
        "include_smc=%s include_realtime=%s completed_only=%s adjustment_as_of=%s",
        instrument_id, timeframe, adj, bars,
        include_smc, include_realtime, completed_only, adjustment_as_of,
    )

    # 2. [CP-V3-B] 调用统一 ChartSnapshotService — 一次 MDAS 读取 → 同一 DataFrame
    #    生成 bars 和 indicators → render_frame（与 Capture 共用同一服务）
    try:
        snapshot_result = await ChartSnapshotService.compute_bars_and_indicators(
            session=db,
            instrument_id=instrument_id,
            timeframe=timeframe,
            adj=adj,
            bars=bars,
            include_smc=include_smc,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=adjustment_as_of,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        logger.warning(
            "[ChartSnapshot] ChartSnapshotService 失败 instrument_id=%s: %s",
            instrument_id, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"行情聚合失败: {exc}",
        ) from exc

    bars_result = snapshot_result.bars_result
    bars_display_frame = snapshot_result.bars_display_frame
    indicators_response = snapshot_result.indicators
    render_frame = snapshot_result.render_frame

    # completed_through 转换为 tz-aware datetime（BarListResponse 需要 datetime 对象）
    _completed_through = bars_result.completed_through
    if _completed_through is not None and isinstance(_completed_through, pd.Timestamp):
        if _completed_through.tzinfo is None:
            _completed_through = _completed_through.tz_localize("Asia/Shanghai")
        _completed_through = _completed_through.to_pydatetime()

    # 3. 构建 BarListResponse（从 service 返回的 page_df 构建 bars items）
    items = _df_to_responses(snapshot_result.page_df, instrument_id, timeframe)
    bars_response = BarListResponse(
        items=items,
        total=len(snapshot_result.page_df),
        page=1,
        page_size=bars,
        timeframe=timeframe,
        adj=adj,
        data_source=bars_result.data_source,
        as_of=bars_result.as_of,
        is_partial=bars_result.is_partial,
        last_persisted_bar_time=(
            bars_result.last_persisted_bar_time.to_pydatetime()
            if bars_result.last_persisted_bar_time is not None
            else None
        ),
        last_live_bar_time=(
            bars_result.last_live_bar_time.to_pydatetime()
            if bars_result.last_live_bar_time is not None
            else None
        ),
        freshness_seconds=bars_result.freshness_seconds,
        degraded=bars_result.degraded,
        degraded_reason=bars_result.degraded_reason,
        source_bar_hash=bars_result.source_bar_hash or None,
        adj_factor_hash=bars_result.adj_factor_hash or None,
        market_data_contract_version=bars_result.market_data_contract_version,
        completed_through=_completed_through,
        adjustment_as_of=bars_result.adjustment_as_of,
        display_frame=bars_display_frame,
    )

    # 4. 响应头
    total_ms = int((time.time() - start_ms) * 1000)
    if response is not None:
        response.headers["X-Data-Source"] = bars_result.data_source
        response.headers["X-Cache-Hit"] = "true" if bars_result.cache_hit else "false"
        response.headers["X-Render-Matched"] = "true" if render_frame.get("matched") else "false"
        response.headers["X-Total-Ms"] = str(total_ms)

    logger.info(
        "[ChartSnapshot] 完成 instrument_id=%s timeframe=%s bars_count=%d "
        "indicators_layers=%d render_matched=%s ms=%d",
        instrument_id, timeframe, len(items),
        len(indicators_response.get("layers", [])),
        render_frame.get("matched"), total_ms,
    )

    return {
        "bars": bars_response.model_dump(mode="json"),
        "indicators": indicators_response,
        "snapshot_time": now_shanghai().isoformat(),
        "render_frame": render_frame,
        "timeframe": timeframe,
    }


if __name__ == "__main__":
    # 自测入口：验证模块加载和 router 定义
    import inspect

    # 1. 验证 router 存在
    assert router is not None, "router 应存在"
    print(f"router prefix={router.prefix} OK")

    # 2. 验证 get_chart_snapshot 函数
    sig = inspect.signature(get_chart_snapshot)
    params = list(sig.parameters.keys())
    assert "instrument_id" in params, "应有 instrument_id 参数"
    assert "timeframe" in params, "应有 timeframe 参数"
    assert "adj" in params, "应有 adj 参数"
    assert "bars" in params, "应有 bars 参数"
    assert "include_smc" in params, "应有 include_smc 参数"
    assert "include_realtime" in params, "应有 include_realtime 参数"
    assert "completed_only" in params, "应有 completed_only 参数"
    assert "adjustment_as_of" in params, "应有 adjustment_as_of 参数"
    assert "db" in params, "应有 db 参数"
    assert "response" in params, "应有 response 参数"
    print(f"get_chart_snapshot params={params} OK")

    # 3. 验证常量
    assert "1d" in _ALLOWED_TIMEFRAMES, "应支持 1d"
    assert "qfq" in _ALLOWED_ADJ, "应支持 qfq"
    print(f"_ALLOWED_TIMEFRAMES={sorted(_ALLOWED_TIMEFRAMES)} OK")
    print(f"_ALLOWED_ADJ={sorted(_ALLOWED_ADJ)} OK")

    # 4. 验证依赖导入（[CP-V3-B] 改为 ChartSnapshotService 唯一入口）
    assert callable(_df_to_responses), "_df_to_responses 应可导入"
    assert callable(ChartSnapshotService.compute_bars_and_indicators), (
        "ChartSnapshotService.compute_bars_and_indicators 应可导入"
    )
    print("依赖导入 OK")

    print("OK")
