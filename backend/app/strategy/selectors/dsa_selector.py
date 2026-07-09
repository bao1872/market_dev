"""DSA 方向稳定性选股策略 - 基于 features/ 算法的 selector 运行时。

从 ref/交易/selection/selection_dsa.py 迁移核心算法，重构为 StrategyRuntime 插件。

核心输出逻辑：
    为全部有效 A 股计算最近交易日 DSA 因子快照
    计算 close 与 VWAP 的偏离率统计、VWAP 收益指标、VWAP/ATR Rope 交叉事件
    不基于 dsa_dir_bars、regime_value、offset_*、收益率、多空方向或 matched 过滤股票

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
    - compute_dsa_bundle(bars, config) 是 DSA 统一计算入口，封装 compute_dsa_history
      与图表字段（pivot_labels/regime_id）。execute() 取 last_row_metrics，
      compute_indicators() 取 factor_per_bar / visual_segments / factor_time，
      两者共享同一份计算结果。

超时控制：已取消单股 100ms 硬超时，改为 strategy_batch_service 的
run 级总超时 + 可取消机制，避免高历史数据股票被误杀。
"""

from __future__ import annotations

import asyncio
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
from app.constants.indicator_contract import DSA_LOOKBACK  # noqa: E402
from app.models.strategy import StrategyVersion  # noqa: E402
from app.strategy.runtime import MarketDataContext, StrategyResult, StrategyRuntime  # noqa: E402
from app.strategy_assets.algorithms.features.atr_rope_event_factor_lab_v4 import (  # noqa: E402
    ATRRopeConfig,
    compute_atr_rope,
)
from app.strategy_assets.algorithms.features.dynamic_swing_anchored_vwap import (  # noqa: E402
    DSAConfig,
    dynamic_swing_anchored_vwap,
    format_dsa_time,
)

# 策略常量
MIN_DIR_BARS = 50  # dir=1 持续超过 50 bars 才视为多头趋势


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

    # [DSA Selector] - offset 统计：多空双向计算（dir=±1 均纳入统计）
    close = df["close"].astype(float)
    with np.errstate(divide="ignore", invalid="ignore"):
        offset_rate = (close - vwap_vals) / vwap_vals
    offset_rate = offset_rate.replace([np.inf, -np.inf], np.nan)

    offset_mean = offset_rate.groupby(group_id).expanding().mean().reset_index(level=0, drop=True)
    offset_std = (
        offset_rate.groupby(group_id).expanding().std(ddof=0).reset_index(level=0, drop=True)
    )

    offset_percentile = pd.Series(np.nan, index=df.index, dtype=float)
    valid_pct = offset_rate.notna() & offset_mean.notna() & offset_std.notna() & (offset_std > 0)
    zero_std_mask = (
        offset_rate.notna() & offset_mean.notna() & offset_std.notna() & (offset_std == 0)
    )
    if valid_pct.any():
        x = offset_rate[valid_pct].to_numpy()
        mu = offset_mean[valid_pct].to_numpy()
        sigma = offset_std[valid_pct].to_numpy()
        z_scores = (x - mu) / (sigma * math.sqrt(2.0))
        cdf_vals = np.array([0.5 * (1.0 + math.erf(z)) for z in z_scores])
        offset_percentile[valid_pct] = cdf_vals
    if zero_std_mask.any():
        offset_percentile[zero_std_mask] = 0.5

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


