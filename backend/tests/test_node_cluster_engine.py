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

import numpy as np
import pandas as pd
import pytest

from app.services.node_cluster_engine import (
    build_engine_cache_key,
    build_node_regions,
    build_price_state,
    compute_node_cluster_profile,
    compute_node_regions_hash,
    derive_state_for_price,
    detect_crossover_signals,
    profile_to_dict,
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
        peak_prices_set = {round(p, 4) for p in result.all_peak_prices}
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


# =============================================================================
# Canonical Node DTO V2 测试（PROMPT.md §三.3）
# =============================================================================
# 验证 build_node_regions / build_price_state / compute_node_regions_hash / profile_to_dict
# 满足四链一致性约束：详情/Capture/Snapshot/Monitor 必须读同一 DTO，
# 前端禁止从 state/peak_rows 重建 Node 列表。


# node_regions 每项必须包含的字段（与前端 BackendNode 接口对齐）
_NODE_REGION_REQUIRED_KEYS = {
    "entity_id", "kind", "low", "mid", "high",
    "bullish_volume", "bearish_volume", "total_volume", "is_poc",
}

# price_state 必须包含的字段
_PRICE_STATE_REQUIRED_KEYS = {
    "current_price", "position_0_1",
    "upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref",
}


class TestCanonicalNodeDtoV2:
    """Canonical Node DTO V2 字段完整性 / 确定性 / 四链一致性 / 引用有效性。"""

    def test_node_regions_field_completeness(self):
        """[PROMPT.md §三.3] 每个 node_region 必须含 9 个必填字段。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions = build_node_regions(profile)
        assert len(regions) > 0, "合成 3 簇数据应产生至少 1 个 peak"
        for r in regions:
            assert set(r.keys()) >= _NODE_REGION_REQUIRED_KEYS, (
                f"node_region 缺少字段：actual={set(r.keys())} expected>={_NODE_REGION_REQUIRED_KEYS}"
            )
            # 类型断言
            assert isinstance(r["entity_id"], str) and r["entity_id"].startswith("peak_")
            assert r["kind"] == "peak"
            assert isinstance(r["low"], float) and r["low"] <= r["mid"] <= r["high"]
            assert isinstance(r["bullish_volume"], float)
            assert isinstance(r["bearish_volume"], float)
            assert isinstance(r["total_volume"], float)
            assert isinstance(r["is_poc"], bool)

    def test_node_regions_entity_id_stable_format(self):
        """entity_id 必须按 peak_rows 顺序索引生成（peak_000/peak_001/...）。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions = build_node_regions(profile)
        expected_ids = [f"peak_{i:03d}" for i in range(len(regions))]
        actual_ids = [r["entity_id"] for r in regions]
        assert actual_ids == expected_ids, (
            f"entity_id 顺序错误：actual={actual_ids} expected={expected_ids}"
        )

    def test_node_regions_determinism(self):
        """同输入重算 node_regions 必须完全一致（含 entity_id 顺序）。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions_1 = build_node_regions(profile)
        regions_2 = build_node_regions(profile)
        assert regions_1 == regions_2, "同 profile 重算 node_regions 不一致"

        hash_1 = compute_node_regions_hash(regions_1)
        hash_2 = compute_node_regions_hash(regions_2)
        assert hash_1 == hash_2, f"node_regions_hash 不一致：{hash_1} vs {hash_2}"

    def test_node_regions_hash_is_16_char_hex(self):
        """node_regions_hash 必须是 16 字符 hex（SHA256 前 16 字符）。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions = build_node_regions(profile)
        h = compute_node_regions_hash(regions)
        assert len(h) == 16, f"hash 长度错误：{h}"
        int(h, 16)  # 必须可解析为 hex

    def test_node_regions_hash_empty_for_no_peaks(self):
        """无 Peak 时 node_regions=[] 且 hash='empty'。"""
        daily = _make_daily_bars()
        bars_15m = _make_flat_15m_bars()  # 均匀分布 → 无 Peak
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions = build_node_regions(profile)
        assert regions == [], "flat bars 不应产生 peak"
        assert compute_node_regions_hash(regions) == "empty"

    def test_node_regions_hash_differs_for_different_profiles(self):
        """不同 profile 的 node_regions_hash 必须不同。"""
        daily_a = _make_daily_bars(seed=43)
        bars_15m_a = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile_a = compute_node_cluster_profile(daily_a, bars_15m_a)

        daily_b = _make_daily_bars(seed=99, price_low=20.0, price_high=30.0)
        bars_15m_b = _make_clustered_15m_bars([
            (22.0, 200_000.0, 0.15),
            (25.0, 500_000.0, 0.65),
            (28.0, 200_000.0, 0.15),
        ])
        profile_b = compute_node_cluster_profile(daily_b, bars_15m_b)

        hash_a = compute_node_regions_hash(build_node_regions(profile_a))
        hash_b = compute_node_regions_hash(build_node_regions(profile_b))
        assert hash_a != hash_b, (
            f"不同 profile 的 hash 不应相同：{hash_a} vs {hash_b}"
        )

    def test_node_regions_exactly_one_poc_when_poc_price_set(self):
        """poc_price 非空时 node_regions 中有且仅有一个 is_poc=True。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        if profile.poc_price is None:
            pytest.skip("poc_price 为空，跳过 POC 唯一性断言")

        regions = build_node_regions(profile)
        poc_regions = [r for r in regions if r["is_poc"]]
        assert len(poc_regions) == 1, (
            f"is_poc=True 的 region 数量应为 1，实际 {len(poc_regions)}"
        )
        # POC region 的 mid 必须等于 poc_price
        assert abs(poc_regions[0]["mid"] - float(profile.poc_price)) < 1e-4

    def test_price_state_field_completeness(self):
        """[PROMPT.md §三.3] price_state 必须含 6 个必填字段。"""
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        price_state = build_price_state(profile, current_price=12.0)
        assert set(price_state.keys()) >= _PRICE_STATE_REQUIRED_KEYS, (
            f"price_state 缺少字段：actual={set(price_state.keys())}"
        )
        assert price_state["current_price"] == pytest.approx(12.0)
        assert isinstance(price_state["position_0_1"], (float, type(None)))

    def test_price_state_refs_resolve_to_node_regions(self):
        """price_state 中的 *_ref 必须能在 node_regions 中找到对应 entity_id。

        [PROMPT.md §三.3] 前端通过 entity_id 在 node_regions 中查找完整节点信息，
        因此 upper_node_ref/lower_node_ref/poc_node_ref/last_touched_node_ref
        非 None 时必须指向 node_regions 中实际存在的 entity_id。
        """
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        # 当前价在 POC 附近（中簇 12.0），应能找到 upper/lower 节点
        price_state = build_price_state(profile, current_price=12.0)
        regions = build_node_regions(profile)
        all_entity_ids = {r["entity_id"] for r in regions}

        for ref_key in ("upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref"):
            ref = price_state[ref_key]
            if ref is not None:
                assert ref in all_entity_ids, (
                    f"price_state.{ref_key}={ref} 未在 node_regions entity_ids={all_entity_ids} 中找到"
                )

    def test_price_state_empty_profile_returns_minimal_structure(self):
        """profile 无 profile_rows 时 price_state 返回最小结构（refs 全 None）。"""
        # 验证 build_price_state 在 profile_rows 空时的兜底逻辑
        # 通过手动构造空 profile 来触发兜底路径
        from app.services.node_cluster_engine import NodeClusterProfileResult

        empty_profile = NodeClusterProfileResult(
            profile_rows=[],
            peak_rows=[],
            all_peak_prices=[],
            poc_price=None,
            vah_price=None,
            val_price=None,
            price_step=None,
            lowest_price=None,
            highest_price=None,
            daily_source_hash="empty",
            bars_15m_source_hash="empty",
            adj_factor_hash=None,
            adjustment_as_of=None,
            daily_bars_count=0,
            bars_15m_count=0,
            profile_hash="empty",
            algorithm_version="nc-v1",
            output_schema_version=1,
            contract_fingerprint="nc-cf-v1",
        )
        price_state = build_price_state(empty_profile, current_price=10.0)
        assert price_state["current_price"] == 10.0
        assert price_state["position_0_1"] is None
        for ref_key in ("upper_node_ref", "lower_node_ref", "poc_node_ref", "last_touched_node_ref"):
            assert price_state[ref_key] is None

    def test_profile_to_dict_contains_v2_fields(self):
        """[PROMPT.md §三.3] profile_to_dict 必须输出 node_regions + node_regions_hash。

        四链一致性：Snapshot 落库 structural_payload 通过 profile_to_dict 序列化，
        必须与详情/Capture 链路的 build_node_regions 输出完全一致。
        """
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        d = profile_to_dict(profile)
        assert "node_regions" in d, "profile_to_dict 缺少 node_regions 字段"
        assert "node_regions_hash" in d, "profile_to_dict 缺少 node_regions_hash 字段"
        assert "profile_hash" in d, "profile_to_dict 缺少 profile_hash 字段"

        # 四链一致性：profile_to_dict 输出的 node_regions 必须与直接调用 build_node_regions 一致
        direct_regions = build_node_regions(profile)
        assert d["node_regions"] == direct_regions, (
            "profile_to_dict.node_regions 与 build_node_regions 输出不一致"
        )

        # hash 也必须一致
        direct_hash = compute_node_regions_hash(direct_regions)
        assert d["node_regions_hash"] == direct_hash, (
            f"profile_to_dict.node_regions_hash={d['node_regions_hash']} "
            f"与 compute_node_regions_hash={direct_hash} 不一致"
        )

    def test_four_chain_hash_consistency(self):
        """[PROMPT.md §三.4] 四链 node_regions_hash 必须一致。

        模拟四条链路（详情/Capture/Snapshot/Monitor）对同一 profile 计算 hash：
        - 链路1（详情 indicator_service）：build_node_regions → compute_node_regions_hash
        - 链路2（Capture stock_capture_service）：同上
        - 链路3（Snapshot feature_snapshot_service）：profile_to_dict.node_regions_hash
        - 链路4（Monitor volume_node_monitor）：同链路1（共享 compute_indicators）

        四条链路对同一 stock/as_of/输入 → node_regions_hash 必须完全相同。
        """
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        # 链路1/2/4：直接调用 build_node_regions + compute_node_regions_hash
        regions_chain_1 = build_node_regions(profile)
        hash_chain_1 = compute_node_regions_hash(regions_chain_1)

        # 链路3：通过 profile_to_dict 序列化（Snapshot 落库路径）
        dict_chain_3 = profile_to_dict(profile)
        hash_chain_3 = dict_chain_3["node_regions_hash"]

        # 再次重算验证确定性（链路2/4 模拟）
        regions_chain_2 = build_node_regions(profile)
        hash_chain_2 = compute_node_regions_hash(regions_chain_2)

        assert hash_chain_1 == hash_chain_2 == hash_chain_3, (
            f"四链 node_regions_hash 不一致："
            f"chain1={hash_chain_1} chain2={hash_chain_2} chain3={hash_chain_3}"
        )

    def test_node_regions_preserves_all_peaks_no_va_filter(self):
        """[PROMPT.md §三.3] node_regions 必须保留全部 Peak（禁止 VA 过滤）。

        peak_rows 包含 VA 外 Peak（VAH 上方 / VAL 下方），node_regions
        必须完整透传，不得因 VA 过滤丢弃远端 Peak。
        """
        daily = _make_daily_bars()
        bars_15m = _make_clustered_15m_bars(THREE_CLUSTER_SPEC)
        profile = compute_node_cluster_profile(daily, bars_15m)

        regions = build_node_regions(profile)
        # node_regions 数量必须等于 peak_rows 数量（不得过滤）
        assert len(regions) == len(profile.peak_rows), (
            f"node_regions 数量 {len(regions)} != peak_rows 数量 {len(profile.peak_rows)}，"
            f"疑似 VA 过滤"
        )

        # 验证 VA 外 Peak 仍保留（低簇 10.0 在 VAL 下方，高簇 14.0 在 VAH 上方）
        if profile.val_price is not None and profile.vah_price is not None:
            below_val = [r for r in regions if r["mid"] < profile.val_price]
            above_vah = [r for r in regions if r["mid"] > profile.vah_price]
            # 3 簇数据中低簇和高簇应为 VA 外 Peak
            assert len(below_val) >= 1, "VAL 下方 Peak 被过滤"
            assert len(above_vah) >= 1, "VAH 上方 Peak 被过滤"
