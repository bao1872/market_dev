"""结构状态因子服务 - 双周期结构状态面板后端。

V1 范围：
- 只按需计算单只股票最新已完成 bar
- 不做全市场/全历史预计算
- 不新增重型定时任务
- 不新增数据库表

5 组因子（每组独立计算，单组失败返回 null + degraded_reasons）：
1. DSA 段质量（当前段/前一段/对比）
2. Swing 结构位置（已确认 pivot，无未来函数）
3. 成本/节点（Volume Profile / POC / Node）
4. 动量/波动（Bollinger Bands + SQZMOM_LB）
5. 成交参与（Volume ratio / percentile）

用法：
    from app.services.structural_factor_service import compute_structural_factors
    result = await compute_structural_factors(
        session, instrument_id, primary_timeframe="1d", secondary_timeframe="15m"
    )

模块自测：
    python -m app.services.structural_factor_service
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import DAILY_HISTORY_BARS
from app.services.market_data_aggregation_service import (
    MarketDataAggregationService,
)
from app.strategy.selectors.dsa_selector import compute_dsa_bundle
from app.strategy_assets.algorithms.features.atr_utils import compute_atr
from app.strategy_assets.algorithms.features.bollinger_features_plotly import (
    bollinger,
)
from app.strategy_assets.algorithms.features.price_action_toolkit_lite_ualgo import (
    _tv_pivots_confirmed,
)
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb
from app.strategy_assets.algorithms.features.unified_volume_profile import (
    compute_unified_volume_profile,
)

logger = logging.getLogger(__name__)

# 固定参数（_PRIMARY_LOOKBACK 引用 DAILY_HISTORY_BARS 唯一真源，禁止硬编码 250）
_PRIMARY_LOOKBACK = DAILY_HISTORY_BARS
_SECONDARY_LOOKBACK = 500
_SWING_LENGTH = 5  # TradingView 默认风格，左右各 5 根确认
_BB_WIN = 20
_BB_K = 2.0
_ATR_LENGTH = 14
_PERCENTILE_LOOKBACK = 120  # 约 6 个月日线


def percentile_rank(
    value: float, series: np.ndarray, lookback: int
) -> float | None:
    """计算 value 在 series 末尾 lookback 窗口内的百分位排名 [0,1]。

    Args:
        value: 待排名的值
        series: 完整序列（取末尾 lookback 个）
        lookback: 回看窗口长度

    Returns:
        float in [0, 1] 或 None（value 为 NaN 或 series 为空时）
    """
    if not np.isfinite(value):
        return None
    if len(series) == 0:
        return None
    window = series[-lookback:] if len(series) >= lookback else series
    finite = window[np.isfinite(window)]
    if len(finite) == 0:
        return None
    return float(np.sum(finite <= value) / len(finite))


# =============================================================================
# 因子组 1：成交参与（V1.8 含段级成交量）
# =============================================================================
def _compute_participation_factors(
    bars: pd.DataFrame,
    dsa_segment_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """计算成交参与因子。

    Returns:
        V1.7 保留字段 + V1.8 段级成交量字段（从 dsa_segment 共享）
    """
    result: dict[str, Any] = {
        "volume_ratio_20": None,
        "volume_percentile_120": None,
        # V1.8 段级成交量字段（从 dsa_segment 共享）
        "current_segment_volume_sum": None,
        "prev_segment_volume_sum": None,
        "current_vs_prev_volume_ratio": None,
        "current_segment_return_per_volume": None,
        "prev_segment_return_per_volume": None,
        "return_per_volume_ratio": None,
    }
    if bars is None or len(bars) < 20 or "volume" not in bars.columns:
        return result
    volumes = bars["volume"].to_numpy(dtype=float)
    last_vol = volumes[-1]
    if not np.isfinite(last_vol):
        return result
    sma_20 = float(np.mean(volumes[-20:]))
    if sma_20 > 0:
        result["volume_ratio_20"] = float(last_vol / sma_20)
    result["volume_percentile_120"] = percentile_rank(
        last_vol, volumes, _PERCENTILE_LOOKBACK
    )

    # V1.8 从 dsa_segment 共享段级成交量字段
    if dsa_segment_result is not None:
        for key in [
            "current_segment_volume_sum",
            "prev_segment_volume_sum",
            "current_vs_prev_volume_ratio",
            "current_segment_return_per_volume",
            "prev_segment_return_per_volume",
            "return_per_volume_ratio",
        ]:
            val = dsa_segment_result.get(key)
            if val is not None:
                result[key] = val

    return result


# =============================================================================
# 因子组 2：动量/波动 (BB + SQZMOM)
# =============================================================================
def _compute_volatility_momentum_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算动量/波动因子 (Bollinger Bands + SQZMOM_LB)。

    V1.8 新增字段：
    - distance_to_bb_upper_atr: (close - bb_upper) / last_atr
    - distance_to_bb_lower_atr: (close - bb_lower) / last_atr
    - sqzmom_abs_percentile: abs(sqzmom_val) 在 abs(sqzmom_series) 120 日内的百分位
    - sqz_on: 最后一根 bar 是否 squeeze on（bool）
    - sqz_off: 最后一根 bar 是否 squeeze off（bool）

    Returns:
        V1.7 保留字段 + V1.8 新增字段
    """
    result: dict[str, Any] = {
        "bb_percent_b": None,
        "bb_bandwidth_pct": None,
        "bb_bandwidth_percentile": None,
        "sqzmom_val": None,
        "sqzmom_delta_1": None,
        "sqzmom_percentile": None,
        # V1.8 新增字段
        "distance_to_bb_upper_atr": None,
        "distance_to_bb_lower_atr": None,
        "sqzmom_abs_percentile": None,
        "sqz_on": None,
        "sqz_off": None,
    }
    if bars is None or len(bars) < _BB_WIN + 10:
        return result

    closes = bars["close"].to_numpy(dtype=float)
    last_close = closes[-1] if len(closes) > 0 else None
    last_atr = (
        float(atr[-1])
        if atr is not None and len(atr) > 0
        and np.isfinite(atr[-1]) and atr[-1] > 0
        else None
    )

    # Bollinger Bands
    mid, upper, lower = bollinger(bars, _BB_WIN, _BB_K)
    mid_arr = mid.to_numpy(dtype=float)
    upper_arr = upper.to_numpy(dtype=float)
    lower_arr = lower.to_numpy(dtype=float)
    last_mid = mid_arr[-1]
    last_upper = upper_arr[-1]
    last_lower = lower_arr[-1]

    if np.isfinite(last_close) and np.isfinite(last_upper) and np.isfinite(last_lower):
        bw = last_upper - last_lower
        if bw > 0:
            result["bb_percent_b"] = float((last_close - last_lower) / bw)
        if np.isfinite(last_mid) and last_mid > 0:
            result["bb_bandwidth_pct"] = float(bw / last_mid)
        # bandwidth percentile
        bandwidth_series = (upper_arr - lower_arr) / np.where(mid_arr > 0, mid_arr, np.nan)
        result["bb_bandwidth_percentile"] = percentile_rank(
            result["bb_bandwidth_pct"], bandwidth_series, _PERCENTILE_LOOKBACK
        )
        # V1.8 distance_to_bb_upper/lower_atr
        if last_atr is not None:
            result["distance_to_bb_upper_atr"] = float(
                (last_close - last_upper) / last_atr
            )
            result["distance_to_bb_lower_atr"] = float(
                (last_close - last_lower) / last_atr
            )

    # SQZMOM_LB
    opens = bars["open"].to_numpy(dtype=float) if "open" in bars.columns else closes
    highs = bars["high"].to_numpy(dtype=float) if "high" in bars.columns else closes
    lows = bars["low"].to_numpy(dtype=float) if "low" in bars.columns else closes
    sqz = compute_sqzmom_lb(opens, highs, lows, closes)
    val_list = sqz.get("val", [])
    # V1.8 sqz_on / sqz_off（bool 列表，取最后一根 bar）
    sqz_on_list = sqz.get("sqzOn", []) or []
    sqz_off_list = sqz.get("sqzOff", []) or []
    if sqz_on_list:
        result["sqz_on"] = bool(sqz_on_list[-1])
    if sqz_off_list:
        result["sqz_off"] = bool(sqz_off_list[-1])
    if val_list:
        last_val = val_list[-1]
        if last_val is not None:
            result["sqzmom_val"] = float(last_val)
            if len(val_list) >= 2 and val_list[-2] is not None:
                result["sqzmom_delta_1"] = float(last_val - val_list[-2])
            val_arr = np.array(
                [v if v is not None else np.nan for v in val_list], dtype=float
            )
            result["sqzmom_percentile"] = percentile_rank(
                float(last_val), val_arr, _PERCENTILE_LOOKBACK
            )
            # V1.8 sqzmom_abs_percentile: 基于 abs 值的百分位
            abs_val_arr = np.abs(val_arr)
            result["sqzmom_abs_percentile"] = percentile_rank(
                abs(float(last_val)), abs_val_arr, _PERCENTILE_LOOKBACK
            )
    return result


