"""DSA 方向稳定性选股策略 - 基于 features/ 算法的 selector 运行时。

从 ref/交易/selection/selection_dsa.py 迁移核心算法，重构为 StrategyRuntime 插件。

核心选股逻辑：
    DSA VWAP dir=1 持续 > 50 bars → 多头趋势确认
    计算 close 与 VWAP 的偏离率统计、VWAP 收益指标、VWAP 交叉事件

features/ 算法调用（SSOT，严格不修改）：
    - features.dynamic_swing_anchored_vwap: DSA VWAP 计算
    - features.atr_rope_event_factor_lab_v4: ATR Rope 趋势线计算

向量化改进（相对原始 selection_dsa.py）：
    - bars 计算：cumsum + groupby 向量化（替代 for 循环）
    - trend_strength：groupby + transform 向量化
    - offset_rate_stats：groupby + expanding 向量化
    - 交叉检测：numpy 向量化（替代 for 循环）
    - remove_vwap_lookahead：保留 for 循环（需调用 features/ 函数，无法向量化）

字段对齐 dsa_selector.yaml：
    - dsa_dir_bars: dir=1 持续 bar 数
    - vwap_ret_avg: 平均每 bar 收益率 = (vwap_end/vwap_start - 1) / bars
    - vwap_ret_total: 总收益率 = vwap_end/vwap_start - 1
    - offset_mean: 偏离率均值
    - offset_std: 偏离率标准差
    - offset_variance_rate: 偏离率变异系数 = offset_std / |offset_mean|
    - offset_percentile: 偏离率百分位（正态分布 CDF）

资源预算：DSA 默认 100ms/股（通过 BudgetGuard 控制）
"""

from __future__ import annotations

import logging
import math
import os
import sys
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger("strategy.dsa_selector")

# features/ 算法模块路径（可通过环境变量覆盖，默认指向 ref/交易）
# dynamic_swing_anchored_vwap.py 顶层有 `from datasource.pytdx_client import ...`，
# 因此需要将 ref/交易 根目录加入 sys.path，使 features 与 datasource 均可导入。
_REF_TRADE_PATH = os.environ.get(
    "REF_TRADE_PATH",
    os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..", "..", "ref", "交易",
        )
    ),
)
if _REF_TRADE_PATH not in sys.path:
    sys.path.insert(0, _REF_TRADE_PATH)


from app.strategy._plotly_mock import ensure_plotly_mock  # noqa: E402

# 导入 features/ 算法（SSOT，严格不修改）
# 依赖 sys.path 中的 ref/交易 路径（上方已设置）
ensure_plotly_mock()
from features.atr_rope_event_factor_lab_v4 import ATRRopeConfig, compute_atr_rope  # noqa: E402
from features.dynamic_swing_anchored_vwap import (  # noqa: E402
    DSAConfig,
    dynamic_swing_anchored_vwap,
)

from app.models.strategy import StrategyVersion  # noqa: E402
from app.strategy.budget import BudgetExceededError, BudgetGuard  # noqa: E402
from app.strategy.runtime import MarketDataContext, StrategyResult, StrategyRuntime  # noqa: E402

# 策略常量
MIN_DIR_BARS = 50  # dir=1 持续超过 50 bars 才视为多头趋势
DEFAULT_LOOKBACK = 360  # 默认回看 bar 数（来自 yaml algorithm.lookback）
DSA_BUDGET_MS = 100  # DSA 默认预算 100ms/股


