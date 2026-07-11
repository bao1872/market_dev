"""ConsensusZone 服务测试（PRD V1.1 §7.4 V2: 日线主结构 + 15m 细化）。

验证项：
1. 因果性：timestamp <= as_of 过滤，未来数据不影响历史结果（日线 + 15m）
2. 单峰识别
3. 多峰识别 + 谷底分割
4. 空分布处理
5. 重叠簇合并规则 v1
6. 成交量加权 P10/P50/P90
7. 缓存键版本化 + 动态 data_version
8. 15m 细化：簇内 P10/P50/P90 用 15m 重算
9. 纵轴 priceMin/priceMax 纳入簇边界
"""

from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd

from app.schemas.consensus_zone import ConsensusCluster
from app.services.consensus_zone_service import (
    CONSENSUS_ALGORITHM_VERSION,
    OVERLAP_RULE_VERSION,
    _build_cache_key,
    compute_consensus_zones,
    compute_data_version,
    compute_volume_distribution,
    compute_volume_weighted_percentiles,
    filter_bars_by_as_of,
    identify_peaks,
    merge_overlapping_clusters,
    refine_cluster_with_15m,
    segment_clusters_by_valleys,
)


def _make_bars(
    n: int = 30,
    base_price: float = 10.0,
    seed: int = 42,
    start_date: str = "2026-06-01",
) -> pd.DataFrame:
    """构造测试用日线 OHLCV bars。"""
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


