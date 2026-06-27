"""DSA 方向稳定性选股策略 - 基于 features/ 算法的 selector 运行时。

从 ref/交易/selection/selection_dsa.py 迁移核心算法，重构为 StrategyRuntime 插件。

核心选股逻辑：
    DSA VWAP dir=1 持续 > 50 bars → 多头趋势确认
    计算 close 与 VWAP 的偏离率统计、VWAP 收益指标、VWAP/ATR Rope 交叉事件

features/ 算法调用（SSOT，严格不修改）：
    - features.dynamic_swing_anchored_vwap: DSA VWAP 计算
    - features.atr_rope_event_factor_lab_v4: ATR Rope 趋势线计算

向量化改进（相对原始 selection_dsa.py）：
    - bars 计算：cumsum + groupby 向量化（替代 for 循环）
    - trend_strength：groupby + transform 向量化
    - offset_rate_stats：groupby + expanding 向量化
    - 交叉检测：numpy 向量化（替代 for 循环）
    - remove_vwap_lookahead：保留 for 循环（需调用 features/ 函数，无法向量化）

SSOT：
    - compute_dsa_history(bars, config) 是 DSA 指标的唯一权威实现。
    - 每日选股与历史回补必须共用此函数，禁止复制两套公式。
    - 每日选股：history = compute_dsa_history(bars, config); today_result = history.iloc[-1]
    - 历史回补：history = compute_dsa_history(bars, config); results = history.loc[target_dates]

资源预算：DSA 默认 100ms/股（通过 BudgetGuard 控制）
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy.dsa_selector")

from app.strategy._plotly_mock import ensure_plotly_mock  # noqa: E402

# 导入 features/ 算法（SSOT，严格不修改）
# 从包内 app.strategy_assets.algorithms.features 导入，Docker 兼容
ensure_plotly_mock()
from app.models.strategy import StrategyVersion  # noqa: E402
from app.strategy.budget import BudgetExceededError, BudgetGuard  # noqa: E402
from app.strategy.runtime import MarketDataContext, StrategyResult, StrategyRuntime  # noqa: E402
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (  # noqa: E402
    ATRRopeConfig,
    compute_atr_rope,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (  # noqa: E402
    DSAConfig,
    dynamic_swing_anchored_vwap,
)

# 策略常量
MIN_DIR_BARS = 50  # dir=1 持续超过 50 bars 才视为多头趋势
DEFAULT_LOOKBACK = 360  # 默认回看 bar 数（来自 yaml algorithm.lookback）
DSA_BUDGET_MS = 100  # DSA 默认预算 100ms/股


def _remove_dsa_lookahead(
    daily_df: pd.DataFrame,
    vwap_series: pd.Series,
    dir_series: pd.Series,
    cfg: DSAConfig | None = None,
) -> tuple[pd.Series, pd.Series]:
    """消除 DSA VWAP 与方向序列的前视偏差。

    原理：全量计算时，方向翻转点 T 处，vwap_out[anchor..T] 被新方向的
    回填递推覆盖，dir_series 也可能因未来翻转而回溯修正。但 anchor 到 T-1 的
    bar 在实时中不可能知道方向会翻转，这些 bar 的 VWAP 和方向应该是旧方向的
    递推结果。

    解决：找到所有方向翻转点，对每个翻转点用截断到 T-1 的数据重算 DSA，
    用截断结果替换被覆盖的值（anchor 到 T-1 之间的 bar）。

    注意：此函数需调用 features/ 的 dynamic_swing_anchored_vwap，无法向量化。
    翻转点数量通常很少（< 10），性能影响可控。

    Args:
        daily_df: 日线数据 DataFrame
        vwap_series: 全量计算的 VWAP 序列
        dir_series: 方向序列（1/-1）
        cfg: DSA 配置

    Returns:
        (修正后的 VWAP 序列, 修正后的方向序列)
    """
    dir_vals = dir_series.fillna(0).astype(int)
    flip_mask = dir_vals != dir_vals.shift(1)
    # 排除第一个 bar（无前一个方向可比较）
    flip_mask.iloc[0] = False
    flip_indices = daily_df.index[flip_mask].tolist()

    if not flip_indices:
        return vwap_series, dir_series

    if cfg is None:
        cfg = DSAConfig()

    vwap_corrected = vwap_series.copy()
    dir_corrected = dir_series.copy()

    for flip_idx in flip_indices:
        loc = daily_df.index.get_loc(flip_idx)
        if loc < 2:
            continue

        # 截断到翻转点前一个 bar（T-1），此时方向还没翻转
        truncated_df = daily_df.iloc[:loc]
        try:
            vwap_trunc, dir_trunc, _, _ = dynamic_swing_anchored_vwap(truncated_df, cfg)
        except Exception as exc:
            logger.debug("截断 DSA 计算异常 flip_idx=%s: %s", flip_idx, exc)
            continue

        # 截断结果中每个 bar 的 VWAP/dir 是该 bar 时刻的"实时值"（无前视偏差）
        # 用截断结果替换全量结果中被回填覆盖的值
        common_idx = vwap_trunc.index.intersection(vwap_corrected.index)
        # 向量化比较：只替换差异超过阈值的值
        trunc_vals = vwap_trunc.loc[common_idx].astype(float)
        corrected_vals = vwap_corrected.loc[common_idx].astype(float)
        valid_mask = trunc_vals.notna() & corrected_vals.notna()
        diff_mask = (trunc_vals[valid_mask] - corrected_vals[valid_mask]).abs() > 0.001
        replace_idx = diff_mask[diff_mask].index
        vwap_corrected.loc[replace_idx] = trunc_vals.loc[replace_idx]
        dir_corrected.loc[replace_idx] = dir_trunc.loc[replace_idx]

    return vwap_corrected, dir_corrected


def _compute_change_pct(daily_df: pd.DataFrame) -> float | None:
    """计算涨跌幅（%）。"""
    if len(daily_df) < 2:
        return None
    close_today = float(daily_df["close"].iloc[-1])
    close_yesterday = float(daily_df["close"].iloc[-2])
    if close_yesterday == 0:
        return None
    return round((close_today - close_yesterday) / close_yesterday * 100, 2)


def _safe_float(val: Any) -> float | None:
    """安全转换为 float，NaN 返回 None。"""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        f = float(val)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _safe_date(val: Any) -> str | None:
    """[DsaSelector] - 安全转换为 ISO 8601 日期字符串，NaT 返回 None。

    返回字符串而非 date 对象：metrics 会被写入 strategy_results.payload (JSONB)，
    json.dumps 无法序列化 date 对象（会抛 Object of type date is not JSON serializable）。
    """
    if val is None or pd.isna(val):
        return None
    if isinstance(val, pd.Timestamp):
        return val.date().isoformat()
    if isinstance(val, date):
        return val.isoformat()
    try:
        ts = pd.to_datetime(val)
        return ts.date().isoformat() if pd.notna(ts) else None
    except (TypeError, ValueError):
        return None


def _detect_cross_events(
    close: pd.Series,
    line: pd.Series,
    group_id: pd.Series,
) -> tuple[
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
    pd.Series,
]:
    """向量化检测 close 与某条线的交叉事件，并按 DSA 趋势区间累计计数。

    上穿：close[t] > line[t] 且 close[t-1] <= line[t-1]
    下穿：close[t] < line[t] 且 close[t-1] >= line[t-1]

    Args:
        close: 收盘价序列
        line: 趋势线序列（VWAP 或 ATR Rope）
        group_id: DSA 趋势区间分组 ID

    Returns:
        (is_cross_up, is_cross_down, cross_up_count, cross_down_count,
         last_cross_up_date, last_cross_up_price,
         last_cross_down_date, last_cross_down_price)
    """
    cross_up = pd.Series(False, index=close.index)
    cross_down = pd.Series(False, index=close.index)

    close_prev = close.shift(1)
    line_prev = line.shift(1)
    valid = close.notna() & close_prev.notna() & line.notna() & line_prev.notna()

    cross_up[valid] = (close > line) & (close_prev <= line_prev)
    cross_down[valid] = (close < line) & (close_prev >= line_prev)

    cross_up_count = cross_up.groupby(group_id).cumsum().astype(int)
    cross_down_count = cross_down.groupby(group_id).cumsum().astype(int)

    cross_date = pd.Series(pd.NaT, index=close.index)
    cross_price = pd.Series(np.nan, index=close.index)
    cross_date[cross_up] = close.index[cross_up]
    cross_price[cross_up] = close[cross_up]

    last_cross_up_date = cross_date.groupby(group_id).ffill()
    last_cross_up_price = cross_price.groupby(group_id).ffill()

    cross_date = pd.Series(pd.NaT, index=close.index)
    cross_price = pd.Series(np.nan, index=close.index)
    cross_date[cross_down] = close.index[cross_down]
    cross_price[cross_down] = close[cross_down]

    last_cross_down_date = cross_date.groupby(group_id).ffill()
    last_cross_down_price = cross_price.groupby(group_id).ffill()

    return (
        cross_up,
        cross_down,
        cross_up_count,
        cross_down_count,
        last_cross_up_date,
        last_cross_up_price,
        last_cross_down_date,
        last_cross_down_price,
    )


def compute_dsa_history(
    bars: pd.DataFrame,
    config: dict[str, Any],
) -> pd.DataFrame:
    """统一 DSA 历史指标计算（SSOT）。

    每日选股与历史回补共用此函数。所有指标均为因果计算，不含未来数据。
    交叉计数按当前 DSA 趋势区间 group_id 累计，保证点时正确性。

    Args:
        bars: 日线行情 DataFrame，index 为 DatetimeIndex，
              必须包含 open/high/low/close/volume/amount 列。
        config: 运行时配置字典，包含：
            - dsa_config: DSAConfig 实例（默认 DSAConfig()）
            - rope_config: ATRRopeConfig 实例（默认 regime_lookback=55）
            - min_dir_bars: 最小趋势 bar 数（默认 MIN_DIR_BARS=50）
            - lookback: 回看 bar 数（默认 None，不截断）

    Returns:
        DataFrame: 以 bar_time 为索引的完整历史指标表，列见 ad2.md 输出字段集合。
                   数据不足时返回空 DataFrame。
    """
    if bars is None or bars.empty or len(bars) < 60:
        return pd.DataFrame()

    df = bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    dsa_config = config.get("dsa_config", DSAConfig())
    rope_config = config.get("rope_config", ATRRopeConfig(regime_lookback=55))
    min_dir_bars = int(config.get("min_dir_bars", MIN_DIR_BARS))
    lookback = config.get("lookback")

    # 应用 lookback 参数截断数据
    if lookback is not None and len(df) > lookback:
        original_len = len(df)
        df = df.tail(lookback)
        logger.debug("DSA lookback 截断: %d -> %d 行", original_len, len(df))

    # 1. 计算 DSA VWAP 与方向
    vwap_series, dir_series, _, _ = dynamic_swing_anchored_vwap(df, dsa_config)
    vwap_series, dir_series = _remove_dsa_lookahead(df, vwap_series, dir_series, dsa_config)
    dir_vals = dir_series.fillna(0).astype(int)

    # 2. 计算每个 dir 持续区间的 group_id 与 bars count
    change_mask = dir_vals != dir_vals.shift(1)
    change_mask.iloc[0] = True  # 第一根作为新区间起点
    group_id = change_mask.cumsum()
    count = group_id.groupby(group_id).cumcount() + 1
    dsa_bars = (count * dir_vals).astype(int)

    # 3. regime 与 trend_strength
    regime = pd.Series(0, index=df.index, dtype=int)
    regime[dsa_bars > min_dir_bars] = 1
    regime[dsa_bars < -min_dir_bars] = -1

    vwap_vals = vwap_series.astype(float)
    vwap_start = vwap_vals.groupby(group_id).transform("first")
    trend_strength = pd.Series(0.0, index=df.index)
    valid_ts = (count > 1) & vwap_start.notna() & vwap_vals.notna() & (vwap_start != 0)
    trend_strength[valid_ts] = (vwap_vals[valid_ts] / vwap_start[valid_ts] - 1) / count[valid_ts]

    # 4. offset 统计（只在 dir=1 时有效）
    close = df["close"].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        offset_rate = (close - vwap_vals) / vwap_vals
    offset_rate = offset_rate.where(dir_vals == 1)

    offset_mean = offset_rate.groupby(group_id).expanding().mean().reset_index(level=0, drop=True)
    offset_std = (
        offset_rate.groupby(group_id).expanding().std(ddof=0).reset_index(level=0, drop=True)
    )

    offset_percentile = pd.Series(np.nan, index=df.index, dtype=float)
    valid_pct = offset_rate.notna() & offset_mean.notna() & offset_std.notna() & (offset_std > 0)
    if valid_pct.any():
        x = offset_rate[valid_pct].to_numpy()
        mu = offset_mean[valid_pct].to_numpy()
        sigma = offset_std[valid_pct].to_numpy()
        z_scores = (x - mu) / (sigma * math.sqrt(2.0))
        cdf_vals = np.array([0.5 * (1.0 + math.erf(z)) for z in z_scores])
        offset_percentile[valid_pct] = cdf_vals

    # 5. VWAP 收益指标（在 DSA 趋势区间内计算）
    vwap_ret_total = pd.Series(np.nan, index=df.index, dtype=float)
    vwap_ret_avg = pd.Series(np.nan, index=df.index, dtype=float)
    vwap_ret_5 = pd.Series(np.nan, index=df.index, dtype=float)
    vwap_ret_10 = pd.Series(np.nan, index=df.index, dtype=float)
    vwap_ret_20 = pd.Series(np.nan, index=df.index, dtype=float)

    for _gid, grp in vwap_vals.groupby(group_id):
        if len(grp) < 2:
            continue
        start_val = grp.iloc[0]
        if not np.isfinite(start_val) or start_val == 0:
            continue
        idx = grp.index
        # 区间起点到当前的累计收益
        total = grp / start_val - 1.0
        vwap_ret_total.loc[idx] = total
        # 平均每 bar 收益
        vwap_ret_avg.loc[idx] = total / np.arange(1, len(grp) + 1)
        # N 期收益
        vwap_ret_5.loc[idx] = grp / grp.shift(5) - 1.0
        vwap_ret_10.loc[idx] = grp / grp.shift(10) - 1.0
        vwap_ret_20.loc[idx] = grp / grp.shift(20) - 1.0

    # 6. close 相对 VWAP 偏离百分比
    with np.errstate(divide="ignore", invalid="ignore"):
        dsa_vwap_dev_pct = (close - vwap_vals) / vwap_vals * 100.0

    # 7. 涨跌幅、成交量 zscore、20 日平均成交额
    change_pct = close.pct_change() * 100.0
    volume = df["volume"].astype(float)
    vol_mean_20 = volume.rolling(window=20, min_periods=1).mean()
    vol_std_20 = volume.rolling(window=20, min_periods=1).std(ddof=0)
    vol_zscore = pd.Series(np.nan, index=df.index, dtype=float)
    valid_vol = vol_std_20 > 0
    vol_zscore[valid_vol] = (volume[valid_vol] - vol_mean_20[valid_vol]) / vol_std_20[valid_vol]
    amount = df["amount"].astype(float)
    avg_amount_20d = amount.rolling(window=20, min_periods=1).mean()

    # 8. ATR Rope 与方向占比
    rope_dir1_pct = pd.Series(np.nan, index=df.index, dtype=float)
    rope_dir0_pct = pd.Series(np.nan, index=df.index, dtype=float)
    rope_dir_neg1_pct = pd.Series(np.nan, index=df.index, dtype=float)
    touch_rope = pd.Series(False, index=df.index)
    touch_vwap = pd.Series(False, index=df.index)
    atr_rope_rope = pd.Series(np.nan, index=df.index, dtype=float)

    try:
        atr_rope_df = compute_atr_rope(df, rope_config)
        if atr_rope_df is not None and not atr_rope_df.empty:
            atr_rope_dir = atr_rope_df["atr_rope_dir"]
            atr_rope_rope = atr_rope_df["atr_rope_rope"]

            # 按 DSA 趋势区间统计 ATR Rope dir 占比
            for _gid, grp in atr_rope_dir.groupby(group_id):
                total = len(grp)
                if total > 0:
                    rope_dir1_pct.loc[grp.index] = float((grp == 1).sum()) / total * 100.0
                    rope_dir0_pct.loc[grp.index] = float((grp == 0).sum()) / total * 100.0
                    rope_dir_neg1_pct.loc[grp.index] = float((grp == -1).sum()) / total * 100.0

            low = df["low"].astype(float)
            valid_touch = atr_rope_rope.notna() & low.notna()
            touch_rope[valid_touch] = low[valid_touch] <= atr_rope_rope[valid_touch]
            touch_vwap[valid_touch] = low[valid_touch] <= vwap_vals[valid_touch]
    except Exception as exc:
        logger.debug("ATR Rope 计算异常: %s", exc)

    # 9. VWAP 交叉事件（按 DSA 趋势区间 groupby 累计）
    (
        vwap_cross_up,
        vwap_cross_down,
        cross_up_count,
        cross_down_count,
        last_cross_up_date,
        last_cross_up_price,
        last_cross_down_date,
        last_cross_down_price,
    ) = _detect_cross_events(close, vwap_vals, group_id)

    # 10. ATR Rope 交叉事件（按 DSA 趋势区间 groupby 累计）
    (
        rope_cross_up,
        rope_cross_down,
        rope_cross_up_count,
        rope_cross_down_count,
        rope_cross_up_date,
        rope_cross_up_price,
        rope_cross_down_date,
        rope_cross_down_price,
    ) = _detect_cross_events(close, atr_rope_rope, group_id)

    # 11. 组装结果
    result = pd.DataFrame(
        {
            "regime_value": regime,
            "regime_strength": trend_strength,
            "dsa_dir_bars": dsa_bars,
            "offset_rate": offset_rate,
            "offset_mean": offset_mean,
            "offset_std": offset_std,
            "offset_percentile": offset_percentile,
            "vwap_ret_avg": vwap_ret_avg,
            "vwap_ret_total": vwap_ret_total,
            "vwap_ret_5": vwap_ret_5,
            "vwap_ret_10": vwap_ret_10,
            "vwap_ret_20": vwap_ret_20,
            "dsa_vwap": vwap_vals,
            "dsa_vwap_dev_pct": dsa_vwap_dev_pct,
            "change_pct": change_pct,
            "vol_zscore": vol_zscore,
            "avg_amount_20d": avg_amount_20d,
            "rope_dir1_pct": rope_dir1_pct,
            "rope_dir0_pct": rope_dir0_pct,
            "rope_dir_neg1_pct": rope_dir_neg1_pct,
            "touch_rope": touch_rope,
            "touch_vwap": touch_vwap,
            "last_cross_up_date": last_cross_up_date,
            "last_cross_up_price": last_cross_up_price,
            "last_cross_down_date": last_cross_down_date,
            "last_cross_down_price": last_cross_down_price,
            "cross_up_count": cross_up_count,
            "cross_down_count": cross_down_count,
            "rope_cross_up_date": rope_cross_up_date,
            "rope_cross_up_price": rope_cross_up_price,
            "rope_cross_down_date": rope_cross_down_date,
            "rope_cross_down_price": rope_cross_down_price,
            "rope_cross_up_count": rope_cross_up_count,
            "rope_cross_down_count": rope_cross_down_count,
        },
        index=df.index,
    )

    # offset_variance_rate: 偏离率变异系数 = offset_std / |offset_mean|
    offset_variance_rate = pd.Series(np.nan, index=df.index, dtype=float)
    valid_var = offset_mean.notna() & offset_std.notna() & offset_mean.abs().gt(1e-10)
    offset_variance_rate[valid_var] = offset_std[valid_var] / offset_mean[valid_var].abs()
    result["offset_variance_rate"] = offset_variance_rate

    return result


def _history_row_to_metrics(row: pd.Series) -> dict[str, Any]:
    """将 compute_dsa_history 的单行结果转为 StrategyResult.metrics 字典。"""
    metrics: dict[str, Any] = {
        # yaml outputs 字段
        "dsa_dir_bars": int(row["dsa_dir_bars"]) if pd.notna(row["dsa_dir_bars"]) else 0,
        "vwap_ret_avg": _safe_float(row["vwap_ret_avg"]),
        "vwap_ret_total": _safe_float(row["vwap_ret_total"]),
        "offset_mean": _safe_float(row["offset_mean"]),
        "offset_std": _safe_float(row["offset_std"]),
        "offset_variance_rate": _safe_float(row["offset_variance_rate"]),
        "offset_percentile": _safe_float(row["offset_percentile"]),
        # 扩展字段（用于详情展示和筛选）
        "regime_value": int(row["regime_value"]) if pd.notna(row["regime_value"]) else 0,
        "regime_strength": _safe_float(row["regime_strength"]),
        "offset_rate": _safe_float(row["offset_rate"]),
        "change_pct": _safe_float(row["change_pct"]),
        "touch_rope": bool(row["touch_rope"]) if pd.notna(row["touch_rope"]) else False,
        "touch_vwap": bool(row["touch_vwap"]) if pd.notna(row["touch_vwap"]) else False,
        "rope_dir1_pct": _safe_float(row["rope_dir1_pct"]),
        "rope_dir0_pct": _safe_float(row["rope_dir0_pct"]),
        "rope_dir_neg1_pct": _safe_float(row["rope_dir_neg1_pct"]),
        "cross_up_count": int(row["cross_up_count"]) if pd.notna(row["cross_up_count"]) else 0,
        "cross_down_count": int(row["cross_down_count"])
        if pd.notna(row["cross_down_count"])
        else 0,
        "last_cross_up_date": _safe_date(row["last_cross_up_date"]),
        "last_cross_down_date": _safe_date(row["last_cross_down_date"]),
        # ad2.md 新增字段
        "vwap_ret_5": _safe_float(row["vwap_ret_5"]),
        "vwap_ret_10": _safe_float(row["vwap_ret_10"]),
        "vwap_ret_20": _safe_float(row["vwap_ret_20"]),
        "dsa_vwap": _safe_float(row["dsa_vwap"]),
        "dsa_vwap_dev_pct": _safe_float(row["dsa_vwap_dev_pct"]),
        "vol_zscore": _safe_float(row["vol_zscore"]),
        "avg_amount_20d": _safe_float(row["avg_amount_20d"]),
        "rope_cross_up_date": _safe_date(row["rope_cross_up_date"]),
        "rope_cross_down_date": _safe_date(row["rope_cross_down_date"]),
        "rope_cross_up_price": _safe_float(row["rope_cross_up_price"]),
        "rope_cross_down_price": _safe_float(row["rope_cross_down_price"]),
        "rope_cross_up_count": int(row["rope_cross_up_count"])
        if pd.notna(row["rope_cross_up_count"])
        else 0,
        "rope_cross_down_count": int(row["rope_cross_down_count"])
        if pd.notna(row["rope_cross_down_count"])
        else 0,
    }
    return metrics


class DSASelector(StrategyRuntime):
    """DSA 方向稳定性选股策略运行时。

    从 ref/交易/selection/selection_dsa.py 迁移，继承 StrategyRuntime ABC。
    kind="selector"，按交易日输出每只股票的 DSA 指标。

    选股逻辑：
    1. 调用 features.dynamic_swing_anchored_vwap 计算 DSA VWAP 和 dir
    2. 消除前视偏差
    3. 调用 compute_dsa_history 统一计算完整历史指标
    4. 取最后一行作为当前交易日结果

    注意：matched 不再基于 regime 判定，所有有效结果均 matched=True。
    命中由用户筛选条件动态决定（如 regime_value == 1 或 dsa_dir_bars > 50）。
    MIN_DIR_BARS=50 是 regime 命中阈值（代码常量），不是算法计算参数，
    不从 manifest 读取，所有股票输出真实 dsa_dir_bars 供用户筛选。

    资源预算：100ms/股（通过 BudgetGuard 控制）
    """

    kind = "selector"

    def __init__(self) -> None:
        self._version: StrategyVersion | None = None
        self._lookback: int = DEFAULT_LOOKBACK
        self._min_dir_bars: int = MIN_DIR_BARS
        self._budget_guard: BudgetGuard = BudgetGuard(timeout_ms=DSA_BUDGET_MS)
        # DSAConfig 和 ATRRopeConfig（initialize() 中从 manifest 读取）
        self._dsa_config: DSAConfig = DSAConfig()
        self._rope_config: ATRRopeConfig = ATRRopeConfig(regime_lookback=55)
        # effective_config 快照（供 BatchService 读取，保存到 strategy_runs.effective_config）
        self._effective_config: dict[str, Any] = {}

    async def initialize(self, version: StrategyVersion) -> None:
        """加载策略版本配置。

        从 manifest.parameters 读取所有算法参数（全部 allowed_scopes: [system]）：
        - algorithm.lookback: 回看 bar 数（默认 800）
        - dsa.*: DSAConfig 参数（prd/base_apt/use_adapt/vol_bias/atr_len）
        - atr_rope.*: ATRRopeConfig 参数（length/multi/regime_lookback/regime_threshold）
        - resource_budget.target_ms_per_instrument: 超时预算（默认 100ms）

        注意：algorithm.min_dir_bars 已从 manifest 移出，它是 regime 命中阈值
        （regime[bars > min_dir_bars] = 1），保留为代码常量 MIN_DIR_BARS=50。
        所有股票输出真实 dsa_dir_bars，由用户筛选条件动态决定。

        Args:
            version: 策略版本 ORM 对象
        """
        self._version = version
        manifest = version.manifest

        # 从 manifest.parameters 提取所有参数
        parameters = manifest.get("parameters", [])
        params: dict[str, Any] = {p["key"]: p.get("default") for p in parameters}

        # 算法参数（min_dir_bars 不从 manifest 读取，使用代码常量）
        self._lookback = int(params.get("algorithm.lookback", DEFAULT_LOOKBACK))

        # DSAConfig 参数
        self._dsa_config = DSAConfig(
            prd=int(params.get("dsa.prd", 50)),
            baseAPT=float(params.get("dsa.base_apt", 20.0)),
            useAdapt=bool(params.get("dsa.use_adapt", False)),
            volBias=float(params.get("dsa.vol_bias", 10.0)),
            atrLen=int(params.get("dsa.atr_len", 50)),
        )

        # ATRRopeConfig 参数（仅计算相关参数，不含绘图参数）
        self._rope_config = ATRRopeConfig(
            length=int(params.get("atr_rope.length", 14)),
            multi=float(params.get("atr_rope.multi", 1.5)),
            regime_lookback=int(params.get("atr_rope.regime_lookback", 55)),
            regime_threshold=float(params.get("atr_rope.regime_threshold", 0.55)),
        )

        # 从 manifest.resource_budget 提取超时预算
        budget = manifest.get("resource_budget", {})
        target_ms = int(budget.get("target_ms_per_instrument", DSA_BUDGET_MS))
        self._budget_guard = BudgetGuard(timeout_ms=target_ms)

        # 保存 effective_config 快照
        self._effective_config = params

        logger.info(
            "DSASelector 初始化: lookback=%d, min_dir_bars=%d(常量), "
            "dsa_config(prd=%d, baseAPT=%.1f, useAdapt=%s, volBias=%.1f, atrLen=%d), "
            "rope_config(length=%d, multi=%.1f, regime_lookback=%d, regime_threshold=%.2f), "
            "budget_ms=%d",
            self._lookback,
            self._min_dir_bars,
            self._dsa_config.prd,
            self._dsa_config.baseAPT,
            self._dsa_config.useAdapt,
            self._dsa_config.volBias,
            self._dsa_config.atrLen,
            self._rope_config.length,
            self._rope_config.multi,
            self._rope_config.regime_lookback,
            self._rope_config.regime_threshold,
            target_ms,
        )

    def _build_history_config(self) -> dict[str, Any]:
        """构建 compute_dsa_history 的运行时配置。"""
        return {
            "dsa_config": self._dsa_config,
            "rope_config": self._rope_config,
            "min_dir_bars": self._min_dir_bars,
            "lookback": self._lookback,
        }

    async def execute(self, context: MarketDataContext) -> StrategyResult:
        """执行 DSA 选股计算。

        流程：
        1. 通过 BudgetGuard 在预算内执行同步计算
        2. 调用 compute_dsa_history 计算完整历史指标
        3. 取最后一行作为当前交易日结果
        4. 返回标准 StrategyResult

        Args:
            context: 市场数据上下文（含日线行情）

        Returns:
            StrategyResult: matched=True 对所有有效结果
        """
        if self._version is None:
            raise RuntimeError("DSASelector 未初始化，请先调用 initialize()")

        # 通过 BudgetGuard 在预算内执行同步计算
        try:
            metrics = await self._budget_guard.run_with_budget(self._compute_metrics_sync, context)
        except BudgetExceededError:
            logger.warning(
                "DSA 计算超时 instrument_id=%s symbol=%s",
                context.instrument_id,
                context.symbol,
            )
            return StrategyResult(
                instrument_id=context.instrument_id,
                strategy_version_id=self._version.id,
                trade_date=context.trade_date or date.today(),
                matched=False,
                metrics={"error": "budget_exceeded"},
                calculation_id=None,
            )

        matched = True
        return StrategyResult(
            instrument_id=context.instrument_id,
            strategy_version_id=self._version.id,
            trade_date=context.trade_date or date.today(),
            matched=matched,
            metrics=metrics,
            calculation_id=uuid.uuid4().hex,
        )

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算 DSA VWAP 图表指标（轻量版，不计算 regime/offset/crossover）。

        供个股详情页面实时计算使用。只计算 DSA VWAP 线和方向。

        Returns:
            {"dsa_vwap": [float...], "dsa_dir": [int...]} 最近 N 根 bar 的 VWAP 和方向
        """
        daily_df = context.bars_daily
        if daily_df is None or len(daily_df) < self._dsa_config.prd:
            return {
                "dsa_vwap": [],
                "dsa_dir": [],
                "direction": [],
                "regime_id": [],
                "anchor_id": [],
                "anchor_time": [],
            }

        vwap_series, dir_series, _, _ = dynamic_swing_anchored_vwap(daily_df, self._dsa_config)
        vwap_series, dir_series = _remove_dsa_lookahead(
            daily_df, vwap_series, dir_series, self._dsa_config
        )

        return {
            "dsa_vwap": [None if pd.isna(v) else float(v) for v in vwap_series],
            "dsa_dir": [int(d) for d in dir_series],
        }

    def _compute_metrics_sync(self, context: MarketDataContext) -> dict[str, Any]:
        """同步计算 DSA 指标（在线程池中执行）。

        此方法由 BudgetGuard.run_with_budget 通过 asyncio.to_thread 调用。

        Args:
            context: 市场数据上下文

        Returns:
            指标字典（包含 yaml 中声明的所有指标）
        """
        daily_df = context.bars_daily
        if daily_df is None or daily_df.empty or len(daily_df) < 60:
            return {"regime_value": 0, "error": "insufficient_data"}

        try:
            history = compute_dsa_history(daily_df, self._build_history_config())
        except Exception as exc:
            logger.warning("DSA history 计算异常 symbol=%s: %s", context.symbol, exc)
            return {"regime_value": 0, "error": f"dsa_compute_failed: {exc}"}

        if history.empty:
            return {"regime_value": 0, "error": "insufficient_data"}

        row = history.iloc[-1]
        metrics = _history_row_to_metrics(row)
        metrics["last_close"] = _safe_float(daily_df["close"].iloc[-1])

        return metrics