def _remove_vwap_lookahead(
    daily_df: pd.DataFrame,
    vwap_series: pd.Series,
    dir_series: pd.Series,
    cfg: DSAConfig | None = None,
) -> pd.Series:
    """消除 DSA VWAP 的前视偏差。

    原理：全量计算时，方向翻转点 T 处，vwap_out[anchor..T] 被新方向的
    回填递推覆盖。但 anchor 到 T-1 的 bar 在实时中不可能知道方向会翻转，
    这些 bar 的 VWAP 应该是旧方向的递推结果。

    解决：找到所有方向翻转点，对每个翻转点用截断到 T-1 的数据重算 DSA VWAP，
    用截断结果替换被覆盖的值（anchor 到 T-1 之间的 bar）。

    注意：此函数需调用 features/ 的 dynamic_swing_anchored_vwap，无法向量化。
    翻转点数量通常很少（< 10），性能影响可控。

    Args:
        daily_df: 日线数据 DataFrame
        vwap_series: 全量计算的 VWAP 序列
        dir_series: 方向序列（1/-1）
        cfg: DSA 配置

    Returns:
        修正前视偏差后的 VWAP 序列
    """
    dir_vals = dir_series.fillna(0).astype(int)
    flip_mask = dir_vals != dir_vals.shift(1)
    # 排除第一个 bar（无前一个方向可比较）
    flip_mask.iloc[0] = False
    flip_indices = daily_df.index[flip_mask].tolist()

    if not flip_indices:
        return vwap_series

    vwap_corrected = vwap_series.copy()

    for flip_idx in flip_indices:
        loc = daily_df.index.get_loc(flip_idx)
        if loc < 2:
            continue

        # 截断到翻转点前一个 bar（T-1），此时方向还没翻转
        truncated_df = daily_df.iloc[:loc]
        try:
            vwap_trunc, _, _, _ = dynamic_swing_anchored_vwap(truncated_df, cfg)
        except Exception as exc:
            logger.debug("截断 DSA 计算异常 flip_idx=%s: %s", flip_idx, exc)
            continue

        # 截断结果中每个 bar 的 VWAP 是该 bar 时刻的"实时值"（无前视偏差）
        # 用截断结果替换全量结果中被回填覆盖的值
        common_idx = vwap_trunc.index.intersection(vwap_corrected.index)
        # 向量化比较：只替换差异超过阈值的值
        trunc_vals = vwap_trunc.loc[common_idx].astype(float)
        corrected_vals = vwap_corrected.loc[common_idx].astype(float)
        valid_mask = trunc_vals.notna() & corrected_vals.notna()
        diff_mask = (trunc_vals[valid_mask] - corrected_vals[valid_mask]).abs() > 0.001
        replace_idx = diff_mask[diff_mask].index
        vwap_corrected.loc[replace_idx] = trunc_vals.loc[replace_idx]

    return vwap_corrected