# =============================================================================
# 因子组 3：Swing 结构位置（V1.8 完整位置分析）
# =============================================================================
def _compute_swing_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算 Swing 结构位置因子（已确认 pivot，无未来函数）。

    Returns:
        V1.7 保留字段 + V1.8 新增字段（swing_range/position/atr_distance/retracement）
    """
    result: dict[str, Any] = {
        "confirmed_swing_high": None,
        "confirmed_swing_low": None,
        "bars_since_swing_high": None,
        "bars_since_swing_low": None,
        "swing_high_to_close_pct": None,
        "swing_low_to_close_pct": None,
        # V1.8 新增字段
        "swing_range": None,
        "price_position_in_swing_0_1": None,
        "distance_to_swing_high_atr": None,
        "distance_to_swing_low_atr": None,
        "retracement_from_high_0_1": None,
        "rebound_from_low_0_1": None,
    }
    if bars is None or len(bars) < 2 * _SWING_LENGTH + 1:
        return result

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    ph, pl, ph_anchor, pl_anchor = _tv_pivots_confirmed(highs, lows, _SWING_LENGTH)

    last_close = closes[-1] if len(closes) > 0 else None
    last_atr = float(atr[-1]) if atr is not None and len(atr) > 0 and np.isfinite(atr[-1]) and atr[-1] > 0 else None

    # 找最后一个非 NaN 的 swing high
    ph_valid = np.where(np.isfinite(ph))[0]
    if len(ph_valid) > 0:
        last_ph_idx = ph_valid[-1]
        last_ph_anchor = int(ph_anchor[last_ph_idx]) if np.isfinite(
            ph_anchor[last_ph_idx]
        ) else last_ph_idx
        result["confirmed_swing_high"] = float(ph[last_ph_idx])
        result["bars_since_swing_high"] = int(len(highs) - 1 - last_ph_anchor)
        if last_close is not None and np.isfinite(last_close) and ph[last_ph_idx] > 0:
            result["swing_high_to_close_pct"] = float(
                (last_close - ph[last_ph_idx]) / ph[last_ph_idx]
            )
        # V1.8 distance_to_swing_high_atr
        if last_close is not None and last_atr is not None:
            result["distance_to_swing_high_atr"] = float(
                (last_close - ph[last_ph_idx]) / last_atr
            )

    # 找最后一个非 NaN 的 swing low
    pl_valid = np.where(np.isfinite(pl))[0]
    if len(pl_valid) > 0:
        last_pl_idx = pl_valid[-1]
        last_pl_anchor = int(pl_anchor[last_pl_idx]) if np.isfinite(
            pl_anchor[last_pl_idx]
        ) else last_pl_idx
        result["confirmed_swing_low"] = float(pl[last_pl_idx])
        result["bars_since_swing_low"] = int(len(lows) - 1 - last_pl_anchor)
        if last_close is not None and np.isfinite(last_close) and pl[last_pl_idx] > 0:
            result["swing_low_to_close_pct"] = float(
                (last_close - pl[last_pl_idx]) / pl[last_pl_idx]
            )
        # V1.8 distance_to_swing_low_atr
        if last_close is not None and last_atr is not None:
            result["distance_to_swing_low_atr"] = float(
                (last_close - pl[last_pl_idx]) / last_atr
            )

    # V1.8 swing_range / position / retracement / rebound
    sh = result["confirmed_swing_high"]
    sl = result["confirmed_swing_low"]
    if sh is not None and sl is not None:
        swing_range = sh - sl
        result["swing_range"] = float(swing_range)
        if last_close is not None and swing_range > 0:
            result["price_position_in_swing_0_1"] = float(
                (last_close - sl) / swing_range
            )
            result["retracement_from_high_0_1"] = float(
                (sh - last_close) / swing_range
            )
            result["rebound_from_low_0_1"] = float(
                (last_close - sl) / swing_range
            )
    return result


# =============================================================================
# 因子组 4：DSA 段质量（V1.8 完整段分析）
# =============================================================================
def _compute_dsa_segment_factors(
    bars: pd.DataFrame,
    dsa_bundle: dict[str, Any],
    atr: np.ndarray | None = None,
) -> dict[str, Any]:
    """计算 DSA 段质量因子（V1.8 完整段分析）。

    从 visual_segments[-1] 推导当前段，visual_segments[-2] 推导前一段。
    段收益/斜率/效率一律基于 close 或 segment 实际端点价格，不用 dsa_vwap 替代价格。

    Returns:
        V1.7 保留字段 + V1.8 新增字段（基础/当前段/前一段/段间对比）
    """
    # V1.7 保留字段
    result: dict[str, Any] = {
        "segment_id": None,
        "segment_dir": None,
        "segment_start_price": None,
        "segment_start_bar_index": None,
        "age_bars": None,
        "segment_extents_pct": None,
        # V1.8 基础字段
        "dsa_value": None,
        "price_vs_dsa_atr": None,
        # V1.8 当前段字段
        "current_dsa_segment_id": None,
        "current_dsa_segment_dir": None,
        "current_dsa_segment_age_bars": None,
        "current_dsa_segment_return_pct": None,
        "current_dsa_segment_slope_pct_per_bar": None,
        "current_dsa_segment_slope_atr_per_bar": None,
        "current_dsa_segment_efficiency_0_1": None,
        "current_segment_volume_sum": None,
        # V1.8 前一段字段
        "prev_dsa_segment_dir": None,
        "prev_dsa_segment_age_bars": None,
        "prev_dsa_segment_return_pct": None,
        "prev_dsa_segment_slope_pct_per_bar": None,
        "prev_dsa_segment_slope_atr_per_bar": None,
        "prev_dsa_segment_efficiency_0_1": None,
        "prev_segment_volume_sum": None,
        # V1.8 段间对比字段
        "segment_return_abs_ratio": None,
        "segment_slope_abs_ratio": None,
        "segment_duration_ratio": None,
        "segment_efficiency_delta": None,
        "current_vs_prev_volume_ratio": None,
        "current_segment_return_per_volume": None,
        "prev_segment_return_per_volume": None,
        "return_per_volume_ratio": None,
        "volume_per_1pct_return": None,
    }
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    visual_segments = dsa_bundle.get("visual_segments", [])
    if factor_per_bar is None or factor_per_bar.empty or len(visual_segments) == 0:
        return result

    closes = bars["close"].to_numpy(dtype=float) if "close" in bars.columns else None
    volumes = bars["volume"].to_numpy(dtype=float) if "volume" in bars.columns else None
    last_bar_index = len(bars) - 1
    last_close = float(closes[-1]) if closes is not None else None
    last_atr = float(atr[-1]) if atr is not None and len(atr) > 0 and np.isfinite(atr[-1]) else None

    # ========== 基础字段 ==========
    if "dsa_vwap" in factor_per_bar.columns:
        dsa_val = factor_per_bar["dsa_vwap"].iloc[-1]
        if pd.notna(dsa_val):
            result["dsa_value"] = float(dsa_val)
            if last_close is not None and last_atr is not None and last_atr > 0:
                result["price_vs_dsa_atr"] = float((last_close - float(dsa_val)) / last_atr)

    # ========== 当前段 = visual_segments[-1] ==========
    current_seg = visual_segments[-1]
    cur_points = current_seg.get("points", [])
    if not cur_points:
        return result

    # V1.7 保留字段（segment_start_price 仍用 points[0].value）
    cur_start_price = float(cur_points[0]["value"])
    cur_dir = int(current_seg.get("direction", 0))
    if "regime_id" in factor_per_bar.columns:
        cur_seg_id = factor_per_bar["regime_id"].iloc[-1]
        if pd.notna(cur_seg_id):
            result["segment_id"] = int(cur_seg_id)
            result["current_dsa_segment_id"] = int(cur_seg_id)
    result["segment_dir"] = cur_dir
    result["current_dsa_segment_dir"] = cur_dir
    result["segment_start_price"] = cur_start_price

    # 定位当前段起始 bar index
    cur_start_bar_idx = _find_bar_index_by_time(factor_per_bar.index, cur_points[0].get("time"))
    if cur_start_bar_idx is not None:
        result["segment_start_bar_index"] = int(cur_start_bar_idx)
        cur_age_bars = last_bar_index - cur_start_bar_idx + 1
        result["age_bars"] = int(last_bar_index - 1 - cur_start_bar_idx)  # V1.7 保留旧公式
        result["current_dsa_segment_age_bars"] = int(cur_age_bars)

        # 段内 close/volume/atr
        seg_closes = closes[cur_start_bar_idx:last_bar_index + 1] if closes is not None else None
        seg_volumes = volumes[cur_start_bar_idx:last_bar_index + 1] if volumes is not None else None
        seg_atr = atr[cur_start_bar_idx:last_bar_index + 1] if atr is not None else None

        # segment_extents_pct: 基于 close 修复 bug
        if last_close is not None and abs(cur_start_price) > 0:
            result["segment_extents_pct"] = float(
                (last_close - cur_start_price) / abs(cur_start_price)
            )

        # current_dsa_segment_return_pct = close_last / current_start_price - 1
        if last_close is not None and cur_start_price > 0:
            cur_return_pct = last_close / cur_start_price - 1.0
            result["current_dsa_segment_return_pct"] = float(cur_return_pct)
            # slope_pct_per_bar
            if cur_age_bars > 0:
                result["current_dsa_segment_slope_pct_per_bar"] = float(cur_return_pct / cur_age_bars)

        # current_dsa_segment_slope_atr_per_bar
        if (last_close is not None and seg_atr is not None and cur_age_bars > 0
                and np.any(np.isfinite(seg_atr))):
            mean_atr = float(np.nanmean(seg_atr))
            if mean_atr > 0:
                result["current_dsa_segment_slope_atr_per_bar"] = float(
                    (last_close - cur_start_price) / (mean_atr * cur_age_bars)
                )

        # current_dsa_segment_efficiency_0_1
        if seg_closes is not None and len(seg_closes) >= 2 and last_close is not None:
            diffs = np.abs(np.diff(seg_closes))
            path_sum = float(np.nansum(diffs))
            net = abs(last_close - cur_start_price)
            if path_sum > 0:
                result["current_dsa_segment_efficiency_0_1"] = float(net / path_sum)

        # current_segment_volume_sum
        if seg_volumes is not None:
            result["current_segment_volume_sum"] = float(np.nansum(seg_volumes))

    # ========== 前一段 = visual_segments[-2] ==========
    if len(visual_segments) >= 2:
        prev_seg = visual_segments[-2]
        prev_points = prev_seg.get("points", [])
        if len(prev_points) >= 2:
            prev_start_price = float(prev_points[0]["value"])
            prev_end_price = float(prev_points[-1]["value"])
            prev_dir = int(prev_seg.get("direction", 0))
            result["prev_dsa_segment_dir"] = prev_dir

            # 定位前一段 bar index
            prev_start_idx = _find_bar_index_by_time(factor_per_bar.index, prev_points[0].get("time"))
            prev_end_idx = _find_bar_index_by_time(factor_per_bar.index, prev_points[-1].get("time"))
            if prev_start_idx is not None and prev_end_idx is not None and prev_end_idx >= prev_start_idx:
                prev_age_bars = prev_end_idx - prev_start_idx + 1
                result["prev_dsa_segment_age_bars"] = int(prev_age_bars)

                # prev return
                if prev_start_price > 0:
                    prev_return_pct = prev_end_price / prev_start_price - 1.0
                    result["prev_dsa_segment_return_pct"] = float(prev_return_pct)
                    if prev_age_bars > 0:
                        result["prev_dsa_segment_slope_pct_per_bar"] = float(prev_return_pct / prev_age_bars)

                # prev slope_atr_per_bar
                prev_seg_atr = atr[prev_start_idx:prev_end_idx + 1] if atr is not None else None
                if prev_seg_atr is not None and np.any(np.isfinite(prev_seg_atr)) and prev_age_bars > 0:
                    mean_atr_prev = float(np.nanmean(prev_seg_atr))
                    if mean_atr_prev > 0:
                        result["prev_dsa_segment_slope_atr_per_bar"] = float(
                            (prev_end_price - prev_start_price) / (mean_atr_prev * prev_age_bars)
                        )

                # prev efficiency
                prev_seg_closes = closes[prev_start_idx:prev_end_idx + 1] if closes is not None else None
                if prev_seg_closes is not None and len(prev_seg_closes) >= 2:
                    prev_diffs = np.abs(np.diff(prev_seg_closes))
                    prev_path_sum = float(np.nansum(prev_diffs))
                    prev_net = abs(prev_end_price - prev_start_price)
                    if prev_path_sum > 0:
                        result["prev_dsa_segment_efficiency_0_1"] = float(prev_net / prev_path_sum)

                # prev volume sum
                prev_seg_volumes = volumes[prev_start_idx:prev_end_idx + 1] if volumes is not None else None
                if prev_seg_volumes is not None:
                    result["prev_segment_volume_sum"] = float(np.nansum(prev_seg_volumes))

        # ========== 段间对比字段 ==========
        cur_ret = result["current_dsa_segment_return_pct"]
        prev_ret = result["prev_dsa_segment_return_pct"]
        cur_slope_atr = result["current_dsa_segment_slope_atr_per_bar"]
        prev_slope_atr = result["prev_dsa_segment_slope_atr_per_bar"]
        cur_age = result["current_dsa_segment_age_bars"]
        prev_age = result["prev_dsa_segment_age_bars"]
        cur_eff = result["current_dsa_segment_efficiency_0_1"]
        prev_eff = result["prev_dsa_segment_efficiency_0_1"]
        cur_vol_sum = result["current_segment_volume_sum"]
        prev_vol_sum = result["prev_segment_volume_sum"]

        if cur_ret is not None and prev_ret is not None and abs(prev_ret) > 0:
            result["segment_return_abs_ratio"] = float(abs(cur_ret) / abs(prev_ret))
        if cur_slope_atr is not None and prev_slope_atr is not None and abs(prev_slope_atr) > 0:
            result["segment_slope_abs_ratio"] = float(abs(cur_slope_atr) / abs(prev_slope_atr))
        if cur_age is not None and prev_age is not None and prev_age > 0:
            result["segment_duration_ratio"] = float(cur_age / prev_age)
        if cur_eff is not None and prev_eff is not None:
            result["segment_efficiency_delta"] = float(cur_eff - prev_eff)
        if cur_vol_sum is not None and prev_vol_sum is not None and prev_vol_sum > 0:
            result["current_vs_prev_volume_ratio"] = float(cur_vol_sum / prev_vol_sum)

        # return per volume
        cur_rpv = cur_ret / cur_vol_sum if (cur_ret is not None and cur_vol_sum and cur_vol_sum > 0) else None
        prev_rpv = prev_ret / prev_vol_sum if (prev_ret is not None and prev_vol_sum and prev_vol_sum > 0) else None
        if cur_rpv is not None:
            result["current_segment_return_per_volume"] = float(cur_rpv)
        if prev_rpv is not None:
            result["prev_segment_return_per_volume"] = float(prev_rpv)
        if cur_rpv is not None and prev_rpv is not None and abs(prev_rpv) > 0:
            result["return_per_volume_ratio"] = float(cur_rpv / prev_rpv)
        if cur_ret is not None and abs(cur_ret) > 0 and cur_vol_sum is not None and cur_vol_sum > 0:
            result["volume_per_1pct_return"] = float(cur_vol_sum / abs(cur_ret * 100))

    return result


def _find_bar_index_by_time(
    idx_array: pd.Index, time_str: str | None
) -> int | None:
    """通过时间字符串匹配 pandas Index，返回位置 index。

    Args:
        idx_array: pandas Index（DatetimeIndex）
        time_str: "YYYY-MM-DD" 格式的时间字符串

    Returns:
        位置 index（int）或 None
    """
    if time_str is None:
        return None
    try:
        ts = pd.Timestamp(time_str)
        match_mask = idx_array == ts
        if match_mask.any():
            return int(np.where(match_mask)[0][0])
    except (ValueError, TypeError):
        pass
    return None


# =============================================================================
# 因子组 5：成本/节点（V1.8 完整成本分析）
# =============================================================================
def _compute_cost_position_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算成本/节点因子 (Volume Profile / POC / Node)。

    Returns:
        V1.7 保留字段 + V1.8 新增字段（atr_distance/value_area/node_strength）
    """
    result: dict[str, Any] = {
        "poc_price": None,
        "nearest_upper_node": None,
        "nearest_lower_node": None,
        "position_0_1": None,
        "close_to_poc_pct": None,
        # V1.8 新增字段
        "price_vs_poc_atr": None,
        "value_area_position_0_1": None,
        "nearest_node_above_price": None,
        "nearest_node_below_price": None,
        "distance_to_node_above_atr": None,
        "distance_to_node_below_atr": None,
        "node_above_strength": None,
        "node_below_strength": None,
    }
    if bars is None or len(bars) < 20:
        return result

    try:
        vp_result = compute_unified_volume_profile(bars)
    except Exception as exc:
        logger.warning("Volume Profile 计算失败: %s", exc)
        return result

    if vp_result is None:
        return result

    closes = bars["close"].to_numpy(dtype=float)
    last_close = closes[-1]
    if not np.isfinite(last_close):
        return result

    last_atr = float(atr[-1]) if atr is not None and len(atr) > 0 and np.isfinite(atr[-1]) and atr[-1] > 0 else None

    # POC
    try:
        poc = vp_result.poc_price
        if np.isfinite(poc):
            result["poc_price"] = float(poc)
            if poc > 0:
                result["close_to_poc_pct"] = float((last_close - poc) / poc)
            # V1.8 price_vs_poc_atr
            if last_atr is not None:
                result["price_vs_poc_atr"] = float((last_close - poc) / last_atr)
    except Exception as exc:
        logger.warning("POC 提取失败: %s", exc)

    # V1.8 value_area_position_0_1
    try:
        vah = vp_result.vah_price
        val = vp_result.val_price
        if np.isfinite(vah) and np.isfinite(val) and (vah - val) != 0:
            result["value_area_position_0_1"] = float(
                (last_close - val) / (vah - val)
            )
    except Exception as exc:
        logger.warning("value_area_position 计算失败: %s", exc)

    # nearest nodes
    try:
        nodes = vp_result.nearest_nodes(last_close)
        upper = nodes.get("upper_node")
        lower = nodes.get("lower_node")
        if upper is not None:
            upper_mid = float(upper.get("price_mid", 0))
            result["nearest_upper_node"] = {
                "price_mid": upper_mid,
                "price_low": float(upper.get("price_low", 0)),
                "price_high": float(upper.get("price_high", 0)),
            }
            result["nearest_node_above_price"] = upper_mid
            # V1.8 distance_to_node_above_atr
            if last_atr is not None:
                result["distance_to_node_above_atr"] = float(
                    (last_close - upper_mid) / last_atr
                )
            # V1.8 node_above_strength from peak_df
            result["node_above_strength"] = _lookup_node_strength(
                vp_result, upper_mid
            )
        if lower is not None:
            lower_mid = float(lower.get("price_mid", 0))
            result["nearest_lower_node"] = {
                "price_mid": lower_mid,
                "price_low": float(lower.get("price_low", 0)),
                "price_high": float(lower.get("price_high", 0)),
            }
            result["nearest_node_below_price"] = lower_mid
            # V1.8 distance_to_node_below_atr
            if last_atr is not None:
                result["distance_to_node_below_atr"] = float(
                    (last_close - lower_mid) / last_atr
                )
            # V1.8 node_below_strength from peak_df
            result["node_below_strength"] = _lookup_node_strength(
                vp_result, lower_mid
            )
    except Exception as exc:
        logger.warning("Node 提取失败: %s", exc)

    # position_0_1
    try:
        pos = vp_result.position_0_1(last_close)
        if np.isfinite(pos):
            result["position_0_1"] = float(pos)
    except Exception as exc:
        logger.warning("position_0_1 计算失败: %s", exc)

    return result