def compute_dsa_bundle(bars: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    """DSA 统一计算入口，封装 VWAP 计算 + 后处理 + metrics 提取。

    [DSA 因子与可视化契约分离] - 描述: 同时产出因子序列（选股用）与可视化契约
    （图表用），因子与可视化彻底分离。factor_per_bar 仅供选股/metrics，
    visual_segments/pivot_labels/anchor 仅供前端 Pine 风格渲染，互不混用。

    - execute() 取 last_row_metrics 作为 StrategyResult.metrics
    - compute_indicators() 取 factor_per_bar / visual_segments / factor_time 转为图表数组

    内部调用 compute_dsa_history（SSOT）获取 metrics，并单独调用
    dynamic_swing_anchored_vwap 获取图表字段（pivot_labels/segments）。
    为避免修改 compute_dsa_history（SSOT，可能被其他地方引用），接受
    dynamic_swing_anchored_vwap 被调用 2 次的开销（每次 < 100ms，预算内）。

    Args:
        bars: 日线行情 DataFrame，index 为 DatetimeIndex，
              必须包含 open/high/low/close/volume/amount 列。
        config: 运行时配置字典（与 compute_dsa_history 相同）：
            - dsa_config: DSAConfig 实例（默认 DSAConfig()）
            - rope_config: ATRRopeConfig 实例（默认 regime_lookback=55）
            - min_dir_bars: 最小趋势 bar 数（默认 MIN_DIR_BARS=50）
            - lookback: 回看 bar 数（默认 None，不截断）

    Returns:
        dict 包含:
        - factor_per_bar: pd.DataFrame，含 compute_dsa_history 全部列 +
          dsa_dir/regime_id/anchor_time/pivot_type/pivot_price 图表字段
        - visual_segments: list[dict]，直接由 dynamic_swing_anchored_vwap
          返回的 segments 转换而来，格式 [{direction, points:[{time,value}]}]
        - factor_time: pd.DatetimeIndex，等于 factor_per_bar.index
        - pivot_labels: list[dict]，直接由 dynamic_swing_anchored_vwap 返回
        - anchor: dict，锚点信息（time/price/direction/type 列表）
        - last_row_metrics: dict，从 factor_per_bar 最后一行提取的标量 metrics
        - per_bar: pd.DataFrame，factor_per_bar 别名（向后兼容历史测试）
        数据不足时返回各字段为空的结构
    """
    if bars is None or bars.empty or len(bars) < 60:
        return {
            "factor_per_bar": pd.DataFrame(),
            "visual_segments": [],
            "factor_time": pd.DatetimeIndex([]),
            "pivot_labels": [],
            "anchor": {},
            "last_row_metrics": {},
            "per_bar": pd.DataFrame(),  # 向后兼容
        }

    # 1. 调用 compute_dsa_history 获取 metrics DataFrame（SSOT）
    history = compute_dsa_history(bars, config)
    if history.empty:
        return {
            "factor_per_bar": pd.DataFrame(),
            "visual_segments": [],
            "factor_time": pd.DatetimeIndex([]),
            "pivot_labels": [],
            "anchor": {},
            "last_row_metrics": {},
            "per_bar": pd.DataFrame(),  # 向后兼容
        }

    # 2. 提取最后一行作为标量 metrics
    last_row = history.iloc[-1]
    last_row_metrics = _history_row_to_metrics(last_row)

    # 3. 单独调用 dynamic_swing_anchored_vwap 获取图表字段（pivot_labels/regime_id）
    # 必须与 compute_dsa_history 使用相同的 lookback 截断，保证 df.index 与 history.index 对齐
    dsa_config = config.get("dsa_config", DSAConfig())
    lookback = config.get("lookback")

    df = bars.copy()
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)
    if lookback is not None and len(df) > lookback:
        df = df.tail(lookback)

    vwap_series, dir_series, pivot_labels, segments = dynamic_swing_anchored_vwap(df, dsa_config)
    vwap_series, dir_series = _remove_dsa_lookahead(df, vwap_series, dir_series, dsa_config)

    # 4. 构建 per_bar = history + 图表字段
    per_bar = history.copy()
    n = len(per_bar)

    # dsa_dir: 方向序列（1/-1，NaN 填 0）
    dir_vals = dir_series.fillna(0).astype(int)
    per_bar["dsa_dir"] = dir_vals.values

    # regime_id: dir 第一次切换时递增（与原 compute_indicators 逻辑一致）
    regime_id: list[int] = [0] * n
    current_regime = 0
    last_dir: int | None = None
    for i, d_val in enumerate(dir_vals.values):
        d_int = int(d_val)
        if last_dir is not None and d_int != last_dir and d_int != 0:
            current_regime += 1
        if d_int != 0:
            last_dir = d_int
        regime_id[i] = current_regime
    per_bar["regime_id"] = regime_id

    # pivot_type / pivot_price / anchor_time: 从稀疏 pivot_labels 构造按 bar 对齐的密集数组
    pivot_type: list[str | None] = [None] * n
    pivot_price: list[float | None] = [None] * n
    anchor_time: list[str | None] = [None] * n

    index_list = list(df.index)
    for label in pivot_labels:
        t = int(label["t"])
        if 0 <= t < n:
            txt = label.get("text")
            if txt in {"HH", "HL", "LH", "LL"}:
                pivot_type[t] = txt
                pivot_price[t] = float(label["y"]) if np.isfinite(label["y"]) else None
            anchor_time[t] = index_list[t].isoformat()

    per_bar["pivot_type"] = pivot_type
    per_bar["pivot_price"] = pivot_price
    per_bar["anchor_time"] = anchor_time

    # 5. 构建 anchor：从 pivot_labels 提取锚点信息（每个 dir 翻转点 = 一个锚点）
    # time 通过 format_dsa_time 序列化（PR #34）：
    #   1d/1w/1mo 为 YYYY-MM-DD，15m/1h 含 THH:MM:SS，与 visual_segments 同口径。
    anchor = {
        "time": [format_dsa_time(lab["x"]) for lab in pivot_labels],
        "price": [float(lab["y"]) if np.isfinite(lab["y"]) else None for lab in pivot_labels],
        "direction": [int(lab["dir"]) for lab in pivot_labels],
        "type": [lab["text"] or None for lab in pivot_labels],
    }

    # 6. 返回因子与可视化契约分离的结构
    # visual_segments 直接透传算法返回的 segments（Pine polyline 契约）
    # per_bar 作为 factor_per_bar 别名保留，向后兼容 test_dsa_bundle_consistency 等历史测试
    return {
        "factor_per_bar": per_bar,
        "visual_segments": segments,
        "factor_time": per_bar.index,
        "pivot_labels": pivot_labels,
        "anchor": anchor,
        "last_row_metrics": last_row_metrics,
        "per_bar": per_bar,  # 向后兼容
    }


