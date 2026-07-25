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
from datetime import date, datetime
from datetime import time as dt_time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.bars import _df_to_responses
from app.constants.capability_keys import MARKET_SCREENING
from app.core.deps import get_db
from app.core.time import now_shanghai
from app.schemas.bar import BarListResponse
from app.services.calendar_service import is_trading_day_async
from app.services.capability_service import (
    CapabilityAccessContext,
    require_capability,
)
from app.services.chart_snapshot_service import ChartSnapshotService
from app.services.market_data_aggregation_service import (
    _call_expected_last_completed_daily_bar,
)
from app.services.market_status_service import (
    MARKET_SESSION_CLOSED,
    MARKET_SESSION_LUNCH,
    MARKET_SESSION_NON_TRADING_DAY,
    MARKET_SESSION_PRE_OPEN,
    TRADING_SESSIONS,
    compute_market_session,
)

logger = logging.getLogger("api.chart_snapshot")

router = APIRouter(prefix="/api/v1", tags=["chart-snapshot"])

# 支持的周期与复权方式（与 bars/indicators API 对齐）
_ALLOWED_TIMEFRAMES = {"1d", "15m", "1h", "1w", "1mo"}
_ALLOWED_ADJ = {"qfq", "none"}


# [P0-7] freshness_state 枚举（详情页唯一行情真源新鲜度状态）
# fresh       = 正常数据（db 或 hybrid，无降级，非 partial）
# partial     = 实时尾部存在（is_partial=True，无降级）
# stale       = 交易时段实时目标周期返回空（actual < expected in trading hours）
# unavailable = 行情不可用（degraded + 其他原因，或数据完全为空）
_FRESHNESS_FRESH = "fresh"
_FRESHNESS_PARTIAL = "partial"
_FRESHNESS_STALE = "stale"
_FRESHNESS_UNAVAILABLE = "unavailable"