def _compute_dsa_regime(
    df: pd.DataFrame,
    cfg: DSAConfig | None = None,
    min_bars: int = MIN_DIR_BARS,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """基于 DSA VWAP dir 计算 regime 和趋势强度（向量化）。

    - dir=1 持续 > min_bars → 1（多头）
    - dir=-1 持续 > min_bars → -1（空头）
    - 其他 → 0（震荡）

    趋势强度 = 当前 dir 持续期间内 VWAP 平均每 bar 收益率

    向量化说明：
    - bars 计算：用 cumsum + groupby.cumcount 替代 for 循环
    - trend_strength：用 groupby.transform('first') 获取区间起始值

    Args:
        df: 日线数据 DataFrame
        cfg: DSA 配置
        min_bars: 最小持续 bar 数

    Returns:
        (regime, trend_strength, bars, vwap_series, dir_series)
    """
    if cfg is None:
        cfg = DSAConfig()
    vwap_series, dir_series, _, _ = dynamic_swing_anchored_vwap(df, cfg)
    # 消除前视偏差
    vwap_series = _remove_vwap_lookahead(df, vwap_series, dir_series, cfg)
    dir_vals = dir_series.fillna(0).astype(int)

    # 向量化：计算每个 dir 持续区间的 bar 数
    # 识别方向变化点
    change_mask = dir_vals != dir_vals.shift(1)
    # 每个持续区间的 group id（从 1 开始）
    group_id = change_mask.cumsum()
    # 每个区间内的累计计数
    count = group_id.groupby(group_id).cumcount() + 1
    # bars = count * dir（dir=1 时为正，dir=-1 时为负）
    bars = (count * dir_vals).astype(int)

    # regime：bars > min_bars → 1，bars < -min_bars → -1，否则 0
    regime = pd.Series(0, index=df.index, dtype=int)
    regime[bars > min_bars] = 1
    regime[bars < -min_bars] = -1

    # 向量化：trend_strength = (vwap_end / vwap_start - 1) / count
    trend_strength = pd.Series(0.0, index=df.index)
    vwap_vals = vwap_series.astype(float)
    # 每个区间的起始 VWAP 值
    vwap_start = vwap_vals.groupby(group_id).transform("first")
    valid = (count > 1) & vwap_start.notna() & vwap_vals.notna() & (vwap_start != 0)
    trend_strength[valid] = (vwap_vals[valid] / vwap_start[valid] - 1) / count[valid]

    return regime, trend_strength, bars, vwap_series, dir_series


def _compute_offset_rate_stats(
    close: pd.Series,
    vwap: pd.Series,
    dir_series: pd.Series,
    dsa_bars: pd.Series,
) -> pd.DataFrame:
    """在 dir=1 区间内计算 close 与 VWAP 的偏离率统计（向量化 expanding window）。

    对每根 bar：
    - offset_rate = (close - VWAP) / VWAP
    - expanding window = 当前 dir=1 区间内从起始 bar 到当前 bar 的全部 offset_rate
    - 计算 mean / std / percentile（正态分布 CDF）

    向量化说明：
    - 用 groupby + expanding 替代 for 循环
    - offset_rate 只在 dir=1 时有效，其他位置设为 NaN

    Args:
        close: 收盘价序列
        vwap: VWAP 序列
        dir_series: 方向序列
        dsa_bars: dir 持续 bar 数序列

    Returns:
        DataFrame: columns=[offset_rate, offset_mean, offset_std, offset_percentile]
    """
    dir_vals = dir_series.fillna(0).astype(int)

    # offset_rate = (close - vwap) / vwap，只在 dir=1 时有效
    with np.errstate(divide="ignore", invalid="ignore"):
        offset_rate = (close.astype(float) - vwap.astype(float)) / vwap.astype(float)
    offset_rate = offset_rate.where(dir_vals == 1)

    # 每个 dir 持续区间的 group id
    change_mask = dir_vals != dir_vals.shift(1)
    group_id = change_mask.cumsum()

    # 向量化 expanding 统计：在每个 group 内做 expanding mean/std
    # 只对 dir=1 的 group 有效（其他 group 的 offset_rate 为 NaN，会被忽略）
    offset_mean = (
        offset_rate.groupby(group_id).expanding().mean().reset_index(level=0, drop=True)
    )
    offset_std = (
        offset_rate.groupby(group_id)
        .expanding()
        .std(ddof=0)
        .reset_index(level=0, drop=True)
    )

    # offset_percentile = norm.cdf(offset_rate, mean, std)
    # 使用 math.erf 实现标准正态分布 CDF，避免引入 scipy 重依赖
    # 公式：norm.cdf(x, mu, sigma) = 0.5 * (1 + erf((x - mu) / (sigma * sqrt(2))))
    offset_percentile = pd.Series(np.nan, index=close.index, dtype=float)
    valid = offset_rate.notna() & offset_mean.notna() & offset_std.notna() & (offset_std > 0)
    if valid.any():
        x = offset_rate[valid].to_numpy()
        mu = offset_mean[valid].to_numpy()
        sigma = offset_std[valid].to_numpy()
        # 向量化 erf 计算（numpy 不直接提供 erf，使用 math.erf 逐元素）
        # 对 valid 数组逐元素计算，valid 数量通常较小（< 400），性能可接受
        z_scores = (x - mu) / (sigma * math.sqrt(2.0))
        cdf_vals = np.array([0.5 * (1.0 + math.erf(z)) for z in z_scores])
        offset_percentile[valid] = cdf_vals

    return pd.DataFrame(
        {
            "offset_rate": offset_rate,
            "offset_mean": offset_mean,
            "offset_std": offset_std,
            "offset_percentile": offset_percentile,
        },
        index=close.index,
    )


def _compute_vwap_return_metrics(
    vwap: pd.Series,
    dsa_bars: pd.Series,
) -> dict[str, float | None]:
    """计算 VWAP 收益指标（仅取最后一根 bar 的结果）。

    字段对齐 dsa_selector.yaml：
    - vwap_ret_avg: 平均每 bar 收益率 = (vwap_end/vwap_start - 1) / bars
    - vwap_ret_total: 总收益率 = vwap_end/vwap_start - 1

    Args:
        vwap: VWAP 序列
        dsa_bars: dir 持续 bar 数序列

    Returns:
        dict with vwap_ret_avg, vwap_ret_total
    """
    vwap_arr = vwap.to_numpy(float)
    bars_val = int(abs(dsa_bars.iloc[-1]))
    result: dict[str, float | None] = {
        "vwap_ret_avg": None,
        "vwap_ret_total": None,
    }

    if bars_val < 2:
        return result

    start_idx = len(vwap_arr) - bars_val
    if start_idx < 0:
        start_idx = 0

    vwap_start = vwap_arr[start_idx]
    vwap_end = vwap_arr[-1]
    if np.isfinite(vwap_start) and np.isfinite(vwap_end) and vwap_start != 0:
        total_ret = float(vwap_end / vwap_start - 1)
        result["vwap_ret_total"] = round(total_ret, 6)
        result["vwap_ret_avg"] = round(total_ret / bars_val, 6)

    return result


def _detect_crossover_events_vectorized(
    close: pd.Series,
    line: pd.Series,
    start_idx: int = 1,
) -> dict[str, Any]:
    """向量化检测 close 与某条线的交叉事件。

    上穿：close[t] > line[t] 且 close[t-1] <= line[t-1]
    下穿：close[t] < line[t] 且 close[t-1] >= line[t-1]

    Args:
        close: 收盘价序列
        line: 趋势线序列（VWAP 或 ATR Rope）
        start_idx: 起始索引（默认 1，从头检测）

    Returns:
        dict with last_cross_up_date, last_cross_up_price, last_cross_down_date,
              last_cross_down_price, cross_up_count, cross_down_count
    """
    result: dict[str, Any] = {
        "last_cross_up_date": None,
        "last_cross_up_price": None,
        "last_cross_down_date": None,
        "last_cross_down_price": None,
        "cross_up_count": 0,
        "cross_down_count": 0,
    }

    if len(close) < 2 or start_idx >= len(close):
        return result

    close_arr = close.to_numpy(float)
    line_arr = line.to_numpy(float)

    # 向量化计算交叉
    close_curr = close_arr[start_idx:]
    close_prev = close_arr[start_idx - 1 : -1]
    line_curr = line_arr[start_idx:]
    line_prev = line_arr[start_idx - 1 : -1]

    # 过滤 NaN
    valid = (
        np.isfinite(close_curr)
        & np.isfinite(close_prev)
        & np.isfinite(line_curr)
        & np.isfinite(line_prev)
    )

    cross_up = valid & (close_curr > line_curr) & (close_prev <= line_prev)
    cross_down = valid & (close_curr < line_curr) & (close_prev >= line_prev)

    cross_up_count = int(cross_up.sum())
    cross_down_count = int(cross_down.sum())
    result["cross_up_count"] = cross_up_count
    result["cross_down_count"] = cross_down_count

    # 最近一次上穿/下穿
    if cross_up_count > 0:
        up_indices = np.where(cross_up)[0]
        last_up = up_indices[-1] + start_idx
        result["last_cross_up_date"] = close.index[last_up]
        result["last_cross_up_price"] = round(float(close_arr[last_up]), 4)

    if cross_down_count > 0:
        down_indices = np.where(cross_down)[0]
        last_down = down_indices[-1] + start_idx
        result["last_cross_down_date"] = close.index[last_down]
        result["last_cross_down_price"] = round(float(close_arr[last_down]), 4)

    return result


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


class DSASelector(StrategyRuntime):
    """DSA 方向稳定性选股策略运行时。

    从 ref/交易/selection/selection_dsa.py 迁移，继承 StrategyRuntime ABC。
    kind="selector"，按交易日输出每只股票的 DSA 指标。

    选股逻辑：
    1. 调用 features.dynamic_swing_anchored_vwap 计算 DSA VWAP 和 dir
    2. 消除前视偏差
    3. 判断 regime：dir=1 持续 > MIN_DIR_BARS(50) bars → 多头
    4. 多头时计算偏离率统计、VWAP 收益指标
    5. 输出 StrategyResult（matched=True 对所有有效结果）

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

        注意：algorithm.min_dir_bars 不从 manifest 读取，它是 regime 命中阈值
        （regime[bars > min_bars] = 1），不是算法计算参数，保留为代码常量 MIN_DIR_BARS=50。
        所有股票输出真实 dsa_dir_bars，由用户筛选条件动态决定。

        读取后构造 DSAConfig 和 ATRRopeConfig 实例，并保存 effective_config 快照。

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
        # self._min_dir_bars 固定为常量 MIN_DIR_BARS，不从 manifest 读取

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

        # 保存 effective_config 快照（供 BatchService 读取，保存到 strategy_runs.effective_config）
        # 不包含 algorithm.min_dir_bars（已移出 manifest 参数）
        self._effective_config = params

        logger.info(
            "DSASelector 初始化: lookback=%d, min_dir_bars=%d(常量), "
            "dsa_config(prd=%d, baseAPT=%.1f, useAdapt=%s, volBias=%.1f, atrLen=%d), "
            "rope_config(length=%d, multi=%.1f, regime_lookback=%d, regime_threshold=%.2f), "
            "budget_ms=%d",
            self._lookback, self._min_dir_bars,
            self._dsa_config.prd, self._dsa_config.baseAPT,
            self._dsa_config.useAdapt, self._dsa_config.volBias, self._dsa_config.atrLen,
            self._rope_config.length, self._rope_config.multi,
            self._rope_config.regime_lookback, self._rope_config.regime_threshold,
            target_ms,
        )

    async def execute(self, context: MarketDataContext) -> StrategyResult:
        """执行 DSA 选股计算。

        流程：
        1. 通过 BudgetGuard 在预算内执行同步计算
        2. 计算 DSA regime（多头/空头/震荡）
        3. 多头时计算所有指标
        4. 返回标准 StrategyResult

        注意：matched 对所有有效结果设为 True（非超时、非数据不足）。
        命中由用户筛选条件动态决定（如 regime_value == 1 或 dsa_dir_bars > 50），
        不作为全局共享的 DSA 计算结果保存。

        Args:
            context: 市场数据上下文（含日线行情）

        Returns:
            StrategyResult: matched=True 对所有有效结果
        """
        if self._version is None:
            raise RuntimeError("DSASelector 未初始化，请先调用 initialize()")

        # 通过 BudgetGuard 在预算内执行同步计算
        try:
            metrics = await self._budget_guard.run_with_budget(
                self._compute_metrics_sync, context
            )
        except BudgetExceededError:
            # 预算超限：返回未命中结果，metrics 标记超时
            logger.warning(
                "DSA 计算超时 instrument_id=%s symbol=%s",
                context.instrument_id, context.symbol,
            )
            return StrategyResult(
                instrument_id=context.instrument_id,
                strategy_version_id=self._version.id,
                trade_date=context.trade_date or date.today(),
                matched=False,
                metrics={"error": "budget_exceeded"},
                calculation_id=None,
            )

        # matched 对所有有效结果设为 True
        # 命中由用户筛选条件动态决定（如 regime_value == 1 或 dsa_dir_bars > 50）
        # 不作为全局共享的 DSA 计算结果保存
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

        供个股详情页面实时计算使用。只计算 DSA VWAP 线和方向，
        参考 features/dynamic_swing_anchored_vwap.py。

        Returns:
            {"dsa_vwap": [float...], "dsa_dir": [int...]} 最近 N 根 bar 的 VWAP 和方向
        """
        daily_df = context.bars_daily
        if daily_df is None or len(daily_df) < self._dsa_config.prd:
            return {"dsa_vwap": [], "dsa_dir": []}

        vwap_series, dir_series, _, _ = dynamic_swing_anchored_vwap(
            daily_df, self._dsa_config
        )
        vwap_series = _remove_vwap_lookahead(
            daily_df, vwap_series, dir_series, self._dsa_config
        )

        # 转为可 JSON 序列化的 list
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

        # 确保 index 是 DatetimeIndex
        if not isinstance(daily_df.index, pd.DatetimeIndex):
            daily_df = daily_df.copy()
            daily_df.index = pd.to_datetime(daily_df.index)

        # 1. 计算 DSA regime
        try:
            regime, trend_strength, dsa_bars, dsa_vwap, dsa_dir = _compute_dsa_regime(
                daily_df, cfg=self._dsa_config, min_bars=self._min_dir_bars
            )
        except Exception as exc:
            logger.warning("DSA regime 计算异常 symbol=%s: %s", context.symbol, exc)
            return {"regime_value": 0, "error": f"dsa_compute_failed: {exc}"}

        regime_val = int(regime.iloc[-1])
        dsa_dir_bars_val = int(dsa_bars.iloc[-1])

        # 2. 计算偏离率统计（所有 regime 都计算，但只在 dir=1 时有值）
        offset_stats = _compute_offset_rate_stats(
            daily_df["close"], dsa_vwap, dsa_dir, dsa_bars
        )
        last_offset = offset_stats.iloc[-1]

        # 3. 计算 VWAP 收益指标
        vwap_metrics = _compute_vwap_return_metrics(dsa_vwap, dsa_bars)

        # 4. 计算 ATR Rope（可选，失败不阻断）
        rope_dir1_pct: float | None = None
        rope_dir0_pct: float | None = None
        rope_dir_neg1_pct: float | None = None
        touch_rope = False
        touch_vwap = False
        try:
            rope_cfg = self._rope_config
            atr_rope_df = compute_atr_rope(daily_df, rope_cfg)
            if atr_rope_df is not None and not atr_rope_df.empty:
                # 统计 DSA 趋势区间内 ATR Rope dir 占比
                n = abs(dsa_dir_bars_val)
                if n > 0 and len(atr_rope_df) >= n:
                    segment = atr_rope_df["atr_rope_dir"].iloc[-n:]
                    total = len(segment)
                    if total > 0:
                        rope_dir1_pct = round(float((segment == 1).sum()) / total * 100, 2)
                        rope_dir0_pct = round(float((segment == 0).sum()) / total * 100, 2)
                        rope_dir_neg1_pct = round(
                            float((segment == -1).sum()) / total * 100, 2
                        )

                # 触碰判断
                last_rope_val = atr_rope_df["atr_rope_rope"].iloc[-1]
                last_low = float(daily_df["low"].iloc[-1])
                if pd.notna(last_rope_val) and pd.notna(last_low):
                    touch_rope = bool(last_low <= float(last_rope_val))
                if pd.notna(dsa_vwap.iloc[-1]) and pd.notna(last_low):
                    touch_vwap = bool(last_low <= float(dsa_vwap.iloc[-1]))
        except Exception as exc:
            logger.debug("ATR Rope 计算异常 symbol=%s: %s", context.symbol, exc)

        # 5. 计算 VWAP 交叉事件（向量化）
        last_bars = int(abs(dsa_bars.iloc[-1]))
        start_idx = max(1, len(daily_df) - last_bars) if last_bars >= 2 else 1
        crossover = _detect_crossover_events_vectorized(
            daily_df["close"], dsa_vwap, start_idx=start_idx
        )

        # 6. 计算涨跌幅（除权防御）
        change_pct = _compute_change_pct(daily_df)
        if change_pct is not None and change_pct < -15:
            logger.debug("疑似除权虚假跌幅 symbol=%s: %.2f%%，跳过", context.symbol, change_pct)
            return {"regime_value": 0, "error": "ex_dividend_detected"}

        # 7. 组装指标（对齐 dsa_selector.yaml outputs）
        offset_mean = _safe_float(last_offset["offset_mean"])
        offset_std = _safe_float(last_offset["offset_std"])

        # offset_variance_rate: 偏离率变异系数 = offset_std / |offset_mean|
        # 衡量偏离率的波动程度，值越大表示偏离率不稳定
        offset_variance_rate: float | None = None
        if offset_mean is not None and offset_std is not None and abs(offset_mean) > 1e-10:
            offset_variance_rate = round(offset_std / abs(offset_mean), 6)

        metrics: dict[str, Any] = {
            # yaml outputs 字段
            "dsa_dir_bars": dsa_dir_bars_val,
            "vwap_ret_avg": vwap_metrics["vwap_ret_avg"],
            "vwap_ret_total": vwap_metrics["vwap_ret_total"],
            "offset_mean": offset_mean,
            "offset_std": offset_std,
            "offset_variance_rate": offset_variance_rate,
            "offset_percentile": _safe_float(last_offset["offset_percentile"]),
            # 扩展字段（非 yaml outputs，用于详情展示）
            "regime_value": regime_val,
            "regime_strength": _safe_float(trend_strength.iloc[-1]),
            "offset_rate": _safe_float(last_offset["offset_rate"]),
            "change_pct": change_pct,
            "touch_rope": touch_rope,
            "touch_vwap": touch_vwap,
            "rope_dir1_pct": rope_dir1_pct,
            "rope_dir0_pct": rope_dir0_pct,
            "rope_dir_neg1_pct": rope_dir_neg1_pct,
            "cross_up_count": crossover["cross_up_count"],
            "cross_down_count": crossover["cross_down_count"],
            "last_cross_up_date": (
                crossover["last_cross_up_date"].isoformat()
                if crossover["last_cross_up_date"] is not None
                else None
            ),
            "last_cross_down_date": (
                crossover["last_cross_down_date"].isoformat()
                if crossover["last_cross_down_date"] is not None
                else None
            ),
            "last_close": _safe_float(daily_df["close"].iloc[-1]),
            "last_vwap": _safe_float(dsa_vwap.iloc[-1]),
        }

        return metrics


if __name__ == "__main__":
    # 自测入口：验证 DSASelector 基础逻辑（无副作用，不连 DB/网络）
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

    # 3. 验证向量化函数可调用
    assert callable(_compute_dsa_regime)
    assert callable(_compute_offset_rate_stats)
    assert callable(_compute_vwap_return_metrics)
    assert callable(_detect_crossover_events_vectorized)
    print("向量化函数可调用 ✓")

    # 4. 验证 _detect_crossover_events_vectorized 基础逻辑
    close = pd.Series([10.0, 11.0, 10.5, 12.0, 11.5], index=pd.date_range("2026-01-01", periods=5))
    line = pd.Series([10.5, 10.5, 10.5, 10.5, 10.5], index=close.index)
    cross = _detect_crossover_events_vectorized(close, line)
    assert cross["cross_up_count"] >= 1, f"应有上穿, 实际 {cross}"
    print(f"交叉检测: {cross} ✓")

    # 5. 验证 _compute_vwap_return_metrics
    vwap = pd.Series([10.0, 10.5, 11.0, 11.5, 12.0])
    bars = pd.Series([1, 2, 3, 4, 5])
    ret = _compute_vwap_return_metrics(vwap, bars)
    # vwap_ret_total = 12.0/10.0 - 1 = 0.2
    assert ret["vwap_ret_total"] is not None and abs(ret["vwap_ret_total"] - 0.2) < 0.01
    # vwap_ret_avg = 0.2 / 5 = 0.04
    assert ret["vwap_ret_avg"] is not None and abs(ret["vwap_ret_avg"] - 0.04) < 0.001
    print(f"VWAP 收益指标: {ret} ✓")

    print("OK")
