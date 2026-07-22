"""统一图表快照服务 — 个股详情和 Capture 共用。

[CP-V3-B] 抽取唯一 ChartSnapshotService，保证个股详情（chart-snapshot）和
Capture（capture snapshot）调用同一服务：一次展示周期 MDAS 读取 → 同一
DataFrame 生成 bars 和 indicators → render_frame。

单输入原子性（CP-16 + CP-V3-B）：
1. 服务调用 MarketDataAggregationService.get_bars() 获取展示窗口 DataFrame
   （仅此一次 MDAS 行情读取）。
2. 服务用同一 BarAggregationResult 通过 preloaded_display_bars 参数传给
   compute_all_indicators()，指标计算直接接收预加载 DataFrame，不再第二次
   调用 MDAS get_bars 获取展示周期行情。
3. 服务用 is_display_frame_match() 校验 bars vs indicators display_frame，
   返回 render_frame.matched。

Node Cluster 输入隔离：
- compute_all_indicators 内部 NodeClusterInputProvider 仍独立查询 completed qfq
  日线/15m（合同常量 250/4000，与页面 include_realtime/completed_only/bars 隔离）。
- 这不算"第二次行情读取"——Node 输入是不同参数（completed_only=True）的独立查询，
  保证 Node 计算不受展示窗口 partial bar 污染。

用法：
    from app.services.chart_snapshot_service import ChartSnapshotService

    result = await ChartSnapshotService.compute_bars_and_indicators(
        session, instrument_id,
        timeframe="1d", adj="qfq", bars=250,
        include_smc=True, include_realtime=True,
    )
    # result.bars_result, result.page_df, result.bars_display_frame,
    # result.indicators, result.render_frame, result.spec
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.indicator_display_frame import (
    DisplayWindowSpec,
    build_display_frame,
    is_display_frame_match,
)
from app.services.indicator_service import compute_all_indicators
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
)

logger = logging.getLogger("services.chart_snapshot_service")


@dataclass
class ChartSnapshotResult:
    """ChartSnapshotService.compute_bars_and_indicators 的返回结果。

    调用方（chart_snapshot API / capture API）基于此结果构建各自的响应结构。
    """

    bars_result: BarAggregationResult
    page_df: pd.DataFrame
    bars_display_frame: dict[str, Any]
    indicators: dict[str, Any]
    render_frame: dict[str, Any]
    spec: DisplayWindowSpec
    is_empty: bool = False
    completed_through_iso: str | None = field(default=None)


class ChartSnapshotService:
    """统一图表快照服务 — 个股详情和 Capture 共用同一入口。

    保证一次 MDAS 读取 → 同一 DataFrame 生成 bars 和 indicators → render_frame。
    禁止 compute_all_indicators 内部二次 MDAS 读取展示周期行情。
    """

    @classmethod
    async def compute_bars_and_indicators(
        cls,
        session: AsyncSession,
        instrument_id: uuid.UUID,
        *,
        timeframe: str,
        adj: str,
        bars: int = 250,
        include_smc: bool = False,
        include_realtime: bool = True,
        completed_only: bool = False,
        adjustment_as_of: date | None = None,
    ) -> ChartSnapshotResult:
        """一次 MDAS 读取 → 同一 DataFrame 生成 bars 和 indicators → render_frame。

        Args:
            session: 异步 DB 会话
            instrument_id: 标的 UUID
            timeframe: 展示周期 1d | 15m | 1h | 1w | 1mo
            adj: 复权方式 qfq | none
            bars: 返回最近 N 根 bar（同时作为展示窗口大小）
            include_smc: 是否计算 SMC 指标
            include_realtime: 是否包含实时 partial bar
            completed_only: 是否只返回已完成 bar（True 时强制 include_realtime=False）
            adjustment_as_of: 复权锚点 YYYY-MM-DD（默认 None=最新）

        Returns:
            ChartSnapshotResult（含 bars_result, page_df, display_frame,
            indicators, render_frame, spec）
        """
        # 1. 构建 DisplayWindowSpec（与 bars/indicators API 共用同一 Spec）
        spec = DisplayWindowSpec(
            instrument_id=str(instrument_id),
            timeframe=timeframe,
            adj=adj,
            requested_count=bars,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=(str(adjustment_as_of) if adjustment_as_of else None),
        )

        # 2. 一次 MDAS get_bars 获取展示窗口 DataFrame（单输入原子性）
        mdas = MarketDataAggregationService()
        bars_result = await mdas.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=adjustment_as_of,
        )

        df = bars_result.bars

        # completed_through 转换为 ISO 字符串（供 display_frame 构建使用）
        _completed_through = bars_result.completed_through
        if _completed_through is not None and isinstance(_completed_through, pd.Timestamp):
            if _completed_through.tzinfo is None:
                _completed_through = _completed_through.tz_localize("Asia/Shanghai")
            _completed_through = _completed_through.to_pydatetime()
        completed_through_iso = (
            _completed_through.isoformat() if _completed_through else None
        )

        # 3. 服务端分页：取末尾 `bars` 根
        if df.empty:
            # 空数据：构建空 display_frame，返回空 indicators
            empty_display_frame = build_display_frame(
                instrument_id=str(instrument_id),
                timeframe=timeframe,
                adj=adj,
                display_df=df,
                completed_through=completed_through_iso,
                spec=spec,
                is_partial=bars_result.is_partial,
            )
            empty_indicators: dict[str, Any] = {
                "layers": [],
                "data": {},
                "errors": {"_chart_snapshot": "no bars data"},
                "timeframe": timeframe,
                "source_bar_times": [],
                "source_bar_hash": "",
                "display_frame": empty_display_frame,
            }
            empty_render_frame: dict[str, Any] = {
                "matched": True,
                "bars_hash": "",
                "indicators_hash": "",
                "bars_count": 0,
                "indicators_count": 0,
                "bars_first_time": None,
                "indicators_first_time": None,
                "bars_last_time": None,
                "indicators_last_time": None,
                "bars_adjustment_as_of": spec.adjustment_as_of,
                "indicators_adjustment_as_of": spec.adjustment_as_of,
            }
            return ChartSnapshotResult(
                bars_result=bars_result,
                page_df=df,
                bars_display_frame=empty_display_frame,
                indicators=empty_indicators,
                render_frame=empty_render_frame,
                spec=spec,
                is_empty=True,
                completed_through_iso=completed_through_iso,
            )

        # 非空数据：截取末尾 `bars` 根
        total = len(df)
        end_idx = total
        start_idx = max(0, end_idx - bars)
        page_df = df.iloc[start_idx:end_idx]

        # 4. 构建 bars display_frame
        bars_display_frame = build_display_frame(
            instrument_id=str(instrument_id),
            timeframe=timeframe,
            adj=adj,
            display_df=page_df,
            completed_through=completed_through_iso,
            spec=spec,
            is_partial=bars_result.is_partial,
        )

        # 5. 调用 compute_all_indicators — 传入预加载的 bars_result（单输入原子性）
        #    [CP-16/CP-V3-B] 不再依赖 Redis 缓存间接同步，直接将同一 DataFrame
        #    传给指标计算。一个请求内，展示周期 MDAS get_bars 调用次数 = 1。
        #    compute_all_indicators 内部 Node Cluster 日线/15m 仍独立查询
        #    （completed qfq 合同，由 NodeClusterInputProvider 提供）。
        indicators = await compute_all_indicators(
            session=session,
            instrument_id=instrument_id,
            timeframe=timeframe,
            adj=adj,
            bars=bars,
            include_smc=include_smc,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=adjustment_as_of,
            preloaded_display_bars=bars_result,
        )

        # 6. 校验 bars vs indicators display_frame（render_frame.matched）
        indicators_display_frame = indicators.get("display_frame")
        render_matched = is_display_frame_match(bars_display_frame, indicators_display_frame)
        if not render_matched:
            logger.warning(
                "[ChartSnapshotService] render_frame mismatch instrument_id=%s "
                "timeframe=%s bars_hash=%s indicators_hash=%s "
                "bars_count=%s indicators_count=%s",
                instrument_id, timeframe,
                bars_display_frame.get("display_hash"),
                (indicators_display_frame or {}).get("display_hash"),
                bars_display_frame.get("actual_count"),
                (indicators_display_frame or {}).get("actual_count"),
            )

        render_frame: dict[str, Any] = {
            "matched": render_matched,
            "bars_hash": bars_display_frame.get("display_hash") or "",
            "indicators_hash": (indicators_display_frame or {}).get("display_hash") or "",
            "bars_count": bars_display_frame.get("actual_count"),
            "indicators_count": (indicators_display_frame or {}).get("actual_count"),
            "bars_first_time": bars_display_frame.get("first_time"),
            "indicators_first_time": (indicators_display_frame or {}).get("first_time"),
            "bars_last_time": bars_display_frame.get("last_time"),
            "indicators_last_time": (indicators_display_frame or {}).get("last_time"),
            "bars_adjustment_as_of": bars_display_frame.get("adjustment_as_of"),
            "indicators_adjustment_as_of": (indicators_display_frame or {}).get("adjustment_as_of"),
        }

        return ChartSnapshotResult(
            bars_result=bars_result,
            page_df=page_df,
            bars_display_frame=bars_display_frame,
            indicators=indicators,
            render_frame=render_frame,
            spec=spec,
            is_empty=False,
            completed_through_iso=completed_through_iso,
        )


if __name__ == "__main__":
    # 自测入口：验证模块加载和类定义
    import inspect

    assert hasattr(ChartSnapshotService, "compute_bars_and_indicators"), "应有 compute_bars_and_indicators 方法"
    sig = inspect.signature(ChartSnapshotService.compute_bars_and_indicators)
    params = list(sig.parameters.keys())
    for required in ("session", "instrument_id", "timeframe", "adj", "bars",
                     "include_smc", "include_realtime", "completed_only",
                     "adjustment_as_of"):
        assert required in params, f"缺少参数: {required}"
    print("ChartSnapshotService 模块加载 OK")
    print(f"compute_bars_and_indicators 参数: {params}")
