"""Event Freshness Service 纯函数测试。

验证 PRD §7 事件新鲜度合同的纯函数逻辑：
- freshness_from_bar_index: bar 级 freshness 公式
- freshness_from_trading_day: 交易日级 freshness 公式
- build_smc_daily_freshness: 18 项 SMC 日线结构新鲜度
- build_observed/never_observed/unavailable: state 构造器
- aggregate_latest_monitor_events: 批量事件聚合映射
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from app.services.event_freshness_service import (
    FreshnessState,
    UNIT_COMPLETED_DAILY_BARS,
    UNIT_TRADING_DAYS,
    aggregate_latest_monitor_events,
    build_empty_event_freshness_payload,
    build_never_observed,
    build_observed,
    build_smc_daily_freshness,
    build_unavailable,
    freshness_from_bar_index,
    freshness_from_trading_day,
)


# =============================================================================
# §3.1 bar freshness
# =============================================================================


class TestFreshnessFromBarIndex:
    """bar 级 freshness 公式（PRD §7.2）。"""

    def test_event_on_current_bar_is_zero(self) -> None:
        assert freshness_from_bar_index(99, 99) == 0

    def test_one_bar_after_event(self) -> None:
        assert freshness_from_bar_index(99, 98) == 1

    def test_nine_bars_after_event(self) -> None:
        assert freshness_from_bar_index(99, 90) == 9

    def test_none_event_index_is_never_observed(self) -> None:
        assert freshness_from_bar_index(99, None) is None

    def test_event_after_current_raises(self) -> None:
        with pytest.raises(ValueError, match="data_quality_error"):
            freshness_from_bar_index(99, 100)


# =============================================================================
# §3.2 trading day freshness
# =============================================================================


class TestFreshnessFromTradingDay:
    """交易日级 freshness 公式（PRD §7.2）。"""

    @pytest.fixture
    def calendar(self) -> list[date]:
        return [
            date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22),
            date(2026, 7, 23), date(2026, 7, 24),
        ]

    def test_same_day_is_zero(self, calendar: list[date]) -> None:
        assert freshness_from_trading_day(
            date(2026, 7, 23), date(2026, 7, 23), calendar,
        ) == 0

    def test_one_day_before(self, calendar: list[date]) -> None:
        assert freshness_from_trading_day(
            date(2026, 7, 23), date(2026, 7, 22), calendar,
        ) == 1

    def test_none_event_is_never_observed(self, calendar: list[date]) -> None:
        assert freshness_from_trading_day(
            date(2026, 7, 23), None, calendar,
        ) is None

    def test_event_after_as_of_raises(self, calendar: list[date]) -> None:
        with pytest.raises(ValueError, match="data_quality_error"):
            freshness_from_trading_day(
                date(2026, 7, 23), date(2026, 7, 24), calendar,
            )

    def test_empty_calendar_raises(self) -> None:
        with pytest.raises(ValueError, match="unavailable"):
            freshness_from_trading_day(
                date(2026, 7, 23), date(2026, 7, 22), [],
            )


# =============================================================================
# State 构造器
# =============================================================================


class TestStateBuilders:
    """FreshnessState 构造器（PRD §7.1）。"""

    def test_observed_has_value(self) -> None:
        item = build_observed("smc_bos_bullish_internal", 5, direction="bullish")
        assert item.state == FreshnessState.OBSERVED
        assert item.value == 5
        d = item.to_dict()
        assert d["value"] == 5
        assert d["state"] == "observed"
        assert d["direction"] == "bullish"

    def test_never_observed_has_null_value(self) -> None:
        item = build_never_observed("smc_bos_bullish_internal")
        assert item.state == FreshnessState.NEVER_OBSERVED
        assert item.value is None
        assert item.to_dict()["state"] == "never_observed"

    def test_unavailable_has_reason(self) -> None:
        item = build_unavailable("smc_bos_bullish_internal", reason="smc_compute_failed")
        assert item.state == FreshnessState.UNAVAILABLE
        assert item.value is None
        d = item.to_dict()
        assert d["state"] == "unavailable"
        assert d["reason"] == "smc_compute_failed"

    def test_unavailable_distinct_from_never_observed(self) -> None:
        ne = build_never_observed("test")
        un = build_unavailable("test", reason="fail")
        assert ne.state != un.state
        assert ne.to_dict().get("reason") is None
        assert un.to_dict().get("reason") == "fail"


# =============================================================================
# §4 SMC 结构新鲜度（18 项）
# =============================================================================


class TestBuildSmcDailyFreshness:
    """SMC 日线结构新鲜度（18 项，PRD §7.3）。"""

    def test_none_dto_returns_all_none(self) -> None:
        result = build_smc_daily_freshness(None, None, 0)
        assert len(result) == 18
        for v in result.values():
            assert v is None

    def test_empty_bars_returns_all_none(self) -> None:
        result = build_smc_daily_freshness({}, pd.DataFrame(), 0)
        assert len(result) == 18
        for v in result.values():
            assert v is None

    def test_has_18_expected_keys(self) -> None:
        result = build_smc_daily_freshness(None, None, 0)
        expected_keys = {
            # BOS (4)
            "smc_bos_bullish_internal_freshness_bars",
            "smc_bos_bullish_swing_freshness_bars",
            "smc_bos_bearish_internal_freshness_bars",
            "smc_bos_bearish_swing_freshness_bars",
            # CHoCH (4)
            "smc_choch_bullish_internal_freshness_bars",
            "smc_choch_bullish_swing_freshness_bars",
            "smc_choch_bearish_internal_freshness_bars",
            "smc_choch_bearish_swing_freshness_bars",
            # EQH/EQL (2)
            "smc_eqh_freshness_bars",
            "smc_eql_freshness_bars",
            # OB formation (4)
            "smc_ob_formation_bullish_internal_freshness_bars",
            "smc_ob_formation_bullish_swing_freshness_bars",
            "smc_ob_formation_bearish_internal_freshness_bars",
            "smc_ob_formation_bearish_swing_freshness_bars",
            # OB first_touch (4)
            "smc_ob_first_touch_bullish_internal_freshness_bars",
            "smc_ob_first_touch_bullish_swing_freshness_bars",
            "smc_ob_first_touch_bearish_internal_freshness_bars",
            "smc_ob_first_touch_bearish_swing_freshness_bars",
        }
        assert set(result.keys()) == expected_keys, (
            f"keys mismatch: {set(result.keys()) - expected_keys} extra, "
            f"{expected_keys - set(result.keys())} missing"
        )

    def test_bos_freshness_correct(self) -> None:
        """BOS 事件在 index=95，current_index=99 → freshness=4。"""
        smc_dto = {
            "events": [
                {"type": "BOS", "bullish": True, "internal": True, "confirmed_index": 95},
            ],
        }
        bars = pd.DataFrame({"high": [10.0] * 100, "low": [9.0] * 100})
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        assert result["smc_bos_bullish_internal_freshness_bars"] == 4

    def test_ob_formation_independent_of_touch(self) -> None:
        """OB formation freshness 基于 confirmed_index，不依赖触碰。"""
        smc_dto = {
            "order_blocks": [
                {
                    "bar_high": 11.0, "bar_low": 10.0,
                    "confirmed_index": 90, "bias": 1, "internal": True,
                },
            ],
        }
        bars = pd.DataFrame({"high": [10.0] * 100, "low": [9.0] * 100})
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        # formation freshness = 99 - 90 = 9
        assert result["smc_ob_formation_bullish_internal_freshness_bars"] == 9
        # first_touch: bars 91-99 high=10.0 >= ob_low=10.0 and low=9.0 <= ob_high=11.0
        # first touch at index 91 → freshness = 99 - 91 = 8
        assert result["smc_ob_first_touch_bullish_internal_freshness_bars"] == 8

    def test_ob_first_touch_must_be_after_creation(self) -> None:
        """OB first_touch 必须在创建 bar 之后。"""
        smc_dto = {
            "order_blocks": [
                {
                    "bar_high": 11.0, "bar_low": 10.0,
                    "confirmed_index": 98, "bias": 1, "internal": True,
                },
            ],
        }
        # bars 99 high=9.0 (不触碰), 只有 index=99 但不满足 high >= 10.0
        bars = pd.DataFrame({"high": [9.0] * 100, "low": [8.0] * 100})
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        assert result["smc_ob_formation_bullish_internal_freshness_bars"] == 1
        assert result["smc_ob_first_touch_bullish_internal_freshness_bars"] is None

    def test_eqh_eql_freshness(self) -> None:
        """EQH 在 index=97 → freshness=2。"""
        smc_dto = {
            "equal_highs_lows": [
                {"type": "EQH", "confirmed_index": 97},
                {"type": "EQL", "confirmed_index": 95},
            ],
        }
        bars = pd.DataFrame({"high": [10.0] * 100, "low": [9.0] * 100})
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        assert result["smc_eqh_freshness_bars"] == 2
        assert result["smc_eql_freshness_bars"] == 4

    def test_same_subtype_takes_latest(self) -> None:
        """同子类型多个事件取最近（最小 freshness）。"""
        smc_dto = {
            "events": [
                {"type": "BOS", "bullish": True, "internal": True, "confirmed_index": 90},
                {"type": "BOS", "bullish": True, "internal": True, "confirmed_index": 95},
            ],
        }
        bars = pd.DataFrame({"high": [10.0] * 100, "low": [9.0] * 100})
        result = build_smc_daily_freshness(smc_dto, bars, 99)
        assert result["smc_bos_bullish_internal_freshness_bars"] == 4  # 99-95


# =============================================================================
# Node crossover 方向（§5）
# =============================================================================


class TestNodeCrossoverDirection:
    """Node crossover cross_direction 显式输出（PRD §7.4）。"""

    def test_up_cross(self) -> None:
        """prev=9.9 → cur=10.1, node_price=10.0 → up。"""
        from app.services.node_cluster_engine import NodeClusterProfileResult, detect_crossover_signals

        # 构造最小 profile
        profile = NodeClusterProfileResult(
            algorithm_version="test",
            output_schema_version=1,
            contract_fingerprint="test",
            profile_rows=[],
            peak_rows=[{"price_mid": 10.0, "bullish_volume": 1.0,
                        "bearish_volume": 1.0, "total_volume": 2.0, "is_peak": True}],
            all_peak_prices=[10.0],
            poc_price=10.0,
            vah_price=None, val_price=None,
            price_step=0.1, lowest_price=9.0, highest_price=11.0,
            daily_source_hash="h1", bars_15m_source_hash="h2",
            adj_factor_hash=None, adjustment_as_of=None,
            daily_bars_count=250, bars_15m_count=4000,
            profile_hash="ph1",
        )
        signals = detect_crossover_signals(profile, 9.9, 10.1)
        assert len(signals) == 1
        assert signals[0]["cross_direction"] == "up"
        assert signals[0]["node_id"] == "peak_000"
        assert signals[0]["node_price"] == 10.0
        assert signals[0]["is_poc"] is True
        assert signals[0]["profile_hash"] == "ph1"

    def test_down_cross(self) -> None:
        """prev=10.1 → cur=9.9, node_price=10.0 → down。"""
        from app.services.node_cluster_engine import NodeClusterProfileResult, detect_crossover_signals

        profile = NodeClusterProfileResult(
            algorithm_version="test",
            output_schema_version=1,
            contract_fingerprint="test",
            profile_rows=[],
            peak_rows=[{"price_mid": 10.0, "bullish_volume": 1.0,
                        "bearish_volume": 1.0, "total_volume": 2.0, "is_peak": True}],
            all_peak_prices=[10.0],
            poc_price=None,
            vah_price=None, val_price=None,
            price_step=0.1, lowest_price=9.0, highest_price=11.0,
            daily_source_hash="h1", bars_15m_source_hash="h2",
            adj_factor_hash=None, adjustment_as_of=None,
            daily_bars_count=250, bars_15m_count=4000,
            profile_hash="ph1",
        )
        signals = detect_crossover_signals(profile, 10.1, 9.9)
        assert len(signals) == 1
        assert signals[0]["cross_direction"] == "down"

    def test_no_cross_when_equal(self) -> None:
        """prev=9.9 → cur=10.0, node_price=10.0 → up（10.0 < 10.0 为 False，但 9.9 <= 10.0 < 10.0 也为 False）。

        实际上 9.9 <= 10.0 < 10.0 → 10.0 < 10.0 为 False → 不触发。
        PRD §5: 9.9→10.0 = 不触发（保持严格不等号语义）。
        """
        from app.services.node_cluster_engine import NodeClusterProfileResult, detect_crossover_signals

        profile = NodeClusterProfileResult(
            algorithm_version="test",
            output_schema_version=1,
            contract_fingerprint="test",
            profile_rows=[],
            peak_rows=[{"price_mid": 10.0, "bullish_volume": 1.0,
                        "bearish_volume": 1.0, "total_volume": 2.0, "is_peak": True}],
            all_peak_prices=[10.0],
            poc_price=None,
            vah_price=None, val_price=None,
            price_step=0.1, lowest_price=9.0, highest_price=11.0,
            daily_source_hash="h1", bars_15m_source_hash="h2",
            adj_factor_hash=None, adjustment_as_of=None,
            daily_bars_count=250, bars_15m_count=4000,
            profile_hash="ph1",
        )
        # 9.9 -> 10.0: up = 9.9 <= 10.0 < 10.0 → False; down = 10.0 <= 10.0 < 9.9 → False
        signals = detect_crossover_signals(profile, 9.9, 10.0)
        assert len(signals) == 0


# =============================================================================
# §8 批量事件聚合
# =============================================================================


class TestAggregateLatestMonitorEvents:
    """批量事件聚合映射（PRD §7.4）。"""

    def test_basic_aggregation(self) -> None:
        calendar = [date(2026, 7, 22), date(2026, 7, 23)]
        raw_events = [
            {
                "event_type": "node_cluster_touch",
                "event_time": "2026-07-22T15:00:00+08:00",
                "payload": {"cross_direction": "up", "boundary": 10.0, "node_id": "peak_000"},
            },
        ]
        result = aggregate_latest_monitor_events(
            raw_events, as_of=date(2026, 7, 23), trading_calendar=calendar,
        )
        assert "node_cluster_touch:up" in result
        item = result["node_cluster_touch:up"]
        assert item["value"] == 1
        assert item["state"] == "observed"
        assert item["unit"] == UNIT_TRADING_DAYS

    def test_legacy_direction_inference(self) -> None:
        """旧事件无 cross_direction 时兼容推断并标记 legacy_inferred。"""
        calendar = [date(2026, 7, 22), date(2026, 7, 23)]
        raw_events = [
            {
                "event_type": "bb_upper_touch",
                "event_time": "2026-07-22T15:00:00+08:00",
                "payload": {"dev_pct": 1.5},
            },
        ]
        result = aggregate_latest_monitor_events(
            raw_events, as_of=date(2026, 7, 23), trading_calendar=calendar,
        )
        assert "bb_upper_touch:up" in result
        assert result["bb_upper_touch:up"].get("legacy_inferred") is True


# =============================================================================
# payload 骨架
# =============================================================================


class TestBuildEmptyPayload:
    """event_freshness_payload 空骨架（PRD §9）。"""

    def test_structure(self) -> None:
        payload = build_empty_event_freshness_payload(as_of=date(2026, 7, 23))
        assert "daily_structure" in payload
        assert "monitor_interaction" in payload
        assert "meta" in payload
        assert payload["meta"]["schema_version"] == 5
        assert payload["meta"]["as_of"] == "2026-07-23"
        assert "smc" in payload["daily_structure"]
        assert "node_cluster" in payload["monitor_interaction"]
