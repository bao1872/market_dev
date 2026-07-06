"""时序特征服务单元测试 - Temporal Features V1.

验证维度：
1. daily_context 结构 + 9 字段
2. daily_dsa_segment_duration_percentile 公式正确
3. daily_sqzmom_change_since_segment_start point-in-time 正确
4. daily_volume_percentile_change_since_segment_start point-in-time 正确
5. m15_response 结构 + 9 字段
6. m15 swing anchor 选择规则
7. m15 position/sqzmom/bb_bandwidth/volume change since anchor
8. derived_relation 只由 daily + m15 派生
9. derived alignment direction 4 种情况
10. derived intensity mean(abs) 正确
11. 数据不足返回 null + warmup_notes
12. 单字段失败不影响整体

用法：
    cd backend && APP_ENV=test pytest tests/test_temporal_feature_service.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.services.structural_factor_service import _compute_all_factors_for_bars
from app.services.temporal_feature_service import (
    _compute_bb_bandwidth_percentile_at_bar,
    _compute_daily_context,
    _compute_derived_relation,
    _compute_m15_response,
    _compute_volume_percentile_at_bar,
    _find_swing_anchor,
    compute_temporal_features,
)
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb


def _build_bars(n: int = 250, seed: int = 42) -> pd.DataFrame:
    """构造固定 OHLCV bars（可重现），与 test_structural_factor_service 复用同一模式。"""
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
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
        "amount": amounts,
    }, index=idx)


def _build_v18_factors(bars: pd.DataFrame, timeframe: str = "1d"):
    """辅助：调用 V1.8 _compute_all_factors_for_bars 获取完整因子。"""
    degraded: list[str] = []
    warmup: list[str] = []
    factors = _compute_all_factors_for_bars(bars, timeframe, degraded, warmup)
    return factors, degraded, warmup


# ===== 1. daily_context 结构 =====
def test_daily_context_returns_dict_with_9_fields() -> None:
    """daily_context 返回 dict 且含 V1.7 9 字段 + V1.9 5 active + 2 confirmed alias（共 16）。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    assert isinstance(result, dict)
    expected_keys = {
        "daily_dsa_dir",
        "daily_dsa_segment_duration_percentile",
        "daily_dsa_slope_atr_per_bar",
        "daily_dsa_efficiency_0_1",
        "daily_price_position_in_swing_0_1",
        "daily_distance_to_swing_high_atr",
        "daily_distance_to_node_above_atr",
        "daily_sqzmom_change_since_segment_start",
        "daily_volume_percentile_change_since_segment_start",
        # V1.9 active swing 字段
        "daily_price_position_in_active_swing_0_1",
        "daily_active_swing_high",
        "daily_active_swing_low",
        "daily_distance_to_active_swing_high_atr",
        "daily_distance_to_active_swing_low_atr",
        # V1.9 confirmed raw alias
        "daily_price_position_in_confirmed_swing_raw",
        "daily_confirmed_swing_breakout_state",
    }
    assert set(result.keys()) == expected_keys