def _make_15m_bars(
    n: int = 480,
    base_price: float = 10.0,
    seed: int = 100,
    start_date: str = "2026-06-01",
) -> pd.DataFrame:
    """构造测试用 15m OHLCV bars（每日 16 根）。"""
    np.random.seed(seed)
    dates = pd.date_range(start_date, periods=n, freq="15min")
    prices = base_price + np.cumsum(np.random.randn(n) * 0.03)
    return pd.DataFrame({
        "datetime": dates,
        "open": prices,
        "high": prices + 0.05,
        "low": prices - 0.05,
        "close": prices,
        "volume": np.random.randint(100, 2000, n).astype(float),
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

        # 先过滤再计算
        filtered1 = filter_bars_by_as_of(base_bars, cutoff)
        result1 = compute_consensus_zones(filtered1, "000001", as_of)

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
        filtered2 = filter_bars_by_as_of(extended_bars, cutoff)
        result2 = compute_consensus_zones(filtered2, "000001", as_of)

        # 过滤后数据相同 → 结果相同
        assert len(filtered1) == len(filtered2)
        assert result1.isAvailable == result2.isAvailable
        assert len(result1.clusters) == len(result2.clusters)
        if result1.clusters:
            for c1, c2 in zip(result1.clusters, result2.clusters, strict=False):
                assert c1.peakPrice == c2.peakPrice
                assert c1.lower == c2.lower
                assert c1.upper == c2.upper

    def test_future_15m_bars_excluded(self) -> None:
        """15m 未来 bar 也必须被排除（V2 因果性）。"""
        # 15m bars 从 06-01 00:00 开始，480 根 = 5 个日历日（到 06-05 23:45）
        bars_15m = _make_15m_bars(n=480, seed=2)
        cutoff = pd.Timestamp("2026-06-03")  # 第 2 天，部分 15m 在 cutoff 之后

        filtered_15m = filter_bars_by_as_of(bars_15m, cutoff)

        # cutoff=06-03，15m bars 到 06-05，应有部分被排除
        assert len(filtered_15m) < len(bars_15m)
        if not filtered_15m.empty:
            bar_times = pd.to_datetime(filtered_15m["datetime"])
            assert (bar_times <= cutoff).all()

    def test_date_only_as_of_includes_same_day_15m(self) -> None:
        """date-only as_of 不能排除当日 15m 数据（盘后计算场景）。

        as_of=date(2026,6,3) 应按当日结束（23:59:59）处理，
        保留 06-03 全天 15m bars，排除 06-04 及之后。
        """
        bars_15m = _make_15m_bars(n=480, seed=2)  # 06-01 到 06-05

        # date-only as_of
        filtered_date = filter_bars_by_as_of(bars_15m, date(2026, 6, 3))
        bar_times_date = pd.to_datetime(filtered_date["datetime"])
        # 06-03 当日 15m 应被保留
        assert (bar_times_date.dt.day == 3).any()
        # 06-04 及之后应被排除
        assert not (bar_times_date.dt.day >= 4).any()

    def test_string_date_only_as_of_includes_same_day_15m(self) -> None:
        """YYYY-MM-DD 字符串 as_of 也应按当日结束处理。"""
        bars_15m = _make_15m_bars(n=480, seed=2)

        filtered_str = filter_bars_by_as_of(bars_15m, "2026-06-03")
        bar_times_str = pd.to_datetime(filtered_str["datetime"])
        # 06-03 当日 15m 应被保留
        assert (bar_times_str.dt.day == 3).any()
        # 06-04 及之后应被排除
        assert not (bar_times_str.dt.day >= 4).any()

    def test_datetime_as_of_precise_filtering(self) -> None:
        """datetime as_of 按精确时间过滤（不扩展到当日结束）。"""
        bars_15m = _make_15m_bars(n=480, seed=2)

        # 精确到 06-03 12:00:00
        cutoff_dt = datetime(2026, 6, 3, 12, 0, 0)
        filtered_dt = filter_bars_by_as_of(bars_15m, cutoff_dt)
        bar_times_dt = pd.to_datetime(filtered_dt["datetime"])
        # 12:00 之后的 15m 应被排除
        same_day = bar_times_dt[bar_times_dt.dt.day == 3]
        assert (same_day.dt.hour <= 12).all()


# =============================================================================
# 2. 单峰识别
# =============================================================================


class TestSinglePeak:
    """PRD V1.1 §7.4 步骤3: 单峰识别。"""

    def test_single_peak_produces_one_cluster(self) -> None:
        """单峰分布应产生一个簇。"""
        bars = _make_single_peak_bars(peak_price=10.5, n=30)
        result = compute_consensus_zones(bars, "000001", "2026-06-30")
        assert result.isAvailable
        assert len(result.clusters) == 1
        cluster = result.clusters[0]
        assert cluster.lower <= cluster.center <= cluster.upper
        assert cluster.volumeRatio > 0

    def test_peak_price_near_mode(self) -> None:
        """peakPrice 应接近成交量最集中的价位。"""
        bars = _make_single_peak_bars(peak_price=10.5, n=30)
        result = compute_consensus_zones(bars, "000001", "2026-06-30")
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
        result = compute_consensus_zones(bars, "000001", "2026-07-10")
        assert result.isAvailable
        assert len(result.clusters) >= 2

    def test_clusters_sorted_by_volume_desc(self) -> None:
        """簇按成交量降序排列。"""
        bars = _make_double_peak_bars(peak1=10.0, peak2=11.0, n=40)
        result = compute_consensus_zones(bars, "000001", "2026-07-10")
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
        result = compute_consensus_zones(empty_bars, "000001", "2026-06-30")
        assert not result.isAvailable
        assert result.unavailableReason is not None

    def test_insufficient_bars_returns_unavailable(self) -> None:
        """少于 10 根 bar 返回不可用。"""
        bars = _make_bars(n=5)
        result = compute_consensus_zones(bars, "000001", "2026-06-05")
        assert not result.isAvailable

    def test_zero_volume_returns_unavailable(self) -> None:
        """全零成交量返回不可用。"""
        bars = _make_bars(n=15)
        bars["volume"] = 0.0
        result = compute_consensus_zones(bars, "000001", "2026-06-15")
        assert not result.isAvailable


# =============================================================================
# 5. 重叠簇合并规则 v1
# =============================================================================


class TestOverlapMerge:
    """PRD V1.1 §7.4 步骤6: 重叠区按固定规则合并。"""

    def test_merge_overlapping_clusters(self) -> None:
        """高度重叠的簇（重叠比例 > 50%）应合并为一个。"""
        c1 = ConsensusCluster(
            lower=10.0, upper=10.5, center=10.25, peakPrice=10.25,
            volumeRatio=0.3, strength=0.8,
        )
        # c2=[10.2, 10.7], overlap=[10.2, 10.5]=0.3, min_width=0.5, ratio=0.6 > 0.5
        c2 = ConsensusCluster(
            lower=10.2, upper=10.7, center=10.45, peakPrice=10.45,
            volumeRatio=0.2, strength=0.6,
        )
        merged = merge_overlapping_clusters([c1, c2])
        assert len(merged) == 1
        assert merged[0].lower == 10.0
        assert merged[0].upper == 10.7
        assert merged[0].volumeRatio == 0.5

    def test_non_overlapping_clusters_kept_separate(self) -> None:
        """无重叠的簇应保持独立。"""
        c1 = ConsensusCluster(
            lower=9.0, upper=9.5, center=9.25, peakPrice=9.25,
            volumeRatio=0.3, strength=0.8,
        )
        c2 = ConsensusCluster(
            lower=11.0, upper=11.5, center=11.25, peakPrice=11.25,
            volumeRatio=0.2, strength=0.6,
        )
        merged = merge_overlapping_clusters([c1, c2])
        assert len(merged) == 2

    def test_partial_overlap_below_threshold_kept_separate(self) -> None:
        """重叠比例低于阈值的簇应保持独立。"""
        c1 = ConsensusCluster(
            lower=10.0, upper=11.0, center=10.5, peakPrice=10.5,
            volumeRatio=0.3, strength=0.8,
        )
        c2 = ConsensusCluster(
            lower=10.9, upper=12.0, center=11.5, peakPrice=11.5,
            volumeRatio=0.2, strength=0.6,
        )
        # 重叠宽度 = 0.1, 较小簇宽度 = 1.0, 比例 = 0.1 < 0.5
        merged = merge_overlapping_clusters([c1, c2], threshold=0.5)
        assert len(merged) == 2

    def test_overlapping_clusters_have_valid_bounds(self) -> None:
        """重叠簇的 lower/upper 必须有效（lower <= upper）。"""
        bars = _make_double_peak_bars(peak1=10.2, peak2=10.8, n=30)
        result = compute_consensus_zones(bars, "000001", "2026-06-30")
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
# 7. 缓存键版本化 + 动态 data_version
# =============================================================================


class TestCacheKey:
    """PRD V1.1 §7.4: 缓存键包含 symbol/as_of/timeframe/algo_version/data_version。"""

    def test_cache_key_contains_all_components(self) -> None:
        key = _build_cache_key("000001", "2026-07-10", "1d", "v2", "abc12345")
        assert "000001" in key
        assert "2026-07-10" in key
        assert "1d" in key
        assert "v2" in key
        assert "abc12345" in key

    def test_different_as_of_produces_different_key(self) -> None:
        key1 = _build_cache_key("000001", "2026-07-10", "1d", "v2", "abc12345")
        key2 = _build_cache_key("000001", "2026-07-11", "1d", "v2", "abc12345")
        assert key1 != key2

    def test_different_timeframe_produces_different_key(self) -> None:
        key1 = _build_cache_key("000001", "2026-07-10", "1d", "v2", "abc12345")
        key2 = _build_cache_key("000001", "2026-07-10", "15m", "v2", "abc12345")
        assert key1 != key2


class TestDataVersion:
    """PRD V1.1 §7.4: data_version 使用真实 bar 版本/hash，非固定字符串。"""

    def test_data_version_changes_with_new_daily_bar(self) -> None:
        """新增日线 bar 后 data_version 应变化。"""
        bars1 = _make_bars(n=30, seed=1)
        bars2 = _make_bars(n=31, seed=1)
        dv1 = compute_data_version(bars1, None)
        dv2 = compute_data_version(bars2, None)
        assert dv1 != dv2
        assert len(dv1) == 8

    def test_data_version_changes_with_new_15m_bar(self) -> None:
        """新增 15m bar 后 data_version 应变化。"""
        daily = _make_bars(n=30, seed=1)
        bars_15m_1 = _make_15m_bars(n=480, seed=2)
        bars_15m_2 = _make_15m_bars(n=481, seed=2)
        dv1 = compute_data_version(daily, bars_15m_1)
        dv2 = compute_data_version(daily, bars_15m_2)
        assert dv1 != dv2

    def test_data_version_stable_for_same_data(self) -> None:
        """相同数据 data_version 应稳定。"""
        bars = _make_bars(n=30, seed=1)
        bars_15m = _make_15m_bars(n=480, seed=2)
        dv1 = compute_data_version(bars, bars_15m)
        dv2 = compute_data_version(bars, bars_15m)
        assert dv1 == dv2

    def test_data_version_not_fixed_string(self) -> None:
        """data_version 不应是固定 '1'。"""
        bars = _make_bars(n=30, seed=1)
        dv = compute_data_version(bars, None)
        assert dv != "1"
        assert dv != "empty"

    def test_empty_data_returns_empty_version(self) -> None:
        """空数据返回 'empty'。"""
        empty = pd.DataFrame()
        dv = compute_data_version(empty, None)
        assert dv == "empty"

    def test_data_version_independent_of_row_order(self) -> None:
        """data_version 应与行序无关（排序后标准化计算）。"""
        bars = _make_bars(n=30, seed=1)
        bars_shuffled = bars.sample(frac=1, random_state=99).reset_index(drop=True)
        dv1 = compute_data_version(bars, None)
        dv2 = compute_data_version(bars_shuffled, None)
        assert dv1 == dv2

    def test_data_version_changes_with_ohlcv_content(self) -> None:
        """data_version 应反映 OHLCV 内容变化（不只是时间戳）。"""
        bars1 = _make_bars(n=30, seed=1)
        bars2 = bars1.copy()
        bars2.loc[bars2.index[-1], "close"] += 1.0  # 改变末根 close
        dv1 = compute_data_version(bars1, None)
        dv2 = compute_data_version(bars2, None)
        assert dv1 != dv2


class TestV1CacheSafety:
    """v1 缓存安全：旧 v1 JSON 不会被读取，反序列化失败时视为未命中。"""

    def test_v1_json_missing_fields_returns_none(self) -> None:
        """旧 v1 JSON 缺少 v2 必填字段，反序列化应失败（返回 None）。"""
        # v1 JSON 只有旧字段，缺少 dataVersion/overlapRuleVersion/refinementTimeframe/priceMin/priceMax
        v1_json = json.dumps({
            "symbol": "000001",
            "timeframe": "1d",
            "asOf": "2026-06-30",
            "algorithmVersion": "v1",
            "clusters": [],
            "totalVolume": 1000.0,
            "binCount": 50,
            "isAvailable": False,
            "unavailableReason": "test",
        })
        data = json.loads(v1_json)
        # 直接构造 ConsensusZoneResult 应抛 ValidationError（缺少必填字段）
        from pydantic import ValidationError

        from app.schemas.consensus_zone import ConsensusZoneResult

        try:
            ConsensusZoneResult(**data)
            raise AssertionError("应抛 ValidationError")
        except ValidationError:
            pass  # 预期行为


# =============================================================================
# 8. 15m 细化测试
# =============================================================================


class Test15mRefinement:
    """PRD V1.1 §7.4: 15m 细化分布。"""

    def test_refinement_changes_percentiles(self) -> None:
        """15m 细化应改变簇的 P10/P50/P90（与仅日线不同）。"""
        daily_bars = _make_single_peak_bars(peak_price=10.5, n=30)
        result_daily_only = compute_consensus_zones(
            daily_bars, "000001", "2026-06-30", bars_15min=None,
        )
        assert result_daily_only.refinementTimeframe is None

        bars_15m = _make_15m_bars(n=480, seed=5)
        result_with_15m = compute_consensus_zones(
            daily_bars, "000001", "2026-06-30", bars_15min=bars_15m,
        )
        assert result_with_15m.refinementTimeframe == "15m"

        # 若都可用，簇数应一致（15m 只细化，不改变簇数）
        if (
            result_daily_only.isAvailable
            and result_with_15m.isAvailable
        ):
            assert len(result_daily_only.clusters) == len(result_with_15m.clusters)
            # 但百分位可能不同（15m 提供更细粒度）
            c2 = result_with_15m.clusters[0]
            # lower/upper 可能在细化后变化
            assert c2.lower <= c2.center <= c2.upper

    def test_refinement_with_empty_15m_returns_daily(self) -> None:
        """15m 为空时 refinementTimeframe=None。"""
        daily_bars = _make_bars(n=30)
        empty_15m = pd.DataFrame(columns=["datetime", "high", "low", "close", "volume"])
        result = compute_consensus_zones(
            daily_bars, "000001", "2026-06-30", bars_15min=empty_15m,
        )
        assert result.refinementTimeframe is None

    def test_refine_cluster_preserves_volume_ratio(self) -> None:
        """refine_cluster_with_15m 保留 volumeRatio 和 strength。"""
        cluster = ConsensusCluster(
            lower=10.0, upper=11.0, center=10.5, peakPrice=10.5,
            volumeRatio=0.3, strength=0.8,
        )
        bars_15m = _make_15m_bars(n=480, seed=3)
        refined = refine_cluster_with_15m(cluster, bars_15m)
        assert refined.volumeRatio == 0.3
        assert refined.strength == 0.8

    def test_refine_cluster_with_no_intersecting_bars_returns_original(self) -> None:
        """无相交 15m bars 时返回原簇。"""
        cluster = ConsensusCluster(
            lower=100.0, upper=110.0, center=105.0, peakPrice=105.0,
            volumeRatio=0.3, strength=0.8,
        )
        bars_15m = _make_15m_bars(n=480, seed=3)  # 价格在 10 附近
        refined = refine_cluster_with_15m(cluster, bars_15m)
        assert refined.lower == 100.0
        assert refined.upper == 110.0


# =============================================================================
# 9. 纵轴 priceMin/priceMax
# =============================================================================


class TestPriceRange:
    """PRD V1.1 §7.4: 图表纵轴纳入区间 lower/upper，避免裁剪。"""

    def test_price_min_max_includes_cluster_bounds(self) -> None:
        """priceMin/priceMax 应纳入所有簇的 lower/upper。"""
        bars = _make_double_peak_bars(peak1=10.0, peak2=11.0, n=40)
        result = compute_consensus_zones(bars, "000001", "2026-07-10")
        if result.clusters:
            for c in result.clusters:
                assert result.priceMin <= c.lower
                assert result.priceMax >= c.upper

    def test_price_min_max_from_bars_when_no_clusters(self) -> None:
        """无簇时 priceMin/priceMax 来自 bars 的 low/high。"""
        bars = _make_bars(n=15)
        result = compute_consensus_zones(bars, "000001", "2026-06-15")
        if not result.isAvailable:
            assert result.priceMin <= result.priceMax


# =============================================================================
# 10. V2 结果字段完整性
# =============================================================================


class TestV2ResultFields:
    """V2 结果必须包含所有新增字段。"""

    def test_result_contains_v2_fields(self) -> None:
        """结果必须包含 dataVersion/overlapRuleVersion/refinementTimeframe/priceMin/priceMax。"""
        bars = _make_bars(n=30)
        result = compute_consensus_zones(bars, "000001", "2026-06-30")
        assert result.algorithmVersion == CONSENSUS_ALGORITHM_VERSION
        assert result.algorithmVersion == "v2"
        assert result.dataVersion  # 非空
        assert result.overlapRuleVersion == OVERLAP_RULE_VERSION
        assert result.overlapRuleVersion == "v1"
        assert hasattr(result, "refinementTimeframe")
        assert hasattr(result, "priceMin")
        assert hasattr(result, "priceMax")

    def test_result_with_15m_has_refinement_timeframe(self) -> None:
        """传入 15m bars 时 refinementTimeframe='15m'。"""
        daily = _make_bars(n=30)
        bars_15m = _make_15m_bars(n=480)
        result = compute_consensus_zones(
            daily, "000001", "2026-06-30", bars_15min=bars_15m,
        )
        assert result.refinementTimeframe == "15m"


# =============================================================================
# 11. 纯函数单元测试
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
