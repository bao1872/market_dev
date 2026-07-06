"""时序特征服务 - Temporal Features V1。

V1 范围：
- 只按需计算单只股票最新已完成 bar
- 补充变化量、持续度、派生关系（不重复 V1.8 当前状态）
- 不做全市场/全历史预计算
- 不新增数据库表/worker
- V1 只支持 as_of=latest，不实现历史回放截断

返回结构：
    {
      "daily_context": {9 字段},
      "m15_response": {9 字段},
      "derived_relation": {3 字段},
      "meta": {as_of, timeframes, degraded_reasons, warmup_notes}
    }

设计原则：
- 复用 V1.8 compute_structural_factors 获取 primary/secondary 因子
- 历史序列重算（SQZMOM/BB/volume_percentile）均为 point-in-time，无未来函数
- 单字段失败返回 null，不影响整体返回

用法：
    from app.services.temporal_feature_service import compute_temporal_features
    result = await compute_temporal_features(session, instrument_id)

模块自测：
    python -m app.services.temporal_feature_service
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.indicator_contract import DAILY_HISTORY_BARS
from app.services.market_data_aggregation_service import MarketDataAggregationService
from app.services.structural_factor_service import (
    _find_bar_index_by_time,
    percentile_rank,
)
from app.strategy.selectors.dsa_selector import compute_dsa_bundle
from app.strategy_assets.algorithms.features.bollinger_features_plotly import bollinger
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb

logger = logging.getLogger(__name__)

# 与 V1.8 structural_factor_service 对齐的常量
_BB_WIN = 20
_BB_K = 2.0
_SWING_LENGTH = 5
_PERCENTILE_LOOKBACK = 120
_MIN_SEGMENTS_FOR_PERCENTILE = 5
_TEMPORAL_PRIMARY_LOOKBACK = DAILY_HISTORY_BARS
_TEMPORAL_SECONDARY_LOOKBACK = 500

# 组级异常隔离：每组失败时返回的 null default dict
_NULL_DAILY_CONTEXT: dict[str, Any] = {
    "daily_dsa_dir": None,
    "daily_dsa_segment_duration_percentile": None,
    "daily_dsa_slope_atr_per_bar": None,
    "daily_dsa_efficiency_0_1": None,
    "daily_price_position_in_swing_0_1": None,
    "daily_distance_to_swing_high_atr": None,
    "daily_distance_to_node_above_atr": None,
    "daily_sqzmom_change_since_segment_start": None,
    "daily_volume_percentile_change_since_segment_start": None,
}
_NULL_M15_RESPONSE: dict[str, Any] = {
    "m15_price_position_in_swing_0_1": None,
    "m15_position_change_since_swing_anchor": None,
    "m15_distance_to_swing_high_atr": None,
    "m15_distance_to_swing_low_atr": None,
    "m15_sqzmom_change_since_swing_anchor": None,
    "m15_sqzmom_abs_percentile": None,
    "m15_sqz_off": None,
    "m15_bb_bandwidth_change_since_swing_anchor": None,
    "m15_volume_percentile_change_since_swing_anchor": None,
}
_NULL_DERIVED_RELATION: dict[str, Any] = {
    "m15_position_relative_to_daily": None,
    "m15_response_direction_relative_to_daily": None,
    "m15_response_intensity": None,
}


def _safe_compute(
    func: Any,
    group_name: str,
    degraded_reasons: list[str],
    null_default: dict[str, Any],
) -> dict[str, Any]:
    """组级异常隔离：调用 func，捕获异常返回 null default + degraded_reasons。

    单组失败不影响其他组或整体 API 返回。
    """
    try:
        return func()
    except Exception as exc:
        logger.error("%s failed: %s", group_name, exc, exc_info=True)
        degraded_reasons.append(f"{group_name} failed: {exc}")
        return dict(null_default)


# =============================================================================
# 辅助函数：point-in-time 历史值计算
# =============================================================================
def _compute_sqzmom_at_bar(bars: pd.DataFrame, bar_idx: int) -> float | None:
    """重新计算 SQZMOM 序列，返回 bar_idx 处的值（point-in-time，无未来函数）。

    SQZMOM 是因果指标（rolling window），val[bar_idx] 只依赖截至 bar_idx 的数据。
    """
    if bars is None or bar_idx < 0 or bar_idx >= len(bars):
        return None
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)
    sqz = compute_sqzmom_lb(opens, highs, lows, closes)
    val_list = sqz.get("val", [])
    if bar_idx < len(val_list):
        val = val_list[bar_idx]
        return float(val) if val is not None else None
    return None


def _compute_volume_percentile_at_bar(
    bars: pd.DataFrame, bar_idx: int
) -> float | None:
    """计算 bar_idx 处的 volume percentile（point-in-time，只用截至该 bar 的数据）。"""
    if bars is None or bar_idx < 0 or bar_idx >= len(bars):
        return None
    if "volume" not in bars.columns:
        return None
    volumes = bars["volume"].to_numpy(dtype=float)
    vol_at = volumes[bar_idx]
    if not np.isfinite(vol_at):
        return None
    # 只用截至 bar_idx 的数据（point-in-time）
    visible_volumes = volumes[: bar_idx + 1]
    return percentile_rank(vol_at, visible_volumes, _PERCENTILE_LOOKBACK)


def _compute_bb_bandwidth_percentile_at_bar(
    bars: pd.DataFrame, bar_idx: int
) -> float | None:
    """计算 bar_idx 处的 BB bandwidth percentile（point-in-time）。"""
    if bars is None or bar_idx < 0 or bar_idx >= len(bars):
        return None
    if "close" not in bars.columns:
        return None
    mid, upper, lower = bollinger(bars, _BB_WIN, _BB_K)
    mid_arr = mid.to_numpy(dtype=float)
    upper_arr = upper.to_numpy(dtype=float)
    lower_arr = lower.to_numpy(dtype=float)
    # bandwidth = (upper - lower) / mid
    safe_mid = np.where(mid_arr > 0, mid_arr, np.nan)
    bw = (upper_arr - lower_arr) / safe_mid
    if bar_idx >= len(bw):
        return None
    bw_at = bw[bar_idx]
    if not np.isfinite(bw_at):
        return None
    # 只用截至 bar_idx 的数据（point-in-time）
    visible_bw = bw[: bar_idx + 1]
    return percentile_rank(float(bw_at), visible_bw, _PERCENTILE_LOOKBACK)


def _collect_historical_segment_ages(bars: pd.DataFrame) -> list[int]:
    """收集 DSA 历史已完成 segments 的 age_bars（排除当前段）。

    通过 compute_dsa_bundle 获取 visual_segments，对每个历史段用
    _find_bar_index_by_time 定位起止 bar index，计算 age = end - start + 1。
    """
    if bars is None or bars.empty or len(bars) < 60:
        return []
    try:
        dsa_bundle = compute_dsa_bundle(bars, {})
        visual_segments = dsa_bundle.get("visual_segments", [])
        factor_per_bar = dsa_bundle.get("factor_per_bar")
        if factor_per_bar is None or factor_per_bar.empty or len(visual_segments) <= 1:
            return []

        ages: list[int] = []
        # 排除最后一个（当前段），只收集历史已完成段
        for seg in visual_segments[:-1]:
            points = seg.get("points", [])
            if len(points) < 2:
                continue
            start_idx = _find_bar_index_by_time(
                factor_per_bar.index, points[0].get("time")
            )
            end_idx = _find_bar_index_by_time(
                factor_per_bar.index, points[-1].get("time")
            )
            if start_idx is not None and end_idx is not None and end_idx >= start_idx:
                age = end_idx - start_idx + 1
                ages.append(int(age))
        return ages
    except Exception as exc:
        logger.warning("收集历史 segment ages 失败: %s", exc)
        return []


def _find_swing_anchor(
    bars: pd.DataFrame,
    swing_factors: dict[str, Any],
) -> tuple[int, float, float] | None:
    """定位 swing anchor bar 及当时的 swing range。

    anchor 规则：bars_since_swing_low < bars_since_swing_high → anchor=swing_low；
    否则 anchor=swing_high。

    返回 (anchor_bar_idx, confirmed_swing_high_at_anchor, confirmed_swing_low_at_anchor)。
    anchor 时 swing range 不完整返回 None。

    注意：anchor_bar_idx 是 pivot bar 位置（通过 bars_since_swing 反推）。
    在该 bar 处，anchor 本身的 swing 值已知（它是 anchor），对侧 swing 取最近的
    已确认值（通过 _tv_pivots_confirmed 查找 pivot 历史）。
    """
    if bars is None or bars.empty:
        return None
    bsh = swing_factors.get("bars_since_swing_high")
    bsl = swing_factors.get("bars_since_swing_low")
    confirmed_sh = swing_factors.get("confirmed_swing_high")
    confirmed_sl = swing_factors.get("confirmed_swing_low")
    if bsh is None or bsl is None:
        return None

    n = len(bars)
    # 选择 anchor：swing_low 更近 → anchor=low；否则 anchor=high
    anchor_is_low = bsl < bsh
    if anchor_is_low:
        anchor_bar_idx = n - 1 - bsl
        anchor_val = confirmed_sl
        opposite_val = confirmed_sh
    else:
        anchor_bar_idx = n - 1 - bsh
        anchor_val = confirmed_sh
        opposite_val = confirmed_sl

    if anchor_val is None or opposite_val is None:
        return None

    # anchor 时的 swing range = [min(anchor, opposite), max(anchor, opposite)]
    swing_high = max(float(anchor_val), float(opposite_val))
    swing_low = min(float(anchor_val), float(opposite_val))
    return (anchor_bar_idx, swing_high, swing_low)


# =============================================================================
# daily_context 计算
# =============================================================================
def _compute_daily_context(
    primary_factors: dict[str, Any],
    primary_bars: pd.DataFrame | None,
    degraded_reasons: list[str],
    warmup_notes: list[str],
) -> dict[str, Any]:
    """计算 daily_context（日线长周期结构背景，9 字段）。

    从 V1.8 primary factors 提取当前状态，并重算 point-in-time 历史值
    （SQZMOM/volume_percentile at segment start）。
    """
    result: dict[str, Any] = {
        "daily_dsa_dir": None,
        "daily_dsa_segment_duration_percentile": None,
        "daily_dsa_slope_atr_per_bar": None,
        "daily_dsa_efficiency_0_1": None,
        "daily_price_position_in_swing_0_1": None,
        "daily_distance_to_swing_high_atr": None,
        "daily_distance_to_node_above_atr": None,
        "daily_sqzmom_change_since_segment_start": None,
        "daily_volume_percentile_change_since_segment_start": None,
    }

    dsa_seg = primary_factors.get("dsa_segment") or {}
    swing_pos = primary_factors.get("swing_position") or {}
    cost_pos = primary_factors.get("cost_position") or {}
    vol_mom = primary_factors.get("volatility_momentum") or {}
    participation = primary_factors.get("participation") or {}

    # 1. daily_dsa_dir
    result["daily_dsa_dir"] = dsa_seg.get("current_dsa_segment_dir")

    # 2. daily_dsa_segment_duration_percentile
    current_age = dsa_seg.get("current_dsa_segment_age_bars")
    if current_age is not None and primary_bars is not None:
        hist_ages = _collect_historical_segment_ages(primary_bars)
        if len(hist_ages) >= _MIN_SEGMENTS_FOR_PERCENTILE:
            result["daily_dsa_segment_duration_percentile"] = percentile_rank(
                float(current_age), np.array(hist_ages, dtype=float), len(hist_ages)
            )
        else:
            warmup_notes.append(
                f"daily: insufficient segments for duration percentile "
                f"({len(hist_ages)} < {_MIN_SEGMENTS_FOR_PERCENTILE})"
            )

    # 3-4. slope + efficiency
    result["daily_dsa_slope_atr_per_bar"] = dsa_seg.get(
        "current_dsa_segment_slope_atr_per_bar"
    )
    result["daily_dsa_efficiency_0_1"] = dsa_seg.get(
        "current_dsa_segment_efficiency_0_1"
    )

    # 5-6. swing position
    result["daily_price_position_in_swing_0_1"] = swing_pos.get(
        "price_position_in_swing_0_1"
    )
    result["daily_distance_to_swing_high_atr"] = swing_pos.get(
        "distance_to_swing_high_atr"
    )

    # 7. cost position
    result["daily_distance_to_node_above_atr"] = cost_pos.get(
        "distance_to_node_above_atr"
    )

    # 8. daily_sqzmom_change_since_segment_start
    seg_start_idx = dsa_seg.get("segment_start_bar_index")
    sqzmom_now = vol_mom.get("sqzmom_val")
    if (
        seg_start_idx is not None
        and sqzmom_now is not None
        and primary_bars is not None
    ):
        sqzmom_at_start = _compute_sqzmom_at_bar(primary_bars, int(seg_start_idx))
        if sqzmom_at_start is not None:
            result["daily_sqzmom_change_since_segment_start"] = (
                float(sqzmom_now) - sqzmom_at_start
            )
        else:
            warmup_notes.append(
                "daily: sqzmom_at_segment_start is None (warmup)"
            )

    # 9. daily_volume_percentile_change_since_segment_start
    vol_pct_now = participation.get("volume_percentile_120")
    if (
        seg_start_idx is not None
        and vol_pct_now is not None
        and primary_bars is not None
    ):
        vol_pct_at_start = _compute_volume_percentile_at_bar(
            primary_bars, int(seg_start_idx)
        )
        if vol_pct_at_start is not None:
            result["daily_volume_percentile_change_since_segment_start"] = (
                float(vol_pct_now) - vol_pct_at_start
            )
        else:
            warmup_notes.append(
                "daily: volume_percentile_at_segment_start is None (warmup)"
            )

    return result


# =============================================================================
# m15_response 计算
# =============================================================================
def _compute_m15_response(
    secondary_factors: dict[str, Any],
    secondary_bars: pd.DataFrame | None,
    degraded_reasons: list[str],
    warmup_notes: list[str],
) -> dict[str, Any]:
    """计算 m15_response（15 分钟短周期响应，9 字段）。

    15m 只描述短周期 swing / 动量 / 波动 / 成交响应，
    不使用 15m DSA 位置类字段作为核心输入。
    """
    result: dict[str, Any] = {
        "m15_price_position_in_swing_0_1": None,
        "m15_position_change_since_swing_anchor": None,
        "m15_distance_to_swing_high_atr": None,
        "m15_distance_to_swing_low_atr": None,
        "m15_sqzmom_change_since_swing_anchor": None,
        "m15_sqzmom_abs_percentile": None,
        "m15_sqz_off": None,
        "m15_bb_bandwidth_change_since_swing_anchor": None,
        "m15_volume_percentile_change_since_swing_anchor": None,
    }

    swing_pos = secondary_factors.get("swing_position") or {}
    vol_mom = secondary_factors.get("volatility_momentum") or {}
    participation = secondary_factors.get("participation") or {}

    # 1. m15_price_position_in_swing_0_1
    m15_pos_now = swing_pos.get("price_position_in_swing_0_1")
    result["m15_price_position_in_swing_0_1"] = m15_pos_now

    # 2. m15_position_change_since_swing_anchor
    anchor_info = None
    if secondary_bars is not None:
        anchor_info = _find_swing_anchor(secondary_bars, swing_pos)

    if anchor_info is not None and m15_pos_now is not None and secondary_bars is not None:
        anchor_bar_idx, swing_high, swing_low = anchor_info
        swing_range = swing_high - swing_low
        if swing_range > 0 and anchor_bar_idx < len(secondary_bars):
            closes = secondary_bars["close"].to_numpy(dtype=float)
            close_anchor = closes[anchor_bar_idx]
            if np.isfinite(close_anchor):
                pos_at_anchor = (close_anchor - swing_low) / swing_range
                result["m15_position_change_since_swing_anchor"] = (
                    float(m15_pos_now) - pos_at_anchor
                )

    # 3-4. distance to swing high/low
    result["m15_distance_to_swing_high_atr"] = swing_pos.get(
        "distance_to_swing_high_atr"
    )
    result["m15_distance_to_swing_low_atr"] = swing_pos.get(
        "distance_to_swing_low_atr"
    )

    # 5. m15_sqzmom_change_since_swing_anchor
    sqzmom_now = vol_mom.get("sqzmom_val")
    if anchor_info is not None and sqzmom_now is not None and secondary_bars is not None:
        anchor_bar_idx = anchor_info[0]
        sqzmom_at_anchor = _compute_sqzmom_at_bar(secondary_bars, anchor_bar_idx)
        if sqzmom_at_anchor is not None:
            result["m15_sqzmom_change_since_swing_anchor"] = (
                float(sqzmom_now) - sqzmom_at_anchor
            )
        else:
            warmup_notes.append("m15: sqzmom_at_anchor is None (warmup)")

    # 6-7. sqzmom_abs_percentile + sqz_off
    result["m15_sqzmom_abs_percentile"] = vol_mom.get("sqzmom_abs_percentile")
    result["m15_sqz_off"] = vol_mom.get("sqz_off")

    # 8. m15_bb_bandwidth_change_since_swing_anchor
    bb_bw_pct_now = vol_mom.get("bb_bandwidth_percentile")
    if anchor_info is not None and bb_bw_pct_now is not None and secondary_bars is not None:
        anchor_bar_idx = anchor_info[0]
        bw_pct_at_anchor = _compute_bb_bandwidth_percentile_at_bar(
            secondary_bars, anchor_bar_idx
        )
        if bw_pct_at_anchor is not None:
            result["m15_bb_bandwidth_change_since_swing_anchor"] = (
                float(bb_bw_pct_now) - bw_pct_at_anchor
            )
        else:
            warmup_notes.append("m15: bb_bandwidth_percentile_at_anchor is None (warmup)")

    # 9. m15_volume_percentile_change_since_swing_anchor
    vol_pct_now = participation.get("volume_percentile_120")
    if anchor_info is not None and vol_pct_now is not None and secondary_bars is not None:
        anchor_bar_idx = anchor_info[0]
        vol_pct_at_anchor = _compute_volume_percentile_at_bar(
            secondary_bars, anchor_bar_idx
        )
        if vol_pct_at_anchor is not None:
            result["m15_volume_percentile_change_since_swing_anchor"] = (
                float(vol_pct_now) - vol_pct_at_anchor
            )
        else:
            warmup_notes.append("m15: volume_percentile_at_anchor is None (warmup)")

    return result


# =============================================================================
# derived_relation 计算
# =============================================================================
def _compute_derived_relation(
    daily_context: dict[str, Any],
    m15_response: dict[str, Any],
    degraded_reasons: list[str],
) -> dict[str, Any]:
    """计算 derived_relation（只由 daily_context + m15_response 派生，3 字段）。

    不引入新信息，不做强弱标签。
    """
    result: dict[str, Any] = {
        "m15_position_relative_to_daily": None,
        "m15_response_direction_relative_to_daily": None,
        "m15_response_intensity": None,
    }

    # 1. m15_position_relative_to_daily
    daily_pos = daily_context.get("daily_price_position_in_swing_0_1")
    m15_pos = m15_response.get("m15_price_position_in_swing_0_1")
    if daily_pos is not None and m15_pos is not None:
        result["m15_position_relative_to_daily"] = float(m15_pos) - float(daily_pos)

    # 2. m15_response_direction_relative_to_daily
    daily_dir = daily_context.get("daily_dsa_dir")
    m15_pos_change = m15_response.get("m15_position_change_since_swing_anchor")
    if daily_dir is not None and m15_pos_change is not None:
        if m15_pos_change == 0:
            result["m15_response_direction_relative_to_daily"] = None
        elif daily_dir == 1 and m15_pos_change > 0:
            result["m15_response_direction_relative_to_daily"] = "aligned"
        elif daily_dir == 1 and m15_pos_change < 0:
            result["m15_response_direction_relative_to_daily"] = "counter"
        elif daily_dir == -1 and m15_pos_change < 0:
            result["m15_response_direction_relative_to_daily"] = "aligned"
        elif daily_dir == -1 and m15_pos_change > 0:
            result["m15_response_direction_relative_to_daily"] = "counter"

    # 3. m15_response_intensity = mean(abs(non_null fields))
    intensity_fields = [
        m15_response.get("m15_position_change_since_swing_anchor"),
        m15_response.get("m15_sqzmom_change_since_swing_anchor"),
        m15_response.get("m15_bb_bandwidth_change_since_swing_anchor"),
        m15_response.get("m15_volume_percentile_change_since_swing_anchor"),
    ]
    abs_vals = [abs(float(v)) for v in intensity_fields if v is not None]
    if abs_vals:
        result["m15_response_intensity"] = float(np.mean(abs_vals))

    return result


# =============================================================================
# 主入口：异步计算所有时序特征
# =============================================================================
async def compute_temporal_features(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    as_of: str = "latest",
) -> dict[str, Any]:
    """计算双周期时序特征。

    Args:
        session: 数据库 session
        instrument_id: 标的 ID
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权（默认 qfq）
        as_of: 截止时间（V1 只支持 latest）

    Returns:
        dict with daily_context / m15_response / derived_relation / meta
    """
    degraded_reasons: list[str] = []
    warmup_notes: list[str] = []

    # 延迟导入避免循环依赖
    from app.services.structural_factor_service import (
        _compute_all_factors_for_bars,
        _fetch_bars,
    )

    service = MarketDataAggregationService()
    primary_bars = await _fetch_bars(
        service, session, instrument_id, primary_timeframe, adj,
        _TEMPORAL_PRIMARY_LOOKBACK, degraded_reasons,
    )
    secondary_bars = await _fetch_bars(
        service, session, instrument_id, secondary_timeframe, adj,
        _TEMPORAL_SECONDARY_LOOKBACK, degraded_reasons,
    )

    # 计算 V1.8 因子
    primary_factors = _compute_all_factors_for_bars(
        primary_bars, primary_timeframe, degraded_reasons, warmup_notes
    )
    secondary_factors = _compute_all_factors_for_bars(
        secondary_bars, secondary_timeframe, degraded_reasons, warmup_notes
    )

    # 计算 temporal 特征：每个组独立 try/except，单组失败返回 null dict + degraded_reasons
    daily_context = _safe_compute(
        lambda: _compute_daily_context(
            primary_factors, primary_bars, degraded_reasons, warmup_notes
        ),
        "daily_context",
        degraded_reasons,
        _NULL_DAILY_CONTEXT,
    )
    m15_response = _safe_compute(
        lambda: _compute_m15_response(
            secondary_factors, secondary_bars, degraded_reasons, warmup_notes
        ),
        "m15_response",
        degraded_reasons,
        _NULL_M15_RESPONSE,
    )
    derived_relation = _safe_compute(
        lambda: _compute_derived_relation(
            daily_context, m15_response, degraded_reasons
        ),
        "derived_relation",
        degraded_reasons,
        _NULL_DERIVED_RELATION,
    )

    # as_of 时间
    as_of_str = "latest"
    if primary_bars is not None and not primary_bars.empty:
        as_of_str = str(primary_bars.index[-1].strftime("%Y-%m-%d"))

    return {
        "daily_context": daily_context,
        "m15_response": m15_response,
        "derived_relation": derived_relation,
        "meta": {
            "as_of": as_of_str,
            "primary_timeframe": primary_timeframe,
            "secondary_timeframe": secondary_timeframe,
            "degraded_reasons": degraded_reasons,
            "warmup_notes": warmup_notes,
        },
    }


if __name__ == "__main__":
    # 模块自测：验证函数可调用且不崩溃
    print("temporal_feature_service 自测...")

    # 构造测试数据
    rng = np.random.default_rng(42)
    n = 250
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
    bars = pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": volumes, "amount": amounts,
    }, index=idx)

    from app.services.structural_factor_service import _compute_all_factors_for_bars

    degraded: list[str] = []
    warmup: list[str] = []
    factors = _compute_all_factors_for_bars(bars, "1d", degraded, warmup)

    daily = _compute_daily_context(factors, bars, degraded, warmup)
    print(f"daily_context keys: {sorted(daily.keys())}")
    print(f"  daily_dsa_dir: {daily['daily_dsa_dir']}")
    print(f"  daily_dsa_segment_duration_percentile: {daily['daily_dsa_segment_duration_percentile']}")
    print(f"  daily_sqzmom_change_since_segment_start: {daily['daily_sqzmom_change_since_segment_start']}")
    print(f"  daily_volume_percentile_change_since_segment_start: {daily['daily_volume_percentile_change_since_segment_start']}")
    print(f"  degraded: {degraded}")
    print(f"  warmup: {warmup}")
    print("OK")