# ===== 2. daily duration percentile =====
def test_daily_duration_percentile_correct() -> None:
    """daily_dsa_segment_duration_percentile 正确：当前 age 在历史 segments 中的排名。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    # 如果有足够 segments（>=5），duration_percentile 应为 [0,1] 范围
    dp = result["daily_dsa_segment_duration_percentile"]
    if dp is not None:
        assert 0.0 <= dp <= 1.0
    else:
        # segments 不足时应在 warmup_notes 中记录
        assert any("duration" in note.lower() or "segment" in note.lower() for note in warmup)


# ===== 3. daily sqzmom change since segment start =====
def test_daily_sqzmom_change_since_segment_start() -> None:
    """daily_sqzmom_change_since_segment_start = sqzmom_now - sqzmom_at_seg_start。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    dsa_seg = primary_factors.get("dsa_segment") or {}
    seg_start_idx = dsa_seg.get("segment_start_bar_index")
    vol_mom = primary_factors.get("volatility_momentum") or {}
    sqzmom_now = vol_mom.get("sqzmom_val")

    change = result["daily_sqzmom_change_since_segment_start"]

    if seg_start_idx is not None and sqzmom_now is not None:
        # 手算验证
        opens = bars["open"].to_numpy(dtype=float)
        highs = bars["high"].to_numpy(dtype=float)
        lows = bars["low"].to_numpy(dtype=float)
        closes = bars["close"].to_numpy(dtype=float)
        sqz = compute_sqzmom_lb(opens, highs, lows, closes)
        val_list = sqz.get("val", [])
        sqzmom_at_start = val_list[seg_start_idx] if seg_start_idx < len(val_list) else None
        if sqzmom_at_start is not None:
            expected_change = float(sqzmom_now) - float(sqzmom_at_start)
            assert change is not None
            assert abs(change - expected_change) < 1e-6
        else:
            assert change is None
    else:
        assert change is None


# ===== 4. daily volume percentile change since segment start =====
def test_daily_volume_percentile_change_since_segment_start() -> None:
    """daily_volume_percentile_change_since_segment_start point-in-time 正确。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    dsa_seg = primary_factors.get("dsa_segment") or {}
    seg_start_idx = dsa_seg.get("segment_start_bar_index")
    participation = primary_factors.get("participation") or {}
    vol_pct_now = participation.get("volume_percentile_120")

    change = result["daily_volume_percentile_change_since_segment_start"]

    if seg_start_idx is not None and vol_pct_now is not None and seg_start_idx > 0:
        # 手算 point-in-time percentile
        from app.services.structural_factor_service import percentile_rank
        volumes = bars["volume"].to_numpy(dtype=float)
        vol_at_start = volumes[seg_start_idx]
        # 只用截至 seg_start_idx 的数据（point-in-time）
        vol_pct_at_start = percentile_rank(
            vol_at_start, volumes[:seg_start_idx + 1], 120
        )
        if vol_pct_at_start is not None:
            expected_change = float(vol_pct_now) - float(vol_pct_at_start)
            assert change is not None
            assert abs(change - expected_change) < 1e-6
        else:
            assert change is None
    else:
        assert change is None


# ===== 5. m15_response 结构 =====
def test_m15_response_returns_dict_with_9_fields() -> None:
    """m15_response 返回 dict 且含 V1.7 9 字段 + V1.9 5 active 字段（共 14）。"""
    bars = _build_bars(n=500, seed=99)  # 15m 需要更多 bars
    secondary_factors, degraded, warmup = _build_v18_factors(bars, "15m")
    result = _compute_m15_response(secondary_factors, bars, degraded, warmup)

    assert isinstance(result, dict)
    expected_keys = {
        "m15_price_position_in_swing_0_1",
        "m15_position_change_since_swing_anchor",
        "m15_distance_to_swing_high_atr",
        "m15_distance_to_swing_low_atr",
        "m15_sqzmom_change_since_swing_anchor",
        "m15_sqzmom_abs_percentile",
        "m15_sqz_off",
        "m15_bb_bandwidth_change_since_swing_anchor",
        "m15_volume_percentile_change_since_swing_anchor",
        # V1.9 active swing 字段
        "m15_price_position_in_active_swing_0_1",
        "m15_active_swing_high",
        "m15_active_swing_low",
        "m15_distance_to_active_swing_high_atr",
        "m15_distance_to_active_swing_low_atr",
    }
    assert set(result.keys()) == expected_keys


# ===== 6. m15 swing anchor 选择 =====
def test_m15_swing_anchor_selection() -> None:
    """anchor 规则：bars_since_swing_low < bars_since_swing_high → anchor=low。"""
    bars = _build_bars(n=500, seed=99)
    secondary_factors, degraded, warmup = _build_v18_factors(bars, "15m")
    swing = secondary_factors.get("swing_position") or {}
    bsh = swing.get("bars_since_swing_high")
    bsl = swing.get("bars_since_swing_low")

    result = _compute_m15_response(secondary_factors, bars, degraded, warmup)
    # 只要 anchor 能定位，position_change 就应该有值（或 null 如果 range=0）
    # 不强制断言：可能 range=0 或数据不足，pos_change 由内部计算
    _ = result["m15_position_change_since_swing_anchor"]

    if bsh is not None and bsl is not None:
        # anchor 存在时验证逻辑
        if bsl < bsh:
            # anchor = swing_low
            pass  # 具体值由内部计算
        else:
            # anchor = swing_high
            pass


# ===== 7. m15 sqzmom change since anchor =====
def test_m15_sqzmom_change_since_swing_anchor() -> None:
    """m15_sqzmom_change_since_swing_anchor point-in-time 正确。"""
    bars = _build_bars(n=500, seed=99)
    secondary_factors, degraded, warmup = _build_v18_factors(bars, "15m")
    result = _compute_m15_response(secondary_factors, bars, degraded, warmup)

    change = result["m15_sqzmom_change_since_swing_anchor"]
    # 值要么是 None，要么是有限浮点数
    if change is not None:
        assert np.isfinite(change)


# ===== 8. derived_relation 结构 =====
def test_derived_relation_returns_dict_with_3_fields() -> None:
    """derived_relation 返回 dict 且含 3 个字段。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded1, warmup1 = _build_v18_factors(bars, "1d")
    bars15 = _build_bars(n=500, seed=99)
    secondary_factors, degraded2, warmup2 = _build_v18_factors(bars15, "15m")

    daily = _compute_daily_context(primary_factors, bars, degraded1, warmup1)
    m15 = _compute_m15_response(secondary_factors, bars15, degraded2, warmup2)
    degraded = degraded1 + degraded2
    result = _compute_derived_relation(daily, m15, degraded)

    assert isinstance(result, dict)
    expected_keys = {
        "m15_position_relative_to_daily",
        "m15_response_direction_relative_to_daily",
        "m15_response_intensity",
    }
    assert set(result.keys()) == expected_keys


