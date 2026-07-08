"""研究特征矩阵计算模块 - per-bar 因果口径特征计算。

与生产 structural_factor_service 的区别：
- 生产 snapshot: 只计算最后一根 bar 的 single-snapshot 值
- 研究矩阵: 计算每根 bar 的 per-bar 值（full series），用于按月回补

4 命名空间：
- causal: 当时可知的滚动特征（ATR/BB/SQZMOM/volume/active_swing/developing_swing/dsa_confirmed）
- confirmed_delay: 仅在确认 bar 生效的字段（confirmed_swing/bars_since）
- hindsight: 允许未来信息的结构标注（dsa_finalized/node_cluster）
- label: 未来收益/胜负标签（future_return/max_drawdown/breakout_success）

设计：
- 输入: bars DataFrame（DatetimeIndex + open/high/low/close/volume/amount），需含 warmup
- 输出: DataFrame indexed by trade_date，包含所有 33 个 feature 列
- 所有底层计算复用现有算法 SSOT（bollinger/compute_atr/compute_sqzmom_lb/_tv_pivots_confirmed/compute_dsa_history）

用法：
    from app.research.feature_computer import compute_all_features
    features_df = compute_all_features(bars)

模块自测：
    python -m app.research.feature_computer
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from app.strategy.selectors.dsa_selector import compute_dsa_history
from app.strategy_assets.algorithms.features.atr_utils import compute_atr
from app.strategy_assets.algorithms.features.bollinger_features_plotly import bollinger
from app.strategy_assets.algorithms.features.price_action_toolkit_lite_ualgo import (
    _tv_pivots_confirmed,
)
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb

# 复用 structural_factor_service 的固定参数（SSOT）
_BB_WIN = 20
_BB_K = 2.0
_ATR_LENGTH = 14
_SWING_LENGTH = 5
_PERCENTILE_LOOKBACK = 120


def _percentile_rank_rolling(series: np.ndarray, lookback: int) -> np.ndarray:
    """计算每个位置在末尾 lookback 窗口内的百分位排名 [0,1]。

    Args:
        series: 完整序列（numpy array）
        lookback: 回看窗口长度

    Returns:
        np.ndarray: 每个位置的百分位排名，与输入等长
    """
    n = len(series)
    result = np.full(n, np.nan)
    for i in range(n):
        if not np.isfinite(series[i]):
            continue
        start = max(0, i - lookback + 1)
        window = series[start : i + 1]
        finite = window[np.isfinite(window)]
        if len(finite) == 0:
            continue
        result[i] = float(np.sum(finite <= series[i]) / len(finite))
    return result


# =============================================================================
# 1. causal rolling features（7 列）
# =============================================================================


def compute_causal_rolling_features(bars: pd.DataFrame) -> pd.DataFrame:
    """计算 causal 命名空间的滚动特征（per-bar full series）。

    包含 7 列：
    - causal_atr: ATR(14) 波动率
    - causal_bb_percent_b: BB %B = (close - lower) / (upper - lower)
    - causal_bb_bandwidth_pct: BB 带宽百分比 = (upper - lower) / mid
    - causal_sqzmom_val: SQZMOM 动量值
    - causal_sqzmom_delta_1: SQZMOM 一阶差分
    - causal_volume_ratio_20: 20 日成交量比率 = volume / SMA(volume, 20)
    - causal_volume_percentile_120: 120 日成交量百分位

    Args:
        bars: 日线 DataFrame，DatetimeIndex + open/high/low/close/volume

    Returns:
        DataFrame indexed same as bars, 7 causal rolling columns
    """
    n = len(bars)
    result = pd.DataFrame(
        index=bars.index,
        data={
            "causal_atr": np.nan,
            "causal_bb_percent_b": np.nan,
            "causal_bb_bandwidth_pct": np.nan,
            "causal_sqzmom_val": np.nan,
            "causal_sqzmom_delta_1": np.nan,
            "causal_volume_ratio_20": np.nan,
            "causal_volume_percentile_120": np.nan,
        },
    )

    if n < _BB_WIN + 10:
        return result

    closes = bars["close"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    opens = bars["open"].to_numpy(dtype=float) if "open" in bars.columns else closes
    volumes = bars["volume"].to_numpy(dtype=float) if "volume" in bars.columns else np.full(n, np.nan)

    # --- ATR ---
    atr_arr = compute_atr(highs, lows, closes, length=_ATR_LENGTH)
    result["causal_atr"] = atr_arr

    # --- Bollinger Bands ---
    mid_s, upper_s, lower_s = bollinger(bars, _BB_WIN, _BB_K)
    mid_arr = mid_s.to_numpy(dtype=float)
    upper_arr = upper_s.to_numpy(dtype=float)
    lower_arr = lower_s.to_numpy(dtype=float)

    bw = upper_arr - lower_arr
    # percent_b = (close - lower) / (upper - lower)
    with np.errstate(divide="ignore", invalid="ignore"):
        pct_b = np.where(bw > 0, (closes - lower_arr) / bw, np.nan)
    result["causal_bb_percent_b"] = pct_b

    # bandwidth_pct = (upper - lower) / mid
    with np.errstate(divide="ignore", invalid="ignore"):
        bw_pct = np.where(mid_arr > 0, bw / mid_arr, np.nan)
    result["causal_bb_bandwidth_pct"] = bw_pct

    # --- SQZMOM ---
    sqz = compute_sqzmom_lb(opens, highs, lows, closes)
    val_list = sqz.get("val", [])
    val_arr = np.array(
        [v if v is not None else np.nan for v in val_list], dtype=float
    )
    result["causal_sqzmom_val"] = val_arr
    # delta_1 = val[i] - val[i-1]
    delta = np.full(n, np.nan)
    if len(val_arr) >= 2:
        delta[1:] = val_arr[1:] - val_arr[:-1]
    result["causal_sqzmom_delta_1"] = delta

    # --- Volume ---
    if np.all(np.isfinite(volumes)) and n >= 20:
        # volume_ratio_20 = volume / SMA(volume, 20)
        vol_s = pd.Series(volumes, index=bars.index)
        sma_20 = vol_s.rolling(20, min_periods=20).mean()
        result["causal_volume_ratio_20"] = (vol_s / sma_20).to_numpy()

        # volume_percentile_120
        result["causal_volume_percentile_120"] = _percentile_rank_rolling(
            volumes, _PERCENTILE_LOOKBACK
        )

    return result


# =============================================================================
# 2. confirmed_delay swing features（4 列）
# =============================================================================


def compute_confirmed_delay_swing_features(bars: pd.DataFrame) -> pd.DataFrame:
    """计算 confirmed_delay 命名空间的 swing 特征（per-bar）。

    包含 4 列：
    - confirmed_delay_confirmed_swing_high: forward-fill 的确认 pivot high
    - confirmed_delay_confirmed_swing_low: forward-fill 的确认 pivot low
    - confirmed_delay_bars_since_confirmed_swing_high: 距上次确认 high 的 bar 数
    - confirmed_delay_bars_since_confirmed_swing_low: 距上次确认 low 的 bar 数

    规则：
    - pivot 在 anchor+length bar 确认（TradingView 风格）
    - 确认前不回填（anchor 之前为 NULL）
    - bars_since 在确认 bar 为 0，之后递增

    Args:
        bars: 日线 DataFrame

    Returns:
        DataFrame indexed same as bars, 4 confirmed_delay columns
    """
    n = len(bars)
    result = pd.DataFrame(
        index=bars.index,
        data={
            "confirmed_delay_confirmed_swing_high": np.nan,
            "confirmed_delay_confirmed_swing_low": np.nan,
            "confirmed_delay_bars_since_confirmed_swing_high": np.nan,
            "confirmed_delay_bars_since_confirmed_swing_low": np.nan,
        },
    )

    if n < 2 * _SWING_LENGTH + 1:
        return result

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)

    ph, pl, _ph_anchor, _pl_anchor = _tv_pivots_confirmed(highs, lows, _SWING_LENGTH)

    # forward-fill: 确认后填充，确认前为 NaN
    confirmed_high = np.full(n, np.nan)
    confirmed_low = np.full(n, np.nan)
    last_high = np.nan
    last_low = np.nan
    bars_since_high = np.full(n, np.nan)
    bars_since_low = np.full(n, np.nan)
    last_high_bar = -1
    last_low_bar = -1

    for i in range(n):
        if np.isfinite(ph[i]):
            last_high = float(ph[i])
            last_high_bar = i
        if np.isfinite(pl[i]):
            last_low = float(pl[i])
            last_low_bar = i
        confirmed_high[i] = last_high
        confirmed_low[i] = last_low
        if last_high_bar >= 0:
            bars_since_high[i] = i - last_high_bar
        if last_low_bar >= 0:
            bars_since_low[i] = i - last_low_bar

    result["confirmed_delay_confirmed_swing_high"] = confirmed_high
    result["confirmed_delay_confirmed_swing_low"] = confirmed_low
    result["confirmed_delay_bars_since_confirmed_swing_high"] = bars_since_high
    result["confirmed_delay_bars_since_confirmed_swing_low"] = bars_since_low

    return result


# =============================================================================
# 3. DSA dual track features（6 列）
# =============================================================================


def compute_dsa_dual_track_features(bars: pd.DataFrame) -> pd.DataFrame:
    """计算 DSA 双轨特征（causal + hindsight，per-bar）。

    包含 6 列：
    - causal_dsa_confirmed_segment: DSA 段编号（当时已确认，_remove_dsa_lookahead）
    - causal_dsa_confirmed_direction: DSA 方向（1/0/-1）
    - causal_dsa_confirmed_age_bars: 段内已持续 bar 数
    - hindsight_dsa_finalized_segment: DSA 段编号（未来确认后，raw）— Phase 1 暂 NULL
    - hindsight_dsa_finalized_direction: DSA 方向（raw）— Phase 1 暂 NULL
    - hindsight_dsa_finalized_age_bars: 段内最终 bar 数（raw）— Phase 1 暂 NULL

    causal = _remove_dsa_lookahead 后的 DSA（无前视偏差）
    hindsight = 原始 DSA（允许未来修正）— **Phase 1 未实现，全 NULL**

    [Blocker Fix] hindsight 不得用 causal 近似冒充：
    真正 hindsight 需要绕过 _remove_dsa_lookahead 直接取 raw DSA full series。
    本 PR 不实现 raw DSA，故 hindsight_dsa_finalized_* 全部保持 NaN。
    run metadata 必须记录 dsa_hindsight_status=not_implemented。

    Args:
        bars: 日线 DataFrame，需 >= 60 行

    Returns:
        DataFrame indexed same as bars, 6 DSA columns（hindsight 3 列全 NaN）
    """
    n = len(bars)
    result = pd.DataFrame(
        index=bars.index,
        data={
            "causal_dsa_confirmed_segment": np.nan,
            "causal_dsa_confirmed_direction": np.nan,
            "causal_dsa_confirmed_age_bars": np.nan,
            # [Blocker Fix] hindsight Phase 1 未实现，保持 NaN，不写入近似值
            "hindsight_dsa_finalized_segment": np.nan,
            "hindsight_dsa_finalized_direction": np.nan,
            "hindsight_dsa_finalized_age_bars": np.nan,
        },
    )

    if n < 60:
        return result

    # --- causal DSA: compute_dsa_history 已应用 _remove_dsa_lookahead ---
    config: dict[str, Any] = {}
    causal_history = compute_dsa_history(bars, config)
    if causal_history.empty:
        return result

    # dsa_dir_bars = direction * bars_count（正=上, 负=下, 0=未确认）
    dir_bars = causal_history["dsa_dir_bars"].to_numpy()
    directions = np.sign(dir_bars).astype(float)  # 1/0/-1
    age_bars = np.abs(dir_bars).astype(float)  # 段内 bar 数

    # segment_id: 用 group_id（通过方向变化检测）
    dir_changes = np.zeros(n, dtype=bool)
    dir_changes[0] = True
    dir_changes[1:] = directions[1:] != directions[:-1]
    segment_ids = np.cumsum(dir_changes).astype(float)

    result["causal_dsa_confirmed_segment"] = segment_ids
    result["causal_dsa_confirmed_direction"] = directions
    result["causal_dsa_confirmed_age_bars"] = age_bars

    # [Blocker Fix] hindsight_dsa_finalized_* 保持 NaN（Phase 1 未实现 raw DSA）
    # 真正 hindsight 需要 compute_dsa_history 绕过 _remove_dsa_lookahead，
    # 取翻转点修正后的 full series。本 PR 不实现，留待后续 PR。
    # 禁止用 causal 近似冒充 hindsight 写入 DB。

    return result


# =============================================================================
# 4. label features（7 列）
# =============================================================================


def compute_label_features(bars: pd.DataFrame) -> pd.DataFrame:
    """计算 label 命名空间的未来标签（per-bar）。

    包含 7 列：
    - label_future_return_5d/10d/20d: 未来 N 日收益率 = close[i+N]/close[i] - 1
    - label_future_max_drawdown_10d/20d: 未来 N 日最大回撤（<= 0）
    - label_breakout_success_10d: 未来 10 日是否突破成功（0/1）
    - label_failure_breakdown_10d: 未来 10 日是否破位失败（0/1）

    规则：
    - 未来数据：最后 N 行为 NaN（无未来数据）
    - breakout_success: 未来 10 日内 close > 当前 confirmed_swing_high
    - failure_breakdown: 未来 10 日内 close < 当前 confirmed_swing_low

    Args:
        bars: 日线 DataFrame

    Returns:
        DataFrame indexed same as bars, 7 label columns
    """
    n = len(bars)
    closes = bars["close"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float) if "high" in bars.columns else closes
    lows = bars["low"].to_numpy(dtype=float) if "low" in bars.columns else closes

    result = pd.DataFrame(
        index=bars.index,
        data={
            "label_future_return_5d": np.nan,
            "label_future_return_10d": np.nan,
            "label_future_return_20d": np.nan,
            "label_future_max_drawdown_10d": np.nan,
            "label_future_max_drawdown_20d": np.nan,
            "label_breakout_success_10d": np.nan,
            "label_failure_breakdown_10d": np.nan,
        },
    )

    # future returns
    for horizon, col in [(5, "label_future_return_5d"), (10, "label_future_return_10d"), (20, "label_future_return_20d")]:
        rets = np.full(n, np.nan)
        for i in range(n - horizon):
            if closes[i] > 0 and np.isfinite(closes[i + horizon]):
                rets[i] = closes[i + horizon] / closes[i] - 1.0
        result[col] = rets

    # future max drawdown: 未来 N 日内最大回撤（<= 0）
    # = min(0, (min(future_lows) - close[i]) / close[i])
    for horizon, col in [(10, "label_future_max_drawdown_10d"), (20, "label_future_max_drawdown_20d")]:
        mdd = np.full(n, np.nan)
        for i in range(n - horizon):
            future_lows = lows[i + 1 : i + 1 + horizon]
            if closes[i] > 0 and len(future_lows) > 0:
                finite_lows = future_lows[np.isfinite(future_lows)]
                if len(finite_lows) > 0:
                    min_low = np.min(finite_lows)
                    dd = (min_low - closes[i]) / closes[i]
                    mdd[i] = min(0.0, dd)  # 上涨时为 0，下跌时为负
        result[col] = mdd

    # breakout / failure: 用当前 close 作为参考，未来是否突破/破位
    # breakout_success: 未来 10 日内 high > close[i] * 1.02 (2% breakout)
    # failure_breakdown: 未来 10 日内 low < close[i] * 0.98 (2% breakdown)
    breakout = np.full(n, np.nan)
    failure = np.full(n, np.nan)
    threshold = 0.02
    horizon_breakout = 10
    for i in range(n - horizon_breakout):
        future_highs = highs[i + 1 : i + 1 + horizon_breakout]
        future_lows = lows[i + 1 : i + 1 + horizon_breakout]
        if closes[i] > 0:
            breakout_target = closes[i] * (1 + threshold)
            breakdown_target = closes[i] * (1 - threshold)
            breakout[i] = 1.0 if np.any(future_highs > breakout_target) else 0.0
            failure[i] = 1.0 if np.any(future_lows < breakdown_target) else 0.0
    result["label_breakout_success_10d"] = breakout
    result["label_failure_breakdown_10d"] = failure

    return result


# =============================================================================
# 5. active / developing swing features（6 列）
# =============================================================================


def _compute_active_swing_per_bar(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    ph: np.ndarray,
    pl: np.ndarray,
    ph_anchor: np.ndarray,
    pl_anchor: np.ndarray,
) -> pd.DataFrame:
    """计算 per-bar active swing 特征（向量化）。

    对每根 bar i，使用 bar i 及之前的 pivot 信息判断 active leg。
    """
    n = len(highs)
    active_high = np.full(n, np.nan)
    active_low = np.full(n, np.nan)
    active_dir = np.full(n, np.nan)

    # forward-fill anchor positions
    last_ph_anchor = np.full(n, np.nan)
    last_pl_anchor = np.full(n, np.nan)
    last_ph_val = np.full(n, np.nan)
    last_pl_val = np.full(n, np.nan)
    cur_ph_anchor = -1
    cur_pl_anchor = -1
    cur_ph_val = np.nan
    cur_pl_val = np.nan
    for i in range(n):
        if np.isfinite(ph[i]):
            cur_ph_anchor = ph_anchor[i] if np.isfinite(ph_anchor[i]) else i
            cur_ph_val = ph[i]
        if np.isfinite(pl[i]):
            cur_pl_anchor = pl_anchor[i] if np.isfinite(pl_anchor[i]) else i
            cur_pl_val = pl[i]
        last_ph_anchor[i] = cur_ph_anchor
        last_pl_anchor[i] = cur_pl_anchor
        last_ph_val[i] = cur_ph_val
        last_pl_val[i] = cur_pl_val

    for i in range(n):
        ph_a = last_ph_anchor[i]
        pl_a = last_pl_anchor[i]
        ph_v = last_ph_val[i]
        pl_v = last_pl_val[i]

        if not np.isfinite(pl_a) and not np.isfinite(ph_a):
            # fallback: 最近 120 根
            start = max(0, i - 119)
            local_highs = highs[start : i + 1]
            local_lows = lows[start : i + 1]
            if len(local_highs) > 0:
                active_high[i] = float(np.max(local_highs))
            if len(local_lows) > 0:
                active_low[i] = float(np.min(local_lows))
        elif np.isfinite(pl_a) and (not np.isfinite(ph_a) or pl_a > ph_a):
            # up leg: confirmed low 晚于 confirmed high
            active_dir[i] = 1
            active_low[i] = pl_v
            anchor = int(pl_a)
            search_highs = highs[anchor : i + 1]
            if len(search_highs) > 0:
                active_high[i] = float(np.max(search_highs))
        elif np.isfinite(ph_a):
            # down leg
            active_dir[i] = -1
            active_high[i] = ph_v
            anchor = int(ph_a)
            search_lows = lows[anchor : i + 1]
            if len(search_lows) > 0:
                active_low[i] = float(np.min(search_lows))

    return pd.DataFrame(
        index=range(n),
        data={
            "active_high": active_high,
            "active_low": active_low,
            "active_dir": active_dir,
        },
    )


def _compute_developing_swing_per_bar(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    active_df: pd.DataFrame,
) -> pd.DataFrame:
    """计算 per-bar developing swing 特征。

    developing swing = 从 active major leg 的高/低点开始计算的回落/反弹。
    简化实现：用 active_swing 的相反方向作为 developing。
    """
    n = len(highs)
    dev_high = np.full(n, np.nan)
    dev_low = np.full(n, np.nan)
    dev_dir = np.full(n, np.nan)

    active_dir = active_df["active_dir"].to_numpy()
    active_high = active_df["active_high"].to_numpy()
    active_low = active_df["active_low"].to_numpy()

    for i in range(n):
        ad = active_dir[i]
        if not np.isfinite(ad):
            continue
        if ad == 1:
            # up leg: developing = 从 active_high 开始的回落
            dev_dir[i] = -1
            dev_high[i] = active_high[i]
            # developing low = 从 active_high bar 到当前的 min(low)
            # 简化：用 active_low 作为 developing_low 近似
            dev_low[i] = active_low[i]
        else:
            # down leg: developing = 从 active_low 开始的反弹
            dev_dir[i] = 1
            dev_low[i] = active_low[i]
            dev_high[i] = active_high[i]

    return pd.DataFrame(
        index=range(n),
        data={
            "dev_high": dev_high,
            "dev_low": dev_low,
            "dev_dir": dev_dir,
        },
    )


# =============================================================================
# 6. 整合：compute_all_features
# =============================================================================


def compute_all_features(bars: pd.DataFrame) -> pd.DataFrame:
    """计算所有 33 个研究特征列（per-bar full series）。

    调度各命名空间的计算模块，合并为单个 DataFrame。
    Node Cluster 列暂返回 NULL（待后续实现 VolumeNodeMonitor 集成）。

    Args:
        bars: 日线 DataFrame，DatetimeIndex + open/high/low/close/volume/amount，
              需含足够 warmup（>= 60 行 for DSA, >= 30 for BB/ATR）

    Returns:
        DataFrame indexed same as bars, 33 feature columns（与 registry.db_columns() 1:1）
    """
    n = len(bars)
    idx = bars.index

    # 1. causal rolling (7 cols)
    df_causal_rolling = compute_causal_rolling_features(bars)

    # 2. confirmed_delay swing (4 cols)
    df_confirmed = compute_confirmed_delay_swing_features(bars)

    # 3. DSA dual track (6 cols)
    df_dsa = compute_dsa_dual_track_features(bars)

    # 4. labels (7 cols)
    df_labels = compute_label_features(bars)

    # 5. active / developing swing (6 cols)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)

    active_cols = [
        "causal_active_swing_dir",
        "causal_active_swing_high",
        "causal_active_swing_low",
        "causal_developing_swing_dir",
        "causal_developing_swing_high",
        "causal_developing_swing_low",
    ]
    df_swing = pd.DataFrame(
        index=idx,
        data=dict.fromkeys(active_cols, np.nan),
    )

    if n >= 2 * _SWING_LENGTH + 1:
        ph, pl, ph_anchor, pl_anchor = _tv_pivots_confirmed(highs, lows, _SWING_LENGTH)
        active_df = _compute_active_swing_per_bar(
            highs, lows, closes, ph, pl, ph_anchor, pl_anchor
        )
        dev_df = _compute_developing_swing_per_bar(
            highs, lows, closes, active_df
        )

        # Convert direction to string for Text columns
        active_dir_str = np.full(n, None, dtype=object)
        ad = active_df["active_dir"].to_numpy()
        for i in range(n):
            if np.isfinite(ad[i]):
                active_dir_str[i] = str(int(ad[i]))

        dev_dir_str = np.full(n, None, dtype=object)
        dd = dev_df["dev_dir"].to_numpy()
        for i in range(n):
            if np.isfinite(dd[i]):
                dev_dir_str[i] = str(int(dd[i]))

        df_swing["causal_active_swing_dir"] = active_dir_str
        df_swing["causal_active_swing_high"] = active_df["active_high"].to_numpy()
        df_swing["causal_active_swing_low"] = active_df["active_low"].to_numpy()
        df_swing["causal_developing_swing_dir"] = dev_dir_str
        df_swing["causal_developing_swing_high"] = dev_df["dev_high"].to_numpy()
        df_swing["causal_developing_swing_low"] = dev_df["dev_low"].to_numpy()

    # 6. Node Cluster (3 cols) - 暂返回 NULL
    node_cluster_cols = [
        "hindsight_node_cluster_label",
        "hindsight_node_cluster_support",
        "hindsight_node_cluster_resistance",
    ]
    df_node = pd.DataFrame(
        index=idx,
        data=dict.fromkeys(node_cluster_cols, np.nan),
    )

    # 合并所有
    result = pd.concat(
        [df_causal_rolling, df_confirmed, df_dsa, df_labels, df_swing, df_node],
        axis=1,
    )

    return result


if __name__ == "__main__":
    # 自测入口：生成合成数据并验证
    from datetime import date

    rng = np.random.default_rng(seed=42)
    n = 500
    dates = pd.date_range(start=date(2025, 1, 1), periods=n, freq="B")
    returns = rng.normal(0.001, 0.02, n)
    close = 10.0 * np.cumprod(1 + returns)
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.01, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.01, n)))
    volume = rng.integers(1_000_000, 10_000_000, n).astype(float)
    amount = volume * close

    bars = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "amount": amount},
        index=dates,
    )

    features = compute_all_features(bars)
    print(f"bars: {len(bars)} 行")
    print(f"features: {len(features.columns)} 列, {len(features)} 行")
    print(f"columns: {list(features.columns)}")

    # 验证 33 列
    from app.research.feature_causality_registry import build_default_registry

    reg = build_default_registry()
    expected = set(reg.db_columns())
    actual = set(features.columns)
    assert expected == actual, f"不匹配: missing={expected-actual}, extra={actual-expected}"
    print("33 列验证 ✓")

    # 非 NULL 统计
    for col in features.columns:
        non_null = features[col].notna().sum()
        print(f"  {col:50s} non_null={non_null}/{len(features)}")

    print("OK")