if __name__ == "__main__":
    # 自测入口：验证 DSASelector 基础逻辑与 compute_dsa_history（无副作用，不连 DB/网络）
    import inspect

    # 1. 验证类属性
    assert DSASelector.kind == "selector"
    print(f"DSASelector.kind={DSASelector.kind} ✓")

    # 2. 验证方法签名
    init_sig = inspect.signature(DSASelector.initialize)
    assert "version" in init_sig.parameters
    print(f"initialize params={list(init_sig.parameters.keys())} ✓")

    exec_sig = inspect.signature(DSASelector.execute)
    assert "context" in exec_sig.parameters
    print(f"execute params={list(exec_sig.parameters.keys())} ✓")

    # 3. 验证 compute_dsa_history 存在
    assert callable(compute_dsa_history)
    print("compute_dsa_history 可调用 ✓")

    # 4. 验证 compute_dsa_history 基础逻辑（构造足够长的随机行情）
    np.random.seed(42)
    n = 100
    idx = pd.date_range("2026-01-01", periods=n)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.2)
    df = pd.DataFrame(
        {
            "open": close * (1 + np.random.randn(n) * 0.01),
            "high": close * (1 + abs(np.random.randn(n)) * 0.02),
            "low": close * (1 - abs(np.random.randn(n)) * 0.02),
            "close": close,
            "volume": np.abs(np.random.randn(n) * 1e6) + 1e5,
            "amount": np.abs(np.random.randn(n) * 1e8) + 1e7,
        },
        index=idx,
    )
    cfg = {
        "dsa_config": DSAConfig(),
        "rope_config": ATRRopeConfig(regime_lookback=55),
        "min_dir_bars": 50,
        "lookback": None,
    }
    history = compute_dsa_history(df, cfg)
    assert not history.empty, "compute_dsa_history 不应返回空"
    assert "regime_value" in history.columns
    assert "dsa_dir_bars" in history.columns
    assert "vwap_ret_5" in history.columns
    assert "rope_cross_up_count" in history.columns
    print(f"compute_dsa_history columns={list(history.columns)} ✓")

    # 5. 验证交叉计数按 DSA 趋势区间累计
    # 构造一个明显上穿 VWAP 的场景
    cross_close = pd.Series(
        [10.0, 11.0, 10.5, 12.0, 11.5], index=pd.date_range("2026-01-01", periods=5)
    )
    cross_line = pd.Series([10.5, 10.5, 10.5, 10.5, 10.5], index=cross_close.index)
    cross_gid = pd.Series([1, 1, 1, 1, 1], index=cross_close.index)
    (
        is_up,
        is_down,
        up_count,
        down_count,
        last_up_date,
        last_up_price,
        last_down_date,
        last_down_price,
    ) = _detect_cross_events(cross_close, cross_line, cross_gid)
    assert up_count.iloc[-1] >= 1, f"应有上穿, 实际 up_count={up_count.iloc[-1]}"
    assert last_up_date.iloc[-1] is not pd.NaT
    print(f"交叉检测: up_count={up_count.iloc[-1]}, last_up_price={last_up_price.iloc[-1]} ✓")

    # 6. 验证 _history_row_to_metrics 转换
    row = history.iloc[-1]
    metrics = _history_row_to_metrics(row)
    assert "dsa_dir_bars" in metrics
    assert "vwap_ret_5" in metrics
    print("metrics keys 包含扩展字段 ✓")

    print("OK")
