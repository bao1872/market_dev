"""Node Cluster engine 单元测试（CHANGE-20260718-004）。

验证 `node_cluster_engine.compute_node_cluster_profile` 的不可变结果合同：
- `NodeClusterProfileResult` 是 frozen dataclass（不可变）
- `profile_hash` 确定性（同输入同输出，三链一致性断言基础）
- 版本字段齐全（algorithm_version / output_schema_version / contract_fingerprint）
- `all_peak_prices` 保留全部 Peak（**禁止 VA 过滤**，VA 外 Peak 有效）
- VAH 上方 Peak / VAL 下方 Peak 仍保留在 peak_rows / all_peak_prices
- 多 Peak 全部保留；无 Peak 时 peak_rows=[]
- `derive_state_for_price` / `detect_crossover_signals` 正常工作

合成数据策略：
- 3 簇 15m bars（低/中/高价格），中簇为主成交量区（POC + VA），
  低簇和高簇为 VA 外 Peak
- 日线 spanning 完整价格范围决定 VP 100 行 profile 的 price_step

约束：
- 不连数据库（纯单元测试）
- 不导入底层 `compute_unified_volume_profile`（架构守护由 test_node_cluster_architecture 覆盖）
"""
from __future__ import annotations

import dataclasses
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    build_engine_cache_key,
    compute_node_cluster_profile,
    detect_crossover_signals,
    derive_state_for_price,
)


# =============================================================================
# 合成数据生成
# =============================================================================


def _make_daily_bars(
    n: int = 260,
    price_low: float = 9.0,
    price_high: float = 15.0,
    end_date: str = "2026-06-18",
    seed: int = 43,
) -> pd.DataFrame:
    """生成日线 bars，价格范围覆盖 [price_low, price_high]。

    VP 用日线 min(low) ~ max(high) 决定 100 行 profile 的 price_step。
    """
    np.random.seed(seed)
    dates = pd.date_range(end=end_date, periods=n, freq="B")
    span = price_high - price_low
    # close 在 [price_low, price_high] 内随机游走
    mid = (price_low + price_high) / 2
    returns = np.random.uniform(-0.01, 0.01, size=n)
    close = mid * np.cumprod(1 + returns)
    close = np.clip(close, price_low + span * 0.1, price_high - span * 0.1)
    open_ = close * (1 + np.random.uniform(-0.005, 0.005, size=n))
    high = np.maximum(open_, close) * (1 + np.random.uniform(0.002, 0.01, size=n))
    low = np.minimum(open_, close) * (1 - np.random.uniform(0.002, 0.01, size=n))
    # 确保 high/low 覆盖目标范围
    high = np.maximum(high, price_high - span * 0.05)
    low = np.minimum(low, price_low + span * 0.05)
    volume = np.random.uniform(1_000_000, 5_000_000, size=n)
    amount = volume * close
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount},
        index=dates,
    )
    df.index.name = "datetime"
    return df


def _make_clustered_15m_bars(
    clusters: list[tuple[float, float, float]],
    n_total: int = 4100,
    end_date: str = "2026-06-18 15:00",
) -> pd.DataFrame:
    """生成 15m bars，成交量集中在指定价格簇。

    Args:
        clusters: [(price, volume_per_bar, fraction_of_total), ...]
            fraction 之和应 ≤ 1.0；余下 bars 用低成交量填充。
        n_total: 总 bar 数（>= 4000 满足 prepare_node_cluster_bars 尾部 4000）
    """
    dates = pd.date_range(end=end_date, periods=n_total, freq="15min")
    parts: list[pd.DataFrame] = []
    consumed = 0
    for price, vol, frac in clusters:
        n_cluster = int(n_total * frac)
        idx = dates[consumed : consumed + n_cluster]
        consumed += n_cluster
        # 窄幅 bar：high≈low≈close=price，确保成交量落入单一 VP 行
        jitter = price * 0.0005
        close = price + np.random.uniform(-jitter, jitter, size=n_cluster)
        parts.append(pd.DataFrame(
            {
                "open": close,
                "high": close + jitter,
                "low": close - jitter,
                "close": close,
                "volume": np.full(n_cluster, vol, dtype=float),
                "amount": close * vol,
            },
            index=idx,
        ))
    # 余下 bars：低成交量填充（避免边缘 0 volume 影响 peak 检测）
    remaining = n_total - consumed
    if remaining > 0:
        idx = dates[consumed:]
        fill_price = clusters[0][0] if clusters else 10.0
        close = np.full(remaining, fill_price, dtype=float)
        parts.append(pd.DataFrame(
            {
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": np.full(remaining, 1000.0, dtype=float),
                "amount": close * 1000.0,
            },
            index=idx,
        ))
    df = pd.concat(parts).sort_index()
    df.index.name = "datetime"
    return df


