"""MarketFeatureComputationService — 盘后统一特征计算服务 (compute-once)。

[CHANGE-20260724-002 Phase 3] 每股只读取一次 bars、计算一次 DSA/SMC/Node，
结果同时供 StrategyResult 和 snapshot 使用，消除重复计算。

核心原则：
- DSA bundle 只算一次 → 传给 structural adapter (precomputed_dsa_bundle)
- SMC DTO 只算一次 → 传给 build_smc_daily_freshness
- Node Cluster 只算一次 → 传给 structural adapter (precomputed_node_cluster)
- 批量查询 StrategyEvent → 禁止 N+1

调用方（盘后编排 Phase 5）：
- 用 dsa_bundle 构建 StrategyResult
- 用 bars_daily + precomputed_dsa_bundle 调 compute_feature_snapshot_for_date
- 用 event_freshness_payload 写 v5 snapshot

模块自测：
    python -m app.services.market_feature_computation_service
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.strategy_event_repository import batch_latest_events
from app.services.canonical_computation_service import CanonicalComputationService
from app.services.event_freshness_service import (
    aggregate_latest_monitor_events,
    build_empty_event_freshness_payload,
    build_smc_daily_freshness,
)
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.node_cluster_engine import NodeClusterProfileResult
from app.services.node_cluster_input_provider import NodeClusterInputProvider

logger = logging.getLogger(__name__)

# 盘后日线回看天数（与 strategy_batch_service._STRATEGY_BATCH_DAILY_LOOKBACK_DAYS 一致）
_DAILY_LOOKBACK_DAYS = 5000

# 默认监控事件类型（用于批量查询 monitor interaction freshness）
DEFAULT_MONITOR_EVENT_TYPES = [
    "node_cluster_touch",
    "bb_upper_touch",
    "bb_mid_touch",
    "bb_lower_touch",
]

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass
class MarketFeatureResult:
    """单股 compute-once 计算结果。

    所有预计算结果供调用方复用，避免重复计算。
    """

    instrument_id: UUID
    trade_date: date
    # 预计算 bars（供 snapshot 复用）
    bars_daily: pd.DataFrame | None
    # 诊断 hash（供 canonical result_hash 一致性）
    primary_source_bar_hash: str | None
    primary_adj_factor_hash: str | None
    # 预计算算法结果
    dsa_bundle: dict[str, Any] | None
    smc_dto: dict[str, Any] | None
    node_cluster_profile: NodeClusterProfileResult | None
    node_availability: str
    node_degraded_reason: str | None
    # event freshness
    smc_daily_freshness: dict[str, Any]
    monitor_event_freshness: dict[str, dict[str, Any]]
    event_freshness_payload: dict[str, Any]


class MarketFeatureComputationService:
    """盘后统一特征计算服务 — compute-once per stock。

    设计：
    - 不复制现有 persistence 逻辑
    - 通过 CanonicalComputationService 调度已注册算法
    - 通过 NodeClusterInputProvider 获取 Node 输入（四链一致）
    - 通过 batch_latest_events 批量查询事件（禁止 N+1）
    """

    @classmethod
    async def compute_features_for_instrument(
        cls,
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
        *,
        monitoring_event_types: list[str] | None = None,
    ) -> MarketFeatureResult:
        """单股 compute-once 计算。

        Args:
            session: 异步 DB 会话
            instrument_id: 股票 ID
            trade_date: 业务交易日
            monitoring_event_types: 监控事件类型列表（None 用默认 4 类）

        Returns:
            MarketFeatureResult（含所有预计算结果）
        """
        # 1. 读取日线 bars（via MDAS 唯一出口）
        bars_daily, primary_source_bar_hash, primary_adj_factor_hash = (
            await cls._read_daily_bars(session, instrument_id, trade_date)
        )

        if bars_daily is None or bars_daily.empty:
            logger.warning(
                "无日线数据，返回空结果: instrument_id=%s trade_date=%s",
                instrument_id, trade_date,
            )
            return cls._build_empty_result(
                instrument_id, trade_date,
                primary_source_bar_hash, primary_adj_factor_hash,
            )

        # 2. 计算 DSA bundle once
        dsa_bundle = await cls._compute_dsa(
            session, instrument_id, trade_date,
            bars_daily, primary_source_bar_hash, primary_adj_factor_hash,
        )

        # 3. 计算 SMC DTO once
        smc_dto = await cls._compute_smc(
            session, instrument_id, trade_date,
            bars_daily, primary_source_bar_hash, primary_adj_factor_hash,
        )

        # 4. 获取 Node Cluster input + 计算
        node_profile, node_avail, node_reason = await cls._compute_node_cluster(
            session, instrument_id, trade_date,
        )

        # 5. 构建 SMC daily freshness（从预计算 SMC DTO，不调 kernel）
        current_index = len(bars_daily) - 1
        smc_daily_freshness = build_smc_daily_freshness(smc_dto, bars_daily, current_index)

        # 6. 批量查询 monitor events + 聚合
        monitor_event_freshness = await cls._build_monitor_event_freshness(
            session, instrument_id, trade_date,
            monitoring_event_types, bars_daily,
        )

        # 7. 组装 event_freshness_payload
        event_freshness_payload = build_empty_event_freshness_payload(as_of=trade_date)
        event_freshness_payload["daily_structure"]["smc"] = smc_daily_freshness
        event_freshness_payload["monitor_interaction"] = monitor_event_freshness

        return MarketFeatureResult(
            instrument_id=instrument_id,
            trade_date=trade_date,
            bars_daily=bars_daily,
            primary_source_bar_hash=primary_source_bar_hash,
            primary_adj_factor_hash=primary_adj_factor_hash,
            dsa_bundle=dsa_bundle,
            smc_dto=smc_dto,
            node_cluster_profile=node_profile,
            node_availability=node_avail,
            node_degraded_reason=node_reason,
            smc_daily_freshness=smc_daily_freshness,
            monitor_event_freshness=monitor_event_freshness,
            event_freshness_payload=event_freshness_payload,
        )

    # ---- 内部方法 ----

    @staticmethod
    async def _read_daily_bars(
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
    ) -> tuple[pd.DataFrame | None, str | None, str | None]:
        """通过 MDAS 读取日线 bars（唯一出口）。"""
        start_date = trade_date - timedelta(days=_DAILY_LOOKBACK_DAYS)
        try:
            bars_result = await MarketDataAggregationService().get_bars(
                session=session,
                instrument_id=instrument_id,
                timeframe="1d",
                adj="qfq",
                include_realtime=False,
                completed_only=True,
                start_date=start_date,
                end_date=trade_date,
                adjustment_as_of=trade_date,
            )
            return (
                bars_result.bars,
                bars_result.source_bar_hash,
                bars_result.adj_factor_hash,
            )
        except Exception as exc:
            logger.warning("MDAS 读取日线失败 instrument_id=%s: %s", instrument_id, exc)
            return None, None, None

    @staticmethod
    async def _compute_dsa(
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
        bars: pd.DataFrame,
        source_bar_hash: str | None,
        adj_factor_hash: str | None,
    ) -> dict[str, Any] | None:
        """计算 DSA bundle（一次）。"""
        try:
            canonical = await CanonicalComputationService.compute(
                algorithm_id="dsa",
                instrument_id=instrument_id,
                as_of=trade_date.isoformat(),
                source_bar_hash=source_bar_hash,
                adj_factor_hash=adj_factor_hash,
                bars=bars,
            )
            return canonical.payload
        except Exception as exc:
            logger.warning("DSA 计算失败 instrument_id=%s: %s", instrument_id, exc)
            return None

    @staticmethod
    async def _compute_smc(
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
        bars: pd.DataFrame,
        source_bar_hash: str | None,
        adj_factor_hash: str | None,
    ) -> dict[str, Any] | None:
        """计算 SMC DTO（一次）。"""
        try:
            canonical = await CanonicalComputationService.compute(
                algorithm_id="smc",
                instrument_id=instrument_id,
                as_of=trade_date.isoformat(),
                source_bar_hash=source_bar_hash,
                adj_factor_hash=adj_factor_hash,
                bars=bars,
                display_bars=250,
            )
            return canonical.payload
        except Exception as exc:
            logger.warning("SMC 计算失败 instrument_id=%s: %s", instrument_id, exc)
            return None

    @staticmethod
    async def _compute_node_cluster(
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
    ) -> tuple[NodeClusterProfileResult | None, str, str | None]:
        """获取 Node Cluster input + 计算（一次）。

        Returns:
            (profile, availability, degraded_reason)
        """
        try:
            node_input = await NodeClusterInputProvider.get_inputs(
                session, instrument_id,
                adjustment_as_of=trade_date, end_date=trade_date,
            )
            if node_input.availability == "unavailable":
                return None, "unavailable", node_input.degraded_reason

            canonical = await CanonicalComputationService.compute(
                algorithm_id="node_cluster",
                instrument_id=instrument_id,
                as_of=trade_date.isoformat(),
                source_bar_hash=node_input.daily_source_hash,
                adj_factor_hash=node_input.daily_adj_factor_hash,
                daily_bars=node_input.daily_bars,
                bars_15m=node_input.bars_15m,
                adjustment_as_of=trade_date.isoformat(),
            )
            profile = canonical.payload
            if profile is None or not profile.profile_rows:
                return None, "unavailable", "PROFILE_EMPTY"
            return profile, node_input.availability, node_input.degraded_reason
        except Exception as exc:
            logger.warning("Node Cluster 计算失败 instrument_id=%s: %s", instrument_id, exc)
            return None, "unavailable", f"COMPUTE_FAILED: {exc}"

    @staticmethod
    async def _build_monitor_event_freshness(
        session: AsyncSession,
        instrument_id: UUID,
        trade_date: date,
        event_types: list[str] | None,
        bars_daily: pd.DataFrame,
    ) -> dict[str, dict[str, Any]]:
        """批量查询 monitor events + 聚合为 freshness。"""
        types = event_types or DEFAULT_MONITOR_EVENT_TYPES
        end_time = datetime.combine(trade_date, time(23, 59, 59), tzinfo=_SHANGHAI)

        raw_events = await batch_latest_events(
            session,
            instrument_ids=[instrument_id],
            event_types=types,
            end_time=end_time,
        )

        # 从 bars_daily 构建交易日历
        trading_calendar = _build_trading_calendar(bars_daily)

        return aggregate_latest_monitor_events(
            raw_events,
            as_of=trade_date,
            trading_calendar=trading_calendar,
        )

    @staticmethod
    def _build_empty_result(
        instrument_id: UUID,
        trade_date: date,
        source_bar_hash: str | None,
        adj_factor_hash: str | None,
    ) -> MarketFeatureResult:
        """无数据时返回空结果（所有算法结果为 None，freshness 为空）。"""
        empty_smc = build_smc_daily_freshness(None, None, 0)
        empty_payload = build_empty_event_freshness_payload(as_of=trade_date)
        empty_payload["daily_structure"]["smc"] = empty_smc
        return MarketFeatureResult(
            instrument_id=instrument_id,
            trade_date=trade_date,
            bars_daily=None,
            primary_source_bar_hash=source_bar_hash,
            primary_adj_factor_hash=adj_factor_hash,
            dsa_bundle=None,
            smc_dto=None,
            node_cluster_profile=None,
            node_availability="unavailable",
            node_degraded_reason="NO_DAILY_BARS",
            smc_daily_freshness=empty_smc,
            monitor_event_freshness={},
            event_freshness_payload=empty_payload,
        )


def _build_trading_calendar(bars: pd.DataFrame) -> list[date]:
    """从日线 bars 的 DatetimeIndex 提取交易日列表。"""
    if bars is None or bars.empty:
        return []
    try:
        return [ts.date() for ts in bars.index]
    except (AttributeError, TypeError):
        return []


if __name__ == "__main__":
    # 自测入口：验证类与方法签名（无副作用，不连接数据库）
    import inspect

    assert MarketFeatureComputationService is not None
    print(f"MarketFeatureComputationService: {MarketFeatureComputationService} ✓")

    methods = ["compute_features_for_instrument"]
    for m in methods:
        assert hasattr(MarketFeatureComputationService, m), f"缺少方法: {m}"
        assert callable(getattr(MarketFeatureComputationService, m)), f"方法不可调用: {m}"
        print(f"  {m} ✓")

    # 验证 MarketFeatureResult 字段
    sig = inspect.signature(MarketFeatureComputationService.compute_features_for_instrument)
    params = list(sig.parameters.keys())
    assert "instrument_id" in params
    assert "trade_date" in params
    print(f"compute_features_for_instrument params: {params} ✓")

    # 验证 DEFAULT_MONITOR_EVENT_TYPES
    assert "node_cluster_touch" in DEFAULT_MONITOR_EVENT_TYPES
    assert "bb_upper_touch" in DEFAULT_MONITOR_EVENT_TYPES
    print(f"DEFAULT_MONITOR_EVENT_TYPES: {DEFAULT_MONITOR_EVENT_TYPES} ✓")

    # 验证 _build_trading_calendar
    import pandas as pd
    idx = pd.DatetimeIndex(["2026-07-21", "2026-07-22", "2026-07-23"])
    df_test = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
    cal = _build_trading_calendar(df_test)
    assert len(cal) == 3
    print(f"_build_trading_calendar: {cal} ✓")

    print("OK")
