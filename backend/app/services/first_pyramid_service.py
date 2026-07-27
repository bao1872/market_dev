"""第一金字塔统一编排服务（Phase 5B-1）。

PRD20 QM-01~QM-43、QM-60~QM-62 的统一编排入口。本服务**不实现算法**，
只编排现有权威实现：
    - 趋势：app.strategy.selectors.dsa_selector.compute_dsa_bundle（SSOT）
    - 结构：app.strategy_assets.algorithms.features.smc_pine_core.compute_smc_pine
    - 动量：bollinger_features_plotly.compute_features + sqzmom_lb.compute_sqzmom_lb
    - 筹码共识：app.services.node_cluster_engine.compute_node_cluster_profile（可选）

输出统一 `FirstPyramidSnapshot`，单股详情/批量/行情列表/盘后 compute 必须复用。
禁止复制四套算法；禁止前端重复判断算法；禁止页面动态组合维度顺序。

用法：
    from app.services.first_pyramid_service import compute_first_pyramid_snapshot

    snapshot = compute_first_pyramid_snapshot(
        bars=daily_bars,
        symbol="000001.SZ",
        trade_date="2026-07-25",
    )
    payload = snapshot.to_dict()

约束：
- 本地开发不启动 Scheduler/Worker；本服务可被测试直接调用（纯计算）
- 必选维度缺失（trend/structure/momentum）必须抛 `ValueError`
- chip_consensus 允许 None（无有效峰或数据不足）
- 同 OHLCV + 参数 + 算法版本 → 同 inputHash/parameterHash/snapshot

模块自测：
    python -m app.services.first_pyramid_service
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import numpy as np
import pandas as pd

from app.constants.indicator_contract import DSA_LOOKBACK
from app.schemas.first_pyramid import (
    FIRST_PYRAMID_ALGORITHM_VERSION,
    ORDERED_DIMENSIONS,
    DimensionResult,
    FirstPyramidSnapshot,
    PyramidEvent,
)
from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    compute_node_cluster_profile,
    detect_crossover_signals,
)
from app.strategy.selectors.dsa_selector import MIN_DIR_BARS, compute_dsa_bundle
from app.strategy_assets.algorithms.features.bollinger_features_plotly import (
    BBcfg,
)
from app.strategy_assets.algorithms.features.bollinger_features_plotly import (
    compute_features as compute_bollinger_features,
)
from app.strategy_assets.algorithms.features.smc_pine_core import (
    DEFAULT_PARAMS as SMC_DEFAULT_PARAMS,
)
from app.strategy_assets.algorithms.features.smc_pine_core import (
    compute_smc_pine,
)
from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb

logger = logging.getLogger(__name__)

# 第一金字塔固定参数（进入 parameterHash，禁止页面动态组合）
_FIRST_PYRAMID_PARAMS: dict[str, Any] = {
    "dsa_lookback": DSA_LOOKBACK,
    "dsa_min_dir_bars": MIN_DIR_BARS,
    "smc_default_params": SMC_DEFAULT_PARAMS,
    "bollinger_config": {"bb_win": 20, "bb_k": 2.0},
    "sqzmom_config": {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
    "node_cluster_required_daily_bars": 250,
}

# 必选维度的最小 bar 数（前三维）
_MIN_BARS_FOR_REQUIRED_DIMS = 60


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def _safe_iso_date(v: Any) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    try:
        ts = pd.to_datetime(v)
        return ts.date().isoformat() if pd.notna(ts) else None
    except (TypeError, ValueError):
        return None


def _compute_input_hash(bars: pd.DataFrame) -> str:
    """计算 OHLCV 输入 hash（同输入 → 同 hash，跨入口一致性基础）。"""
    if bars is None or bars.empty:
        return "sha256:empty"
    cols = [c for c in ("open", "high", "low", "close", "volume", "amount") if c in bars.columns]
    if not cols:
        return "sha256:no_ohlcv"
    try:
        idx_str = pd.Series(bars.index.astype(str)).str.cat(sep=",")
        vals_str = bars[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
        content = f"{idx_str}#{vals_str}"
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:
        logger.warning("input_hash 计算失败: %s", exc)
        return "sha256:error"


def _compute_parameter_hash() -> str:
    """计算参数 hash（含算法版本与固定参数）。"""
    try:
        content = json.dumps(
            {
                "algorithm_version": FIRST_PYRAMID_ALGORITHM_VERSION,
                "ordered_dimensions": list(ORDERED_DIMENSIONS),
                "params": _FIRST_PYRAMID_PARAMS,
            },
            sort_keys=True,
            default=str,
        )
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:
        logger.warning("parameter_hash 计算失败: %s", exc)
        return "sha256:error"


# =============================================================================
# 趋势维度（DSA SSOT）
# =============================================================================


def _build_trend_dimension(dsa_bundle: dict[str, Any], n_bars: int) -> DimensionResult:
    """从 DSA bundle 构建趋势维度结果。

    DSA bundle 由 compute_dsa_bundle 产生，含 last_row_metrics（标量指标）。
    """
    metrics = dsa_bundle.get("last_row_metrics", {}) or {}
    if not metrics:
        raise ValueError(
            "趋势维度必选，但 DSA bundle last_row_metrics 为空；"
            "调用方必须传入足够数据（>=60 根日线）"
        )

    regime_value = int(metrics.get("regime_value", 0) or 0)
    dsa_dir_bars = int(metrics.get("dsa_dir_bars", 0) or 0)
    dsa_vwap_dev_pct = _safe_float(metrics.get("dsa_vwap_dev_pct"))

    # 段内成交量（Phase 5B-1 迁回 DSA SSOT）
    current_seg_vol_mean = _safe_float(metrics.get("current_segment_volume_mean"))
    current_seg_amt_mean = _safe_float(metrics.get("current_segment_amount_mean"))
    prev_seg_vol_sum = _safe_float(metrics.get("prev_segment_volume_sum"))
    prev_seg_amt_sum = _safe_float(metrics.get("prev_segment_amount_sum"))
    current_vs_prev_vol = _safe_float(metrics.get("current_vs_prev_volume_ratio"))
    current_vs_prev_amt = _safe_float(metrics.get("current_vs_prev_amount_ratio"))

    continuous_factors: dict[str, Any] = {
        "regime_value": regime_value,
        "dsa_dir_bars": dsa_dir_bars,
        "dsa_vwap_dev_pct": dsa_vwap_dev_pct,
        "current_segment_volume_mean": current_seg_vol_mean,
        "current_segment_amount_mean": current_seg_amt_mean,
        "prev_segment_volume_sum": prev_seg_vol_sum,
        "prev_segment_amount_sum": prev_seg_amt_sum,
        "current_vs_prev_volume_ratio": current_vs_prev_vol,
        "current_vs_prev_amount_ratio": current_vs_prev_amt,
        "trend_strength": _safe_float(metrics.get("trend_strength")),
        "vwap_ret_total": _safe_float(metrics.get("vwap_ret_total")),
    }

    # 状态文本（由结构化结果生成，禁止前端重复判断）
    if regime_value > 0:
        regime_text = f"DSA 趋势上行，连续 {dsa_dir_bars} 根"
    elif regime_value < 0:
        regime_text = f"DSA 趋势下行，连续 {abs(dsa_dir_bars)} 根"
    else:
        regime_text = f"DSA 方向未确认，dir_bars={dsa_dir_bars}"

    vol_text = ""
    if current_seg_vol_mean is not None and prev_seg_vol_sum is not None and prev_seg_vol_sum > 0:
        if current_vs_prev_vol is not None:
            if current_vs_prev_vol > 1.2:
                vol_text = "；当前段放量"
            elif current_vs_prev_vol < 0.8:
                vol_text = "；当前段缩量"
            else:
                vol_text = "；当前段量能持平"

    status_text = regime_text + vol_text

    evidence = {
        "anchor_time": _safe_iso_date(metrics.get("anchor_time")),
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": DSA_LOOKBACK,
        "n_bars_input": n_bars,
    }

    return DimensionResult(
        name="trend",
        available=True,
        continuousFactors=continuous_factors,
        events=[],  # DSA 是连续因子，无离散事件
        statusText=status_text,
        evidence=evidence,
    )


# =============================================================================
# 结构维度（SMC Pine core）
# =============================================================================


def _build_structure_dimension(
    smc_result: dict[str, Any], n_bars: int, last_bar_index: int
) -> DimensionResult:
    """从 SMC Pine core 结果构建结构维度。

    输出 BOS、CHoCH、进入 OB、连续高低点事件；禁止 FVG。
    """
    events_raw = smc_result.get("events", []) or []
    order_blocks = smc_result.get("order_blocks", []) or []
    equal_highs_lows = smc_result.get("equal_highs_lows", []) or []
    swing_bias = int(smc_result.get("swing_bias", 0) or 0)

    # 离散事件：BOS / CHoCH
    pyramid_events: list[PyramidEvent] = []
    for evt in events_raw:
        evt_type = evt.get("type", "")  # "BOS" / "CHoCH"
        direction_raw = evt.get("direction")
        direction = None
        if direction_raw == 1 or direction_raw == "bullish":
            direction = "up"
        elif direction_raw == -1 or direction_raw == "bearish":
            direction = "down"
        occurred_at = _safe_iso_date(evt.get("confirmed_time") or evt.get("time"))
        bar_index = evt.get("confirmed_index") or evt.get("barIndex")
        price = _safe_float(evt.get("broken_level") or evt.get("price"))
        # 新鲜度 = 最后 bar 索引 - 事件确认索引
        fresh = max(0, int(last_bar_index - int(bar_index))) if bar_index is not None else 0
        pyramid_events.append(
            PyramidEvent(
                type=str(evt_type),
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
                extra={"anchor_index": evt.get("anchor_index")},
            )
        )

    # 进入 OB 事件：价格由区外进入区域（非区域存在）
    # SMC Pine core 已通过 mitigation 字段标识；只保留 is_active 且最近被进入的 OB
    for ob in order_blocks:
        if not ob.get("is_active", False):
            continue
        ob_type = "OB_ENTRY"
        ob_direction_raw = ob.get("direction", ob.get("type"))
        direction = None
        if ob_direction_raw in (1, "bullish", "demand"):
            direction = "up"
        elif ob_direction_raw in (-1, "bearish", "supply"):
            direction = "down"
        occurred_at = _safe_iso_date(ob.get("mitigation_time") or ob.get("confirmed_time"))
        bar_index = ob.get("mitigation_index") or ob.get("confirmed_index")
        price = _safe_float(ob.get("mitigation_price") or ob.get("high") or ob.get("low"))
        fresh = max(0, int(last_bar_index - int(bar_index))) if bar_index is not None else 0
        pyramid_events.append(
            PyramidEvent(
                type=ob_type,
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
                extra={
                    "ob_high": _safe_float(ob.get("high")),
                    "ob_low": _safe_float(ob.get("low")),
                },
            )
        )

    # 连续高点/低点（EQH/EQL）
    for eq in equal_highs_lows:
        eq_type = eq.get("type", "EQH_EQL")
        direction = None
        if eq_type in ("EQH", "equal_high"):
            direction = "up"
        elif eq_type in ("EQL", "equal_low"):
            direction = "down"
        occurred_at = _safe_iso_date(eq.get("confirmed_time"))
        bar_index = eq.get("confirmed_index")
        price = _safe_float(eq.get("second_pivot_price") or eq.get("price"))
        fresh = max(0, int(last_bar_index - int(bar_index))) if bar_index is not None else 0
        pyramid_events.append(
            PyramidEvent(
                type=str(eq_type),
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
            )
        )

    # 按时间升序
    pyramid_events.sort(key=lambda e: (e.barIndex if e.barIndex is not None else 0))

    continuous_factors = {
        "swing_bias": swing_bias,
        "active_ob_count": sum(1 for ob in order_blocks if ob.get("is_active", False)),
        "trailing_top": _safe_float(smc_result.get("trailing", {}).get("top")),
        "trailing_bottom": _safe_float(smc_result.get("trailing", {}).get("bottom")),
    }

    # 状态文本
    bias_text = {1: "Swing 偏多", -1: "Swing 偏空", 0: "Swing 未定"}.get(swing_bias, "Swing 未定")
    last_event_text = ""
    if pyramid_events:
        last_evt = pyramid_events[-1]
        last_event_text = f"；最近 {last_evt.type}（{last_evt.direction or '-'}, 新鲜度 {last_evt.freshnessBars}）"

    status_text = bias_text + last_event_text

    evidence = {
        "n_pivots": len(smc_result.get("pivots", []) or []),
        "n_order_blocks": len(order_blocks),
        "n_equal_highs_lows": len(equal_highs_lows),
        "smc_params": smc_result.get("params", {}),
        "n_bars_input": n_bars,
    }

    return DimensionResult(
        name="structure",
        available=True,
        continuousFactors=continuous_factors,
        events=pyramid_events,
        statusText=status_text,
        evidence=evidence,
    )


# =============================================================================
# 动量维度（Bollinger + SQZMOM）
# =============================================================================


def _build_momentum_dimension(
    bb_df: pd.DataFrame, sqzmom_result: dict[str, Any], n_bars: int, last_bar_index: int
) -> DimensionResult:
    """从 Bollinger + SQZMOM 结果构建动量维度。

    输出 squeeze 状态、带宽、扩张/扩散事件、相对前期变化、匹配成交量和事件新鲜度。
    """
    if bb_df is None or bb_df.empty:
        raise ValueError("动量维度必选，但 Bollinger 结果为空")

    last_row = bb_df.iloc[-1]
    bb_width = _safe_float(last_row.get("bb_width"))
    bb_position = _safe_float(last_row.get("bb_pos") or last_row.get("bb_position"))
    bb_upper = _safe_float(last_row.get("bb_upper"))
    bb_lower = _safe_float(last_row.get("bb_lower"))
    bb_middle = _safe_float(last_row.get("bb_mid") or last_row.get("bb_basis"))

    # SQZMOM 状态
    sqz_on_list = sqzmom_result.get("sqzOn", []) or []
    sqz_off_list = sqzmom_result.get("sqzOff", []) or []
    no_sqz_list = sqzmom_result.get("noSqz", []) or []
    val_list = sqzmom_result.get("val", []) or []

    last_sqz_on = bool(sqz_on_list[-1]) if sqz_on_list else False
    last_sqz_off = bool(sqz_off_list[-1]) if sqz_off_list else False
    last_no_sqz = bool(no_sqz_list[-1]) if no_sqz_list else False
    last_val = _safe_float(val_list[-1]) if val_list else None
    prev_val = _safe_float(val_list[-2]) if len(val_list) >= 2 else None

    # 扩张（expansion）：BB 带宽由收窄转为放大
    bb_width_series = bb_df.get("bb_width") if "bb_width" in bb_df.columns else None
    expansion_event_idx = None
    expansion_price = None
    if bb_width_series is not None and len(bb_width_series) >= 2:
        # 检测最近一次 squeeze off（BB 突破 KC）
        for i in range(len(sqz_off_list) - 1, -1, -1):
            if sqz_off_list[i] and i > 0 and sqz_on_list[i - 1]:
                expansion_event_idx = i
                if i < len(bb_df):
                    expansion_price = _safe_float(bb_df.iloc[i].get("close"))
                break

    # 扩散（diffusion）：动量值由负转正（或正转负），相对前期变化
    diffusion_event_idx = None
    diffusion_direction = None
    if val_list and len(val_list) >= 2:
        for i in range(len(val_list) - 1, 0, -1):
            v_curr = _safe_float(val_list[i])
            v_prev = _safe_float(val_list[i - 1])
            if v_curr is None or v_prev is None:
                continue
            if v_prev <= 0 < v_curr:
                diffusion_event_idx = i
                diffusion_direction = "up"
                break
            if v_prev >= 0 > v_curr:
                diffusion_event_idx = i
                diffusion_direction = "down"
                break

    events: list[PyramidEvent] = []

    # Squeeze off 事件（扩张）
    if expansion_event_idx is not None:
        fresh = max(0, last_bar_index - expansion_event_idx)
        occurred = None
        if expansion_event_idx < len(bb_df):
            occurred = _safe_iso_date(bb_df.index[expansion_event_idx])
        events.append(
            PyramidEvent(
                type="SQZ_OFF",
                direction="up",  # squeeze 释放默认方向上（BB 向外突破）
                occurredAt=occurred,
                barIndex=expansion_event_idx,
                price=expansion_price,
                freshnessBars=fresh,
                extra={"trigger": "bb_breaks_kc"},
            )
        )

    # 动量扩散事件
    if diffusion_event_idx is not None:
        fresh = max(0, last_bar_index - diffusion_event_idx)
        occurred = None
        if diffusion_event_idx < len(bb_df):
            occurred = _safe_iso_date(bb_df.index[diffusion_event_idx])
        events.append(
            PyramidEvent(
                type="MOMENTUM_DIFFUSION",
                direction=diffusion_direction,
                occurredAt=occurred,
                barIndex=diffusion_event_idx,
                price=None,
                freshnessBars=fresh,
                extra={
                    "val_prev": prev_val,
                    "val_curr": last_val,
                },
            )
        )

    # Squeeze 状态文本
    if last_sqz_on:
        squeeze_text = "Squeeze 开启（BB 收于 KC 内）"
    elif last_sqz_off:
        squeeze_text = "Squeeze 释放（BB 突破 KC）"
    elif last_no_sqz:
        squeeze_text = "无 Squeeze（部分重叠）"
    else:
        squeeze_text = "Squeeze 状态未知"

    # 动量方向文本
    mom_dir_text = ""
    if last_val is not None:
        if last_val > 0:
            mom_dir_text = "；动量偏多"
        elif last_val < 0:
            mom_dir_text = "；动量偏空"
        else:
            mom_dir_text = "；动量中性"

    bw_text = f"；BB 带宽 {bb_width:.4f}" if bb_width is not None else ""

    status_text = squeeze_text + mom_dir_text + bw_text

    continuous_factors = {
        "squeeze_on": last_sqz_on,
        "squeeze_off": last_sqz_off,
        "no_squeeze": last_no_sqz,
        "sqzmom_val": last_val,
        "sqzmom_val_prev": prev_val,
        "bb_width": bb_width,
        "bb_position": bb_position,
        "bb_upper": bb_upper,
        "bb_middle": bb_middle,
        "bb_lower": bb_lower,
    }

    evidence = {
        "bb_length": _FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_win"],
        "bb_mult": _FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_k"],
        "sqzmom_params": _FIRST_PYRAMID_PARAMS["sqzmom_config"],
        "n_bars_input": n_bars,
    }

    return DimensionResult(
        name="momentum",
        available=True,
        continuousFactors=continuous_factors,
        events=events,
        statusText=status_text,
        evidence=evidence,
    )


# =============================================================================
# 筹码共识维度（Node Cluster engine，可选）
# =============================================================================


def _build_chip_consensus_dimension(
    profile: NodeClusterProfileResult | None,
    daily_bars: pd.DataFrame,
    n_bars: int,
    last_bar_index: int,
) -> DimensionResult | None:
    """从 Node Cluster profile 构建筹码共识维度。

    PRD20 QM-40：可选维度。无有效峰或数据不足时返回 None，不阻塞前三维。
    禁止用 VAH/VAL 范围替代；禁止独立成交量过滤。
    """
    if profile is None or not profile.peak_rows:
        return None

    poc_price = profile.poc_price
    all_peak_prices = profile.all_peak_prices or []
    if not all_peak_prices:
        return None

    # 最近 bar 的 close
    if daily_bars is None or daily_bars.empty:
        return None
    last_close = float(daily_bars["close"].iloc[-1])
    prev_close = float(daily_bars["close"].iloc[-2]) if len(daily_bars) >= 2 else last_close

    # 峰上穿/下穿：使用 detect_crossover_signals
    signals = detect_crossover_signals(profile, prev_close, last_close)

    events: list[PyramidEvent] = []
    for sig in signals or []:
        sig_type = sig.get("type", "")
        direction = None
        if "cross_up" in sig_type or "touch_up" in sig_type:
            direction = "up"
        elif "cross_down" in sig_type or "touch_down" in sig_type:
            direction = "down"
        events.append(
            PyramidEvent(
                type=str(sig_type),
                direction=direction,
                occurredAt=_safe_iso_date(daily_bars.index[-1]),
                barIndex=last_bar_index,
                price=_safe_float(sig.get("price") or last_close),
                freshnessBars=0,  # 当前 bar 触发
                extra={
                    "node_price": _safe_float(sig.get("node_price")),
                    "poc_price": _safe_float(poc_price),
                },
            )
        )

    # 状态文本
    if events:
        last_evt = events[-1]
        status_text = f"Node {last_evt.type}（{last_evt.direction or '-'}）"
    else:
        if poc_price is not None:
            position = "上方" if last_close > poc_price else "下方" if last_close < poc_price else "贴合"
            status_text = f"价格在 POC {position}（{len(all_peak_prices)} 个峰）"
        else:
            status_text = "Node Cluster 无有效 POC"

    continuous_factors = {
        "poc_price": _safe_float(poc_price),
        "vah_price": _safe_float(profile.vah_price),
        "val_price": _safe_float(profile.val_price),
        "n_peak_nodes": len(all_peak_prices),
        "last_close": last_close,
        "profile_hash": profile.profile_hash,
    }

    evidence = {
        "daily_bars_count": profile.daily_bars_count,
        "bars_15m_count": profile.bars_15m_count,
        "algorithm_version": profile.algorithm_version,
        "n_bars_input": n_bars,
    }

    return DimensionResult(
        name="chip_consensus",
        available=True,
        continuousFactors=continuous_factors,
        events=events,
        statusText=status_text,
        evidence=evidence,
    )


# =============================================================================
# 聚合状态文本
# =============================================================================


def _build_aggregate_status_text(
    trend: DimensionResult, structure: DimensionResult, momentum: DimensionResult,
    chip: DimensionResult | None,
) -> str:
    """按固定顺序 trend→structure→momentum→chip_consensus 聚合中文状态文本。

    修正历史 trend→momentum→structure→volume 错误顺序（PRD20 QM-01）。
    """
    parts = [trend.statusText, structure.statusText, momentum.statusText]
    if chip is not None:
        parts.append(chip.statusText)
    return " | ".join(parts)


# =============================================================================
# 主入口
# =============================================================================


def compute_first_pyramid_snapshot(
    bars: pd.DataFrame,
    symbol: str,
    trade_date: str | None = None,
    bars_15m: pd.DataFrame | None = None,
) -> FirstPyramidSnapshot:
    """计算第一金字塔统一快照（SSOT 编排入口）。

    单股详情、批量、行情列表、盘后 compute 必须复用此函数。
    本函数不实现算法，只编排现有权威实现。

    Args:
        bars: 日线 OHLCV DataFrame（DatetimeIndex，含 open/high/low/close/volume/amount）
        symbol: 股票代码
        trade_date: 交易日（ISO YYYY-MM-DD）；为 None 时取 bars 最后一根 bar 的日期
        bars_15m: 15 分钟 bars（可选；筹码共识维度用）

    Returns:
        FirstPyramidSnapshot

    Raises:
        ValueError: 前三维（trend/structure/momentum）任一缺失或数据不足
    """
    if bars is None or bars.empty:
        raise ValueError("bars 为空，无法计算第一金字塔快照")
    if len(bars) < _MIN_BARS_FOR_REQUIRED_DIMS:
        raise ValueError(
            f"bars 长度 {len(bars)} 不足（需 >= {_MIN_BARS_FOR_REQUIRED_DIMS}），"
            f"前三维必选维度无法计算"
        )

    if not isinstance(bars.index, pd.DatetimeIndex):
        bars = bars.copy()
        bars.index = pd.to_datetime(bars.index)

    # trade_date 默认取最后一根 bar 的日期
    if trade_date is None:
        trade_date = bars.index[-1].date().isoformat()

    n_bars = len(bars)
    last_bar_index = n_bars - 1

    # 1. 趋势维度（DSA SSOT）
    dsa_config_dict: dict[str, Any] = {
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": DSA_LOOKBACK,
    }
    dsa_bundle = compute_dsa_bundle(bars, dsa_config_dict)
    trend_dim = _build_trend_dimension(dsa_bundle, n_bars)

    # 2. 结构维度（SMC Pine core）
    opens = bars["open"].astype(float).tolist()
    highs = bars["high"].astype(float).tolist()
    lows = bars["low"].astype(float).tolist()
    closes = bars["close"].astype(float).tolist()
    times = [d.isoformat() for d in bars.index]
    smc_result = compute_smc_pine(opens, highs, lows, closes, times, params=None)
    structure_dim = _build_structure_dimension(smc_result, n_bars, last_bar_index)

    # 3. 动量维度（Bollinger + SQZMOM）
    bb_cfg = BBcfg(
        bb_win=_FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_win"],
        bb_k=_FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_k"],
    )
    bb_df = compute_bollinger_features(bars, bb_cfg)
    sqzmom_result = compute_sqzmom_lb(
        opens=np.array(opens, dtype=float),
        highs=np.array(highs, dtype=float),
        lows=np.array(lows, dtype=float),
        closes=np.array(closes, dtype=float),
        params=_FIRST_PYRAMID_PARAMS["sqzmom_config"],
    )
    momentum_dim = _build_momentum_dimension(bb_df, sqzmom_result, n_bars, last_bar_index)

    # 4. 筹码共识维度（Node Cluster，可选）
    chip_dim: DimensionResult | None = None
    try:
        profile = compute_node_cluster_profile(
            daily_bars=bars,
            bars_15m=bars_15m,
            adjustment_as_of=trade_date,
        )
        chip_dim = _build_chip_consensus_dimension(profile, bars, n_bars, last_bar_index)
    except Exception as exc:
        logger.info("Node Cluster 计算失败，chip_consensus 设为 None: %s", exc)
        chip_dim = None

    # 5. 聚合状态文本
    aggregate_status = _build_aggregate_status_text(
        trend_dim, structure_dim, momentum_dim, chip_dim
    )

    # 6. 输入与参数 hash
    input_hash = _compute_input_hash(bars)
    parameter_hash = _compute_parameter_hash()

    return FirstPyramidSnapshot(
        symbol=symbol,
        tradeDate=trade_date,
        trend=trend_dim,
        structure=structure_dim,
        momentum=momentum_dim,
        chipConsensus=chip_dim,
        statusText=aggregate_status,
        inputHash=input_hash,
        parameterHash=parameter_hash,
    )


# 模块自测
if __name__ == "__main__":
    # 构造最小测试 fixture：60 根上涨 bars
    np.random.seed(42)
    dates = pd.date_range("2026-01-01", periods=80, freq="B")
    close = 10.0 + np.cumsum(np.random.randn(80) * 0.1 + 0.05)
    df = pd.DataFrame(
        {
            "open": close - np.random.rand(80) * 0.1,
            "high": close + np.random.rand(80) * 0.2,
            "low": close - np.random.rand(80) * 0.2,
            "close": close,
            "volume": np.random.randint(100000, 500000, 80).astype(float),
            "amount": close * np.random.randint(100000, 500000, 80).astype(float),
        },
        index=dates,
    )

    try:
        snap = compute_first_pyramid_snapshot(df, symbol="TEST.MOCK", trade_date="2026-04-24")
        print(f"OK: {snap.symbol} {snap.tradeDate}")
        print(f"  ordered: {snap.orderedDimensions}")
        print(f"  algo: {snap.algorithmVersion}")
        print(f"  inputHash: {snap.inputHash}")
        print(f"  parameterHash: {snap.parameterHash}")
        print(f"  trend.available: {snap.trend.available}")
        print(f"  structure.available: {snap.structure.available}")
        print(f"  momentum.available: {snap.momentum.available}")
        print(f"  chipConsensus: {snap.chipConsensus}")
        print(f"  statusText: {snap.statusText}")
    except Exception as exc:
        print(f"FAIL: {type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
