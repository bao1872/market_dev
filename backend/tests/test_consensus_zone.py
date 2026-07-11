"""ConsensusZone 服务测试（PRD V1.1 §7.4）。

验证项：
1. 因果性：timestamp <= as_of 过滤，未来数据不影响历史结果
2. 单峰识别
3. 多峰识别 + 谷底分割
4. 空分布处理
5. 重叠簇处理
6. 成交量加权 P10/P50/P90
7. 缓存键版本化
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.consensus_zone_service import (
    _build_cache_key,
    compute_consensus_zones,
    compute_volume_distribution,
    compute_volume_weighted_percentiles,
    filter_bars_by_as_of,
    identify_peaks,
    segment_clusters_by_valleys,
)


def _make_bars(
    n: int = 30,
    base_price: float = 10.0,
    seed: int = 42,
    start_date: str = "2026-06-01",
) -> pd.DataFrame:
    """构造测试用 OHLCV bars。"""
    np.random.seed(seed)
    dates = pd.date_range(start_date, periods=n, freq="D")
    prices = base_price + np.cumsum(np.random.randn(n) * 0.1)
    return pd.DataFrame({
        "datetime": dates,
        "open": prices,
        "high": prices + 0.2,
        "low": prices - 0.2,
        "close": prices,
        "volume": np.random.randint(1000, 10000, n).astype(float),
    })


def _make_single_peak_bars(
    peak_price: float = 10.5,
    spread: float = 0.3,
    n: int = 30,
) -> pd.DataFrame:
    """构造单峰分布的 bars：成交量集中在 peak_price 附近。"""
    dates = pd.date_range("2026-06-01", periods=n, freq="D")
    prices = np.linspace(peak_price - spread, peak_price + spread, n)
    volumes = np.array([
        max(100, 10000 * (1 - abs(p - peak_price) / spread))
        for p in prices
    ])
    return pd.DataFrame({
        "datetime": dates,
        "open": prices,
        "high": prices + 0.05,
        "low": prices - 0.05,
        "close": prices,
        "volume": volumes,
    })


def _make_double_peak_bars(
    peak1: float = 10.0,
    peak2: float = 11.0,
    n: int = 40,
) -> pd.DataFrame:
    """构造双峰分布的 bars：两个成交量集中区。"""
    dates = pd.date_range("2026-06-01", periods=n, freq="D")
    prices = np.linspace(9.5, 11.5, n)
    volumes = np.array([
        max(100, 8000 * (1 - abs(p - peak1) / 0.8)) +
        max(100, 8000 * (1 - abs(p - peak2) / 0.8))
        for p in prices
    ])
    return pd.DataFrame({
        "datetime": dates,
        "open": prices,
        "high": prices + 0.05,
        "low": prices - 0.05,
        "close": prices,
        "volume": volumes,
    })


# =============================================================================
# 1. 因果性测试
# =============================================================================


class TestCausality:
    """PRD V1.1 §7.4 步骤1: timestamp <= as_of 过滤。"""

    def test_filter_excludes_future_bars(self) -> None:
        """as_of 之后的 bar 必须被排除。"""
        bars = _make_bars(n=30, start_date="2026-06-01")
        cutoff = pd.Timestamp("2026-06-20")
        filtered = filter_bars_by_as_of(bars, cutoff)
        assert len(filtered) <= 20
        bar_times = pd.to_datetime(filtered["datetime"])
        assert (bar_times <= cutoff).all()

    def test_filter_none_returns_all(self) -> None:
        """as_of=None 返回全部数据。"""
        bars = _make_bars(n=10)
        filtered = filter_bars_by_as_of(bars, None)
        assert len(filtered) == 10

    def test_future_data_does_not_affect_historical_result(self) -> None:
        """未来数据不影响历史结果：添加未来 bar 后历史 as_of 结果不变。"""
        base_bars = _make_bars(n=20, seed=1, start_date="2026-06-01")
        as_of = "2026-06-20"
        cutoff = pd.Timestamp("2026-06-20")

        # 先过滤再计算（服务层编排：filter → compute）
        filtered1 = filter_bars_by_as_of(base_bars, cutoff)
        result1 = compute_consensus_zones(filtered1, "000001", "1d", as_of)

        # 添加 10 天未来数据
        future_dates = pd.date_range("2026-06-21", periods=10, freq="D")
        future_bars = pd.DataFrame({
            "datetime": future_dates,
            "open": [15.0] * 10,
            "high": [16.0] * 10,
            "low": [14.0] * 10,
            "close": [15.5] * 10,
            "volume": [999999.0] * 10,  # 极高成交量
        })
        extended_bars = pd.concat([base_bars, future_bars], ignore_index=True)
        # 过滤后未来数据被排除
        filtered2 = filter_bars_by_as_of(extended_bars, cutoff)
        result2 = compute_consensus_zones(filtered2, "000001", "1d", as_of)

        # 过滤后数据相同 → 结果相同
        assert len(filtered1) == len(filtered2)
        assert result1.isAvailable == result2.isAvailable
        assert len(result1.clusters) == len(result2.clusters)
        if result1.clusters:
            for c1, c2 in zip(result1.clusters, result2.clusters, strict=False):
                assert c1.peakPrice == c2.peakPrice
                assert c1.lower == c2.lower
                assert c1.upper == c2.upper


# =============================================================================
# 2. 单峰识别
# =============================================================================


class TestSinglePeak:
    """PRD V1.1 §7.4 步骤3: 单峰识别。"""

    def test_single_peak_produces_one_cluster(self) -> None:
        """单峰分布应产生一个簇。"""
        bars = _make_single_peak_bars(peak_price=10.5, n=30)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-06-30")
        assert result.isAvailable
        assert len(result.clusters) == 1
        cluster = result.clusters[0]
        assert cluster.lower <= cluster.center <= cluster.upper
        assert cluster.volumeRatio > 0

    def test_peak_price_near_mode(self) -> None:
        """peakPrice 应接近成交量最集中的价位。"""
        bars = _make_single_peak_bars(peak_price=10.5, n=30)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-06-30")
        if result.clusters:
            assert abs(result.clusters[0].peakPrice - 10.5) < 1.0


# =============================================================================
# 3. 多峰识别 + 谷底分割
# =============================================================================


class TestMultiPeak:
    """PRD V1.1 §7.4 步骤3: 多峰 + 谷底分割。"""

    def test_double_peak_produces_two_clusters(self) -> None:
        """双峰分布应产生两个簇。"""
        bars = _make_double_peak_bars(peak1=10.0, peak2=11.0, n=40)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-07-10")
        assert result.isAvailable
        assert len(result.clusters) >= 2

    def test_clusters_sorted_by_volume_desc(self) -> None:
        """簇按成交量降序排列。"""
        bars = _make_double_peak_bars(peak1=10.0, peak2=11.0, n=40)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-07-10")
        for i in range(len(result.clusters) - 1):
            assert result.clusters[i].volumeRatio >= result.clusters[i + 1].volumeRatio


# =============================================================================
# 4. 空分布处理
# =============================================================================


class TestEmptyDistribution:
    """PRD V1.1 §7.4: 空分布/数据不足处理。"""

    def test_empty_bars_returns_unavailable(self) -> None:
        """空 bars 返回 isAvailable=False。"""
        empty_bars = pd.DataFrame(columns=["datetime", "high", "low", "close", "volume"])
        result = compute_consensus_zones(empty_bars, "000001", "1d", "2026-06-30")
        assert not result.isAvailable
        assert result.unavailableReason is not None

    def test_insufficient_bars_returns_unavailable(self) -> None:
        """少于 10 根 bar 返回不可用。"""
        bars = _make_bars(n=5)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-06-05")
        assert not result.isAvailable

    def test_zero_volume_returns_unavailable(self) -> None:
        """全零成交量返回不可用。"""
        bars = _make_bars(n=15)
        bars["volume"] = 0.0
        result = compute_consensus_zones(bars, "000001", "1d", "2026-06-15")
        assert not result.isAvailable


# =============================================================================
# 5. 重叠簇处理
# =============================================================================


class TestOverlapClusters:
    """PRD V1.1 §7.4 步骤5: 重叠区按固定规则处理。"""

    def test_overlapping_clusters_have_valid_bounds(self) -> None:
        """重叠簇的 lower/upper 必须有效（lower <= upper）。"""
        bars = _make_double_peak_bars(peak1=10.2, peak2=10.8, n=30)
        result = compute_consensus_zones(bars, "000001", "1d", "2026-06-30")
        for c in result.clusters:
            assert c.lower <= c.center
            assert c.center <= c.upper


# =============================================================================
# 6. 成交量加权 P10/P50/P90
# =============================================================================


class TestWeightedPercentiles:
    """PRD V1.1 §7.4 步骤4: 成交量加权百分位。"""

    def test_percentiles_ordered(self) -> None:
        """P10 <= P50 <= P90。"""
        prices = np.array([9.0, 9.5, 10.0, 10.5, 11.0])
        volumes = np.array([100, 200, 500, 200, 100])
        pcts = compute_volume_weighted_percentiles(prices, volumes, [10, 50, 90])
        assert pcts[10] <= pcts[50]
        assert pcts[50] <= pcts[90]

    def test_high_volume_skews_percentile(self) -> None:
        """高成交量价位应使 P50 更接近该价位。"""
        prices = np.array([9.0, 10.0, 11.0])
        volumes = np.array([1, 1000, 1])
        pcts = compute_volume_weighted_percentiles(prices, volumes, [50])
        assert abs(pcts[50] - 10.0) < 0.5

    def test_empty_prices_returns_zeros(self) -> None:
        """空价格返回 0。"""
        pcts = compute_volume_weighted_percentiles(np.array([]), np.array([]), [50])
        assert pcts[50] == 0.0


# =============================================================================
# 7. 缓存键版本化
# =============================================================================


class TestCacheKey:
    """PRD V1.1 §7.4: 缓存键包含 symbol/as_of/timeframe/algo_version/data_version。"""

    def test_cache_key_contains_all_components(self) -> None:
        key = _build_cache_key("000001", "2026-07-10", "1d", "v1", "1")
        assert "000001" in key
        assert "2026-07-10" in key
        assert "1d" in key
        assert "v1" in key
        assert "1" in key

    def test_different_as_of_produces_different_key(self) -> None:
        key1 = _build_cache_key("000001", "2026-07-10", "1d", "v1", "1")
        key2 = _build_cache_key("000001", "2026-07-11", "1d", "v1", "1")
        assert key1 != key2

    def test_different_timeframe_produces_different_key(self) -> None:
        key1 = _build_cache_key("000001", "2026-07-10", "1d", "v1", "1")
        key2 = _build_cache_key("000001", "2026-07-10", "15m", "v1", "1")
        assert key1 != key2


# =============================================================================
# 8. 纯函数单元测试
# =============================================================================


class TestPureFunctions:
    """底层纯函数直接测试。"""

    def test_identify_peaks_finds_local_maxima(self) -> None:
        """identify_peaks 正确识别局部最大值。"""
        volumes = np.array([1, 5, 3, 2, 8, 4, 1], dtype=float)
        peaks = identify_peaks(volumes, prominence=0.05)
        assert 1 in peaks  # 5 is a peak
        assert 4 in peaks  # 8 is a peak

    def test_identify_peaks_empty_array(self) -> None:
        assert identify_peaks(np.array([])) == []

    def test_segment_clusters_single_peak(self) -> None:
        volumes = np.array([1, 5, 3], dtype=float)
        peaks = [1]
        bounds = segment_clusters_by_valleys(volumes, peaks)
        assert len(bounds) == 1
        assert bounds[0] == (0, 2)

    def test_segment_clusters_double_peak(self) -> None:
        volumes = np.array([1, 8, 2, 5, 1], dtype=float)
        peaks = [1, 3]
        bounds = segment_clusters_by_valleys(volumes, peaks)
        assert len(bounds) == 2
        # valley between peak1(idx=1) and peak2(idx=3) is idx=2
        assert bounds[0][1] == 2  # first cluster ends at valley
        assert bounds[1][0] == 2  # second cluster starts at valley

    def test_volume_distribution_basic(self) -> None:
        """成交量分布基本计算。"""
        highs = np.array([10.5, 11.0, 10.8])
        lows = np.array([10.0, 10.5, 10.3])
        volumes = np.array([1000, 2000, 1500])
        bin_volumes, bin_centers, pmin, pmax = compute_volume_distribution(
            highs, lows, volumes, num_bins=10
        )
        assert len(bin_volumes) == 10
        assert pmin == 10.0
        assert pmax == 11.0
        assert abs(sum(bin_volumes) - 4500) < 100  # 总量守恒（允许分配误差）
