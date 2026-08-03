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

from app.constants.indicator_contract import (
    DSA_LOOKBACK,
    NODE_CLUSTER_LOW_BARS,
    NODE_CLUSTER_PRIMARY_BARS,
)
from app.schemas.first_pyramid import (
    CHIP_CONSENSUS_ALGORITHM_VERSION,
    FIRST_PYRAMID_ALGORITHM_VERSION,
    FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
    ORDERED_DIMENSIONS,
    ChipConsensusResult,
    ChipStatus,
    DimensionResult,
    FirstPyramidCoreSnapshot,
    FirstPyramidSnapshot,
    PyramidEvent,
    VolumeContextSchema,
)
from app.services.node_cluster_engine import (
    NodeClusterProfileResult,
    compute_node_cluster_profile,
    detect_crossover_signals,
)
from app.services.volume_context import (
    VolumeContextData,
    compute_volume_context_series,
    extract_last_volume_context,
    extract_volume_context_at,
    volume_badge,
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
from app.strategy_assets.algorithms.features.sqzmom_lb import (
    build_momentum_history,
    compute_sqzmom_lb,
)

logger = logging.getLogger(__name__)

# 第一金字塔固定参数（进入 parameterHash，禁止页面动态组合）
_FIRST_PYRAMID_PARAMS: dict[str, Any] = {
    "dsa_lookback": DSA_LOOKBACK,
    "dsa_min_dir_bars": MIN_DIR_BARS,
    "smc_default_params": SMC_DEFAULT_PARAMS,
    "bollinger_config": {"bb_win": 20, "bb_k": 2.0},
    "sqzmom_config": {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
    "node_cluster_required_daily_bars": NODE_CLUSTER_PRIMARY_BARS,
}

# [CHANGE-20260729-003 核心与筹码解耦] Core 专用参数（排除 Node Cluster 参数）
# 用于 compute_first_pyramid_core_snapshot 的 parameterHash，禁止包含 Node 参数
_FIRST_PYRAMID_CORE_PARAMS: dict[str, Any] = {
    "dsa_lookback": DSA_LOOKBACK,
    "dsa_min_dir_bars": MIN_DIR_BARS,
    "smc_default_params": SMC_DEFAULT_PARAMS,
    "bollinger_config": {"bb_win": 20, "bb_k": 2.0},
    "sqzmom_config": {"length": 20, "mult": 2.0, "lengthKC": 20, "multKC": 1.5, "useTrueRange": True},
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


def _vc_to_schema(vc: VolumeContextData | None) -> VolumeContextSchema | None:
    """将 VolumeContextData 转为 VolumeContextSchema（前端 JSON 契约）。"""
    if vc is None:
        return None
    return VolumeContextSchema(
        volume=vc.volume,
        amount=vc.amount,
        turnoverRate=vc.turnover_rate,
        volumeMa20=vc.volume_ma_20,
        volumeMa200=vc.volume_ma_200,
        volumeRatio20=vc.volume_ratio_20,
        volumeRatio200=vc.volume_ratio_200,
        volumePercentile20=vc.volume_percentile_20,
        volumePercentile200=vc.volume_percentile_200,
        volumeZscore20=vc.volume_zscore_20,
        volumeZscore200=vc.volume_zscore_200,
        readiness=vc.readiness,
        badge=volume_badge(vc),
    )


def _event_vc(
    vc_series: pd.DataFrame | None, bar_index: int | None
) -> tuple[VolumeContextSchema | None, str | None]:
    """提取事件 bar 的 VolumeContext Schema 和徽标。"""
    if vc_series is None or vc_series.empty or bar_index is None:
        return None, None
    vc = extract_volume_context_at(vc_series, bar_index)
    return _vc_to_schema(vc), volume_badge(vc) if vc else None


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


def _compute_core_parameter_hash() -> str:
    """[CHANGE-20260729-003] 计算 core 参数 hash（排除 Node Cluster 参数）。

    core 的 parameterHash 只包含 trend/structure/momentum 算法参数，
    禁止包含 Node Cluster 参数（node_cluster_required_daily_bars）。
    """
    try:
        content = json.dumps(
            {
                "algorithm_version": FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                "required_dimensions": ["trend", "structure", "momentum"],
                "params": _FIRST_PYRAMID_CORE_PARAMS,
            },
            sort_keys=True,
            default=str,
        )
        return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    except Exception as exc:
        logger.warning("core_parameter_hash 计算失败: %s", exc)
        return "sha256:error"


def _compute_chip_hash(
    daily_bars: pd.DataFrame | None,
    bars_15m: pd.DataFrame | None,
) -> str:
    """[CHANGE-20260729-003] 计算 chip 输入 hash（daily + 15m bars）。

    chip hash 独立于 core inputHash，关联独立 chip run。
    """
    parts: list[str] = []
    for label, b in (("daily", daily_bars), ("15m", bars_15m)):
        if b is None or b.empty:
            parts.append(f"{label}:empty")
            continue
        cols = [c for c in ("open", "high", "low", "close", "volume") if c in b.columns]
        if not cols:
            parts.append(f"{label}:no_ohlcv")
            continue
        try:
            idx_str = pd.Series(b.index.astype(str)).str.cat(sep=",")
            vals_str = b[cols].astype(str).agg(",".join, axis=1).str.cat(sep="|")
            parts.append(f"{len(b)}:{hashlib.sha256((idx_str + '#' + vals_str).encode('utf-8')).hexdigest()[:8]}")
        except Exception:
            parts.append(f"{len(b)}:error")
    return "sha256:" + hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


# =============================================================================
# 趋势维度（DSA SSOT）
# =============================================================================


def _build_trend_dimension(
    dsa_bundle: dict[str, Any],
    n_bars: int,
    vc_series: pd.DataFrame | None = None,
) -> DimensionResult:
    """从 DSA bundle 构建趋势维度结果。

    DSA bundle 由 compute_dsa_bundle 产生，含 last_row_metrics（标量指标）。
    Gate1：集成统一 VolumeContext（20/200 日分位、zscore、徽标）。
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

    # 段内成交量（Phase 5B-1 迁回 DSA SSOT；CHANGE-20260729-002 改 mean/mean 口径）
    current_seg_vol_mean = _safe_float(metrics.get("current_segment_volume_mean"))
    current_seg_amt_mean = _safe_float(metrics.get("current_segment_amount_mean"))
    prev_seg_vol_mean = _safe_float(metrics.get("prev_segment_volume_mean"))
    prev_seg_amt_mean = _safe_float(metrics.get("prev_segment_amount_mean"))
    # [权威口径] mean/mean ratio（旧 sum/sum 字段保留兼容但禁止消费）
    current_vs_prev_vol = _safe_float(metrics.get("current_vs_prev_volume_mean_ratio"))
    current_vs_prev_amt = _safe_float(metrics.get("current_vs_prev_amount_mean_ratio"))
    # deprecated sum/sum 字段（保留兼容，前端如需要可读，但不得用于新筛选逻辑）
    prev_seg_vol_sum = _safe_float(metrics.get("prev_segment_volume_sum"))
    prev_seg_amt_sum = _safe_float(metrics.get("prev_segment_amount_sum"))

    # Gate1：统一 VolumeContext
    last_vc = extract_last_volume_context(vc_series) if vc_series is not None else None
    vc_schema = _vc_to_schema(last_vc)

    # 趋势段信息（Gate1 补充）
    segment_start_time = _safe_iso_date(metrics.get("segment_start_time") or metrics.get("anchor_time"))
    segment_end_time = _safe_iso_date(metrics.get("segment_end_time"))
    segment_start_price = _safe_float(metrics.get("segment_start_price"))
    segment_end_price = _safe_float(metrics.get("segment_end_price"))
    segment_bars = int(metrics.get("segment_bars", dsa_dir_bars) or dsa_dir_bars)
    segment_change_pct = _safe_float(metrics.get("segment_change_pct"))
    segment_slope = _safe_float(metrics.get("segment_slope"))
    current_price_vs_dsa_vwap = dsa_vwap_dev_pct

    continuous_factors: dict[str, Any] = {
        "regime_value": regime_value,
        "dsa_dir_bars": dsa_dir_bars,
        "dsa_vwap_dev_pct": dsa_vwap_dev_pct,
        # 趋势段信息（Gate1）
        "segment_start_time": segment_start_time,
        "segment_end_time": segment_end_time,
        "segment_start_price": segment_start_price,
        "segment_end_price": segment_end_price,
        "segment_bars": segment_bars,
        "segment_change_pct": segment_change_pct,
        "segment_slope": segment_slope,
        "current_price_vs_dsa_vwap": current_price_vs_dsa_vwap,
        # 段内成交量（[CHANGE-20260729-002] mean/mean 权威口径 + deprecated sum/sum 兼容）
        "current_segment_volume_mean": current_seg_vol_mean,
        "current_segment_amount_mean": current_seg_amt_mean,
        "prev_segment_volume_mean": prev_seg_vol_mean,
        "prev_segment_amount_mean": prev_seg_amt_mean,
        "current_vs_prev_volume_mean_ratio": current_vs_prev_vol,
        "current_vs_prev_amount_mean_ratio": current_vs_prev_amt,
        # deprecated 字段保留兼容，禁止新逻辑消费
        "prev_segment_volume_sum": prev_seg_vol_sum,
        "prev_segment_amount_sum": prev_seg_amt_sum,
        # 统一 VolumeContext 字段（Gate1，扁平化便于前端直接取用）
        "volume_ratio_20": last_vc.volume_ratio_20 if last_vc else None,
        "volume_ratio_200": last_vc.volume_ratio_200 if last_vc else None,
        "volume_percentile_20": last_vc.volume_percentile_20 if last_vc else None,
        "volume_percentile_200": last_vc.volume_percentile_200 if last_vc else None,
        "volume_zscore_20": last_vc.volume_zscore_20 if last_vc else None,
        "volume_zscore_200": last_vc.volume_zscore_200 if last_vc else None,
        "trend_strength": _safe_float(metrics.get("trend_strength")),  # deprecated 别名
        # [CHANGE-20260729-002] 权威字段名 regime_strength（DSA SSOT 输出），
        # 旧代码误读不存在的 trend_strength 字段导致静默 None，已修正。
        "regime_strength": _safe_float(metrics.get("regime_strength")),
        "trend_transition": metrics.get("trend_transition", "NONE"),
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
    # [CHANGE-20260729-002] 使用 mean/mean ratio（权威口径）
    if current_vs_prev_vol is not None:
        if current_vs_prev_vol > 1.2:
            vol_text = "；当前段放量"
        elif current_vs_prev_vol < 0.8:
            vol_text = "；当前段缩量"
        else:
            vol_text = "；当前段量能持平"

    # Gate1：量能徽标
    vc_badge = volume_badge(last_vc) if last_vc else "未知"
    vc_text = f"；量能{vc_badge}" if last_vc and last_vc.readiness else ""

    status_text = regime_text + vol_text + vc_text

    evidence = {
        "anchor_time": _safe_iso_date(metrics.get("anchor_time")),
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": DSA_LOOKBACK,
        "n_bars_input": n_bars,
        "volume_readiness": last_vc.readiness if last_vc else False,
    }

    return DimensionResult(
        name="trend",
        available=True,
        continuousFactors=continuous_factors,
        events=[],  # DSA 是连续因子，无离散事件
        statusText=status_text,
        evidence=evidence,
        volumeContext=vc_schema,
    )


# =============================================================================
# 结构维度（SMC Pine core）
# =============================================================================


def _build_structure_dimension(
    smc_result: dict[str, Any],
    n_bars: int,
    last_bar_index: int,
    vc_series: pd.DataFrame | None = None,
) -> DimensionResult:
    """从 SMC Pine core 结果构建结构维度。

    输出 BOS、CHoCH、进入 OB、连续高低点事件；禁止 FVG。
    Gate1：每个事件附加事件 bar 的 VolumeContext 和量能徽标。
    """
    events_raw = smc_result.get("events", []) or []
    order_blocks = smc_result.get("order_blocks", []) or []
    equal_highs_lows = smc_result.get("equal_highs_lows", []) or []
    # [CHANGE-20260729-002] 消费 SMC 权威输出的 OB 生命周期事件，不再派生 OB_ENTRY
    ob_lifecycle_events = smc_result.get("ob_lifecycle_events", []) or []
    swing_bias = int(smc_result.get("swing_bias", 0) or 0)
    # [Round 2026-07-28 第一金字塔定稿] 同时输出 internal_direction（短线结构方向）
    internal_bias = int(smc_result.get("internal_bias", 0) or 0)

    # 离散事件：BOS / CHoCH
    pyramid_events: list[PyramidEvent] = []
    for evt in events_raw:
        evt_type = evt.get("type", "")  # "BOS" / "CHoCH"
        # [Round 2026-07-28] SMC 事件用 bullish(bool)/bias(1/-1) 表达方向，
        # 兼容旧 direction 字段；定稿要求 BOS/CHoCH 必须保存 bullish/bearish
        direction = None
        if evt.get("bullish") is True or evt.get("bias") == 1:
            direction = "up"
        elif evt.get("bullish") is False or evt.get("bias") == -1:
            direction = "down"
        else:
            direction_raw = evt.get("direction")
            if direction_raw in (1, "bullish", "up"):
                direction = "up"
            elif direction_raw in (-1, "bearish", "down"):
                direction = "down"
        occurred_at = _safe_iso_date(evt.get("confirmed_time") or evt.get("time"))
        bar_index = evt.get("confirmed_index") or evt.get("barIndex")
        price = _safe_float(evt.get("level") or evt.get("broken_level") or evt.get("price"))
        # 新鲜度 = 最后 bar 索引 - 事件确认索引
        fresh = max(0, int(last_bar_index - int(bar_index))) if bar_index is not None else 0
        # Gate1：事件 bar 的 VolumeContext
        evt_vc, evt_badge = _event_vc(vc_series, int(bar_index) if bar_index is not None else None)
        # [Round 2026-07-28] BOS/CHoCH 必须标注 structure_level: swing/internal
        is_internal = bool(evt.get("internal", False))
        structure_level = "internal" if is_internal else "swing"
        pyramid_events.append(
            PyramidEvent(
                type=str(evt_type),
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
                volumeContext=evt_vc,
                volumeBadge=evt_badge,
                extra={
                    "anchor_index": evt.get("anchor_index"),
                    "structure_level": structure_level,
                },
            )
        )

    # [CHANGE-20260729-002] 消费 SMC 权威 OB 生命周期事件（OB_CREATED/ENTERED/MITIGATED）
    # 删除"活跃OB = OB_ENTRY"派生，统一从 SMC ob_lifecycle_events 输出。
    # 字段：type/internal/bias/anchor_index/anchor_time/confirmed_index/confirmed_time/
    #       bar_high/bar_low/structure_level/[enter_index/enter_time/mitigated_index/mitigated_time]
    for ob_evt in ob_lifecycle_events:
        evt_type = ob_evt.get("type", "")
        # 方向：bias(1/-1) → up/down
        ob_bias = ob_evt.get("bias")
        if ob_bias == 1:
            direction = "up"
        elif ob_bias == -1:
            direction = "down"
        else:
            direction = None
        # 事件发生 bar：CREATED 用 confirmed_index；ENTERED 用 enter_index；MITIGATED 用 mitigated_index
        bar_index = (
            ob_evt.get("enter_index")
            or ob_evt.get("mitigated_index")
            or ob_evt.get("confirmed_index")
        )
        occurred_at = _safe_iso_date(
            ob_evt.get("enter_time")
            or ob_evt.get("mitigated_time")
            or ob_evt.get("confirmed_time")
        )
        ob_high = _safe_float(ob_evt.get("bar_high"))
        ob_low = _safe_float(ob_evt.get("bar_low"))
        # 价格：CREATED 用 anchor 端点；ENTERED/MITIGATED 用对应 bar 的 high/low（无则用 OB 边界）
        if evt_type == "OB_CREATED":
            price = ob_high if ob_bias == 1 else ob_low
        else:
            price = ob_high if ob_bias == 1 else ob_low
        fresh = max(0, int(last_bar_index - int(bar_index))) if bar_index is not None else 0
        evt_vc, evt_badge = _event_vc(
            vc_series, int(bar_index) if bar_index is not None else None
        )
        ob_internal = bool(ob_evt.get("internal", False))
        ob_level = "internal" if ob_internal else "swing"
        extra: dict[str, Any] = {
            "ob_high": ob_high,
            "ob_low": ob_low,
            "structure_level": ob_level,
            "anchor_index": ob_evt.get("anchor_index"),
            "anchor_time": _safe_iso_date(ob_evt.get("anchor_time")),
            "confirmed_index": ob_evt.get("confirmed_index"),
            "confirmed_time": _safe_iso_date(ob_evt.get("confirmed_time")),
        }
        # ENTERED/MITIGATED 额外携带生命周期时点
        if evt_type == "OB_ENTERED":
            extra["enter_index"] = ob_evt.get("enter_index")
            extra["enter_time"] = _safe_iso_date(ob_evt.get("enter_time"))
        elif evt_type == "OB_MITIGATED":
            extra["mitigated_index"] = ob_evt.get("mitigated_index")
            extra["mitigated_time"] = _safe_iso_date(ob_evt.get("mitigated_time"))
            extra["entered_before_mitigation"] = ob_evt.get("entered_before_mitigation", False)
            extra["enter_index"] = ob_evt.get("enter_index")
            extra["enter_time"] = _safe_iso_date(ob_evt.get("enter_time"))
        pyramid_events.append(
            PyramidEvent(
                type=str(evt_type),
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
                volumeContext=evt_vc,
                volumeBadge=evt_badge,
                extra=extra,
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
        evt_vc, evt_badge = _event_vc(vc_series, int(bar_index) if bar_index is not None else None)
        # [Round 2026-07-28] EQH/EQL 不属于 swing/internal，structure_level=null（禁止推测）
        pyramid_events.append(
            PyramidEvent(
                type=str(eq_type),
                direction=direction,
                occurredAt=occurred_at,
                barIndex=int(bar_index) if bar_index is not None else None,
                price=price,
                freshnessBars=fresh,
                volumeContext=evt_vc,
                volumeBadge=evt_badge,
                extra={"structure_level": None},
            )
        )

    # 按时间升序
    pyramid_events.sort(key=lambda e: (e.barIndex if e.barIndex is not None else 0))

    # Gate1：当前 bar 的 VolumeContext
    last_vc = extract_last_volume_context(vc_series) if vc_series is not None else None
    vc_schema = _vc_to_schema(last_vc)

    continuous_factors = {
        "swing_bias": swing_bias,
        # [Round 2026-07-28 第一金字塔定稿] swing_direction（主要结构）/ internal_direction（短线结构）
        # 取值：1=bullish, -1=bearish, 0=未形成；与 swing_bias 同义，命名对齐定稿
        "swing_direction": swing_bias,
        "internal_direction": internal_bias,
        "active_ob_count": sum(1 for ob in order_blocks if not ob.get("mitigated", False)),
        "trailing_top": _safe_float(smc_result.get("trailing", {}).get("top")),
        "trailing_bottom": _safe_float(smc_result.get("trailing", {}).get("bottom")),
    }

    # 状态文本（纯中文：主要结构/短线结构）
    bias_text = {1: "主要结构偏多", -1: "主要结构偏空", 0: "主要结构未形成"}.get(swing_bias, "主要结构未形成")
    internal_text = {1: "短线结构偏多", -1: "短线结构偏空", 0: "短线结构未形成"}.get(internal_bias, "短线结构未形成")
    last_event_text = ""
    if pyramid_events:
        last_evt = pyramid_events[-1]
        last_event_text = f"；最近 {last_evt.type}（{last_evt.direction or '-'}, 新鲜度 {last_evt.freshnessBars}）"

    status_text = bias_text + "；" + internal_text + last_event_text

    evidence = {
        "n_pivots": len(smc_result.get("pivots", []) or []),
        "n_order_blocks": len(order_blocks),
        "n_equal_highs_lows": len(equal_highs_lows),
        "smc_params": smc_result.get("params", {}),
        "n_bars_input": n_bars,
        "volume_readiness": last_vc.readiness if last_vc else False,
    }

    return DimensionResult(
        name="structure",
        available=True,
        continuousFactors=continuous_factors,
        events=pyramid_events,
        statusText=status_text,
        evidence=evidence,
        volumeContext=vc_schema,
    )


# =============================================================================
# 动量维度（Bollinger + SQZMOM）
# =============================================================================


def _build_momentum_dimension(
    bb_df: pd.DataFrame,
    sqzmom_result: dict[str, Any],
    n_bars: int,
    last_bar_index: int,
    vc_series: pd.DataFrame | None = None,
    bars: pd.DataFrame | None = None,
) -> DimensionResult:
    """从 Bollinger + SQZMOM 结果构建动量维度。

    输出 squeeze 状态、带宽、扩张/扩散事件、相对前期变化、匹配成交量和事件新鲜度。
    Gate1：集成统一 VolumeContext + 挤压期平均量 + 缩量挤压/放量释放判断。
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

    # Gate1：挤压期平均量 + 缩量挤压/放量释放
    squeeze_period_vol_mean: float | None = None
    release_vs_squeeze_vol_ratio: float | None = None
    vol_divergence: str | None = None  # "缩量挤压" / "放量释放" / "量价背离" / None
    if bars is not None and sqz_on_list:
        # 找到最近的连续 squeeze on 区间
        sqz_end = len(sqz_on_list) - 1
        sqz_start = sqz_end
        for i in range(sqz_end, -1, -1):
            if sqz_on_list[i]:
                sqz_start = i
            else:
                break
        if sqz_end > sqz_start:
            vol_series = bars["volume"].astype(float) if "volume" in bars.columns else None
            if vol_series is not None:
                squeeze_vols = vol_series.iloc[sqz_start:sqz_end + 1].dropna()
                if len(squeeze_vols) > 0:
                    squeeze_period_vol_mean = _safe_float(squeeze_vols.mean())
                    # 释放期量 / 挤压期均量
                    if sqz_end + 1 < len(vol_series):
                        release_vol = _safe_float(vol_series.iloc[sqz_end + 1])
                        if release_vol is not None and squeeze_period_vol_mean and squeeze_period_vol_mean > 0:
                            release_vs_squeeze_vol_ratio = _safe_float(
                                release_vol / squeeze_period_vol_mean
                            )
                    # 判断量价关系
                    if last_sqz_on and squeeze_period_vol_mean is not None:
                        last_vc = extract_last_volume_context(vc_series) if vc_series is not None else None
                        if last_vc and last_vc.readiness and last_vc.volume_percentile_20 is not None:
                            if last_vc.volume_percentile_20 < 20:
                                vol_divergence = "缩量挤压"
                    if last_sqz_off and release_vs_squeeze_vol_ratio is not None:
                        if release_vs_squeeze_vol_ratio > 1.5:
                            vol_divergence = "放量释放"

    # Gate1：统一 VolumeContext
    last_vc = extract_last_volume_context(vc_series) if vc_series is not None else None
    vc_schema = _vc_to_schema(last_vc)
    vc_badge = volume_badge(last_vc) if last_vc else "未知"
    vc_text = f"；量能{vc_badge}" if last_vc and last_vc.readiness else ""
    div_text = f"；{vol_divergence}" if vol_divergence else ""

    status_text = squeeze_text + mom_dir_text + bw_text + vc_text + div_text

    # Gate1：为事件附加 VolumeContext
    for evt in events:
        if evt.barIndex is not None:
            evt_vc, evt_badge = _event_vc(vc_series, evt.barIndex)
            evt.volumeContext = evt_vc
            evt.volumeBadge = evt_badge

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
        # Gate1：量能字段
        "squeeze_period_volume_mean": squeeze_period_vol_mean,
        "release_vs_squeeze_volume_ratio": release_vs_squeeze_vol_ratio,
        "vol_divergence": vol_divergence,
        "volume_ratio_20": last_vc.volume_ratio_20 if last_vc else None,
        "volume_ratio_200": last_vc.volume_ratio_200 if last_vc else None,
        "volume_percentile_20": last_vc.volume_percentile_20 if last_vc else None,
        "volume_percentile_200": last_vc.volume_percentile_200 if last_vc else None,
        "volume_zscore_20": last_vc.volume_zscore_20 if last_vc else None,
        "volume_zscore_200": last_vc.volume_zscore_200 if last_vc else None,
    }

    evidence = {
        "bb_length": _FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_win"],
        "bb_mult": _FIRST_PYRAMID_PARAMS["bollinger_config"]["bb_k"],
        "sqzmom_params": _FIRST_PYRAMID_PARAMS["sqzmom_config"],
        "n_bars_input": n_bars,
        "volume_readiness": last_vc.readiness if last_vc else False,
    }

    return DimensionResult(
        name="momentum",
        available=True,
        continuousFactors=continuous_factors,
        events=events,
        statusText=status_text,
        evidence=evidence,
        volumeContext=vc_schema,
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
# 主入口（拆分：core / chip / assemble）
# =============================================================================


def compute_first_pyramid_core_snapshot(
    bars: pd.DataFrame,
    symbol: str,
    trade_date: str | None = None,
) -> FirstPyramidCoreSnapshot:
    """[CHANGE-20260729-003] 计算第一金字塔核心快照（trend/structure/momentum）。

    盘后 review core 关键路径使用本函数，禁止 Node Cluster 和 15m Node 输入。
    core 的 inputHash/parameterHash 排除 Node 参数，与 chip 解耦。

    Args:
        bars: 日线 OHLCV DataFrame（DatetimeIndex，含 open/high/low/close/volume/amount）
        symbol: 股票代码
        trade_date: 交易日（ISO YYYY-MM-DD）；为 None 时取 bars 最后一根 bar 的日期

    Returns:
        FirstPyramidCoreSnapshot（不含 chip_consensus）

    Raises:
        ValueError: 前三维（trend/structure/momentum）任一缺失或数据不足
    """
    if bars is None or bars.empty:
        raise ValueError("bars 为空，无法计算第一金字塔核心快照")
    if len(bars) < _MIN_BARS_FOR_REQUIRED_DIMS:
        raise ValueError(
            f"bars 长度 {len(bars)} 不足（需 >= {_MIN_BARS_FOR_REQUIRED_DIMS}），"
            f"前三维必选维度无法计算"
        )

    if not isinstance(bars.index, pd.DatetimeIndex):
        bars = bars.copy()
        bars.index = pd.to_datetime(bars.index)

    if trade_date is None:
        trade_date = bars.index[-1].date().isoformat()

    n_bars = len(bars)
    last_bar_index = n_bars - 1

    # Gate1：统一 VolumeContext（计算一次，趋势/结构/动量复用）
    vc_series = compute_volume_context_series(bars)
    last_vc = extract_last_volume_context(vc_series)
    vc_schema = _vc_to_schema(last_vc)

    # 1. 趋势维度（DSA SSOT）
    dsa_config_dict: dict[str, Any] = {
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": DSA_LOOKBACK,
    }
    dsa_bundle = compute_dsa_bundle(bars, dsa_config_dict)
    trend_dim = _build_trend_dimension(dsa_bundle, n_bars, vc_series)

    # 2. 结构维度（SMC Pine core）
    opens = bars["open"].astype(float).tolist()
    highs = bars["high"].astype(float).tolist()
    lows = bars["low"].astype(float).tolist()
    closes = bars["close"].astype(float).tolist()
    times = [d.isoformat() for d in bars.index]
    smc_result = compute_smc_pine(opens, highs, lows, closes, times, params=None)
    structure_dim = _build_structure_dimension(smc_result, n_bars, last_bar_index, vc_series)

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
    momentum_dim = _build_momentum_dimension(
        bb_df, sqzmom_result, n_bars, last_bar_index, vc_series, bars
    )

    # 4. core hash（排除 Node 参数）
    input_hash = _compute_input_hash(bars)
    parameter_hash = _compute_core_parameter_hash()
    aggregate_status = _build_aggregate_status_text(
        trend_dim, structure_dim, momentum_dim, None
    )

    return FirstPyramidCoreSnapshot(
        symbol=symbol,
        tradeDate=trade_date,
        trend=trend_dim,
        structure=structure_dim,
        momentum=momentum_dim,
        statusText=aggregate_status,
        volumeContext=vc_schema,
        inputHash=input_hash,
        parameterHash=parameter_hash,
        nBars=n_bars,
        lastBarIndex=last_bar_index,
    )


def compute_chip_consensus_snapshot(
    daily_bars: pd.DataFrame,
    bars_15m: pd.DataFrame | None = None,
    trade_date: str | None = None,
    *,
    n_bars: int | None = None,
    last_bar_index: int | None = None,
) -> ChipConsensusResult:
    """[CHANGE-20260729-003] 计算筹码共识快照（独立于 core）。

    chip 使用独立 version/hash/run 关联。可独立失败/重试，
    绝不反改主 run 或重算 core。

    Args:
        daily_bars: 日线 OHLCV DataFrame
        bars_15m: 15 分钟 bars（可选）
        trade_date: 交易日（用于 adjustment_as_of）
        n_bars: core 的 nBars（用于 evidence）
        last_bar_index: core 的 lastBarIndex（用于 evidence）

    Returns:
        ChipConsensusResult（chip 可为 None；error 字段记录失败原因）
    """
    chip_hash = _compute_chip_hash(daily_bars, bars_15m)
    if daily_bars is None or daily_bars.empty:
        return ChipConsensusResult(
            chip=None,
            chipHash=chip_hash,
            dailyBarsCount=0,
            bars15mCount=0 if bars_15m is None else len(bars_15m),
            error="daily_bars 为空",
        )

    try:
        profile = compute_node_cluster_profile(
            daily_bars=daily_bars,
            bars_15m=bars_15m,
            adjustment_as_of=trade_date,
        )
        chip_dim = _build_chip_consensus_dimension(
            profile,
            daily_bars,
            n_bars or len(daily_bars),
            last_bar_index if last_bar_index is not None else len(daily_bars) - 1,
        )
        return ChipConsensusResult(
            chip=chip_dim,
            chipHash=chip_hash,
            dailyBarsCount=len(daily_bars),
            bars15mCount=0 if bars_15m is None else len(bars_15m),
        )
    except Exception as exc:
        logger.info("Node Cluster 计算失败，chip 设为 None: %s", exc)
        return ChipConsensusResult(
            chip=None,
            chipHash=chip_hash,
            dailyBarsCount=len(daily_bars),
            bars15mCount=0 if bars_15m is None else len(bars_15m),
            error=str(exc),
        )


def assemble_first_pyramid_view(
    core: FirstPyramidCoreSnapshot,
    chip: ChipConsensusResult | None,
) -> FirstPyramidSnapshot:
    """[CHANGE-20260729-003] 组合核心快照与筹码快照为完整第一金字塔视图。

    保留必要兼容包装：组装后的 FirstPyramidSnapshot 使用 core 的 inputHash/parameterHash
    （含 chip 时升级为完整版本），chip 失败时仍可返回完整 core 视图。

    [CHANGE-20260729-004 P0-2] 同步构建 chipStatus 结构化状态（替代统一"暂不可用"），
    前端可读取 reasonCode/reasonText 显示真实原因（如 M15_BARS_INSUFFICIENT + 实际数量）。

    Args:
        core: 核心快照（trend/structure/momentum）
        chip: 筹码快照（可为 None 或含 error）

    Returns:
        FirstPyramidSnapshot（chip_consensus 为 None 时不阻塞）
    """
    chip_dim = chip.chip if chip is not None else None
    aggregate_status = _build_aggregate_status_text(
        core.trend, core.structure, core.momentum, chip_dim
    )
    # 组装视图使用完整 parameterHash（含 Node 参数，向后兼容）
    parameter_hash = _compute_parameter_hash()
    chip_status = _build_chip_status(chip)
    semantic_meta = _chip_semantic_meta(chip_status, chip_dim)

    return FirstPyramidSnapshot(
        symbol=core.symbol,
        tradeDate=core.tradeDate,
        trend=core.trend,
        structure=core.structure,
        momentum=core.momentum,
        chipConsensus=chip_dim,
        chipStatus=chip_status.model_copy(update=semantic_meta),
        statusText=aggregate_status,
        volumeContext=core.volumeContext,
        inputHash=core.inputHash,
        parameterHash=parameter_hash,
    )


# =============================================================================
# [CHANGE-20260729-004 P0-2] chipStatus 构建辅助
# =============================================================================


def _chip_semantic_meta(status: ChipStatus, chip_dim: Any) -> dict[str, Any]:
    available = status.state == "ready" and chip_dim is not None
    if not available:
        semantic = "unavailable" if status.state != "pending" else "not_applicable"
    else:
        support = getattr(chip_dim, "supportPressure", None)
        pressure = float(support) if support is not None else 0.0
        semantic = "strong_support" if pressure >= 0.6 else "weak_support" if pressure > 0.1 else "strong_pressure" if pressure <= -0.6 else "weak_pressure" if pressure < -0.1 else "neutral"
    meta = {"strong_support": {"label": "强支撑", "tone": "positive", "order": 1}, "weak_support": {"label": "弱支撑", "tone": "positive", "order": 2}, "neutral": {"label": "中性", "tone": "neutral", "order": 3}, "weak_pressure": {"label": "弱压力", "tone": "negative", "order": 4}, "strong_pressure": {"label": "强压力", "tone": "negative", "order": 5}, "unavailable": {"label": "不可用", "tone": "muted", "order": 6}, "not_applicable": {"label": "不适用", "tone": "muted", "order": 7}}[semantic]
    return {"semanticState": semantic, **meta}


def _build_chip_status(chip: ChipConsensusResult | None) -> ChipStatus:
    """从 ChipConsensusResult 构建结构化 ChipStatus。

    优先级：
    1. chip is None → pending（chip job 未运行或未完成）
    2. chip.error 非空 → 解析错误码（M15_BARS_INSUFFICIENT / DAILY_BARS_INSUFFICIENT /
       CHIP_JOB_FAILED / NO_VALID_PEAK）
    3. chip.chip is None → NO_VALID_PEAK
    4. chip.chip.available=True → ready

    Args:
        chip: ChipConsensusResult 或 None

    Returns:
        ChipStatus（state/reasonCode/reasonText/computedAt）
    """
    if chip is None:
        return ChipStatus(
            state="pending",
            reasonCode="CHIP_JOB_PENDING",
            reasonText="筹码共识计算尚未运行",
            computedAt=None,
        )

    # 已有 chip 结果，根据 error 和 chip 内容判断
    if chip.error:
        # 解析 error 字符串映射到稳定 reasonCode
        err_lower = chip.error.lower()
        if "insufficient_daily" in err_lower or "daily_bars" in err_lower:
            return ChipStatus(
                state="unavailable",
                reasonCode="DAILY_BARS_INSUFFICIENT",
                reasonText=f"日线数据不足（{chip.dailyBarsCount} 根，需 ≥10）",
                computedAt=None,
            )
        if (
            "input_contract_violation" in err_lower
            or "insufficient_15m" in err_lower
            or "missing_15m" in err_lower
            or "m15" in err_lower
        ):
            # [CHANGE-20260729-009] 两个 15m 门槛：
            # - _CHIP_MIN_15M_BARS=500：批量服务最低门槛（after_close_chip_consensus_service）
            # - NODE_CLUSTER_LOW_BARS=4000：Node Cluster 完整质量门槛（250日×16根/日）
            # 个股详情实时计算使用 Node Cluster 完整质量门槛（4000）；
            # 批量服务使用 500 作为最低可行门槛（degraded）。
            return ChipStatus(
                state="unavailable",
                reasonCode="M15_BARS_INSUFFICIENT",
                reasonText=(
                    f"15 分钟数据不足（{chip.bars15mCount} 根，"
                    f"完整质量需 ≥{NODE_CLUSTER_LOW_BARS}，"
                    f"批量降级门槛 ≥500）"
                ),
                computedAt=None,
            )
        if "profile_empty" in err_lower or "no_valid_peak" in err_lower:
            return ChipStatus(
                state="unavailable",
                reasonCode="NO_VALID_PEAK",
                reasonText="Node Cluster 未生成有效筹码峰",
                computedAt=None,
            )
        # 其他异常
        return ChipStatus(
            state="failed",
            reasonCode="CHIP_JOB_FAILED",
            reasonText=f"筹码计算失败：{chip.error[:200]}",
            computedAt=None,
        )

    # 无 error 但 chip 为 None：未生成有效峰
    if chip.chip is None:
        return ChipStatus(
            state="unavailable",
            reasonCode="NO_VALID_PEAK",
            reasonText="Node Cluster 运行完成但无有效筹码峰",
            computedAt=None,
        )

    # chip 可用
    if chip.chip.available:
        return ChipStatus(
            state="ready",
            reasonCode=None,
            reasonText=None,
            computedAt=None,
        )

    # chip 存在但 available=False：归入 NO_VALID_PEAK
    return ChipStatus(
        state="unavailable",
        reasonCode="NO_VALID_PEAK",
        reasonText="Node Cluster 标记 unavailable 但未提供 error",
        computedAt=None,
    )


def compute_first_pyramid_snapshot(
    bars: pd.DataFrame,
    symbol: str,
    trade_date: str | None = None,
    bars_15m: pd.DataFrame | None = None,
) -> FirstPyramidSnapshot:
    """计算第一金字塔统一快照（SSOT 编排入口；向后兼容包装）。

    [CHANGE-20260729-003] 内部拆分为：
        1. compute_first_pyramid_core_snapshot（core，不含 Node）
        2. compute_chip_consensus_snapshot（chip，独立 hash/version）
        3. assemble_first_pyramid_view（组装为完整视图）

    单股详情、批量、行情列表、盘后 compute 必须复用此函数（或拆分后的子函数）。
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
    # 1. core 快照（不含 Node Cluster）
    core = compute_first_pyramid_core_snapshot(bars, symbol, trade_date)

    # 2. chip 快照（独立路径，失败不阻塞 core）
    chip = compute_chip_consensus_snapshot(
        daily_bars=bars,
        bars_15m=bars_15m,
        trade_date=core.tradeDate,
        n_bars=core.nBars,
        last_bar_index=core.lastBarIndex,
    )

    # 3. 组装完整视图
    return assemble_first_pyramid_view(core, chip)


# =============================================================================
# 历史 SSOT：compute_first_pyramid_history
# =============================================================================


def compute_first_pyramid_history(
    bars: pd.DataFrame,
    symbol: str = "HISTORY",
    output_bars: int = 250,
    include_chip: bool = False,
    bars_15m: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-003] 第一金字塔历史 SSOT 一次计算。

    单股完整可用日线一次输入，一次计算 DSA/SMC/Bollinger/SQZMOM/VolumeContext，
    输出最近 N 个有效日的 daily state 及不可变 events。

    约束（ref/instruction.md §五）：
        - 禁止循环 N 次调用 snapshot；本函数一次计算所有指标 series
        - 历史 DSA 用 lookback=None（完整历史，不截断）
        - 当前 snapshot 的 250-bar 合同保持不变（compute_first_pyramid_snapshot 仍用 DSA_LOOKBACK）
        - 默认 include_chip=False；本轮不读取/写入全市场历史数据

    Args:
        bars: 单股完整可用日线 OHLCV DataFrame（DatetimeIndex）
        symbol: 股票代码（用于 meta）
        output_bars: 输出最近 N 个有效日的 daily state（默认 250）
        include_chip: 是否同时计算 chip_consensus（默认 False）
        bars_15m: 15 分钟 bars（仅 include_chip=True 时使用）

    Returns:
        dict 包含:
        - daily_state: list[dict] 最近 N 个有效日的状态
            字段: bar_index/time/trend_transition/regime_value/regime_strength/
                  dsa_dir_bars/dsa_vwap_dev_pct/
                  swing_bias/internal_bias/active_internal_ob_count/active_swing_ob_count/
                  volatility_phase/momentum_direction/momentum_change/sqzmom_delta/
                  volume_ratio_20/volume_percentile_20/volume_zscore_20/
                  core_factor_ready/history_sufficient/valid_for_market_aggregation/invalid_reason
        - events: list[dict] 不可变事件流（仅最近 output_bars 范围内）
            类型: BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL/SQZ_RELEASE/ZERO_CROSS_*
        - meta: dict 元数据
            symbol/output_bars/n_input/n_output/input_hash/parameter_hash_core/
            algorithm_version_core/include_chip/chip_hash(若 include_chip)
        - chip: ChipConsensusResult | None（仅 include_chip=True）
    """
    if bars is None or bars.empty:
        return {
            "daily_state": [],
            "events": [],
            "meta": {
                "symbol": symbol,
                "output_bars": output_bars,
                "n_input": 0,
                "n_output": 0,
                "error": "bars 为空",
            },
            "chip": None,
        }

    if not isinstance(bars.index, pd.DatetimeIndex):
        bars = bars.copy()
        bars.index = pd.to_datetime(bars.index)

    n_input = len(bars)
    # 数据不足直接返回空
    if n_input < _MIN_BARS_FOR_REQUIRED_DIMS:
        return {
            "daily_state": [],
            "events": [],
            "meta": {
                "symbol": symbol,
                "output_bars": output_bars,
                "n_input": n_input,
                "n_output": 0,
                "error": f"bars 长度 {n_input} 不足（需 >= {_MIN_BARS_FOR_REQUIRED_DIMS}）",
            },
            "chip": None,
        }

    # ===== 一次计算所有指标 series（lookback=None，完整历史）=====
    # 1. DSA history（lookback=None，不截断）
    dsa_config_history: dict[str, Any] = {
        "min_dir_bars": MIN_DIR_BARS,
        "lookback": None,  # 历史用完整数据
    }
    dsa_bundle = compute_dsa_bundle(bars, dsa_config_history)
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    if factor_per_bar is None or factor_per_bar.empty:
        return {
            "daily_state": [],
            "events": [],
            "meta": {
                "symbol": symbol,
                "output_bars": output_bars,
                "n_input": n_input,
                "n_output": 0,
                "error": "DSA factor_per_bar 为空",
            },
            "chip": None,
        }

    # 2. SMC（emit_timeline=True 获取逐 bar 状态）
    opens = bars["open"].astype(float).tolist()
    highs = bars["high"].astype(float).tolist()
    lows = bars["low"].astype(float).tolist()
    closes = bars["close"].astype(float).tolist()
    times = [d.isoformat() for d in bars.index]
    smc_result = compute_smc_pine(
        opens, highs, lows, closes, times, params=None, emit_timeline=True
    )
    smc_timeline = smc_result.get("state_timeline") or []
    ob_lifecycle_events = smc_result.get("ob_lifecycle_events") or []
    smc_events = smc_result.get("events") or []
    equal_highs_lows = smc_result.get("equal_highs_lows") or []

    # 3. SQZMOM（history 路径用 build_momentum_history，不需要 bb_df）
    sqzmom_result = compute_sqzmom_lb(
        opens=np.array(opens, dtype=float),
        highs=np.array(highs, dtype=float),
        lows=np.array(lows, dtype=float),
        closes=np.array(closes, dtype=float),
        params=_FIRST_PYRAMID_PARAMS["sqzmom_config"],
    )
    # 使用 build_momentum_history 获取逐 bar 动量状态 + SQZ_RELEASE 事件
    volume_series = bars["volume"].astype(float).tolist()
    momentum_history = build_momentum_history(
        sqzmom_result, volume_series=volume_series, times=times
    )
    momentum_daily = momentum_history.get("daily_state") or []
    sqz_release_events = momentum_history.get("sqz_release_events") or []
    zero_cross_events = momentum_history.get("momentum_zero_cross_events") or []

    # 4. VolumeContext series
    vc_series = compute_volume_context_series(bars)

    # ===== 组装 daily_state（最近 output_bars 根）=====
    n_total = len(factor_per_bar)
    start_idx = max(0, n_total - output_bars)
    daily_state: list[dict[str, Any]] = []

    # SMC timeline 按 bar_index 索引
    smc_timeline_by_idx = {t["bar_index"]: t for t in smc_timeline}
    momentum_daily_by_idx = {d["bar_index"]: d for d in momentum_daily}

    for i in range(start_idx, n_total):
        row = factor_per_bar.iloc[i]
        bar_idx = i
        bar_time = times[i] if i < len(times) else None

        # DSA 字段
        trend_transition = (
            str(row.get("trend_transition", "NONE"))
            if pd.notna(row.get("trend_transition"))
            else "NONE"
        )
        regime_value = int(row["regime_value"]) if pd.notna(row.get("regime_value")) else 0
        regime_strength = _safe_float(row.get("regime_strength"))
        dsa_dir_bars = int(row["dsa_dir_bars"]) if pd.notna(row.get("dsa_dir_bars")) else 0
        dsa_vwap_dev_pct = _safe_float(row.get("dsa_vwap_dev_pct"))

        # SMC 字段（从 timeline 读取；timeline 缺失时取最后已知值）
        smc_t = smc_timeline_by_idx.get(bar_idx, {})
        swing_bias = int(smc_t.get("swing_bias", 0))
        internal_bias = int(smc_t.get("internal_bias", 0))
        active_internal_ob_count = int(smc_t.get("active_internal_ob_count", 0))
        active_swing_ob_count = int(smc_t.get("active_swing_ob_count", 0))

        # 动量字段（从 momentum_daily 读取）
        m_daily = momentum_daily_by_idx.get(bar_idx, {})
        volatility_phase = m_daily.get("volatility_phase", "no_squeeze")
        momentum_direction = m_daily.get("momentum_direction")
        momentum_change = m_daily.get("momentum_change")
        sqzmom_delta = m_daily.get("sqzmom_delta")

        # VolumeContext 字段（从 vc_series 读取）
        vol_ratio_20 = vol_pct_20 = vol_zscore_20 = None
        if vc_series is not None and not vc_series.empty and i < len(vc_series):
            vc_row = vc_series.iloc[i]
            vol_ratio_20 = _safe_float(vc_row.get("volume_ratio_20"))
            vol_pct_20 = _safe_float(vc_row.get("volume_percentile_20"))
            vol_zscore_20 = _safe_float(vc_row.get("volume_zscore_20"))

        # [P0-1 修复 2026-07-29] 聚合有效性逐 bar 判定（禁止用完整 n_input 判断过去日期）
        # available_bars = i + 1（截至当前 bar 的可用 bar 数，含当前 bar）
        available_bars = i + 1

        # 各维度独立 readiness（禁止依赖筹码，禁止用默认字符串充当 ready）
        # trend_ready: DSA regime_strength 非 None 且 regime_value 非 None
        trend_ready = regime_strength is not None and regime_value is not None
        # structure_ready: SMC timeline 真实存在（非空字典）且 swing_bias/internal_bias 为有效数值
        #   禁止默认字符串 'null'/'unknown' 充当 ready
        smc_timeline_valid = bool(smc_t) and "swing_bias" in smc_t
        structure_ready = smc_timeline_valid
        # momentum_ready: volatility_phase/momentum_direction 真实有效
        #   禁止默认 'no_squeeze'/'null'/'unknown' 充当 ready
        momentum_ready = (
            volatility_phase is not None
            and volatility_phase not in ("no_squeeze", "null", "unknown", "")
            and momentum_direction is not None
            and momentum_direction not in ("null", "unknown", "")
        )
        # volume20_ready: 20日量能上下文可用
        volume20_ready = vol_ratio_20 is not None and vol_pct_20 is not None
        # volume200_ready: 当前 i 足够支持 200 日均量（i+1 >= 200）
        #   实际 vol_ratio_200 由 vc_series 计算时已判定；此处用 i+1 >= 200 作为最低门槛
        volume200_ready = available_bars >= 200

        # history_sufficient: 截至当前 bar 的可用 bar 数 >= 60（最小必选维度要求）
        history_sufficient = available_bars >= _MIN_BARS_FOR_REQUIRED_DIMS
        # core_factor_ready: 三大维度因子均有效（非 None，非默认字符串）
        core_factor_ready = trend_ready and structure_ready and momentum_ready
        # valid_for_market_aggregation: 当前 bar 有效且不处于 warmup
        valid_for_market_aggregation = (
            core_factor_ready and history_sufficient and available_bars >= _MIN_BARS_FOR_REQUIRED_DIMS
        )
        invalid_reason: str | None = None
        if not valid_for_market_aggregation:
            if not core_factor_ready:
                invalid_reason = "core_factor_not_ready"
            elif not history_sufficient:
                invalid_reason = "history_insufficient"
            elif available_bars < _MIN_BARS_FOR_REQUIRED_DIMS:
                invalid_reason = "warmup_period"

        daily_state.append({
            "bar_index": bar_idx,
            "time": bar_time,
            # 趋势原子特征
            "trend_transition": trend_transition,
            "regime_value": regime_value,
            "regime_strength": regime_strength,
            "dsa_dir_bars": dsa_dir_bars,
            "dsa_vwap_dev_pct": dsa_vwap_dev_pct,
            # SMC 原子特征
            "swing_bias": swing_bias,
            "internal_bias": internal_bias,
            "active_internal_ob_count": active_internal_ob_count,
            "active_swing_ob_count": active_swing_ob_count,
            # 动量原子特征
            "volatility_phase": volatility_phase,
            "momentum_direction": momentum_direction,
            "momentum_change": momentum_change,
            "sqzmom_delta": sqzmom_delta,
            # VolumeContext 摘要
            "volume_ratio_20": vol_ratio_20,
            "volume_percentile_20": vol_pct_20,
            "volume_zscore_20": vol_zscore_20,
            # [P0-1] 逐 bar readiness 细分（禁止依赖筹码，禁止用完整 n_input 判断过去日期）
            "available_bars": available_bars,
            "trend_ready": trend_ready,
            "structure_ready": structure_ready,
            "momentum_ready": momentum_ready,
            "volume20_ready": volume20_ready,
            "volume200_ready": volume200_ready,
            # 聚合有效性（不依赖筹码）
            "core_factor_ready": core_factor_ready,
            "history_sufficient": history_sufficient,
            "valid_for_market_aggregation": valid_for_market_aggregation,
            "invalid_reason": invalid_reason,
        })

    # ===== 收集最近 output_bars 范围内的事件（不可变）=====
    events: list[dict[str, Any]] = []

    # SMC BOS/CHoCH 事件
    for evt in smc_events:
        evt_idx = evt.get("confirmed_index")
        if evt_idx is not None and start_idx <= evt_idx < n_total:
            events.append({
                "type": evt.get("type", ""),
                "bar_index": int(evt_idx),
                "time": evt.get("confirmed_time"),
                "direction": "up" if evt.get("bullish") else "down" if evt.get("bullish") is False else None,
                "internal": bool(evt.get("internal", False)),
                "anchor_index": evt.get("anchor_index"),
                "level": _safe_float(evt.get("level")),
            })

    # OB 生命周期事件
    for ob_evt in ob_lifecycle_events:
        # CREATED/ENTERED/MITIGATED 分别用不同 index 字段定位
        evt_type = ob_evt.get("type", "")
        if evt_type == "OB_CREATED":
            evt_idx = ob_evt.get("confirmed_index")
        elif evt_type == "OB_ENTERED":
            evt_idx = ob_evt.get("enter_index")
        elif evt_type == "OB_MITIGATED":
            evt_idx = ob_evt.get("mitigated_index")
        else:
            evt_idx = None
        if evt_idx is not None and start_idx <= evt_idx < n_total:
            events.append({
                "type": evt_type,
                "bar_index": int(evt_idx),
                "time": ob_evt.get("enter_time") or ob_evt.get("mitigated_time") or ob_evt.get("confirmed_time"),
                "direction": "up" if ob_evt.get("bias") == 1 else "down" if ob_evt.get("bias") == -1 else None,
                "internal": bool(ob_evt.get("internal", False)),
                "anchor_index": ob_evt.get("anchor_index"),
                "bar_high": _safe_float(ob_evt.get("bar_high")),
                "bar_low": _safe_float(ob_evt.get("bar_low")),
                "structure_level": ob_evt.get("structure_level"),
            })

    # EQH/EQL 事件
    for eq in equal_highs_lows:
        evt_idx = eq.get("confirmed_index")
        if evt_idx is not None and start_idx <= evt_idx < n_total:
            events.append({
                "type": eq.get("type", ""),
                "bar_index": int(evt_idx),
                "time": eq.get("confirmed_time"),
                "direction": "up" if eq.get("type") == "EQH" else "down" if eq.get("type") == "EQL" else None,
                "anchor_index": eq.get("anchor_index"),
                "level": _safe_float(eq.get("level")),
            })

    # SQZ_RELEASE 事件
    for sqz_evt in sqz_release_events:
        evt_idx = sqz_evt.get("bar_index")
        if evt_idx is not None and start_idx <= evt_idx < n_total:
            events.append({
                "type": "SQZ_RELEASE",
                "bar_index": int(evt_idx),
                "time": sqz_evt.get("time"),
                "direction": sqz_evt.get("direction"),
                "squeeze_start_index": sqz_evt.get("squeeze_start_index"),
                "squeeze_length": sqz_evt.get("squeeze_length"),
                "release_volume_ratio": sqz_evt.get("release_volume_ratio"),
            })

    # 动量零轴穿越事件
    for zc_evt in zero_cross_events:
        evt_idx = zc_evt.get("bar_index")
        if evt_idx is not None and start_idx <= evt_idx < n_total:
            events.append({
                "type": zc_evt.get("type", ""),
                "bar_index": int(evt_idx),
                "time": zc_evt.get("time"),
                "from_val": zc_evt.get("from_val"),
                "to_val": zc_evt.get("to_val"),
            })

    # 事件按 bar_index 升序排序
    events.sort(key=lambda e: e.get("bar_index", 0))

    # ===== meta =====
    input_hash = _compute_input_hash(bars)
    parameter_hash_core = _compute_core_parameter_hash()

    meta: dict[str, Any] = {
        "symbol": symbol,
        "output_bars": output_bars,
        "n_input": n_input,
        "n_output": len(daily_state),
        "start_bar_index": start_idx,
        "end_bar_index": n_total - 1,
        "input_hash": input_hash,
        "parameter_hash_core": parameter_hash_core,
        "algorithm_version_core": FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        "include_chip": include_chip,
    }

    # ===== 可选 chip 计算（默认 False）=====
    chip_result: ChipConsensusResult | None = None
    if include_chip:
        trade_date = bars.index[-1].date().isoformat()
        chip_result = compute_chip_consensus_snapshot(
            daily_bars=bars,
            bars_15m=bars_15m,
            trade_date=trade_date,
            n_bars=n_input,
            last_bar_index=n_input - 1,
        )
        meta["chip_hash"] = chip_result.chipHash
        meta["chip_algorithm_version"] = CHIP_CONSENSUS_ALGORITHM_VERSION
        meta["chip_error"] = chip_result.error

    return {
        "daily_state": daily_state,
        "events": events,
        "meta": meta,
        "chip": chip_result,
    }


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


# =============================================================================
# [P0-symbol合同 2026-07-30] 公共 symbol 规范化 adapter
# =============================================================================


def serialize_first_pyramid_for_instrument(
    payload: dict[str, Any],
    symbol: str,
) -> dict[str, Any]:
    """第一金字塔公共 symbol 规范化 adapter。

    [P0-symbol合同 2026-07-30] 根因修复：
    - 旧生产路径 compute_feature_snapshot_for_date / compute_review_core_for_trade_date
      曾把 str(instrument_id) 写入 FirstPyramidSnapshot.symbol，API 原样返回，
      前端比较 300369 与 UUID 失败。
    - 新写入数据已修复（instrument_symbol 参数），但旧已发布快照可能仍含 UUID。
    - 本 adapter 在 API 返回前统一校验/覆盖公共 symbol 为规范化股票代码。

    规则：
    - symbol 必须是非空字符串（规范化6位A股代码，如 '300369'）
    - payload.symbol 为 UUID 时：覆盖为 symbol，附 legacy_symbol_repaired=True 诊断
    - payload.symbol 为空/缺失时：覆盖为 symbol
    - payload.symbol 已等于 symbol：原样返回
    - 禁止原地修改输入 payload（deep copy）

    Args:
        payload: 第一金字塔 dict（来自 snapshot.summary_payload["first_pyramid"] 或实时计算）
        symbol: 规范化股票代码（来自 instruments.symbol）

    Returns:
        新 dict，symbol 字段已规范化；附 legacy_symbol_repaired 诊断（仅在修复时）
    """
    import copy
    import re

    if not isinstance(payload, dict):
        return payload
    if not symbol or not isinstance(symbol, str):
        # symbol 无效，原样返回（上游应保证非空）
        return payload

    # 规范化 symbol：去除可能的 .SZ/.SH 后缀，保留6位数字
    normalized = symbol.strip()
    # A股代码格式：6位数字（可能含后缀如 300369.SZ）
    m = re.match(r"^(\d{6})", normalized)
    if m:
        normalized = m.group(1)

    result = copy.deepcopy(payload)
    current = result.get("symbol")
    # 判断是否为 UUID（36字符含连字符）或与 normalized 不一致
    uuid_like = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
    if not current or uuid_like.match(str(current)) or str(current) != normalized:
        result["symbol"] = normalized
        # 附诊断标记（不修改ORM JSON，只在此返回 dict 中）
        result["_legacy_symbol_repaired"] = True
    return result