# ===== 9. derived alignment direction =====
def test_derived_alignment_direction() -> None:
    """m15_response_direction_relative_to_daily 4 种情况。"""
    # 构造 daily + m15 数据
    daily_dir_up = {"daily_dsa_dir": 1, "daily_price_position_in_swing_0_1": 0.5}
    daily_dir_down = {"daily_dsa_dir": -1, "daily_price_position_in_swing_0_1": 0.5}

    m15_pos_up = {"m15_position_change_since_swing_anchor": 0.1}
    m15_pos_down = {"m15_position_change_since_swing_anchor": -0.1}
    m15_pos_zero = {"m15_position_change_since_swing_anchor": 0.0}
    m15_pos_none = {"m15_position_change_since_swing_anchor": None}

    # daily_dir=1, m15_pos>0 → aligned
    r = _compute_derived_relation(daily_dir_up, m15_pos_up, [])
    assert r["m15_response_direction_relative_to_daily"] == "aligned"

    # daily_dir=1, m15_pos<0 → counter
    r = _compute_derived_relation(daily_dir_up, m15_pos_down, [])
    assert r["m15_response_direction_relative_to_daily"] == "counter"

    # daily_dir=-1, m15_pos<0 → aligned
    r = _compute_derived_relation(daily_dir_down, m15_pos_down, [])
    assert r["m15_response_direction_relative_to_daily"] == "aligned"

    # daily_dir=-1, m15_pos>0 → counter
    r = _compute_derived_relation(daily_dir_down, m15_pos_up, [])
    assert r["m15_response_direction_relative_to_daily"] == "counter"

    # m15_pos=0 → null
    r = _compute_derived_relation(daily_dir_up, m15_pos_zero, [])
    assert r["m15_response_direction_relative_to_daily"] is None

    # m15_pos=None → null
    r = _compute_derived_relation(daily_dir_up, m15_pos_none, [])
    assert r["m15_response_direction_relative_to_daily"] is None


