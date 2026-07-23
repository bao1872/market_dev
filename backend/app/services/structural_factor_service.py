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

# [CHANGE-20260718-004 Node Cluster engine] 不再直接导入 unified_volume_profile；
# Node Cluster 三链（盘后 primary / 详情 / 监控）经 node_cluster_engine 统一入口，
# 15m secondary 单周期 VP 经 engine.compute_single_period_volume_profile。
# 架构守护测试 test_node_cluster_architecture 强制此约束。
from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    compute_single_period_volume_profile,
    derive_state_for_price,
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
    last_close: float | None = float(closes[-1]) if len(closes) > 0 else None
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

    if last_close is not None and np.isfinite(last_close) and np.isfinite(last_upper) and np.isfinite(last_lower):
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
# 因子组 3：Swing 结构位置（V1.8 完整位置分析 + V1.9 active swing 当前状态）
# =============================================================================
def _classify_confirmed_swing_breakout_state(
    last_close: float | None,
    confirmed_high: float | None,
    confirmed_low: float | None,
) -> str | None:
    """分类 close 相对 confirmed pivot 的突破状态。

    - close > confirmed_high → "above_confirmed_high"
    - close < confirmed_low → "below_confirmed_low"
    - confirmed_low <= close <= confirmed_high → "inside"
    - confirmed_high/low 任一缺失 → None
    """
    if last_close is None or confirmed_high is None or confirmed_low is None:
        return None
    if last_close > confirmed_high:
        return "above_confirmed_high"
    if last_close < confirmed_low:
        return "below_confirmed_low"
    return "inside"


def _compute_active_swing_factors(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    ph: np.ndarray,
    pl: np.ndarray,
    ph_anchor: np.ndarray,
    pl_anchor: np.ndarray,
    last_close: float,
    last_atr: float,
    fallback_n: int = 120,
) -> dict[str, Any]:
    """计算 active swing 当前状态因子（V1.9 新增）。

    active swing 表示"当前正在发展的结构区间"，必须更贴近图上 K 线。
    与 confirmed pivot（滞后）不同，active high/low 跟随最新价格。

    口径：
    - 若最近 confirmed low anchor 晚于最近 confirmed high anchor → up leg
      active_low = confirmed_low；active_high = max(highs[anchor:])
    - 若最近 confirmed high anchor 晚于最近 confirmed low anchor → down leg
      active_high = confirmed_high；active_low = min(lows[anchor:])
    - 都没有 → fallback 最近 min(fallback_n, len) 根

    无未来函数：只用到当前 bar 及之前的数据。
    """
    n = len(highs)
    result: dict[str, Any] = {
        "active_swing_dir": None,
        "active_swing_high": None,
        "active_swing_low": None,
        "bars_since_active_swing_high": None,
        "bars_since_active_swing_low": None,
        "active_swing_range": None,
        "price_position_in_active_swing_raw": None,
        "price_position_in_active_swing_0_1": None,
        "distance_to_active_swing_high_atr": None,
        "distance_to_active_swing_low_atr": None,
        "active_retracement_from_high_0_1": None,
        "active_rebound_from_low_0_1": None,
    }

    # 找最后一个非 NaN 的 ph/pl anchor
    ph_valid = np.where(np.isfinite(ph))[0]
    pl_valid = np.where(np.isfinite(pl))[0]
    last_ph_anchor_val = (
        float(ph_anchor[ph_valid[-1]])
        if len(ph_valid) > 0 and np.isfinite(ph_anchor[ph_valid[-1]])
        else None
    )
    last_pl_anchor_val = (
        float(pl_anchor[pl_valid[-1]])
        if len(pl_valid) > 0 and np.isfinite(pl_anchor[pl_valid[-1]])
        else None
    )

    active_high: float | None = None
    active_low: float | None = None
    active_high_bar_idx: int | None = None
    active_low_bar_idx: int | None = None

    # 判断 active leg
    # active_low/active_high 使用 confirmed pivot 值（pl/ph），与用户规格
    # "active_low=confirmed_low；active_high=confirmed_high" 一致。
    # anchor_idx 用于定位 anchor bar，搜索 max/min high/low 从 anchor 到当前 bar。
    if last_pl_anchor_val is not None and (
        last_ph_anchor_val is None or last_pl_anchor_val > last_ph_anchor_val
    ):
        # up leg: confirmed low 晚于 confirmed high
        result["active_swing_dir"] = 1
        anchor_idx = int(last_pl_anchor_val)
        active_low = float(pl[pl_valid[-1]])
        active_low_bar_idx = anchor_idx
        search_highs = highs[anchor_idx:]
        if len(search_highs) > 0:
            local_idx = int(np.argmax(search_highs))
            active_high = float(search_highs[local_idx])
            active_high_bar_idx = anchor_idx + local_idx
    elif last_ph_anchor_val is not None:
        # down leg: confirmed high 晚于 confirmed low
        result["active_swing_dir"] = -1
        anchor_idx = int(last_ph_anchor_val)
        active_high = float(ph[ph_valid[-1]])
        active_high_bar_idx = anchor_idx
        search_lows = lows[anchor_idx:]
        if len(search_lows) > 0:
            local_idx = int(np.argmin(search_lows))
            active_low = float(search_lows[local_idx])
            active_low_bar_idx = anchor_idx + local_idx
    else:
        # fallback: 最近 min(fallback_n, n) 根
        result["active_swing_dir"] = None
        n_fb = min(fallback_n, n)
        search_start = n - n_fb
        search_highs = highs[search_start:]
        search_lows = lows[search_start:]
        if len(search_highs) > 0:
            local_idx = int(np.argmax(search_highs))
            active_high = float(search_highs[local_idx])
            active_high_bar_idx = search_start + local_idx
        if len(search_lows) > 0:
            local_idx = int(np.argmin(search_lows))
            active_low = float(search_lows[local_idx])
            active_low_bar_idx = search_start + local_idx

    if active_high is None or active_low is None:
        return result

    result["active_swing_high"] = active_high
    result["active_swing_low"] = active_low
    if active_high_bar_idx is not None:
        result["bars_since_active_swing_high"] = int(n - 1 - active_high_bar_idx)
    if active_low_bar_idx is not None:
        result["bars_since_active_swing_low"] = int(n - 1 - active_low_bar_idx)

    active_range = active_high - active_low
    result["active_swing_range"] = float(active_range)

    if active_range > 0:
        raw = (last_close - active_low) / active_range
        result["price_position_in_active_swing_raw"] = float(raw)
        result["price_position_in_active_swing_0_1"] = float(max(0.0, min(1.0, raw)))
        result["active_retracement_from_high_0_1"] = float(
            (active_high - last_close) / active_range
        )
        result["active_rebound_from_low_0_1"] = float(
            (last_close - active_low) / active_range
        )

    if last_atr > 0:
        result["distance_to_active_swing_high_atr"] = float(
            (last_close - active_high) / last_atr
        )
        result["distance_to_active_swing_low_atr"] = float(
            (last_close - active_low) / last_atr
        )

    return result