class DSASelector(StrategyRuntime):
    """DSA 方向稳定性选股策略运行时。

    从 ref/交易/selection/selection_dsa.py 迁移，继承 StrategyRuntime ABC。
    kind="selector"，按交易日输出每只股票的 DSA 指标。

    输出逻辑：
    1. 调用 features.dynamic_swing_anchored_vwap 计算 DSA VWAP 和 dir
    2. 消除前视偏差
    3. 调用 compute_dsa_history 统一计算完整历史指标
    4. 取最后一行作为当前交易日结果

    注意：matched 不再基于 regime 判定，所有有效结果均 matched=True。
    命中由用户筛选条件动态决定（如 regime_value == 1 或 dsa_dir_bars > 50）。
    MIN_DIR_BARS=50 是 regime 命中阈值（代码常量），不是算法计算参数，
    不从 manifest 读取，所有股票输出真实 dsa_dir_bars 供用户筛选。

    注意：已取消单股 100ms 硬超时。run 级总超时与可取消机制由
    strategy_batch_service 统一控制，避免高历史数据股票被误杀。
    """

    kind = "selector"

    def __init__(self) -> None:
        self._version: StrategyVersion | None = None
        self._lookback: int = DSA_LOOKBACK
        self._min_dir_bars: int = MIN_DIR_BARS
        # DSAConfig 和 ATRRopeConfig（initialize() 中从 manifest 读取）
        self._dsa_config: DSAConfig = DSAConfig()
        self._rope_config: ATRRopeConfig = ATRRopeConfig(regime_lookback=55)
        # effective_config 快照（供 BatchService 读取，保存到 strategy_runs.effective_config）
        self._effective_config: dict[str, Any] = {}

    async def initialize(self, version: StrategyVersion) -> None:
        """加载策略版本配置。

        从 manifest.parameters 读取所有算法参数（全部 allowed_scopes: [system]）：
        - algorithm.lookback: 回看 bar 数（默认 DSA_LOOKBACK=250）
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
        self._lookback = int(params.get("algorithm.lookback", DSA_LOOKBACK))

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

        # 保存 effective_config 快照
        self._effective_config = params

        logger.info(
            "DSASelector 初始化: lookback=%d, min_dir_bars=%d(常量), "
            "dsa_config(prd=%d, baseAPT=%.1f, useAdapt=%s, volBias=%.1f, atrLen=%d), "
            "rope_config(length=%d, multi=%.1f, regime_lookback=%d, regime_threshold=%.2f)",
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
        1. 通过 compute_dsa_bundle 统一入口计算（内部调用 compute_dsa_history）
        2. 取 last_row_metrics 作为当前交易日结果
        3. 返回标准 StrategyResult

        注意：已取消单股 100ms 硬超时。超时控制上移到 strategy_batch_service
        的 run 级总超时 + 可取消机制，避免高历史数据股票被误杀。

        Args:
            context: 市场数据上下文（含日线行情）

        Returns:
            StrategyResult: matched=True 对所有有效结果
        """
        if self._version is None:
            raise RuntimeError("DSASelector 未初始化，请先调用 initialize()")

        if context.trade_date is None:
            raise ValueError("DSASelector 需要 context.trade_date，但收到 None")

        metrics = await asyncio.to_thread(self._compute_metrics_sync, context)

        matched = True
        return StrategyResult(
            instrument_id=context.instrument_id,
            strategy_version_id=self._version.id,
            trade_date=context.trade_date,
            matched=matched,
            metrics=metrics,
            calculation_id=uuid.uuid4().hex,
        )

    async def compute_indicators(self, context: MarketDataContext) -> dict[str, Any]:
        """计算 DSA VWAP 图表指标（含 Pine 标签、regime 分段与可视化线段）。

        供个股详情页面实时计算使用。通过 compute_dsa_bundle 统一入口获取
        factor_per_bar / visual_segments / factor_time（与 execute() 共享同一份
        计算结果，消除双路径不一致）。应用与选股相同的 lookback=250
        （self._lookback，与 indicator_contract.DSA_LOOKBACK 对齐），确保图表与
        选股指标口径一致。

        [DSA 因子与可视化契约分离] - 描述: time/visual_segments 直接由 bundle 透传，
        与 indicator_service 的 source_bar_times 对齐；visual_segments 供前端
        dsa_polyline renderer 逐段绘制，不再依赖 regime_id 切段。

        输出字段按 bar 对齐，长度一致（= min(len(bars_daily), lookback)）。

        Returns:
            dict 包含:
            - time: list[str] ISO 日期字符串（YYYY-MM-DD），与 source_bar_times 一致
            - visual_segments: list[dict] Pine polyline 契约 [{direction, points:[{time,value}]}]
            - dsa_vwap: list[float|None] 每 bar 的 DSA VWAP 值
            - dsa_dir: list[int] 每 bar 的方向（1/-1）
            - regime_id: list[int] 每 bar 所属 regime 编号（切换点递增）
            - anchor_time: list[str|None] 每 bar 的锚点时间（仅锚点 bar 非空）
            - pivot_type: list[str|None] 每 bar 的 pivot 类型（HH/HL/LH/LL）
            - pivot_price: list[float|None] 每 bar 的 pivot 价格
        """
        daily_df = context.bars_daily
        n_input = len(daily_df) if daily_df is not None else 0
        _empty: dict[str, list] = {
            "time": [],
            "visual_segments": [],
            "dsa_vwap": [],
            "dsa_dir": [],
            "regime_id": [],
            "anchor_time": [],
            "pivot_type": [],
            "pivot_price": [],
        }
        if n_input < self._dsa_config.prd:
            return _empty

        # [DSA 统一入口] - 通过 compute_dsa_bundle 与 execute() 共享同一份计算，
        # 应用相同 lookback=250，消除原 compute_indicators 用全量 bars 的口径不一致
        try:
            bundle = compute_dsa_bundle(daily_df, self._build_history_config())
        except Exception as exc:
            logger.warning(
                "DSA bundle 计算异常 symbol=%s: %s",
                getattr(context, "symbol", "?"),
                exc,
            )
            return _empty

        factor_per_bar = bundle["factor_per_bar"]
        if factor_per_bar.empty:
            return _empty

        # [DSA 图表数组] - 从 factor_per_bar 提取为 list，NaN -> None
        # pivot_type/anchor_time/pivot_price 经 DataFrame 中转后 None 可能被转为 NaN，需还原
        # time 通过 format_dsa_time 序列化（PR #34）：
        #   1d/1w/1mo 为 YYYY-MM-DD，15m/1h 含 THH:MM:SS，与 source_bar_times 同口径对齐。
        # visual_segments 直接透传 bundle 返回的 Pine polyline 契约
        return {
            "time": [format_dsa_time(idx) for idx in bundle["factor_time"]],
            "visual_segments": bundle["visual_segments"],
            "dsa_vwap": [None if pd.isna(v) else float(v) for v in factor_per_bar["dsa_vwap"]],
            "dsa_dir": [int(d) for d in factor_per_bar["dsa_dir"]],
            "regime_id": [int(x) for x in factor_per_bar["regime_id"]],
            "anchor_time": [None if pd.isna(t) else str(t) for t in factor_per_bar["anchor_time"]],
            "pivot_type": [None if pd.isna(t) else str(t) for t in factor_per_bar["pivot_type"]],
            "pivot_price": [None if pd.isna(p) else float(p) for p in factor_per_bar["pivot_price"]],
        }

    def _compute_metrics_sync(self, context: MarketDataContext) -> dict[str, Any]:
        """同步计算 DSA 指标（在线程池中执行）。

        此方法由 execute() 通过 asyncio.to_thread 调用。已取消单股 100ms 硬超时，
        超时控制上移到 strategy_batch_service 的 run 级总超时。
        通过 compute_dsa_bundle 统一入口获取 last_row_metrics（与 compute_indicators
        共享同一份计算结果，消除双路径不一致）。

        Args:
            context: 市场数据上下文

        Returns:
            指标字典（包含 yaml 中声明的所有指标）
        """
        daily_df = context.bars_daily
        if daily_df is None or daily_df.empty or len(daily_df) < 60:
            return {"regime_value": 0, "error": "insufficient_data"}

        try:
            bundle = compute_dsa_bundle(daily_df, self._build_history_config())
        except Exception as exc:
            logger.warning("DSA bundle 计算异常 symbol=%s: %s", context.symbol, exc)
            return {"regime_value": 0, "error": f"dsa_compute_failed: {exc}"}

        if bundle["factor_per_bar"].empty:
            return {"regime_value": 0, "error": "insufficient_data"}

        metrics = bundle["last_row_metrics"]
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

    # 7. 验证 compute_indicators 输出 Pine 标签与 regime 分段字段
    from types import SimpleNamespace

    mock_version = SimpleNamespace(
        id=uuid.uuid4(),
        manifest={
            "strategy_id": "dsa_selector",
            "kind": "selector",
            "version": "1.3.0",
            "parameters": [
                {"key": "algorithm.lookback", "default": 800},
                {"key": "dsa.prd", "default": 50},
                {"key": "dsa.base_apt", "default": 20.0},
                {"key": "dsa.use_adapt", "default": False},
                {"key": "dsa.vol_bias", "default": 10.0},
                {"key": "dsa.atr_len", "default": 50},
                {"key": "atr_rope.length", "default": 14},
                {"key": "atr_rope.multi", "default": 1.5},
                {"key": "atr_rope.regime_lookback", "default": 55},
                {"key": "atr_rope.regime_threshold", "default": 0.55},
            ],
            "resource_budget": {"target_ms_per_instrument": 5000},
        },
    )

    async def _run_compute_indicators():
        selector = DSASelector()
        await selector.initialize(mock_version)  # type: ignore[arg-type]
        ctx = MarketDataContext(
            instrument_id=uuid.uuid4(),
            symbol="600519",
            bars_daily=df,
            trade_date=date(2026, 6, 18),
        )
        return await selector.compute_indicators(ctx)

    indicators = asyncio.run(_run_compute_indicators())
    n = len(df)
    for key in ["dsa_vwap", "dsa_dir", "regime_id", "anchor_time", "pivot_type", "pivot_price"]:
        assert key in indicators, f"compute_indicators 缺少字段: {key}"
        assert len(indicators[key]) == n, f"{key} 长度应与 K 线一致"

    # 验证 pivot_type 只出现 HH/HL/LH/LL/null
    allowed_pivots = {"HH", "HL", "LH", "LL"}
    actual_pivots = {t for t in indicators["pivot_type"] if t is not None}
    assert actual_pivots.issubset(allowed_pivots), f"非法 pivot_type: {actual_pivots - allowed_pivots}"

    # 验证 regime_id 在 dir 切换时递增
    regime_changes = sum(
        1 for i in range(1, n) if indicators["regime_id"][i] != indicators["regime_id"][i - 1]
    )
    dir_changes = sum(
        1 for i in range(1, n) if indicators["dsa_dir"][i] != indicators["dsa_dir"][i - 1]
    )
    assert regime_changes == dir_changes, "regime_id 切换次数应与 dsa_dir 切换次数一致"

    # 验证所有 pivot 标签位置都有 anchor_time（anchor bar 不一定有 pivot 标签，
    # 例如第一个 bar 作为初始锚点无 prev 参考，text 为空）
    for i in range(n):
        if indicators["pivot_type"][i] is not None:
            assert indicators["anchor_time"][i] is not None, f"pivot bar {i} 缺少 anchor_time"

    print("compute_indicators Pine 标签与 regime 分段 ✓")

    print("OK")
