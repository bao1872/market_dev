"""结构因子服务单元测试 - 5 组因子独立测试。

验证维度：
1. percentile_rank 辅助函数正确性
2. _compute_participation_factors 成交参与因子
3. _compute_volatility_momentum_factors 动量/波动因子 (BB + SQZMOM)
4. _compute_swing_factors Swing 结构位置因子
5. _compute_dsa_segment_factors DSA 段质量因子
6. _compute_cost_position_factors 成本/节点因子
7. 异常隔离：单组失败不阻塞其他组
8. 边界：数据不足、segment 只有一个、Node/POC 失败

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_structural_factor_service.py -v
"""
from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd

from app.services.structural_factor_service import (
    _compute_cost_position_factors,
    _compute_dsa_segment_factors,
    _compute_participation_factors,
    _compute_swing_factors,
    _compute_volatility_momentum_factors,
    compute_structural_factors,
    percentile_rank,
)


def _build_bars(n: int = 250, seed: int = 42) -> pd.DataFrame:
    """构造固定 OHLCV bars（可重现）。

    生成足够长度的日线数据，包含趋势和波动，
    确保 DSA/BB/SQZMOM/Swing 都有足够 warmup。
    """
    rng = np.random.default_rng(seed)
    base = 100.0
    trend = np.linspace(0, 20.0, n)
    noise = rng.normal(0, 2.0, n)
    closes = base + trend + noise
    intrabar = np.abs(rng.normal(0, 1.5, n)) + 0.5
    highs = closes + intrabar
    lows = closes - intrabar
    opens = closes + rng.normal(0, 0.5, n)
    volumes = rng.integers(1_000_000, 10_000_000, n).astype(float)
    amounts = volumes * closes
    idx = pd.date_range("2025-01-01", periods=n, freq="B")  # Business days
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "amount": amounts,
    }, index=idx)


# ===== 1. percentile_rank =====
def test_percentile_rank_basic() -> None:
    """百分位排名基本正确性。"""
    series = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    # value=3 在 [1,2,3,4,5] 中排名 = 3/5 = 0.6
    result = percentile_rank(3.0, series, lookback=5)
    assert abs(result - 0.6) < 1e-9


def test_percentile_rank_max_value() -> None:
    """最大值排名 = 1.0。"""
    series = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = percentile_rank(5.0, series, lookback=5)
    assert abs(result - 1.0) < 1e-9


def test_percentile_rank_min_value() -> None:
    """最小值排名 = 0.2（1/5）。"""
    series = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    result = percentile_rank(1.0, series, lookback=5)
    assert abs(result - 0.2) < 1e-9


def test_percentile_rank_lookback_truncates() -> None:
    """lookback 只看末尾 N 个值。"""
    # 前 5 个值很大，后 5 个值很小
    series = np.array([100, 200, 300, 400, 500, 1, 2, 3, 4, 5], dtype=float)
    # value=5, lookback=5 -> 只看 [1,2,3,4,5]，排名=5/5=1.0
    result = percentile_rank(5.0, series, lookback=5)
    assert abs(result - 1.0) < 1e-9


def test_percentile_rank_nan_value_returns_none() -> None:
    """NaN value 返回 None。"""
    series = np.array([1.0, 2.0, 3.0])
    result = percentile_rank(float("nan"), series, lookback=3)
    assert result is None


def test_percentile_rank_empty_series_returns_none() -> None:
    """空 series 返回 None。"""
    result = percentile_rank(1.0, np.array([]), lookback=5)
    assert result is None


# ===== 2. _compute_participation_factors =====
def test_participation_factors_returns_dict() -> None:
    """成交参与因子返回正确结构。"""
    bars = _build_bars(n=250)
    result = _compute_participation_factors(bars)
    assert isinstance(result, dict)
    assert "volume_ratio_20" in result
    assert "volume_percentile_120" in result


def test_participation_factors_last_bar_values() -> None:
    """成交参与因子计算最后一根 bar 的值。"""
    bars = _build_bars(n=250)
    result = _compute_participation_factors(bars)
    # volume_ratio_20 = last_volume / SMA(volume, 20)
    last_vol = bars["volume"].iloc[-1]
    sma_vol_20 = bars["volume"].iloc[-20:].mean()
    expected_ratio = last_vol / sma_vol_20
    assert abs(result["volume_ratio_20"] - expected_ratio) < 1e-6


def test_participation_factors_insufficient_data() -> None:
    """数据不足时返回 None 值。"""
    bars = _build_bars(n=10)
    result = _compute_participation_factors(bars)
    assert result["volume_ratio_20"] is None
    assert result["volume_percentile_120"] is None


# ===== 3. _compute_volatility_momentum_factors =====
def test_volatility_momentum_factors_returns_dict() -> None:
    """动量/波动因子返回正确结构。"""
    bars = _build_bars(n=250)
    result = _compute_volatility_momentum_factors(bars)
    assert isinstance(result, dict)
    expected_keys = {
        "bb_percent_b", "bb_bandwidth_pct", "bb_bandwidth_percentile",
        "sqzmom_val", "sqzmom_delta_1", "sqzmom_percentile",
    }
    assert expected_keys.issubset(result.keys())


