"""[CHANGE-20260808] Review Historical Fact Replay correctness 纯单元测试。

验证（Phase 2A）：
1. historical adapter contract：previous_state_to_flat 映射 Review fp_* 字段
   （含 DSA segment / latest structure events / rolling facts）
2. compute_first_pyramid_history 的 daily_state 扩展字段：
   - segment 字段（segment_id/direction/start/bars/change/slope）
   - current_vs_prev_volume/amount_mean_ratio
   - latest BOS/CHoCH/OB（direction/freshness/active）
   - price_position_120d
3. prefix invariance：FullSeries[T] == TruncatedSeries[:T].last
   - _price_position_120d 只依赖前 i 根 bar
   - latest event freshness 相对已确认事件时间，不读取未来确认
4. Chip excluded：history 回补不引入 chip

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest \
        tests/test_review_historical_fact_replay.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.domain.review.member_fact import previous_state_to_flat
from app.services.first_pyramid_service import (
    _price_position_120d,
    compute_first_pyramid_history,
)


def _build_bars(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """构造 OHLCV 日线 fixture（含 amount 列，满足 compute_first_pyramid_history）。"""
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2025-01-01", periods=n, freq="B")
    close = 10.0 + np.cumsum(rng.normal(0.05, 0.15, n))
    volume = rng.integers(100000, 500000, n).astype(float)
    return pd.DataFrame({
        "open": close - 0.05,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": volume,
        "amount": close * volume,
    }, index=dates)


# =============================================================================
# 1. Historical adapter contract
# =============================================================================


class TestHistoricalAdapterContract:
    """previous_state_to_flat 的 Review fp_* 字段映射。"""

    def test_maps_trend_and_structure(self) -> None:
        state = {
            "regime_value": 1,
            "swing_bias": 1,
            "internal_bias": 1,
            "structure_alignment": "共振",
            "momentum_direction": "up",
            "momentum_change": "improving",
            "volume_ratio_20": 1.5,
            "volume_percentile_20": 80.0,
            "price_position_120d": 0.7,
            "latest_bos_direction": "up",
            "latest_bos_freshness": 3,
            "latest_choch_direction": "down",
            "latest_choch_freshness": 10,
            "latest_ob_direction": "up",
            "latest_ob_freshness": 5,
            "current_vs_prev_volume_mean_ratio": 1.3,
            "prev_segment_volume_mean": 1000.0,
        }
        flat = previous_state_to_flat(state)
        assert flat["fp_trend_direction"] == "up"
        assert flat["fp_swing_direction"] == 1
        assert flat["fp_internal_direction"] == 1
        assert flat["fp_structure_alignment"] == "共振"
        assert flat["fp_momentum_direction"] == "up"
        assert flat["fp_momentum_change"] == "improving"
        assert flat["review_price_position"] == 0.7
        assert flat["fp_latest_bos_direction"] == "up"
        assert flat["fp_latest_bos_freshness"] == 3
        assert flat["fp_latest_choch_direction"] == "down"
        assert flat["fp_latest_choch_freshness"] == 10
        assert flat["fp_latest_ob_direction"] == "up"
        assert flat["fp_latest_ob_freshness"] == 5
        assert flat["fp_segment_volume_ratio"] == 1.3
        assert flat["fp_prev_segment_volume"] == 1000.0

    def test_trend_from_regime_value_confirmed(self) -> None:
        """trend 必须由 confirmed regime_value 映射，禁止 raw dir 直接映射。"""
        assert previous_state_to_flat({"regime_value": 1})["fp_trend_direction"] == "up"
        assert previous_state_to_flat({"regime_value": -1})["fp_trend_direction"] == "down"
        assert previous_state_to_flat({"regime_value": 0})["fp_trend_direction"] == "sideways"
        assert previous_state_to_flat({"regime_value": None})["fp_trend_direction"] is None

    def test_structure_alignment_derived_when_missing(self) -> None:
        """structure_alignment 缺省时由 swing/internal bias 派生。"""
        assert previous_state_to_flat(
            {"swing_bias": 1, "internal_bias": 1},
        )["fp_structure_alignment"] == "共振"
        assert previous_state_to_flat(
            {"swing_bias": 1, "internal_bias": -1},
        )["fp_structure_alignment"] == "背离"

    def test_chip_excluded(self) -> None:
        """Chip 字段不得进入历史 adapter。"""
        flat = previous_state_to_flat({
            "regime_value": 1,
            "chip_consensus": {"consensus": 0.8},
        })
        assert "chip_consensus" not in flat
        assert "fp_chip_consensus" not in flat


# =============================================================================
# 2. compute_first_pyramid_history daily_state 扩展字段
# =============================================================================


class TestHistoricalDailyStateExtension:
    """compute_first_pyramid_history 的 daily_state 提供 Review 消费字段。"""

    def test_daily_state_has_review_fields(self) -> None:
        bars = _build_bars(n=300)
        result = compute_first_pyramid_history(bars, symbol="TEST", output_bars=250)
        daily_state = result["daily_state"]
        assert len(daily_state) > 0
        latest = daily_state[-1]
        # Trend segment 字段
        assert "segment_id" in latest
        assert "segment_direction" in latest
        assert "segment_bars" in latest
        assert "segment_change_pct" in latest
        assert "segment_slope" in latest
        assert "current_vs_prev_volume_mean_ratio" in latest
        assert "current_vs_prev_amount_mean_ratio" in latest
        # Structure latest events
        assert "latest_bos_direction" in latest
        assert "latest_bos_freshness" in latest
        assert "latest_choch_direction" in latest
        assert "latest_choch_freshness" in latest
        assert "latest_ob_direction" in latest
        assert "latest_ob_freshness" in latest
        assert "structure_alignment" in latest
        # Rolling facts
        assert "price_position_120d" in latest
        # Chip 不引入
        assert "chip_consensus" not in latest

    def test_events_emitted(self) -> None:
        bars = _build_bars(n=400, seed=3)
        result = compute_first_pyramid_history(bars, symbol="TEST", output_bars=250)
        events = result["events"]
        # 应有至少一个结构事件（BOS/CHoCH/OB）
        assert isinstance(events, list)
        assert len(events) > 0


# =============================================================================
# 3. Prefix invariance
# =============================================================================


class TestPrefixInvariance:
    """FullSeries[T] == TruncatedSeries[:T].last。"""

    def test_price_position_120d_prefix_invariant(self) -> None:
        """price_position_120d 必须只依赖前 i 根 bar，不读未来。"""
        closes = [10.0 + i * 0.1 for i in range(300)]
        lows = [c - 0.5 for c in closes]
        highs = [c + 0.5 for c in closes]
        t = 150
        # 全量序列的 T 位置
        full = _price_position_120d(closes, lows, highs, t)
        # 截断到 T 的序列，同样位置
        trunc_closes = closes[: t + 1]
        trunc_lows = lows[: t + 1]
        trunc_highs = highs[: t + 1]
        trunc = _price_position_120d(trunc_closes, trunc_lows, trunc_highs, t)
        assert full == trunc

    def test_price_position_low_window(self) -> None:
        """窗口不足 120 时仍用可用 bar（不报错，prefix-causal）。"""
        closes = [10.0, 10.2, 10.5]
        lows = [9.8, 10.0, 10.3]
        highs = [10.2, 10.4, 10.7]
        val = _price_position_120d(closes, lows, highs, 2)
        assert val is not None
        assert 0.0 <= val <= 1.0

    def test_regime_value_prefix_causal(self) -> None:
        """regime_value 由截断序列重算（compute_dsa_history lookback=None 时仍 prefix-causal）。"""
        bars_full = _build_bars(n=300, seed=11)
        full_hist = compute_first_pyramid_history(bars_full, symbol="T", output_bars=300)
        full_regime = [s["regime_value"] for s in full_hist["daily_state"]]

        # 截断到 T 再算，比较 T 位置 regime
        t = 150
        bars_trunc = bars_full.iloc[: t + 1]
        trunc_hist = compute_first_pyramid_history(bars_trunc, symbol="T", output_bars=t + 1)
        trunc_regime = [s["regime_value"] for s in trunc_hist["daily_state"]]
        assert full_regime[t] == trunc_regime[-1]

    def test_freshness_never_negative(self) -> None:
        """latest event freshness 必须 >= 0（相对已确认事件时间，不读未来）。"""
        bars = _build_bars(n=400, seed=5)
        result = compute_first_pyramid_history(bars, symbol="T", output_bars=250)
        for state in result["daily_state"]:
            for key in ("latest_bos_freshness", "latest_choch_freshness", "latest_ob_freshness"):
                val = state.get(key)
                if val is not None:
                    assert val >= 0, f"{key}={val} < 0 at {state.get('bar_index')}"