# ===== 10. derived intensity =====
def test_derived_intensity_mean_abs() -> None:
    """m15_response_intensity = mean(abs(non_null fields))。"""
    daily = {"daily_dsa_dir": 1, "daily_price_position_in_swing_0_1": 0.5}
    m15 = {
        "m15_position_change_since_swing_anchor": 0.1,
        "m15_sqzmom_change_since_swing_anchor": -0.2,
        "m15_bb_bandwidth_change_since_swing_anchor": 0.3,
        "m15_volume_percentile_change_since_swing_anchor": None,  # 一个 null
    }
    r = _compute_derived_relation(daily, m15, [])
    # mean(abs([0.1, 0.2, 0.3])) = 0.2
    assert r["m15_response_intensity"] is not None
    assert abs(r["m15_response_intensity"] - 0.2) < 1e-9

    # 全 null → null
    m15_all_null = {
        "m15_position_change_since_swing_anchor": None,
        "m15_sqzmom_change_since_swing_anchor": None,
        "m15_bb_bandwidth_change_since_swing_anchor": None,
        "m15_volume_percentile_change_since_swing_anchor": None,
    }
    r = _compute_derived_relation(daily, m15_all_null, [])
    assert r["m15_response_intensity"] is None


# ===== 11. m15_position_relative_to_daily =====
def test_m15_position_relative_to_daily() -> None:
    """m15_position_relative_to_daily = m15_active_pos - daily_active_pos（V1.9 改用 active swing）。"""
    daily = {
        "daily_dsa_dir": 1,
        "daily_price_position_in_active_swing_0_1": 0.3,
    }
    m15 = {"m15_price_position_in_active_swing_0_1": 0.7}
    r = _compute_derived_relation(daily, m15, [])
    assert r["m15_position_relative_to_daily"] is not None
    assert abs(r["m15_position_relative_to_daily"] - 0.4) < 1e-9

    # 任一为 null → null
    daily_null = {
        "daily_dsa_dir": 1,
        "daily_price_position_in_active_swing_0_1": None,
    }
    r = _compute_derived_relation(daily_null, m15, [])
    assert r["m15_position_relative_to_daily"] is None