def _compute_developing_swing_factors(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    active_swing: dict[str, Any],
    last_close: float,
    last_atr: float,
) -> dict[str, Any]:
    """计算 developing swing 当前状态因子（V1.10 新增）。

    developing swing 表示"当前正在发生的回落/反弹结构"，
    从 active major leg 的高/低点开始计算，反映当前价格行为。

    口径：
    - major up leg + active_high_bar_idx < current_idx → 回落
      developing_dir=-1, developing_high=active_high,
      developing_low=min(lows[active_high_bar_idx:now])
    - major up leg + active_high_bar_idx == current_idx → 仍在创新高
      developing_dir=1, developing=active
    - major down leg + active_low_bar_idx < current_idx → 反弹
      developing_dir=1, developing_low=active_low,
      developing_high=max(highs[active_low_bar_idx:now])
    - major down leg + active_low_bar_idx == current_idx → 仍在创新低
      developing_dir=-1, developing=active
    - fallback（active_dir=None）→ developing=active

    无未来函数：只用到当前 bar 及之前的数据。
    """
    n = len(highs)
    result: dict[str, Any] = {
        "developing_swing_dir": None,
        "developing_swing_high": None,
        "developing_swing_low": None,
        "developing_swing_high_bar_index": None,
        "developing_swing_low_bar_index": None,
        "bars_since_developing_swing_high": None,
        "bars_since_developing_swing_low": None,
        "developing_swing_range": None,
        "price_position_in_developing_swing_raw": None,
        "price_position_in_developing_swing_0_1": None,
        "distance_to_developing_swing_high_atr": None,
        "distance_to_developing_swing_low_atr": None,
        "developing_retracement_from_high_0_1": None,
        "developing_rebound_from_low_0_1": None,
    }

    active_dir = active_swing.get("active_swing_dir")
    active_high = active_swing.get("active_swing_high")
    active_low = active_swing.get("active_swing_low")
    bars_since_high = active_swing.get("bars_since_active_swing_high")
    bars_since_low = active_swing.get("bars_since_active_swing_low")

    if active_high is None or active_low is None:
        return result

    # [DevelopingSwing] - 反推 active bar_index
    current_idx = n - 1
    active_high_bar_idx = (
        int(n - 1 - bars_since_high) if bars_since_high is not None else None
    )
    active_low_bar_idx = (
        int(n - 1 - bars_since_low) if bars_since_low is not None else None
    )

    dev_high: float | None = None
    dev_low: float | None = None
    dev_high_bar_idx: int | None = None
    dev_low_bar_idx: int | None = None
    dev_dir: int | None = None

    if active_dir == 1:
        # [DevelopingSwing] - major up leg
        if active_high_bar_idx is not None and active_high_bar_idx < current_idx:
            # 回落：developing_low = min(lows[active_high_bar_idx:now])
            dev_dir = -1
            dev_high = float(active_high)
            dev_high_bar_idx = int(active_high_bar_idx)
            search_lows = lows[active_high_bar_idx:]
            if len(search_lows) > 0:
                local_idx = int(np.argmin(search_lows))
                dev_low = float(search_lows[local_idx])
                dev_low_bar_idx = int(active_high_bar_idx) + local_idx
        else:
            # 仍在创新高：developing = active
            dev_dir = 1
            dev_high = float(active_high)
            dev_low = float(active_low)
            dev_high_bar_idx = active_high_bar_idx
            dev_low_bar_idx = active_low_bar_idx
    elif active_dir == -1:
        # [DevelopingSwing] - major down leg
        if active_low_bar_idx is not None and active_low_bar_idx < current_idx:
            # 反弹：developing_high = max(highs[active_low_bar_idx:now])
            dev_dir = 1
            dev_low = float(active_low)
            dev_low_bar_idx = int(active_low_bar_idx)
            search_highs = highs[active_low_bar_idx:]
            if len(search_highs) > 0:
                local_idx = int(np.argmax(search_highs))
                dev_high = float(search_highs[local_idx])
                dev_high_bar_idx = int(active_low_bar_idx) + local_idx
        else:
            # 仍在创新低：developing = active
            dev_dir = -1
            dev_high = float(active_high)
            dev_low = float(active_low)
            dev_high_bar_idx = active_high_bar_idx
            dev_low_bar_idx = active_low_bar_idx
    else:
        # [DevelopingSwing] - fallback（无 confirmed pivot）：developing = active
        dev_dir = active_dir
        dev_high = float(active_high)
        dev_low = float(active_low)
        dev_high_bar_idx = active_high_bar_idx
        dev_low_bar_idx = active_low_bar_idx

    if dev_high is None or dev_low is None:
        return result

    result["developing_swing_dir"] = dev_dir
    result["developing_swing_high"] = dev_high
    result["developing_swing_low"] = dev_low
    result["developing_swing_high_bar_index"] = dev_high_bar_idx
    result["developing_swing_low_bar_index"] = dev_low_bar_idx
    if dev_high_bar_idx is not None:
        result["bars_since_developing_swing_high"] = int(n - 1 - dev_high_bar_idx)
    if dev_low_bar_idx is not None:
        result["bars_since_developing_swing_low"] = int(n - 1 - dev_low_bar_idx)

    dev_range = dev_high - dev_low
    result["developing_swing_range"] = float(dev_range)

    if dev_range > 0:
        raw = (last_close - dev_low) / dev_range
        result["price_position_in_developing_swing_raw"] = float(raw)
        result["price_position_in_developing_swing_0_1"] = float(
            max(0.0, min(1.0, raw))
        )
        result["developing_retracement_from_high_0_1"] = float(
            (dev_high - last_close) / dev_range
        )
        result["developing_rebound_from_low_0_1"] = float(
            (last_close - dev_low) / dev_range
        )

    if last_atr > 0:
        result["distance_to_developing_swing_high_atr"] = float(
            (last_close - dev_high) / last_atr
        )
        result["distance_to_developing_swing_low_atr"] = float(
            (last_close - dev_low) / last_atr
        )

    return result


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
        # V1.9 confirmed alias（与 active 对齐的别名，便于前端区分 confirmed vs active）
        "bars_since_confirmed_swing_high": None,
        "bars_since_confirmed_swing_low": None,
        "price_position_in_confirmed_swing_raw": None,
        "confirmed_swing_breakout_state": None,
        # V1.9 active swing 字段（当前正在发展的结构区间，跟随最新价格）
        "active_swing_dir": None,
        "active_swing_high": None,
        "active_swing_low": None,
        "bars_since_active_swing_high": None,
        "bars_since_active_swing_low": None,
        "active_swing_range": None,
        "price_position_in_active_swing_raw": None,
        "price_position_in_active_swing_0_1": None,
        "distance_to_active_swing_high_atr": None,
        "distance_to_active_swing_low_atr": None,
        "active_retracement_from_high_0_1": None,
        "active_rebound_from_low_0_1": None,
        # V1.10 developing swing 字段（当前正在发生的回落/反弹结构）
        "developing_swing_dir": None,
        "developing_swing_high": None,
        "developing_swing_low": None,
        "developing_swing_high_bar_index": None,
        "developing_swing_low_bar_index": None,
        "bars_since_developing_swing_high": None,
        "bars_since_developing_swing_low": None,
        "developing_swing_range": None,
        "price_position_in_developing_swing_raw": None,
        "price_position_in_developing_swing_0_1": None,
        "distance_to_developing_swing_high_atr": None,
        "distance_to_developing_swing_low_atr": None,
        "developing_retracement_from_high_0_1": None,
        "developing_rebound_from_low_0_1": None,
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
        # V1.9 confirmed alias（与 active 对齐）
        result["bars_since_confirmed_swing_high"] = int(len(highs) - 1 - last_ph_anchor)
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
        # V1.9 confirmed alias（与 active 对齐）
        result["bars_since_confirmed_swing_low"] = int(len(lows) - 1 - last_pl_anchor)
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
            raw_position = (last_close - sl) / swing_range
            result["price_position_in_swing_0_1"] = float(raw_position)
            # V1.9 confirmed raw alias（可能 <0 或 >1，反映突破 confirmed pivot）
            result["price_position_in_confirmed_swing_raw"] = float(raw_position)
            result["retracement_from_high_0_1"] = float(
                (sh - last_close) / swing_range
            )
            result["rebound_from_low_0_1"] = float(
                (last_close - sl) / swing_range
            )
        # V1.9 confirmed breakout state（close 相对 confirmed pivot 的突破状态）
        result["confirmed_swing_breakout_state"] = _classify_confirmed_swing_breakout_state(
            last_close, sh, sl,
        )

    # V1.9 active swing 因子（当前正在发展的结构区间，跟随最新价格）
    # active high/low 从 anchor 到当前 bar 的 max/min，比 confirmed pivot 更贴近图上 K 线
    if last_close is not None:
        active_factors = _compute_active_swing_factors(
            highs=highs,
            lows=lows,
            closes=closes,
            ph=ph,
            pl=pl,
            ph_anchor=ph_anchor,
            pl_anchor=pl_anchor,
            last_close=float(last_close),
            last_atr=last_atr if last_atr is not None else 0.0,
        )
        result.update(active_factors)

        # V1.10 developing swing 因子（当前正在发生的回落/反弹结构）
        # 从 active major leg 的高/低点开始计算，反映当前价格行为
        developing_factors = _compute_developing_swing_factors(
            highs=highs,
            lows=lows,
            closes=closes,
            active_swing=active_factors,
            last_close=float(last_close),
            last_atr=last_atr if last_atr is not None else 0.0,
        )
        result.update(developing_factors)

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
        # V1.9 统一 age_bars = current_dsa_segment_age_bars（含起始 bar，+1 口径）
        result["age_bars"] = int(cur_age_bars)
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
                prev_seg_atr: np.ndarray | None = atr[prev_start_idx:prev_end_idx + 1] if atr is not None else None
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
def _classify_cost_position_zone(
    last_close: float, upper: float | None, lower: float | None
) -> str | None:
    """分类 close 相对上下节点的位置 zone。

    规则:
        - upper/lower 都存在:
            close < lower → "below_lower_node"
            close > upper → "above_upper_node"
            lower <= close <= upper → "between_nodes"
        - 只存在 upper → "below_upper_node"
        - 只存在 lower → "above_lower_node"
        - 都不存在 → None
    """
    if upper is None and lower is None:
        return None
    if upper is not None and lower is not None:
        if last_close < lower:
            return "below_lower_node"
        if last_close > upper:
            return "above_upper_node"
        return "between_nodes"
    if upper is not None:
        return "below_upper_node"
    return "above_lower_node"