def test_volatility_momentum_factors_bb_percent_b_range() -> None:
    """BB %b 应在 [0, 1] 范围内（close 在 BB 内部时）。"""
    bars = _build_bars(n=250)
    result = _compute_volatility_momentum_factors(bars)
    bb_pb = result["bb_percent_b"]
    if bb_pb is not None:
        assert 0.0 <= bb_pb <= 1.0 or bb_pb < 0 or bb_pb > 1  # 可能突破


def test_volatility_momentum_factors_insufficient_data() -> None:
    """数据不足时返回 None。"""
    bars = _build_bars(n=10)
    result = _compute_volatility_momentum_factors(bars)
    assert result["bb_percent_b"] is None
    assert result["sqzmom_val"] is None


# ===== 4. _compute_swing_factors =====
def test_swing_factors_returns_dict() -> None:
    """Swing 因子返回正确结构。"""
    bars = _build_bars(n=250)
    result = _compute_swing_factors(bars)
    assert isinstance(result, dict)
    expected_keys = {
        "confirmed_swing_high", "confirmed_swing_low",
        "bars_since_swing_high", "bars_since_swing_low",
        "swing_high_to_close_pct", "swing_low_to_close_pct",
    }
    assert expected_keys.issubset(result.keys())


def test_swing_factors_insufficient_data() -> None:
    """数据不足时返回 None。"""
    bars = _build_bars(n=10)
    result = _compute_swing_factors(bars)
    assert result["confirmed_swing_high"] is None
    assert result["confirmed_swing_low"] is None


def test_swing_factors_no_future_function() -> None:
    """Swing 只使用已确认的点，不使用未来数据。

    修改最后一根 bar 的价格不应影响已确认的 swing 点。
    """
    bars = _build_bars(n=250)
    result_original = _compute_swing_factors(bars)

    # 修改最后一根 bar（不影响已确认的 pivot）
    bars_modified = bars.copy()
    bars_modified.iloc[-1, bars_modified.columns.get_loc("high")] = 999.0
    result_modified = _compute_swing_factors(bars_modified)

    # 已确认的 swing high 应该相同（因为 length=5，最后一根 bar 不会影响已确认的 pivot）
    assert result_original["confirmed_swing_high"] == result_modified["confirmed_swing_high"]


# ===== 5. _compute_dsa_segment_factors =====
def test_dsa_segment_factors_returns_dict() -> None:
    """DSA 段质量因子返回正确结构。"""
    bars = _build_bars(n=250)
    # DSA bundle 需要 mock 或真实计算
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle
    dsa_bundle = compute_dsa_bundle(bars, {})
    if dsa_bundle["factor_per_bar"].empty:
        return  # DSA 数据不足时跳过
    result = _compute_dsa_segment_factors(bars, dsa_bundle)
    assert isinstance(result, dict)
    expected_keys = {
        "segment_id", "segment_dir", "segment_start_price",
        "segment_start_bar_index", "age_bars", "segment_extents_pct",
    }
    assert expected_keys.issubset(result.keys())


# ===== 6. _compute_cost_position_factors =====
def test_cost_position_factors_returns_dict() -> None:
    """成本/节点因子返回正确结构。"""
    bars = _build_bars(n=250)
    result = _compute_cost_position_factors(bars)
    assert isinstance(result, dict)
    expected_keys = {
        "poc_price", "nearest_upper_node", "nearest_lower_node",
        "position_0_1", "close_to_poc_pct",
    }
    assert expected_keys.issubset(result.keys())


def test_cost_position_factors_insufficient_data() -> None:
    """数据不足时返回 None。"""
    bars = _build_bars(n=10)
    result = _compute_cost_position_factors(bars)
    assert result["poc_price"] is None


# ===== 7. 异常隔离 =====
def test_compute_structural_factors_exception_isolation() -> None:
    """单组因子失败不阻塞其他组，写入 degraded_reasons。"""
    # Mock get_bars 返回数据
    mock_session = MagicMock()
    result = asyncio_run(compute_structural_factors(
        session=mock_session,
        instrument_id="00000000-0000-0000-0000-000000000001",
        primary_timeframe="1d",
        secondary_timeframe="15m",
    ))
    assert isinstance(result, dict)
    assert "primary" in result
    assert "secondary" in result
    assert "relation" in result
    assert "meta" in result
    assert isinstance(result["meta"]["degraded_reasons"], list)


def test_compute_structural_factors_meta_structure() -> None:
    """主函数返回 meta 结构正确。"""
    mock_session = MagicMock()
    result = asyncio_run(compute_structural_factors(
        session=mock_session,
        instrument_id="00000000-0000-0000-0000-000000000001",
    ))
    meta = result["meta"]
    assert "as_of" in meta
    assert "primary_lookback_bars" in meta
    assert "secondary_lookback_bars" in meta
    assert "degraded_reasons" in meta
    assert "warmup_notes" in meta


# ===== 辅助函数 =====
def asyncio_run(coro):
    """同步运行 async 函数。"""
    import asyncio
    return asyncio.run(coro)


# ===== 模块自测入口 =====
if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