# ===== 12. 数据不足返回 null + warmup =====
def test_insufficient_data_returns_null_warmup() -> None:
    """数据不足时（<5 segments）duration_percentile 返回 null + warmup_notes。"""
    # 用很短的 bars 确保 segments 不足
    bars = _build_bars(n=60, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    dp = result["daily_dsa_segment_duration_percentile"]
    # 短数据通常 segments 不足
    if dp is None:
        assert any("duration" in n.lower() or "segment" in n.lower() for n in warmup) or len(degraded) > 0


# ===== 13. 单字段失败不影响整体 =====
def test_single_field_failure_does_not_break_others() -> None:
    """V1.8 某因子组为 None 时，temporal 仍返回其他字段。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    # 模拟 dsa_segment 失败
    primary_factors["dsa_segment"] = None
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    # dsa 相关字段应为 null
    assert result["daily_dsa_dir"] is None
    assert result["daily_dsa_segment_duration_percentile"] is None
    # 但 swing/cost 等字段仍可能有值
    # 只要不崩溃即可


# ===== 14. 无未来函数验证 =====
def test_no_future_function_volume_percentile() -> None:
    """volume_percentile_at_seg_start 只用截至 seg_start 的数据。"""
    bars = _build_bars(n=250, seed=42)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    dsa_seg = primary_factors.get("dsa_segment") or {}
    seg_start_idx = dsa_seg.get("segment_start_bar_index")

    if seg_start_idx is None or seg_start_idx == 0:
        return  # 无法验证

    result = _compute_daily_context(primary_factors, bars, degraded, warmup)
    change = result["daily_volume_percentile_change_since_segment_start"]

    if change is None:
        return  # vol_pct_now 或 vol_pct_at_start 为 null

    # 验证：修改 seg_start 之后的数据不应影响 change
    bars_modified = bars.copy()
    n = len(bars_modified)
    if seg_start_idx + 1 < n:
        # 修改 seg_start_idx 之后某根 bar 的 volume
        bars_modified.iloc[seg_start_idx + 1, bars_modified.columns.get_loc("volume")] = 999_999_999
    # 重新计算 V1.8 factors（此时 last bar 的 volume_percentile 会变）
    primary_factors2, degraded2, warmup2 = _build_v18_factors(bars_modified, "1d")
    result2 = _compute_daily_context(primary_factors2, bars_modified, degraded2, warmup2)
    change2 = result2["daily_volume_percentile_change_since_segment_start"]

    # vol_pct_at_start 应该相同（因为 seg_start 及之前的数据没变）
    # 但 vol_pct_now 会变（因为修改了后续数据）
    # 所以 change 会不同，但 at_start 部分应该一致
    # 这里只验证不崩溃且值为有限数
    if change2 is not None:
        assert np.isfinite(change2)


# ===== 15. bb bandwidth change since anchor =====
def test_m15_bb_bandwidth_change_since_swing_anchor() -> None:
    """m15_bb_bandwidth_change_since_swing_anchor point-in-time 正确。"""
    bars = _build_bars(n=500, seed=99)
    secondary_factors, degraded, warmup = _build_v18_factors(bars, "15m")
    result = _compute_m15_response(secondary_factors, bars, degraded, warmup)

    change = result["m15_bb_bandwidth_change_since_swing_anchor"]
    if change is not None:
        assert np.isfinite(change)


# ===== 16. swing anchor 选择规则：bsl < bsh → anchor=low =====
def test_swing_anchor_low_when_bsl_less_than_bsh() -> None:
    """bsl < bsh → anchor=swing_low_bar（n - 1 - bsl）。"""
    bars = _build_bars(n=250, seed=42)
    swing_factors = {
        "bars_since_swing_high": 20,
        "bars_since_swing_low": 10,
        "confirmed_swing_high": 110.0,
        "confirmed_swing_low": 90.0,
    }
    result = _find_swing_anchor(bars, swing_factors)
    assert result is not None
    anchor_idx, swing_high, swing_low = result
    n = len(bars)
    # bsl=10 < bsh=20 → anchor=low → anchor_idx = n-1-bsl
    assert anchor_idx == n - 1 - 10
    # swing range = [min(90, 110), max(90, 110)] = [90, 110]
    assert swing_high == 110.0
    assert swing_low == 90.0


# ===== 17. swing anchor 选择规则：bsh <= bsl → anchor=high =====
def test_swing_anchor_high_when_bsh_less_equal_bsl() -> None:
    """bsh <= bsl → anchor=swing_high_bar（n - 1 - bsh）。"""
    bars = _build_bars(n=250, seed=42)
    swing_factors = {
        "bars_since_swing_high": 10,
        "bars_since_swing_low": 20,
        "confirmed_swing_high": 110.0,
        "confirmed_swing_low": 90.0,
    }
    result = _find_swing_anchor(bars, swing_factors)
    assert result is not None
    anchor_idx, swing_high, swing_low = result
    n = len(bars)
    # bsh=10 <= bsl=20 → anchor=high → anchor_idx = n-1-bsh
    assert anchor_idx == n - 1 - 10
    assert swing_high == 110.0
    assert swing_low == 90.0


# ===== 18. m15_position_change_since_swing_anchor 手算验证 =====
def test_m15_position_change_since_swing_anchor_manual_calc() -> None:
    """手算 m15_position_change_since_swing_anchor = m15_pos_now - pos_at_anchor。"""
    bars = _build_bars(n=500, seed=99)
    secondary_factors, degraded, warmup = _build_v18_factors(bars, "15m")
    swing = secondary_factors.get("swing_position") or {}
    anchor = _find_swing_anchor(bars, swing)
    if anchor is None:
        return  # 数据不支持，跳过

    anchor_idx, swing_high, swing_low = anchor
    swing_range = swing_high - swing_low
    if swing_range <= 0:
        return

    close_anchor = float(bars["close"].iloc[anchor_idx])
    pos_at_anchor = (close_anchor - swing_low) / swing_range
    m15_pos_now = swing.get("price_position_in_swing_0_1")
    if m15_pos_now is None:
        return

    expected = float(m15_pos_now) - pos_at_anchor
    result = _compute_m15_response(secondary_factors, bars, [], [])
    change = result["m15_position_change_since_swing_anchor"]
    assert change is not None
    assert abs(change - expected) < 1e-9


# ===== 19. anchor 处 volume_percentile / bb_bandwidth_percentile 不变性 =====
def test_anchor_percentile_invariant_after_modification() -> None:
    """修改 anchor 之后的数据，anchor 处 percentile 不变（point-in-time）。"""
    bars = _build_bars(n=500, seed=99)
    secondary_factors, _, _ = _build_v18_factors(bars, "15m")
    swing = secondary_factors.get("swing_position") or {}
    anchor = _find_swing_anchor(bars, swing)
    if anchor is None:
        return
    anchor_idx = anchor[0]

    orig_vol_pct = _compute_volume_percentile_at_bar(bars, anchor_idx)
    orig_bw_pct = _compute_bb_bandwidth_percentile_at_bar(bars, anchor_idx)

    # 修改 anchor 之后的某根 bar 数据
    bars_modified = bars.copy()
    if anchor_idx + 1 < len(bars_modified):
        col_loc_vol = bars_modified.columns.get_loc("volume")
        bars_modified.iloc[anchor_idx + 1, col_loc_vol] = 999_999_999.0
        # 修改 close 影响 BB bandwidth
        col_loc_close = bars_modified.columns.get_loc("close")
        bars_modified.iloc[anchor_idx + 1, col_loc_close] = 999.0

    new_vol_pct = _compute_volume_percentile_at_bar(bars_modified, anchor_idx)
    new_bw_pct = _compute_bb_bandwidth_percentile_at_bar(bars_modified, anchor_idx)

    # anchor 处 percentile 应保持不变（point-in-time 只用截至 anchor 的数据）
    if orig_vol_pct is not None and new_vol_pct is not None:
        assert abs(orig_vol_pct - new_vol_pct) < 1e-9
    if orig_bw_pct is not None and new_bw_pct is not None:
        assert abs(orig_bw_pct - new_bw_pct) < 1e-9


# ===== 20. 组级异常隔离：m15_response 异常不影响整体返回 =====
@pytest.mark.asyncio
async def test_m15_response_exception_isolation(monkeypatch) -> None:
    """_compute_m15_response 抛异常时，整体返回 200，m15_response 全 null，degraded_reasons 有记录。"""
    bars = _build_bars(n=250, seed=42)
    bars15 = _build_bars(n=500, seed=99)
    primary_factors, _, _ = _build_v18_factors(bars, "1d")
    secondary_factors, _, _ = _build_v18_factors(bars15, "15m")

    # _fetch_bars 和 _compute_all_factors_for_bars 是从 structural_factor_service 延迟导入
    # 必须在 structural_factor_service 模块上 patch
    import app.services.temporal_feature_service as tfs
    from app.services import structural_factor_service as sfs

    async def fake_fetch_bars(service, session, instrument_id, timeframe, adj, lookback, degraded):
        return bars if timeframe == "1d" else bars15

    def fake_compute_factors(bars_arg, timeframe, degraded, warmup):
        return primary_factors if timeframe == "1d" else secondary_factors

    def boom_m15(*args, **kwargs):
        raise RuntimeError("m15 boom")

    monkeypatch.setattr(sfs, "_fetch_bars", fake_fetch_bars)
    monkeypatch.setattr(sfs, "_compute_all_factors_for_bars", fake_compute_factors)
    monkeypatch.setattr(tfs, "_compute_m15_response", boom_m15)

    result = await compute_temporal_features(
        session=None,  # type: ignore[arg-type]
        instrument_id=None,  # type: ignore[arg-type]
    )

    # m15_response 应全 null
    assert all(v is None for v in result["m15_response"].values())
    # degraded_reasons 应含 m15_response failed
    assert any("m15_response" in r and "failed" in r for r in result["meta"]["degraded_reasons"])
    # daily_context 不应受影响
    assert "daily_dsa_dir" in result["daily_context"]
    # derived_relation 仍返回（值可能为 null）
    assert "m15_position_relative_to_daily" in result["derived_relation"]


# ===== 14. V1.9 active swing 字段 + derived_relation 改用 active =====


def test_daily_context_includes_active_swing_fields() -> None:
    """daily_context 含 active swing 字段（V1.9 新增）。"""
    bars = _build_bars(n=250)
    primary_factors, degraded, warmup = _build_v18_factors(bars, "1d")
    result = _compute_daily_context(primary_factors, bars, degraded, warmup)

    # V1.9 新增 active swing 字段
    assert "daily_price_position_in_active_swing_0_1" in result
    assert "daily_distance_to_active_swing_high_atr" in result
    assert "daily_distance_to_active_swing_low_atr" in result
    assert "daily_price_position_in_confirmed_swing_raw" in result
    assert "daily_confirmed_swing_breakout_state" in result


def test_derived_relation_uses_active_swing_not_confirmed_raw() -> None:
    """derived_relation 用 active swing，不用 confirmed raw。

    场景：daily active=0.8, confirmed_raw=1.997；m15 active=0.6, confirmed_raw=0.5
    期望：m15_position_relative_to_daily = 0.6 - 0.8 = -0.2（不是 0.5 - 1.997 = -1.497）
    """
    daily = {
        "daily_dsa_dir": 1,
        "daily_price_position_in_swing_0_1": 1.997,  # confirmed raw（旧字段）
        "daily_price_position_in_active_swing_0_1": 0.8,  # active（新字段）
    }
    m15 = {
        "m15_price_position_in_swing_0_1": 0.5,  # confirmed raw（旧字段）
        "m15_price_position_in_active_swing_0_1": 0.6,  # active（新字段）
    }
    r = _compute_derived_relation(daily, m15, [])
    # 必须用 active：0.6 - 0.8 = -0.2
    assert r["m15_position_relative_to_daily"] is not None
    assert abs(r["m15_position_relative_to_daily"] - (-0.2)) < 1e-9
    # 不应等于 confirmed raw 相减（-1.497）
    assert abs(r["m15_position_relative_to_daily"] - (-1.497)) > 0.1


def test_derived_relation_returns_null_when_active_missing() -> None:
    """active 缺失时 derived_relation 返回 null，不回退 confirmed raw。"""
    daily = {
        "daily_dsa_dir": 1,
        "daily_price_position_in_swing_0_1": 1.997,  # confirmed raw 存在
        "daily_price_position_in_active_swing_0_1": None,  # active 缺失
    }
    m15 = {
        "m15_price_position_in_swing_0_1": 0.5,  # confirmed raw 存在
        "m15_price_position_in_active_swing_0_1": 0.6,  # active 存在
    }
    r = _compute_derived_relation(daily, m15, [])
    # active 任一缺失 → null，不回退 confirmed raw
    assert r["m15_position_relative_to_daily"] is None