def _lookup_node_strength(vp_result: Any, price_mid: float) -> float | None:
    """从 peak_df 按 price_mid 精确匹配 total_volume。

    Args:
        vp_result: UnifiedVolumeProfileResult
        price_mid: 节点的 price_mid 值

    Returns:
        total_volume（float）或 None
    """
    try:
        peak_df = getattr(vp_result, "peak_df", None)
        if peak_df is None or peak_df.empty or "price_mid" not in peak_df.columns:
            return None
        # 精确匹配 price_mid（round 4 与 _node_row_to_json 对齐）
        matched = peak_df[peak_df["price_mid"] == price_mid]
        if matched.empty:
            # 尝试近似匹配（浮点容差）
            matched = peak_df[np.isclose(peak_df["price_mid"], price_mid, atol=1e-4)]
        if matched.empty or "total_volume" not in matched.columns:
            return None
        val = matched["total_volume"].iloc[0]
        if pd.notna(val) and val > 0:
            return float(val)
    except Exception:
        pass
    return None


# =============================================================================
# 主入口：异步计算所有因子
# =============================================================================
async def compute_structural_factors(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    as_of: str = "latest",
) -> dict[str, Any]:
    """计算双周期结构状态因子。

    Args:
        session: 数据库 session
        instrument_id: 标的 ID
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权（默认 qfq）
        as_of: 截止时间（默认 latest）

    Returns:
        dict with:
        - primary: {timeframe: {5 factor groups}}
        - secondary: {timeframe: {5 factor groups}}
        - relation: primary vs secondary 对比
        - meta: {as_of, lookback_bars, degraded_reasons, warmup_notes}
    """
    degraded_reasons: list[str] = []
    warmup_notes: list[str] = []

    # 获取 K 线
    service = MarketDataAggregationService()

    primary_bars = await _fetch_bars(
        service, session, instrument_id, primary_timeframe, adj,
        _PRIMARY_LOOKBACK, degraded_reasons,
    )
    secondary_bars = await _fetch_bars(
        service, session, instrument_id, secondary_timeframe, adj,
        _SECONDARY_LOOKBACK, degraded_reasons,
    )

    # 计算各周期因子
    primary_factors = _compute_all_factors_for_bars(
        primary_bars, primary_timeframe, degraded_reasons, warmup_notes
    )
    secondary_factors = _compute_all_factors_for_bars(
        secondary_bars, secondary_timeframe, degraded_reasons, warmup_notes
    )

    # 对比关系
    relation = _compute_relation(primary_factors, secondary_factors)

    # as_of 时间
    as_of_str = _extract_as_of(primary_bars, secondary_bars)

    return {
        "primary": {primary_timeframe: primary_factors},
        "secondary": {secondary_timeframe: secondary_factors},
        "relation": relation,
        "meta": {
            "as_of": as_of_str,
            "primary_lookback_bars": _PRIMARY_LOOKBACK,
            "secondary_lookback_bars": _SECONDARY_LOOKBACK,
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }


async def _fetch_bars(
    service: MarketDataAggregationService,
    session: AsyncSession,
    instrument_id: uuid.UUID,
    timeframe: str,
    adj: str,
    lookback: int,
    degraded_reasons: list[str],
) -> pd.DataFrame | None:
    """获取 K 线数据，失败时返回 None 并写入 degraded_reasons。"""
    try:
        result = await service.get_bars(
            session,
            instrument_id,
            timeframe=timeframe,
            adj=adj,
            include_realtime=False,  # 只使用已完成 bar
        )
        bars = result.bars
        if bars is None or bars.empty:
            degraded_reasons.append(f"{timeframe}: no bars returned")
            return None
        if len(bars) < 60:
            degraded_reasons.append(
                f"{timeframe}: insufficient bars ({len(bars)} < 60)"
            )
            return bars  # 返回部分数据，让因子函数自行处理
        return bars.tail(lookback) if len(bars) > lookback else bars
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: get_bars failed: {exc}")
        logger.warning("%s get_bars 失败: %s", timeframe, exc)
        return None


def _compute_all_factors_for_bars(
    bars: pd.DataFrame | None,
    timeframe: str,
    degraded_reasons: list[str],
    warmup_notes: list[str],
) -> dict[str, Any]:
    """计算单周期所有因子组，每组独立异常隔离。"""
    factors: dict[str, Any] = {
        "dsa_segment": None,
        "swing_position": None,
        "cost_position": None,
        "volatility_momentum": None,
        "participation": None,
    }
    if bars is None or bars.empty:
        degraded_reasons.append(f"{timeframe}: bars is None or empty")
        return factors

    # ATR
    atr: np.ndarray | None = None
    try:
        highs = bars["high"].to_numpy(dtype=float)
        lows = bars["low"].to_numpy(dtype=float)
        closes = bars["close"].to_numpy(dtype=float)
        atr = compute_atr(highs, lows, closes, _ATR_LENGTH)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: atr failed: {exc}")
        logger.warning("%s ATR 计算失败: %s", timeframe, exc)

    # 1. DSA 段质量
    try:
        dsa_bundle = compute_dsa_bundle(bars, {})
        factors["dsa_segment"] = _compute_dsa_segment_factors(bars, dsa_bundle, atr)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: dsa_segment failed: {exc}")
        logger.warning("%s DSA 段质量计算失败: %s", timeframe, exc)

    # 2. Swing 结构位置
    try:
        factors["swing_position"] = _compute_swing_factors(bars, atr)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: swing_position failed: {exc}")
        logger.warning("%s Swing 计算失败: %s", timeframe, exc)

    # 3. 成本/节点
    try:
        factors["cost_position"] = _compute_cost_position_factors(bars, atr)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: cost_position failed: {exc}")
        logger.warning("%s 成本/节点计算失败: %s", timeframe, exc)

    # 4. 动量/波动
    try:
        factors["volatility_momentum"] = _compute_volatility_momentum_factors(bars, atr)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: volatility_momentum failed: {exc}")
        logger.warning("%s 动量/波动计算失败: %s", timeframe, exc)

    # 5. 成交参与（V1.8 从 dsa_segment 共享段级成交量字段）
    try:
        factors["participation"] = _compute_participation_factors(
            bars, factors.get("dsa_segment")
        )
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: participation failed: {exc}")
        logger.warning("%s 成交参与计算失败: %s", timeframe, exc)

    return factors


def _compute_relation(
    primary: dict[str, Any], secondary: dict[str, Any]
) -> dict[str, Any]:
    """计算 primary vs secondary 对比关系（V1.8 客观关系字段）。

    V1.8 变更：
    - 移除 momentum_alignment（按 spec "不要输出事件，只输出客观关系"）
    - 新增 primary_dir/secondary_dir/trend_alignment/primary_swing_position/
      secondary_swing_position/primary_slope_atr/secondary_slope_atr/
      secondary_vs_primary_position_delta

    Returns:
        dict with V1.8 客观关系字段（不输出事件）
    """
    relation: dict[str, Any] = {
        "primary_dir": None,
        "secondary_dir": None,
        "trend_alignment": None,
        "primary_swing_position": None,
        "secondary_swing_position": None,
        "primary_slope_atr": None,
        "secondary_slope_atr": None,
        "secondary_vs_primary_position_delta": None,
        "notes": [],
    }

    # 提取 DSA 段方向（优先 V1.8 current_dsa_segment_dir，fallback V1.7 segment_dir）
    p_dsa = primary.get("dsa_segment") or {}
    s_dsa = secondary.get("dsa_segment") or {}
    p_dir = p_dsa.get("current_dsa_segment_dir")
    if p_dir is None:
        p_dir = p_dsa.get("segment_dir")
    s_dir = s_dsa.get("current_dsa_segment_dir")
    if s_dir is None:
        s_dir = s_dsa.get("segment_dir")

    relation["primary_dir"] = p_dir
    relation["secondary_dir"] = s_dir
    if p_dir is not None and s_dir is not None:
        relation["trend_alignment"] = "aligned" if p_dir == s_dir else "divergent"

    # 提取 swing_position（V1.8 price_position_in_swing_0_1）
    p_swing = primary.get("swing_position") or {}
    s_swing = secondary.get("swing_position") or {}
    p_swing_pos = p_swing.get("price_position_in_swing_0_1")
    s_swing_pos = s_swing.get("price_position_in_swing_0_1")
    relation["primary_swing_position"] = p_swing_pos
    relation["secondary_swing_position"] = s_swing_pos
    if p_swing_pos is not None and s_swing_pos is not None:
        relation["secondary_vs_primary_position_delta"] = float(
            s_swing_pos - p_swing_pos
        )

    # 提取 slope_atr（V1.8 current_dsa_segment_slope_atr_per_bar）
    relation["primary_slope_atr"] = p_dsa.get(
        "current_dsa_segment_slope_atr_per_bar"
    )
    relation["secondary_slope_atr"] = s_dsa.get(
        "current_dsa_segment_slope_atr_per_bar"
    )

    return relation


def _extract_as_of(
    primary_bars: pd.DataFrame | None, secondary_bars: pd.DataFrame | None
) -> str:
    """从 bars 中提取 as_of 时间。"""
    for bars in [primary_bars, secondary_bars]:
        if bars is not None and not bars.empty:
            last_idx = bars.index[-1]
            if hasattr(last_idx, "strftime"):
                return last_idx.strftime("%Y-%m-%d %H:%M:%S")
            return str(last_idx)
    return "unknown"


if __name__ == "__main__":
    # 模块自测：用合成数据验证（n 引用 DAILY_HISTORY_BARS 真源，避免硬编码受控字面量）
    rng = np.random.default_rng(42)
    n = DAILY_HISTORY_BARS
    closes = 100.0 + np.linspace(0, 20.0, n) + rng.normal(0, 2.0, n)
    intrabar = np.abs(rng.normal(0, 1.5, n)) + 0.5
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    bars = pd.DataFrame({
        "open": closes + rng.normal(0, 0.5, n),
        "high": closes + intrabar,
        "low": closes - intrabar,
        "close": closes,
        "volume": rng.integers(1_000_000, 10_000_000, n).astype(float),
        "amount": closes * 1_000_000,
    }, index=idx)

    # 测试各因子组
    print("=== 成交参与 ===")
    print(_compute_participation_factors(bars))
    print("=== 动量/波动 ===")
    vm = _compute_volatility_momentum_factors(bars)
    print(dict(vm))
    print("=== Swing ===")
    sw = _compute_swing_factors(bars)
    print(dict(sw))
    print("=== 成本/节点 ===")
    cp = _compute_cost_position_factors(bars)
    print({k: (v if not isinstance(v, dict) else "...") for k, v in cp.items()})
    print("=== DSA 段 ===")
    bundle = compute_dsa_bundle(bars, {})
    ds = _compute_dsa_segment_factors(bars, bundle, None)
    print(ds)
    print("自测完成")