def _make_flat_15m_bars(
    n: int = 4100,
    price_low: float = 9.0,
    price_high: float = 15.0,
    end_date: str = "2026-06-18 15:00",
) -> pd.DataFrame:
    """生成宽幅均匀成交量 15m bars（volume 均匀分布到全部 100 行 → 无局部极大值 → 无 Peak）。

    每个 bar 的 high/low 覆盖完整价格范围，VP 将 volume 均匀分配到所有行，
    所有行 total_volume 相等 → 不存在严格大于邻居的行 → peak_rows=[]。
    """
    dates = pd.date_range(end=end_date, periods=n, freq="15min")
    close = np.full(n, (price_low + price_high) / 2, dtype=float)
    high = np.full(n, price_high, dtype=float)
    low = np.full(n, price_low, dtype=float)
    volume = np.full(n, 100_000.0, dtype=float)
    df = pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": volume, "amount": close * volume},
        index=dates,
    )
    df.index.name = "datetime"
    return df


# 3 簇 fixture：低(10) / 中(12,POC) / 高(14)，中簇占主成交量
THREE_CLUSTER_SPEC = [
    (10.0, 200_000.0, 0.15),  # 低簇：15% bars，VA 外 Peak（VAL 下方）
    (12.0, 500_000.0, 0.65),  # 中簇：65% bars，POC + 主 VA
    (14.0, 200_000.0, 0.15),  # 高簇：15% bars，VA 外 Peak（VAH 上方）
]


# =============================================================================
# 测试
# =============================================================================


class TestNodeClusterEngineImmutable:
    """1. NodeClusterProfileResult 不可变。"""

    def test_result_is_frozen_dataclass(self):
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        assert dataclasses.is_dataclass(result)
        # frozen dataclass: 赋值应抛 FrozenInstanceError
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            result.profile_hash = "tampered"  # type: ignore[misc]
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            result.poc_price = 999.0  # type: ignore[misc]


class TestProfileHashDeterministic:
    """2. profile_hash 确定性（同输入同输出）。"""

    def test_same_input_same_hash(self):
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        r1 = compute_node_cluster_profile(daily, bars_15m)
        r2 = compute_node_cluster_profile(daily, bars_15m)
        assert r1.profile_hash == r2.profile_hash
        assert r1.profile_hash != "empty"
        assert r1.profile_hash != ""

    def test_different_input_different_hash(self):
        daily = _make_daily_bars()
        bars_a = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        # 改变簇分布 → 不同 profile
        bars_b = _make_clustered_15m_bars([
            (10.0, 300_000.0, 0.30),
            (12.0, 400_000.0, 0.40),
            (14.0, 300_000.0, 0.30),
        ])
        ra = compute_node_cluster_profile(daily, bars_a)
        rb = compute_node_cluster_profile(daily, bars_b)
        assert ra.profile_hash != rb.profile_hash


class TestVersionFields:
    """3. 版本字段齐全。"""

    def test_version_fields_present(self):
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        assert result.algorithm_version, "algorithm_version 非空"
        assert isinstance(result.output_schema_version, int)
        assert result.output_schema_version >= 1
        assert result.contract_fingerprint, "contract_fingerprint 非空"
        # source hash 诊断字段
        assert result.daily_source_hash, "daily_source_hash 非空"
        assert result.bars_15m_source_hash, "bars_15m_source_hash 非空"
        assert result.daily_bars_count > 0
        assert result.bars_15m_count > 0


