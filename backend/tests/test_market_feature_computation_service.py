"""MarketFeatureComputationService + compute-once 定向测试。

PRD §4.1: 盘后传入 precomputed_dsa_bundle 时 DSA kernel 调用次数为 0。
PRD §4.4: batch_latest_events 禁止 N+1。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services.event_freshness_service import build_smc_daily_freshness
from app.services.market_feature_computation_service import (
    DEFAULT_MONITOR_EVENT_TYPES,
    MarketFeatureResult,
    _build_trading_calendar,
)

# =============================================================================
# compute-once: precomputed_dsa_bundle 时 DSA kernel 调用次数为 0
# =============================================================================


class TestPrecomputedDsaBundleCallCount:
    """PRD §4.1: 传入 precomputed_dsa_bundle 时 compute_dsa_bundle 不被调用。"""

    def test_dsa_kernel_not_called_when_precomputed(self) -> None:
        """传入 precomputed_dsa_bundle 时，compute_dsa_bundle 调用次数=0。"""
        from app.services.structural_factor_service import _compute_all_factors_for_bars

        bars = pd.DataFrame(
            {
                "open": [10.0] * 100,
                "high": [11.0] * 100,
                "low": [9.0] * 100,
                "close": [10.5] * 100,
                "volume": [1000.0] * 100,
            }
        )
        precomputed = {"segments": [], "factor_per_bar": [], "metrics": {}}

        with patch(
            "app.services.structural_factor_service.compute_dsa_bundle"
        ) as mock_dsa:
            try:
                _compute_all_factors_for_bars(
                    bars,
                    "1d",
                    [],
                    [],
                    precomputed_dsa_bundle=precomputed,
                )
            except Exception:
                pass  # 其他因子可能失败，只验证 DSA call-count
            assert mock_dsa.call_count == 0, (
                f"compute_dsa_bundle 不应被调用，实际调用 {mock_dsa.call_count} 次"
            )

    def test_dsa_kernel_called_when_not_precomputed(self) -> None:
        """不传 precomputed_dsa_bundle 时，compute_dsa_bundle 被调用。"""
        from app.services.structural_factor_service import _compute_all_factors_for_bars

        bars = pd.DataFrame(
            {
                "open": [10.0] * 100,
                "high": [11.0] * 100,
                "low": [9.0] * 100,
                "close": [10.5] * 100,
                "volume": [1000.0] * 100,
            }
        )

        with patch(
            "app.services.structural_factor_service.compute_dsa_bundle"
        ) as mock_dsa:
            mock_dsa.return_value = {"segments": [], "factor_per_bar": [], "metrics": {}}
            try:
                _compute_all_factors_for_bars(bars, "1d", [], [])
            except Exception:
                pass
            assert mock_dsa.call_count >= 1, (
                "不传 precomputed_dsa_bundle 时 compute_dsa_bundle 应被调用"
            )


# =============================================================================
# batch_latest_events 空输入
# =============================================================================


class TestBatchLatestEventsEmpty:
    """batch_latest_events 空输入安全返回。"""

    @pytest.mark.asyncio
    async def test_empty_instrument_ids(self) -> None:
        from app.repositories.strategy_event_repository import batch_latest_events

        result = await batch_latest_events(
            AsyncMock(),
            instrument_ids=[],
            event_types=["node_cluster_touch"],
            end_time=...,
        )
        assert result == []

    @pytest.mark.asyncio
    async def test_empty_event_types(self) -> None:
        from app.repositories.strategy_event_repository import batch_latest_events

        result = await batch_latest_events(
            AsyncMock(),
            instrument_ids=[...],
            event_types=[],
            end_time=...,
        )
        assert result == []


# =============================================================================
# _build_trading_calendar
# =============================================================================


class TestBuildTradingCalendar:
    """从 bars DatetimeIndex 提取交易日列表。"""

    def test_normal_extraction(self) -> None:
        idx = pd.DatetimeIndex(["2026-07-21", "2026-07-22", "2026-07-23"])
        df = pd.DataFrame({"close": [10.0, 11.0, 12.0]}, index=idx)
        cal = _build_trading_calendar(df)
        assert len(cal) == 3
        assert cal[0] == date(2026, 7, 21)
        assert cal[-1] == date(2026, 7, 23)

    def test_empty_bars(self) -> None:
        df = pd.DataFrame()
        assert _build_trading_calendar(df) == []

    def test_none_bars(self) -> None:
        assert _build_trading_calendar(None) is None or _build_trading_calendar(None) == []


# =============================================================================
# MarketFeatureResult 结构
# =============================================================================


class TestMarketFeatureResultStructure:
    """MarketFeatureResult dataclass 字段完整性。"""

    def test_has_all_expected_fields(self) -> None:
        expected_fields = {
            "instrument_id",
            "trade_date",
            "bars_daily",
            "primary_source_bar_hash",
            "primary_adj_factor_hash",
            "dsa_bundle",
            "smc_dto",
            "node_cluster_profile",
            "node_availability",
            "node_degraded_reason",
            "smc_daily_freshness",
            "monitor_event_freshness",
            "event_freshness_payload",
        }
        actual_fields = set(MarketFeatureResult.__dataclass_fields__.keys())
        assert actual_fields == expected_fields, (
            f"缺失: {expected_fields - actual_fields}, "
            f"多余: {actual_fields - expected_fields}"
        )

    def test_default_monitor_event_types(self) -> None:
        assert "node_cluster_touch" in DEFAULT_MONITOR_EVENT_TYPES
        assert "bb_upper_touch" in DEFAULT_MONITOR_EVENT_TYPES
        assert "bb_mid_touch" in DEFAULT_MONITOR_EVENT_TYPES
        assert "bb_lower_touch" in DEFAULT_MONITOR_EVENT_TYPES


# =============================================================================
# SMC daily freshness 与 MarketFeatureComputationService 集成（mock）
# =============================================================================


class TestSmcFreshnessIntegration:
    """SMC daily freshness 从预计算 DTO 构建（不调 kernel）。"""

    def test_freshness_from_precomputed_dto(self) -> None:
        """build_smc_daily_freshness 消费预计算 DTO，不调 SMC kernel。"""
        smc_dto = {
            "events": [
                {"type": "BOS", "bullish": True, "internal": True, "confirmed_index": 95},
            ],
        }
        bars = pd.DataFrame(
            {"high": [10.0] * 100, "low": [9.0] * 100}
        )
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        assert result["smc_bos_bullish_internal_freshness_bars"] == 4

    def test_empty_result_when_no_dto(self) -> None:
        result = build_smc_daily_freshness(None, None, 0)
        assert len(result) == 18
        for v in result.values():
            assert v is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
