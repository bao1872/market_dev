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
- **但读取模式必须继承本次请求语义**：历史请求时 Node 输入走 ``HISTORICAL_DB``
  （daily + 15m 全 DB_ONLY），否则历史响应会混入当前 provider 15m。

Source policy（[PANJI-INTRADAY-DIRECT-SOURCE]）：
- 本层调用 MDAS ``resolve_request_source_policy``（与 /bars 共用的唯一判定点）：
  - live 15m/1h → ``PROVIDER_DIRECT``（实时分钟行情归 Provider）
  - live 1d / 1w / 1mo / 1m → ``HYBRID``（行为与既有完全一致）
  - **历史/PIT（显式 ``adjustment_as_of`` 或 ``end_date`` 早于今天）→ 全周期 ``DB_ONLY``**
    —— 历史分钟归 DB、历史日线也不得回补 provider / 不得合并今日 partial daily，
    否则历史截图/重放会拉当前行情，引入未来数据
- 判定先看 historical 再看 timeframe：historical 没有周期例外（zero-network 合同）。

[P1-chart-snapshot-historical] 请求语义只判定一次（``is_historical_market_data_request``），
然后**同时**驱动两个消费者：展示 bars 的 source policy 与
``compute_all_indicators(market_data_mode=...)``。只修展示半边是不够的 ——
指标内部的日线/分钟/SMC 预热/Node 输入若仍按 live 取数，同一个历史响应里就会
出现两个时间截面（表面算得出结果、不报错）。

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
from app.services.indicator_service import (
    IndicatorMarketDataMode,
    compute_all_indicators,
)
from app.services.market_data_aggregation_service import (
    BarAggregationResult,
    MarketDataAggregationService,
    MarketDataSourcePolicy,
    is_historical_market_data_request,
    resolve_request_source_policy,
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
        #    [PANJI-INTRADAY-DIRECT-SOURCE] source policy 由 MDAS 唯一判定点派生：
        #    live 15m/1h → provider_direct；历史（显式 adjustment_as_of）15m/1h → db_only；
        #    1d/1w/1mo/1m → hybrid（行为不变）。不依赖 MDAS 默认值，也不在本层另写判定。
        #    注意：不额外传 limit —— 保持既有「MDAS 取窗口 → 本层 tail(bars) 分页」语义，
        #    避免顺手改变 1d 展示窗口/hash/coverage 的既有合同。
        #
        #    [P1-chart-snapshot-historical] 请求语义（历史 vs 实时）必须只判定一次，
        #    然后**同时**驱动展示 bars 与 compute_all_indicators 的取数模式：
        #    否则同一个历史响应里会出现「K 线来自 DB + Node/指标来自当前 provider」的
        #    时间截面混用（不报错、静默错）。
        historical_request = is_historical_market_data_request(
            adjustment_as_of=adjustment_as_of,
        )
        market_data_mode = (
            IndicatorMarketDataMode.HISTORICAL_DB
            if historical_request
            else IndicatorMarketDataMode.LIVE
        )
        source_policy = resolve_request_source_policy(
            timeframe, adjustment_as_of=adjustment_as_of,
        )
        mdas = MarketDataAggregationService()
        bars_result = await mdas.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=include_realtime,
            completed_only=completed_only,
            adjustment_as_of=adjustment_as_of,
            source_policy=source_policy,
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
        #    [P1-chart-snapshot-historical] 但必须把**同一份请求语义**传下去：
        #    历史请求时指标内部（日线 / 分钟 / 周月 / SMC 预热 / Node 输入）全部 DB_ONLY，
        #    保证整条 historical chart-snapshot 是 PIT zero-network。
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
            market_data_mode=market_data_mode,
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

    # [PANJI-INTRADAY-DIRECT-SOURCE] source policy 判定（live vs 历史/PIT）
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    assert resolve_request_source_policy(
        "1h", adjustment_as_of=None,
    ) is MarketDataSourcePolicy.PROVIDER_DIRECT
    # 历史/PIT（显式 as-of）→ DB_ONLY：历史分钟行情归 DB，禁止联网
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=date(2026, 7, 1),
    ) is MarketDataSourcePolicy.DB_ONLY
    assert resolve_request_source_policy(
        "1h", adjustment_as_of=date(2026, 7, 1),
    ) is MarketDataSourcePolicy.DB_ONLY
    # 回溯窗口（end_date 早于今天）同样落到 DB_ONLY
    assert resolve_request_source_policy(
        "15m", adjustment_as_of=None, end_date=date(2020, 1, 2),
    ) is MarketDataSourcePolicy.DB_ONLY
    for _tf in ("1d", "1w", "1mo", "1m"):
        assert resolve_request_source_policy(
            _tf, adjustment_as_of=None,
        ) is MarketDataSourcePolicy.HYBRID, _tf
        # [P1-db-only-zero-network] historical 优先于 timeframe：
        # 非分钟周期在历史请求下同样是 DB_ONLY（否则 HYBRID 的 need_tail 回补
        # 与今日 partial daily 合并仍会触网）。
        assert resolve_request_source_policy(
            _tf, adjustment_as_of=date(2026, 7, 1),
        ) is MarketDataSourcePolicy.DB_ONLY, _tf
        assert resolve_request_source_policy(
            _tf, adjustment_as_of=None, end_date=date(2020, 1, 2),
        ) is MarketDataSourcePolicy.DB_ONLY, _tf
    print(
        "chart source policy ✓ (live: 15m/1h=provider_direct, 其余=hybrid; "
        "历史: 全周期=db_only)"
    )

    # [P1-chart-snapshot-historical] 请求语义 → 指标读取模式（只判定一次，两个消费者共用）
    from app.services.indicator_service import (
        _indicator_node_source_mode,
        _indicator_source_policy,
    )
    from app.services.node_cluster_input_provider import NodeClusterSourceMode

    assert is_historical_market_data_request(adjustment_as_of=None) is False
    assert is_historical_market_data_request(
        adjustment_as_of=date(2026, 7, 1),
    ) is True
    assert _indicator_source_policy("1d", IndicatorMarketDataMode.LIVE) \
        is MarketDataSourcePolicy.HYBRID
    assert _indicator_source_policy("15m", IndicatorMarketDataMode.LIVE) \
        is MarketDataSourcePolicy.PROVIDER_DIRECT
    for _tf in ("1d", "15m", "1h", "1w", "1mo", "1m"):
        assert _indicator_source_policy(
            _tf, IndicatorMarketDataMode.HISTORICAL_DB,
        ) is MarketDataSourcePolicy.DB_ONLY, _tf
    assert _indicator_node_source_mode(IndicatorMarketDataMode.LIVE) \
        is NodeClusterSourceMode.LIVE_DIRECT
    assert _indicator_node_source_mode(IndicatorMarketDataMode.HISTORICAL_DB) \
        is NodeClusterSourceMode.HISTORICAL_DB
    print(
        "indicator market_data_mode ✓ (HISTORICAL_DB: 全周期 db_only + "
        "Node historical_db)"
    )