class TestPeakPreservation:
    """4-7. Peak 保留合同（VA 外 Peak 有效，禁止过滤）。"""

    def test_all_peak_prices_match_is_peak_rows(self):
        """all_peak_prices 必须包含 profile 中所有 is_peak 行，无 VA 过滤。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        # peak_rows 是 is_peak 行的列表
        assert len(result.peak_rows) == len(result.all_peak_prices)
        # all_peak_prices 是排序去重的 price_mid
        peak_prices_set = set(round(p, 4) for p in result.all_peak_prices)
        for row in result.peak_rows:
            assert round(float(row["price_mid"]), 4) in peak_prices_set

    def test_peak_above_vah_preserved(self):
        """VAH 上方 Peak 仍保留在 all_peak_prices。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        assert result.vah_price is not None, "VAH 应存在"
        peaks_above_vah = [p for p in result.all_peak_prices if p > result.vah_price]
        # 高簇 (14.0) 应产生 VAH 上方 Peak
        assert len(peaks_above_vah) > 0, (
            f"VAH={result.vah_price} 上方应有 Peak，all_peak_prices={result.all_peak_prices}"
        )
        # 至少一个 Peak 接近 14.0（高簇价格）
        assert any(p > result.vah_price + 0.5 for p in result.all_peak_prices), (
            f"VAH={result.vah_price} 上方 0.5 外应有 Peak，all_peak_prices={result.all_peak_prices}"
        )

    def test_peak_below_val_preserved(self):
        """VAL 下方 Peak 仍保留在 all_peak_prices。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        assert result.val_price is not None, "VAL 应存在"
        peaks_below_val = [p for p in result.all_peak_prices if p < result.val_price]
        # 低簇 (10.0) 应产生 VAL 下方 Peak
        assert len(peaks_below_val) > 0, (
            f"VAL={result.val_price} 下方应有 Peak，all_peak_prices={result.all_peak_prices}"
        )
        assert any(p < result.val_price - 0.5 for p in result.all_peak_prices), (
            f"VAL={result.val_price} 下方 0.5 外应有 Peak，all_peak_prices={result.all_peak_prices}"
        )

    def test_multi_peak_all_preserved(self):
        """多 Peak（>= 2）全部保留在 all_peak_prices。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        result = compute_node_cluster_profile(daily, bars_15m)
        # 3 簇应产生 >= 2 个 Peak（低 + 高，可能还有中簇 POC）
        assert len(result.all_peak_prices) >= 2, (
            f"3 簇数据应产生 >= 2 Peak，实际 {len(result.all_peak_prices)}: {result.all_peak_prices}"
        )

    def test_no_peak_empty_when_flat(self):
        """均匀成交量 → 无局部极大值 → peak_rows=[]。"""
        daily = _make_daily_bars()
        bars_15m = _make_flat_15m_bars()
        result = compute_node_cluster_profile(daily, bars_15m)
        # 均匀 volume 落入单一 price row → 无局部极大值（邻居相等不满足 strictly greater）
        assert len(result.peak_rows) == 0
        assert result.all_peak_prices == []
        # profile_hash 仍应有效（非 "empty"）
        assert result.profile_hash != "empty"


class TestDeriveStateAndCrossover:
    """8. derive_state_for_price + detect_crossover_signals。"""

    def test_derive_state_returns_immutable_state(self):
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)
        state = derive_state_for_price(profile, 12.0)
        assert state.current_price == pytest.approx(12.0)
        # NodeClusterPriceState 也是 frozen
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            state.current_price = 999.0  # type: ignore[misc]

    def test_detect_crossover_signals_for_vah_above_peak(self):
        """价格穿越 VAH 上方 Peak 时触发信号。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)
        # 找到 VAH 上方的一个 Peak
        peaks_above = [p for p in profile.all_peak_prices if profile.vah_price and p > profile.vah_price]
        assert len(peaks_above) > 0
        target_peak = peaks_above[0]
        # prev_close 在 Peak 下方，cur_close 在 Peak 上方 → 穿越触发
        signals = detect_crossover_signals(profile, prev_close=target_peak - 0.5, cur_close=target_peak + 0.5)
        assert len(signals) >= 1
        triggered_prices = {s["cluster_price"] for s in signals}
        assert target_peak in triggered_prices or any(
            abs(target_peak - tp) < 0.01 for tp in triggered_prices
        )

    def test_detect_crossover_no_signal_when_no_cross(self):
        """价格未穿越任何 Peak → 无信号。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)
        # 两根 close 都在所有 Peak 下方 → 无穿越
        signals = detect_crossover_signals(profile, prev_close=9.5, cur_close=9.6)
        assert signals == []


class TestBuildCacheKey:
    """9. build_engine_cache_key 含版本 + 指纹 + hash。"""

    def test_cache_key_contains_version_and_fingerprint(self):
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)
        key = build_engine_cache_key("instr-123", profile)
        assert "instr-123" in key
        assert profile.algorithm_version in key
        assert profile.contract_fingerprint in key
        assert profile.daily_source_hash in key
        assert profile