# 上海时区（chart_snapshot 内部 expected/actual 比较）
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _compute_expected_latest_bar_time(
    now: datetime,
    timeframe: str,
    market_session: str,
    last_completed_daily_date: date,
) -> datetime:
    """[CHANGE-20260724-004] 按周期和市场阶段计算 expected_latest_bar_time。

    合同：当前周期本应存在的最新已完成 bar 时间（tz-aware Asia/Shanghai）。

    - 1d/1w/1mo: 最近已完成日线交易日 15:00
    - 非交易日 / 盘前: 上一交易日收盘 15:00
    - 收盘后: 今日收盘 15:00
    - 15m 盘中: 向下取整到 15m 边界；午休为 11:15
    - 1h 盘中: 按 A 股 1h bar 完成时间（10:30/11:30/14:00/15:00）
    """
    # 1d/1w/1mo: 期望 = 最近已完成日线交易日 15:00
    if timeframe in ("1d", "1w", "1mo"):
        return datetime.combine(last_completed_daily_date, dt_time(15, 0), tzinfo=_SHANGHAI_TZ)

    # 非交易日 / 盘前：期望 = 上一交易日收盘
    if market_session in (MARKET_SESSION_NON_TRADING_DAY, MARKET_SESSION_PRE_OPEN):
        return datetime.combine(last_completed_daily_date, dt_time(15, 0), tzinfo=_SHANGHAI_TZ)

    # 收盘后：期望 = 今日收盘
    if market_session == MARKET_SESSION_CLOSED:
        return datetime.combine(now.date(), dt_time(15, 0), tzinfo=_SHANGHAI_TZ)

    # 盘中交易时段（MORNING / LUNCH / AFTERNOON）
    if timeframe == "15m":
        # 午休：上午最后一根 15m bar = 11:15
        if market_session == MARKET_SESSION_LUNCH:
            return datetime.combine(now.date(), dt_time(11, 15), tzinfo=_SHANGHAI_TZ)
        # 交易时段：向下取整到 15m 边界（当前已完成 bar 时间）
        minute = (now.minute // 15) * 15
        return datetime.combine(now.date(), dt_time(now.hour, minute), tzinfo=_SHANGHAI_TZ)

    if timeframe == "1h":
        # 午休：上午最后一根 1h bar = 11:30
        if market_session == MARKET_SESSION_LUNCH:
            return datetime.combine(now.date(), dt_time(11, 30), tzinfo=_SHANGHAI_TZ)
        # 1h bar 完成时间：10:30, 11:30, 14:00, 15:00
        h, m = now.hour, now.minute
        if h < 10 or (h == 10 and m < 30):
            # 09:30-10:30 第一根 1h 未完成 → 期望上一交易日收盘
            return datetime.combine(last_completed_daily_date, dt_time(15, 0), tzinfo=_SHANGHAI_TZ)
        if h < 11 or (h == 11 and m < 30):
            # 10:30-11:30 已完成 10:30
            return datetime.combine(now.date(), dt_time(10, 30), tzinfo=_SHANGHAI_TZ)
        if h < 14:
            # 11:30-14:00 已完成 11:30
            return datetime.combine(now.date(), dt_time(11, 30), tzinfo=_SHANGHAI_TZ)
        if h < 15:
            # 14:00-15:00 已完成 14:00
            return datetime.combine(now.date(), dt_time(14, 0), tzinfo=_SHANGHAI_TZ)
        return datetime.combine(now.date(), dt_time(15, 0), tzinfo=_SHANGHAI_TZ)

    # fallback：返回当前时间
    return now.astimezone(_SHANGHAI_TZ) if now.tzinfo else now.replace(tzinfo=_SHANGHAI_TZ)


def _compute_freshness_state(
    bars_result: Any,
    page_df: pd.DataFrame,
    actual_latest_bar_time: datetime | None,
    expected_latest_bar_time: datetime | None,
    market_session: str,
    timeframe: str,
) -> str:
    """[P0-7] 真实比较 actual vs expected 计算 freshness_state。

    禁止静默返回普通 db 状态：交易时段实时返回空时必须标记 stale。
    [CHANGE-20260724-004] latest_daily_quote 缺失时标记 unavailable。
    [CHANGE-20260724-004] actual < expected 在交易时段 → stale；其他时段 → unavailable。
    [CHANGE-20260725-001] 日级周期（1d/1w/1mo）bar 时间戳为当日 00:00，
        expected 为 15:00；按日期比较避免误判 unavailable。
    """
    if page_df.empty:
        return _FRESHNESS_UNAVAILABLE
    # [CHANGE-20260724-004] quote 真源缺失 → unavailable（禁止从 page_df 兜底）
    if getattr(bars_result, "latest_daily_quote", None) is None:
        return _FRESHNESS_UNAVAILABLE
    if bars_result.degraded:
        reason = bars_result.degraded_reason or ""
        if "realtime_" in reason and "_empty_in_trading_hours" in reason:
            return _FRESHNESS_STALE
        return _FRESHNESS_UNAVAILABLE
    # [CHANGE-20260724-004] 真实比较 actual vs expected
    if actual_latest_bar_time is not None and expected_latest_bar_time is not None:
        # [CHANGE-20260725-001] 日级周期 bar 时间戳为当日 00:00，按日期比较
        if timeframe in ("1d", "1w", "1mo"):
            stale = actual_latest_bar_time.date() < expected_latest_bar_time.date()
        else:
            # 日内周期（15m/1h）按精确时间比较
            stale = actual_latest_bar_time < expected_latest_bar_time
        if stale:
            # 交易时段实时目标缺失 → stale；非交易时段 → unavailable
            if market_session in TRADING_SESSIONS:
                return _FRESHNESS_STALE
            return _FRESHNESS_UNAVAILABLE
    if bars_result.is_partial:
        return _FRESHNESS_PARTIAL
    return _FRESHNESS_FRESH


def _derive_quote_from_bars(
    page_df: pd.DataFrame,
    bars_result: Any,
    timeframe: str,
) -> dict[str, Any] | None:
    """[CHANGE-20260724-004] 从 bars_result.latest_daily_quote 派生 quote。

    合同：
    - 所有周期（1d/15m/1h/1w/1mo）quote 唯一真源为 latest_daily_quote
    - latest_daily_quote 由 MDAS 单次读取派生，禁止第二次行情请求
    - 缺失时返回 None（quote=null），freshness 标记 unavailable
    - 不得从 1w/1mo page_df 派生日行情
    - 所有周期返回 current/open/high/low/prev_close/change_pct/volume/amount

    Returns:
        quote dict；latest_daily_quote 缺失时返回 None
    """
    ldq = getattr(bars_result, "latest_daily_quote", None)
    if not ldq:
        return None

    _close = ldq.get("close")
    if _close is None:
        return None
    current_price = float(_close)

    # update_time：优先 last_live_bar_time，回退 last_persisted_bar_time
    update_time: Any
    if bars_result.last_live_bar_time is not None:
        update_time = bars_result.last_live_bar_time
    elif bars_result.last_persisted_bar_time is not None:
        update_time = bars_result.last_persisted_bar_time
    else:
        update_time = bars_result.as_of
    if hasattr(update_time, "isoformat"):
        update_time = update_time.isoformat()

    return {
        "current_price": round(current_price, 4),
        "open": round(float(ldq["open"]), 4) if ldq.get("open") is not None else None,
        "high": round(float(ldq["high"]), 4) if ldq.get("high") is not None else None,
        "low": round(float(ldq["low"]), 4) if ldq.get("low") is not None else None,
        "close": round(current_price, 4),
        "volume": round(float(ldq["volume"]), 2) if ldq.get("volume") is not None else None,
        "amount": round(float(ldq["amount"]), 2) if ldq.get("amount") is not None else None,
        "prev_close": round(float(ldq["prev_close"]), 4) if ldq.get("prev_close") is not None else None,
        "change_pct": round(float(ldq["change_pct"]), 2) if ldq.get("change_pct") is not None else None,
        "update_time": update_time,
        "is_realtime": bars_result.is_partial,
    }


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
        description="复权锚点 YYYY-MM-DD（None=最新；历史回算传业务日，禁止未来除权事件泄漏）",
    ),
    db: AsyncSession = Depends(get_db),
    # [V2.1] /chart-snapshot 属于个股详情行情，要求 market_screening（PRD §10.2）
    _ctx: CapabilityAccessContext = Depends(require_capability(MARKET_SCREENING)),
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

    # [P0-7] 派生 quote + freshness_state + market_session + 时间字段
    # 详情页不得同时以 /quote 和 /chart-snapshot 作为行情真源
    quote = _derive_quote_from_bars(snapshot_result.page_df, bars_result, timeframe)

    # market_session：当前市场阶段（freshness_state 与 expected_latest_bar_time 都需要）
    now_sh = now_shanghai()
    is_trading_day = await is_trading_day_async(db, now_sh.date())
    market_session = compute_market_session(now_sh, is_trading_day)

    # actual_latest_bar_time：实际最新 bar 时间（含 partial/realtime）
    actual_latest_bar_time_raw: datetime | None = None
    if bars_result.last_live_bar_time is not None:
        actual_latest_bar_time_raw = bars_result.last_live_bar_time
    elif bars_result.last_persisted_bar_time is not None:
        actual_latest_bar_time_raw = bars_result.last_persisted_bar_time
    # 统一为 tz-aware datetime 用于比较
    actual_for_compare: datetime | None = None
    if actual_latest_bar_time_raw is not None:
        if isinstance(actual_latest_bar_time_raw, pd.Timestamp):
            actual_for_compare = actual_latest_bar_time_raw.to_pydatetime()
        else:
            actual_for_compare = actual_latest_bar_time_raw
        if actual_for_compare.tzinfo is None:
            actual_for_compare = actual_for_compare.replace(tzinfo=_SHANGHAI_TZ)

    # expected_latest_bar_time：按周期和市场阶段计算（不再简单写成 now）
    last_completed_daily = await _call_expected_last_completed_daily_bar(db, now_sh)
    expected_dt = _compute_expected_latest_bar_time(
        now=now_sh,
        timeframe=timeframe,
        market_session=market_session,
        last_completed_daily_date=last_completed_daily,
    )

    # freshness_state：真实比较 actual vs expected
    freshness_state = _compute_freshness_state(
        bars_result=bars_result,
        page_df=snapshot_result.page_df,
        actual_latest_bar_time=actual_for_compare,
        expected_latest_bar_time=expected_dt,
        market_session=market_session,
        timeframe=timeframe,
    )

    # 序列化 actual/expected 为 ISO 字符串
    actual_latest_bar_time: Any = (
        actual_for_compare.isoformat() if actual_for_compare is not None else None
    )
    expected_latest_bar_time: Any = expected_dt.isoformat()

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
        # [P0-7] 详情页唯一行情真源：quote 从同一 snapshot 派生，禁止详情页调用 /quote
        "quote": quote,
        # [CHANGE-20260724-004] 暴露 latest_daily_quote 供前端/测试断言当日行情事实
        "latest_daily_quote": bars_result.latest_daily_quote,
        "market_session": market_session,
        "as_of": bars_result.as_of.isoformat() if hasattr(bars_result.as_of, "isoformat") else bars_result.as_of,
        "actual_latest_bar_time": actual_latest_bar_time,
        "expected_latest_bar_time": expected_latest_bar_time,
        "freshness_state": freshness_state,
        "data_source": bars_result.data_source,
        "is_partial": bars_result.is_partial,
        "degraded_reason": bars_result.degraded_reason,
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