def _classify_value_area_zone(
    last_close: float, vah: float | None, val: float | None
) -> str | None:
    """分类 close 相对 VA（Value Area）的位置 zone。

    规则:
        - vah/val 任一缺失 → None
        - close < val → "below_va"
        - close > vah → "above_va"
        - val <= close <= vah → "inside_va"
    """
    if vah is None or val is None:
        return None
    if last_close < val:
        return "below_va"
    if last_close > vah:
        return "above_va"
    return "inside_va"


def _compute_node_interval_position(
    last_close: float,
    upper: float | None,
    lower: float | None,
    clip: bool = True,
) -> float | None:
    """计算 close 在节点区间 [lower, upper] 中的位置。

    公式: (last_close - lower) / (upper - lower)
    - clip=True: 限制到 [0, 1]
    - clip=False: 原始值（可 > 1 或 < 0），用于诊断
    - upper/lower 任一缺失，或 upper <= lower → None
    """
    if upper is None or lower is None:
        return None
    if upper <= lower:
        return None
    raw = (last_close - lower) / (upper - lower)
    if clip:
        return float(max(0.0, min(1.0, raw)))
    return float(raw)


def _compute_cost_position_factors(
    bars: pd.DataFrame,
    atr: np.ndarray | None = None,
    *,
    precomputed_profile: NodeClusterProfileResult | None = None,
) -> dict[str, Any]:
    """计算成本/节点因子 (Volume Profile / POC / Node)。

    Args:
        bars: K 线 DataFrame
        atr: ATR 数组（可选）
        precomputed_profile: 预计算的 Node Cluster Profile（由调用方通过
            `node_cluster_engine.compute_node_cluster_profile` 一次计算后注入）。
            当提供时（盘后 primary 1d 链路），直接消费其公共字段 + `derive_state_for_price`，
            **不再调用底层 VP**（修复三链不一致缺陷：原实现 `compute_unified_volume_profile(bars)`
            只用单周期 bars，与详情/监控链 1d 价格范围 + 15m 成交量分配口径不一致）。
            当为 None（15m secondary 单周期链路），经 engine.compute_single_period_volume_profile
            计算单周期 VP，结果落库为 `timeframe_volume_profile`（显式非 Node Cluster）。

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
        # V1.8 位置语义修复字段（区分 VP 全区间 / 节点区间 / VA 区间）
        "node_interval_position_0_1": None,
        "node_interval_position_raw": None,
        "cost_position_zone": None,
        "value_area_zone": None,
        "val_price": None,
        "vah_price": None,
    }
    if bars is None or len(bars) < 20:
        return result

    closes = bars["close"].to_numpy(dtype=float)
    last_close = closes[-1]
    if not np.isfinite(last_close):
        return result

    last_atr = float(atr[-1]) if atr is not None and len(atr) > 0 and np.isfinite(atr[-1]) and atr[-1] > 0 else None

    # ===== Profile 来源：优先消费预计算 engine 结果（三链同核） =====
    # [CHANGE-20260718-004] 修复缺陷：原实现单独调用 compute_unified_volume_profile(bars)
    # （profile_df=None，单周期 VP），导致盘后链与详情/监控链口径不一致。
    # 当 precomputed_profile 提供时，直接消费 engine 结果，不再调用底层 VP。
    # 当 precomputed_profile 为 None（15m secondary），经 engine 计算单周期 VP。
    profile = precomputed_profile
    poc: float | None
    vah: float | None
    val: float | None
    upper: dict[str, float] | None
    lower: dict[str, float] | None
    pos_0_1: float | None
    peak_rows_for_strength: list[dict[str, Any]] | None
    vp_result_legacy: Any = None

    if profile is not None:
        # 三链同核路径：消费 engine 结果（Node Cluster Profile）
        if not profile.profile_rows:
            # engine 返回空 Profile（数据不足），保持默认 result
            return result
        poc = profile.poc_price
        vah = profile.vah_price
        val = profile.val_price
        peak_rows_for_strength = profile.peak_rows
        upper = None
        lower = None
        pos_0_1 = None
        try:
            state = derive_state_for_price(profile, last_close)
            upper = state.upper_node
            lower = state.lower_node
            pos_0_1 = state.position_0_1
        except Exception as exc:
            logger.warning("engine derive_state_for_price 失败: %s", exc)
    else:
        # 15m secondary 单周期路径（非 Node Cluster）
        try:
            vp_result = compute_single_period_volume_profile(bars)
        except Exception as exc:
            logger.warning("Volume Profile 计算失败: %s", exc)
            return result
        if vp_result is None:
            return result
        poc = vp_result.poc_price
        vah = vp_result.vah_price
        val = vp_result.val_price
        peak_rows_for_strength = None
        vp_result_legacy = vp_result
        upper = None
        lower = None
        pos_0_1 = None
        try:
            nodes = vp_result.nearest_nodes(last_close)
            upper = nodes.get("upper_node")
            lower = nodes.get("lower_node")
        except Exception as exc:
            logger.warning("Node 提取失败: %s", exc)
        try:
            pos_0_1 = vp_result.position_0_1(last_close)
        except Exception as exc:
            logger.warning("position_0_1 计算失败: %s", exc)

    # POC（poc 可能为 None（engine 路径）或 NaN（单周期路径），统一处理）
    try:
        if poc is not None and np.isfinite(poc):
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
        if vah is not None and val is not None and np.isfinite(vah) and np.isfinite(val) and (vah - val) != 0:
            result["value_area_position_0_1"] = float(
                (last_close - val) / (vah - val)
            )
        # V1.8 暴露 VAL/VAH 原值供前端显示
        if val is not None and np.isfinite(val):
            result["val_price"] = float(val)
        if vah is not None and np.isfinite(vah):
            result["vah_price"] = float(vah)
        # V1.8 value_area_zone（close 相对 VA 的位置分类）
        result["value_area_zone"] = _classify_value_area_zone(
            last_close,
            float(vah) if (vah is not None and np.isfinite(vah)) else None,
            float(val) if (val is not None and np.isfinite(val)) else None,
        )
    except Exception as exc:
        logger.warning("value_area_position 计算失败: %s", exc)

    # nearest nodes
    try:
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
            # V1.8 node_above_strength（engine 路径用 peak_rows，单周期路径用 vp_result.peak_df）
            result["node_above_strength"] = _lookup_node_strength_unified(
                peak_rows_for_strength, vp_result_legacy, upper_mid
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
            # V1.8 node_below_strength
            result["node_below_strength"] = _lookup_node_strength_unified(
                peak_rows_for_strength, vp_result_legacy, lower_mid
            )
    except Exception as exc:
        logger.warning("Node 提取失败: %s", exc)

    # V1.8 节点区间位置 + cost_position_zone（区分于 VP 全区间 position_0_1）
    upper_price = result.get("nearest_node_above_price")
    lower_price = result.get("nearest_node_below_price")
    result["node_interval_position_0_1"] = _compute_node_interval_position(
        last_close, upper_price, lower_price, clip=True
    )
    result["node_interval_position_raw"] = _compute_node_interval_position(
        last_close, upper_price, lower_price, clip=False
    )
    result["cost_position_zone"] = _classify_cost_position_zone(
        last_close, upper_price, lower_price
    )

    # position_0_1（保持原 VP 全区间语义：lowest_price~highest_price 中的位置，不 clip）
    try:
        if pos_0_1 is not None and np.isfinite(pos_0_1):
            result["position_0_1"] = float(pos_0_1)
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


def _lookup_node_strength_from_rows(
    peak_rows: list[dict[str, Any]], price_mid: float
) -> float | None:
    """从 peak_rows（list[dict]，engine 路径）按 price_mid 近似匹配 total_volume。

    与 _lookup_node_strength 的 peak_df 路径语义一致（atol=1e-4），
    仅数据源不同：engine NodeClusterProfileResult.peak_rows 是 list[dict]。

    Args:
        peak_rows: NodeClusterProfileResult.peak_rows（含 price_mid/total_volume）
        price_mid: 节点的 price_mid 值

    Returns:
        total_volume（float）或 None
    """
    try:
        for row in peak_rows:
            row_mid = row.get("price_mid")
            if row_mid is None:
                continue
            if abs(float(row_mid) - float(price_mid)) < 1e-4:
                val = row.get("total_volume")
                if val is not None and val > 0:
                    return float(val)
                return None
        return None
    except Exception:
        return None


def _lookup_node_strength_unified(
    peak_rows: list[dict[str, Any]] | None,
    vp_result_legacy: Any | None,
    price_mid: float,
) -> float | None:
    """统一节点强度查找入口（engine 路径 + 单周期路径派发）。

    - engine 路径（peak_rows 非空）：从 NodeClusterProfileResult.peak_rows 查找
    - 单周期路径（vp_result_legacy 非空）：从 UnifiedVolumeProfileResult.peak_df 查找
    """
    if peak_rows is not None:
        return _lookup_node_strength_from_rows(peak_rows, price_mid)
    if vp_result_legacy is not None:
        return _lookup_node_strength(vp_result_legacy, price_mid)
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


def _compute_smc_freshness_factors(bars: pd.DataFrame) -> dict[str, Any]:
    """计算 14 个细分日线 SMC freshness 因子。

    [PROMPT.md §三] 按方向(bullish/bearish)和结构级别(internal/swing)拆分：
    - BOS: 4 个因子 (bullish/bearish × internal/swing)
    - CHoCH: 4 个因子 (bullish/bearish × internal/swing)
    - OB touch: 4 个因子 (bullish/bearish × internal/swing)
    - EQH: 1 个因子 (Canonical DTO 无 internal/swing 字段，不拆分)
    - EQL: 1 个因子 (同 EQH)

    计算规则：
    - BOS/CHoCH: freshness=0 在 confirmed_index 所在已完成日线 bar
    - EQH/EQL: freshness=0 在 confirmed_index（第二 pivot 确认 bar）
    - OB touch: freshness=0 在价格第一次实际触碰 zone 的日线 bar
      （不得使用订单块创建 bar 或当前 bar）
    - 从未发生: null
    - 每完成一根日线 +1
    - 同子类型多个事件取最近事件（最小 freshness）

    复用一次 Canonical SMC 计算结果，禁止重复运行 SMC 算法。

    Args:
        bars: 已完成日线 DataFrame（需 >= 250 根）

    Returns:
        dict with 14 个 freshness 因子（int 或 null）
    """
    from app.services.canonical_adapters import compute_smc_adapter

    result: dict[str, Any] = {
        # BOS (4)
        "smc_bos_bullish_internal_freshness_bars": None,
        "smc_bos_bullish_swing_freshness_bars": None,
        "smc_bos_bearish_internal_freshness_bars": None,
        "smc_bos_bearish_swing_freshness_bars": None,
        # CHoCH (4)
        "smc_choch_bullish_internal_freshness_bars": None,
        "smc_choch_bullish_swing_freshness_bars": None,
        "smc_choch_bearish_internal_freshness_bars": None,
        "smc_choch_bearish_swing_freshness_bars": None,
        # OB touch (4)
        "smc_order_block_touch_bullish_internal_freshness_bars": None,
        "smc_order_block_touch_bullish_swing_freshness_bars": None,
        "smc_order_block_touch_bearish_internal_freshness_bars": None,
        "smc_order_block_touch_bearish_swing_freshness_bars": None,
        # EQH/EQL (2) — Canonical DTO 无 internal/swing 字段
        "smc_eqh_freshness_bars": None,
        "smc_eql_freshness_bars": None,
    }

    if bars is None or bars.empty or len(bars) < 250:
        return result

    try:
        smc_dto = compute_smc_adapter(bars, display_bars=len(bars))
    except Exception as exc:
        logger.warning("SMC freshness 计算失败: %s", exc)
        return result

    current_index = len(bars) - 1

    # --- BOS/CHoCH: 按 bullish/bearish × internal/swing 拆分 ---
    # 取每子类型最近事件（最大 confirmed_index → 最小 freshness）
    bos_choch_subtypes: dict[str, int] = {}  # key → best_confirmed_index
    for e in smc_dto.get("events", []):
        etype = e.get("type")
        if etype not in ("BOS", "CHoCH"):
            continue
        bullish = e.get("bullish")
        internal = e.get("internal")
        confirmed_idx = e.get("confirmed_index")
        if bullish is None or internal is None or confirmed_idx is None:
            continue
        direction = "bullish" if bullish else "bearish"
        level = "internal" if internal else "swing"
        key = f"{etype.lower()}_{direction}_{level}"
        idx = int(confirmed_idx)
        if key not in bos_choch_subtypes or idx > bos_choch_subtypes[key]:
            bos_choch_subtypes[key] = idx

    for key, best_idx in bos_choch_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- OB touch: 按 bullish/bearish × internal/swing 拆分 ---
    # 每个子类型取最近首次触碰（最大 first_touch_index → 最小 freshness）
    bars_high = bars["high"].to_numpy(dtype=float)
    bars_low = bars["low"].to_numpy(dtype=float)
    ob_touch_subtypes: dict[str, int] = {}  # key → best_first_touch_index
    for ob in smc_dto.get("order_blocks", []):
        ob_high = ob.get("bar_high")
        ob_low = ob.get("bar_low")
        confirmed_idx = ob.get("confirmed_index")
        bias = ob.get("bias")
        internal = ob.get("internal")
        if ob_high is None or ob_low is None or confirmed_idx is None:
            continue
        if bias is None or internal is None:
            continue
        ob_high_f = float(ob_high)
        ob_low_f = float(ob_low)
        # 从创建 bar 之后开始搜索首次触碰
        start_idx = int(confirmed_idx) + 1
        direction = "bullish" if bias == 1 else "bearish"
        level = "internal" if internal else "swing"
        key = f"order_block_touch_{direction}_{level}"
        first_touch = -1
        for i in range(start_idx, len(bars)):
            if bars_high[i] >= ob_low_f and bars_low[i] <= ob_high_f:
                first_touch = i
                break
        if first_touch >= 0:
            if key not in ob_touch_subtypes or first_touch > ob_touch_subtypes[key]:
                ob_touch_subtypes[key] = first_touch

    for key, best_idx in ob_touch_subtypes.items():
        factor_key = f"smc_{key}_freshness_bars"
        if factor_key in result:
            result[factor_key] = current_index - best_idx

    # --- EQH/EQL: 无 internal/swing 字段，单因子 ---
    for eqhl in smc_dto.get("equal_highs_lows", []):
        etype = eqhl.get("type")
        confirmed_idx = eqhl.get("confirmed_index")
        if etype is None or confirmed_idx is None:
            continue
        factor_key = f"smc_{etype.lower()}_freshness_bars"
        if factor_key not in result:
            continue
        idx = int(confirmed_idx)
        if result[factor_key] is None or idx > (current_index - result[factor_key]):
            result[factor_key] = current_index - idx

    return result


def _compute_all_factors_for_bars(
    bars: pd.DataFrame | None,
    timeframe: str,
    degraded_reasons: list[str],
    warmup_notes: list[str],
    *,
    precomputed_node_cluster: NodeClusterProfileResult | None = None,
) -> dict[str, Any]:
    """计算单周期所有因子组，每组独立异常隔离。

    Args:
        bars: K 线 DataFrame
        timeframe: 周期标识（"1d" / "15m" 等）
        degraded_reasons: 降级原因列表（异常时追加）
        warmup_notes: 预热提示列表
        precomputed_node_cluster: 预计算的 Node Cluster Profile（仅盘后 primary 1d 链路
            由 feature_snapshot_service 注入）。当提供时，cost_position 消费 engine 结果
            （三链同核）；当为 None，cost_position 走单周期 VP（15m secondary 或
            compute_structural_factors 独立调用）。
    """
    factors: dict[str, Any] = {
        "dsa_segment": None,
        "swing_position": None,
        "cost_position": None,
        "volatility_momentum": None,
        "participation": None,
        "smc_freshness": None,
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

    # 3. 成本/节点（precomputed_node_cluster 非空时消费 engine 结果，否则单周期 VP）
    try:
        factors["cost_position"] = _compute_cost_position_factors(
            bars, atr, precomputed_profile=precomputed_node_cluster
        )
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

    # 6. SMC freshness（5 个独立日线因子，仅 1d 周期计算）
    #    [PROMPT.md §四.4] 事件 bar=0，此后按已完成日线 bar 递增，从未发生为 null
    if timeframe == "1d":
        try:
            factors["smc_freshness"] = _compute_smc_freshness_factors(bars)
        except Exception as exc:
            degraded_reasons.append(f"{timeframe}: smc_freshness failed: {exc}")
            logger.warning("%s SMC freshness 计算失败: %s", timeframe, exc)

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
