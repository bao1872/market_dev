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

# 固定参数
_PRIMARY_LOOKBACK = 250
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
# 因子组 1：成交参与
# =============================================================================
def _compute_participation_factors(bars: pd.DataFrame) -> dict[str, Any]:
    """计算成交参与因子。

    Returns:
        dict with:
        - volume_ratio_20: last_volume / SMA(volume, 20)
        - volume_percentile_120: last_volume 在 120 日内的百分位
    """
    result: dict[str, Any] = {
        "volume_ratio_20": None,
        "volume_percentile_120": None,
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
    return result


# =============================================================================
# 因子组 2：动量/波动 (BB + SQZMOM)
# =============================================================================
def _compute_volatility_momentum_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算动量/波动因子 (Bollinger Bands + SQZMOM_LB)。

    Returns:
        dict with:
        - bb_percent_b: (close - lower) / (upper - lower)
        - bb_bandwidth_pct: (upper - lower) / mid
        - bb_bandwidth_percentile: bandwidth 在 120 日内的百分位
        - sqzmom_val: SQZMOM linreg 值
        - sqzmom_delta_1: val[-1] - val[-2]
        - sqzmom_percentile: val 在 120 日内的百分位
    """
    result: dict[str, Any] = {
        "bb_percent_b": None,
        "bb_bandwidth_pct": None,
        "bb_bandwidth_percentile": None,
        "sqzmom_val": None,
        "sqzmom_delta_1": None,
        "sqzmom_percentile": None,
    }
    if bars is None or len(bars) < _BB_WIN + 10:
        return result

    closes = bars["close"].to_numpy(dtype=float)
    # Bollinger Bands
    mid, upper, lower = bollinger(bars, _BB_WIN, _BB_K)
    mid_arr = mid.to_numpy(dtype=float)
    upper_arr = upper.to_numpy(dtype=float)
    lower_arr = lower.to_numpy(dtype=float)
    last_close = closes[-1]
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

    # SQZMOM_LB
    opens = bars["open"].to_numpy(dtype=float) if "open" in bars.columns else closes
    highs = bars["high"].to_numpy(dtype=float) if "high" in bars.columns else closes
    lows = bars["low"].to_numpy(dtype=float) if "low" in bars.columns else closes
    sqz = compute_sqzmom_lb(opens, highs, lows, closes)
    val_list = sqz.get("val", [])
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
    return result


# =============================================================================
# 因子组 3：Swing 结构位置
# =============================================================================
def _compute_swing_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算 Swing 结构位置因子（已确认 pivot，无未来函数）。

    Returns:
        dict with:
        - confirmed_swing_high: 最后一个已确认 swing high 价格
        - confirmed_swing_low: 最后一个已确认 swing low 价格
        - bars_since_swing_high: 距离最后一个 swing high 的 bar 数
        - bars_since_swing_low: 距离最后一个 swing low 的 bar 数
        - swing_high_to_close_pct: (close - swing_high) / swing_high
        - swing_low_to_close_pct: (close - swing_low) / swing_low
    """
    result: dict[str, Any] = {
        "confirmed_swing_high": None,
        "confirmed_swing_low": None,
        "bars_since_swing_high": None,
        "bars_since_swing_low": None,
        "swing_high_to_close_pct": None,
        "swing_low_to_close_pct": None,
    }
    if bars is None or len(bars) < 2 * _SWING_LENGTH + 1:
        return result

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    ph, pl, ph_anchor, pl_anchor = _tv_pivots_confirmed(highs, lows, _SWING_LENGTH)

    # 找最后一个非 NaN 的 swing high
    ph_valid = np.where(np.isfinite(ph))[0]
    if len(ph_valid) > 0:
        last_ph_idx = ph_valid[-1]
        last_ph_anchor = int(ph_anchor[last_ph_idx]) if np.isfinite(
            ph_anchor[last_ph_idx]
        ) else last_ph_idx
        result["confirmed_swing_high"] = float(ph[last_ph_idx])
        result["bars_since_swing_high"] = int(len(highs) - 1 - last_ph_anchor)
        last_close = closes[-1]
        if np.isfinite(last_close) and ph[last_ph_idx] > 0:
            result["swing_high_to_close_pct"] = float(
                (last_close - ph[last_ph_idx]) / ph[last_ph_idx]
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
        last_close = closes[-1]
        if np.isfinite(last_close) and pl[last_pl_idx] > 0:
            result["swing_low_to_close_pct"] = float(
                (last_close - pl[last_pl_idx]) / pl[last_pl_idx]
            )
    return result


# =============================================================================
# 因子组 4：DSA 段质量
# =============================================================================
def _compute_dsa_segment_factors(
    bars: pd.DataFrame, dsa_bundle: dict[str, Any]
) -> dict[str, Any]:
    """计算 DSA 段质量因子。

    从 visual_segments[-1] 推导当前段，visual_segments[-2] 推导前一段。
    segment_id 用 factor_per_bar["regime_id"] 最后一个值。

    Returns:
        dict with:
        - segment_id: 当前段 ID
        - segment_dir: 当前段方向 (1/-1)
        - segment_start_price: 当前段起始价格
        - segment_start_bar_index: 当前段起始 bar index
        - age_bars: 当前段已持续的 bar 数
        - segment_extents_pct: 当前段价格幅度 / ATR
    """
    result: dict[str, Any] = {
        "segment_id": None,
        "segment_dir": None,
        "segment_start_price": None,
        "segment_start_bar_index": None,
        "age_bars": None,
        "segment_extents_pct": None,
    }
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    visual_segments = dsa_bundle.get("visual_segments", [])
    if factor_per_bar is None or factor_per_bar.empty or len(visual_segments) == 0:
        return result

    # 当前段 = visual_segments[-1]
    current_seg = visual_segments[-1]
    points = current_seg.get("points", [])
    if not points:
        return result

    # segment_id: regime_id 最后一个值
    if "regime_id" in factor_per_bar.columns:
        result["segment_id"] = int(factor_per_bar["regime_id"].iloc[-1])

    # segment_dir
    result["segment_dir"] = int(current_seg.get("direction", 0))

    # segment_start_price: 第一个 point 的 value
    result["segment_start_price"] = float(points[0]["value"])

    # segment_start_bar_index: 通过 points[0]["time"] 匹配 factor_per_bar.index
    start_time = points[0].get("time")
    if start_time is not None:
        try:
            start_ts = pd.Timestamp(start_time)
            idx_array = factor_per_bar.index
            # 找到匹配的 bar index
            match_mask = idx_array == start_ts
            if match_mask.any():
                start_bar_idx = int(np.where(match_mask)[0][0])
                result["segment_start_bar_index"] = start_bar_idx
                result["age_bars"] = len(factor_per_bar) - 1 - start_bar_idx
        except (ValueError, TypeError):
            pass

    # segment_extents_pct: (last_close - start_price) / |start_price|
    last_close = float(factor_per_bar["dsa_vwap"].iloc[-1]) if "dsa_vwap" in factor_per_bar.columns else None
    if last_close is not None and result["segment_start_price"] is not None:
        start_price = result["segment_start_price"]
        if abs(start_price) > 0:
            result["segment_extents_pct"] = float(
                (last_close - start_price) / abs(start_price)
            )
    return result


# =============================================================================
# 因子组 5：成本/节点
# =============================================================================
def _compute_cost_position_factors(
    bars: pd.DataFrame, atr: np.ndarray | None = None
) -> dict[str, Any]:
    """计算成本/节点因子 (Volume Profile / POC / Node)。

    Returns:
        dict with:
        - poc_price: POC 价格
        - nearest_upper_node: 上方最近节点 {price_mid, price_low, price_high}
        - nearest_lower_node: 下方最近节点
        - position_0_1: close 在 [lowest, highest] 中的位置 [0,1]
        - close_to_poc_pct: (close - poc) / poc
    """
    result: dict[str, Any] = {
        "poc_price": None,
        "nearest_upper_node": None,
        "nearest_lower_node": None,
        "position_0_1": None,
        "close_to_poc_pct": None,
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

    # POC
    try:
        poc = vp_result.poc_price
        if np.isfinite(poc):
            result["poc_price"] = float(poc)
            if poc > 0:
                result["close_to_poc_pct"] = float((last_close - poc) / poc)
    except Exception as exc:
        logger.warning("POC 提取失败: %s", exc)

    # nearest nodes
    try:
        nodes = vp_result.nearest_nodes(last_close)
        upper = nodes.get("upper_node")
        lower = nodes.get("lower_node")
        if upper is not None:
            result["nearest_upper_node"] = {
                "price_mid": float(upper.get("price_mid", 0)),
                "price_low": float(upper.get("price_low", 0)),
                "price_high": float(upper.get("price_high", 0)),
            }
        if lower is not None:
            result["nearest_lower_node"] = {
                "price_mid": float(lower.get("price_mid", 0)),
                "price_low": float(lower.get("price_low", 0)),
                "price_high": float(lower.get("price_high", 0)),
            }
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
        factors["dsa_segment"] = _compute_dsa_segment_factors(bars, dsa_bundle)
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

    # 5. 成交参与
    try:
        factors["participation"] = _compute_participation_factors(bars)
    except Exception as exc:
        degraded_reasons.append(f"{timeframe}: participation failed: {exc}")
        logger.warning("%s 成交参与计算失败: %s", timeframe, exc)

    return factors


def _compute_relation(
    primary: dict[str, Any], secondary: dict[str, Any]
) -> dict[str, Any]:
    """计算 primary vs secondary 对比关系。"""
    relation: dict[str, Any] = {
        "trend_alignment": None,
        "momentum_alignment": None,
        "notes": [],
    }
    # 提取关键值
    p_dsa = primary.get("dsa_segment") or {}
    s_dsa = secondary.get("dsa_segment") or {}
    p_dir = p_dsa.get("segment_dir")
    s_dir = s_dsa.get("segment_dir")
    if p_dir is not None and s_dir is not None:
        relation["trend_alignment"] = "aligned" if p_dir == s_dir else "divergent"

    p_sqz = primary.get("volatility_momentum") or {}
    s_sqz = secondary.get("volatility_momentum") or {}
    p_val = p_sqz.get("sqzmom_val")
    s_val = s_sqz.get("sqzmom_val")
    if p_val is not None and s_val is not None:
        if p_val > 0 and s_val > 0:
            relation["momentum_alignment"] = "both_bullish"
        elif p_val < 0 and s_val < 0:
            relation["momentum_alignment"] = "both_bearish"
        else:
            relation["momentum_alignment"] = "mixed"

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
    # 模块自测：用合成数据验证
    rng = np.random.default_rng(42)
    n = 250
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
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle
    bundle = compute_dsa_bundle(bars, {})
    ds = _compute_dsa_segment_factors(bars, bundle)
    print(ds)
    print("自测完成")
