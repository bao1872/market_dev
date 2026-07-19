"""Canonical 统一 adapter 层 — 把签名各异的 kernel 包为统一 (bars: pd.DataFrame, **kwargs) 签名。

CHANGE-20260718-007 S3.2 + CHANGE-20260719-001 §二：
每个算法使用统一 adapter 签名，使 CanonicalComputationService
能经 compute_with_mdas() 统一调度，无需调用方了解各 kernel 的参数差异。

设计：
- adapter 是薄封装，不改算法公式（SMC/DSA/Node 不动）
- adapter 接受 MDAS 返回的 pd.DataFrame（含 open/high/low/close/volume/amount/adj_factor 列）
- adapter 提取所需列后调用真实 kernel
- adapter 返回值即 kernel 返回值（Canonical 计算 result_hash）

统一签名约定（单 timeframe 算法）：
    def compute_<algo>_adapter(bars: pd.DataFrame, **params) -> Any

多 timeframe 算法（如 node_cluster）：
    def compute_<algo>_adapter(daily_bars: pd.DataFrame, bars_15m: pd.DataFrame, **params) -> Any

异步编排算法（如 temporal_features / snapshot_derived_features）：
    async def compute_<algo>_adapter(session, instrument_id, ...) -> Any
    （内部自行调 MDAS 获取 bars，调用方用 CanonicalComputationService.compute() 直接调度）

注册：在 algorithm_registry.py 把 kernel_entrypoint 指向 adapter，并设
migration_status="production_wired"。

新增 adapter 时：
1. 在本文件定义 compute_<algo>_adapter
2. 在 algorithm_registry.py 更新对应 AlgorithmContract 的 kernel_entrypoint + migration_status
3. 运行 test_algorithm_registry_architecture.py::test_wired_algorithms_have_existing_callables 验证
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.indicator_service import compute_macd

logger = logging.getLogger("services.canonical_adapters")


# =============================================================================
# 输入校验辅助
# =============================================================================


def _require_bars(bars: pd.DataFrame | None, algo: str) -> None:
    """校验 bars 非空，否则抛 ValueError。"""
    if bars is None or bars.empty:
        raise ValueError(f"compute_{algo}_adapter: bars 为空，无法计算")


def _require_columns(bars: pd.DataFrame, algo: str, cols: list[str]) -> None:
    """校验 bars 含所需列，否则抛 ValueError。"""
    missing = [c for c in cols if c not in bars.columns]
    if missing:
        raise ValueError(
            f"compute_{algo}_adapter: bars 缺少列 {missing}，实际列={list(bars.columns)}"
        )


def _bars_to_ohlc_lists(bars: pd.DataFrame) -> tuple[list[float], list[float], list[float], list[float]]:
    """从 bars 提取 OHLC 为 list[float]（部分 kernel 需要 list 而非 np.ndarray）。"""
    return (
        bars["open"].to_numpy(dtype=float).tolist(),
        bars["high"].to_numpy(dtype=float).tolist(),
        bars["low"].to_numpy(dtype=float).tolist(),
        bars["close"].to_numpy(dtype=float).tolist(),
    )


def _bars_to_ohlc_arrays(bars: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """从 bars 提取 OHLC 为 np.ndarray（部分 kernel 需要 ndarray）。"""
    return (
        bars["open"].to_numpy(dtype=float),
        bars["high"].to_numpy(dtype=float),
        bars["low"].to_numpy(dtype=float),
        bars["close"].to_numpy(dtype=float),
    )


# =============================================================================
# MACD（已接线，保留原签名）
# =============================================================================


def compute_macd_adapter(
    bars: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """MACD 统一 adapter — 从 bars 提取 close 后调用 compute_macd。

    统一签名：接受 MDAS 返回的 DataFrame（任意周期：1d/15m/1h/1w/1mo），
    提取 close 列为 numpy 数组，转发到 compute_macd
    （A 股 2× 版本：DIF=EMA(fast)-EMA(slow), DEA=EMA(DIF,signal), HIST=2*(DIF-DEA)）。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 "close" 列（周期由合同 input_timeframes 约束）
        fast: 快线周期（默认 12）
        slow: 慢线周期（默认 26）
        signal: 信号线周期（默认 9）

    Returns:
        dict: macd_dif / macd_dea / macd_hist 数组（与 compute_macd 返回一致）

    Raises:
        ValueError: bars 为空或缺少 close 列
    """
    _require_bars(bars, "macd")
    _require_columns(bars, "macd", ["close"])
    closes = bars["close"].to_numpy(dtype=float)
    return compute_macd(closes, fast=fast, slow=slow, signal=signal)


# =============================================================================
# Bollinger Bands
# =============================================================================


def compute_bollinger_adapter(
    bars: pd.DataFrame,
    win: int = 20,
    k: float = 2.0,
) -> dict[str, list[float | None]]:
    """Bollinger Bands 统一 adapter — wraps bollinger_features_plotly.bollinger。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 "close" 列
        win: BB 窗口（默认 20）
        k: BB 标准差倍数（默认 2.0）

    Returns:
        dict: bb_mid / bb_upper / bb_lower 数组（NaN 转 None，便于 JSON 序列化）
    """
    from app.strategy_assets.algorithms.features.bollinger_features_plotly import bollinger

    _require_bars(bars, "bollinger")
    _require_columns(bars, "bollinger", ["close"])
    mid, upper, lower = bollinger(bars, win, k)
    return {
        "bb_mid": [None if pd.isna(v) else float(v) for v in mid.tolist()],
        "bb_upper": [None if pd.isna(v) else float(v) for v in upper.tolist()],
        "bb_lower": [None if pd.isna(v) else float(v) for v in lower.tolist()],
    }


# =============================================================================
# SQZMOM (Squeeze Momentum)
# =============================================================================


def compute_sqzmom_adapter(
    bars: pd.DataFrame,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SQZMOM 统一 adapter — wraps sqzmom_lb.compute_sqzmom_lb。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 open/high/low/close 列
        params: SQZMOM 参数（默认 None → 内部默认值）

    Returns:
        dict: val/sqzOn/sqzOff/noSqz/bcolor/scolor/params/_debug_bb_kc
    """
    from app.strategy_assets.algorithms.features.sqzmom_lb import compute_sqzmom_lb

    _require_bars(bars, "sqzmom")
    _require_columns(bars, "sqzmom", ["open", "high", "low", "close"])
    opens, highs, lows, closes = _bars_to_ohlc_arrays(bars)
    return compute_sqzmom_lb(opens, highs, lows, closes, params)


# =============================================================================
# Breakout (Trendlines with Breaks)
# =============================================================================


def compute_breakout_adapter(
    bars: pd.DataFrame,
    *,
    length: int = 14,
    mult: float = 1.0,
    calc_method: str = "Atr",
    backpaint: bool = True,
    show_ext: bool = True,
    up_color: str = "#00897b",
    down_color: str = "#e53935",
) -> pd.DataFrame:
    """Breakout 统一 adapter — wraps trendlines_with_breaks_luxalgo.trendlines_with_breaks。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 close/high/low 列
        length: pivot 长度（默认 14）
        mult: 斜率倍数（默认 1.0）
        calc_method: 计算方法 Atr/Stdev/Linreg（默认 Atr）
        其他参数: 透传 TLBConfig

    Returns:
        pd.DataFrame: 含 upper/lower/slope_ph/slope_pl/upos/dnos 等列
    """
    from app.strategy_assets.algorithms.features.trendlines_with_breaks_luxalgo import (
        TLBConfig,
        trendlines_with_breaks,
    )

    _require_bars(bars, "breakout")
    _require_columns(bars, "breakout", ["close", "high", "low"])
    cfg = TLBConfig(
        length=length,
        mult=mult,
        calc_method=calc_method,
        backpaint=backpaint,
        show_ext=show_ext,
        up_color=up_color,
        down_color=down_color,
    )
    return trendlines_with_breaks(bars, cfg)


# =============================================================================
# Participation (S/R Event Factor Lab)
# =============================================================================


def compute_participation_adapter(
    bars: pd.DataFrame,
    *,
    pivot_len: int = 10,
    use_prev_confirmed_level: bool = True,
    low_zone_thresholds: tuple[float, ...] = (0.25, 0.35, 0.50),
    low_zone_windows: tuple[int, ...] = (5, 10),
    horizons: tuple[int, ...] = (1, 3, 5, 10, 20),
    cluster_lookback: int = 120,
    cluster_tolerance_pct: float = 0.015,
    cluster_tolerance_atr: float = 0.50,
    strong_cluster_count: int = 3,
    strong_cluster_score: float = 3.0,
) -> pd.DataFrame:
    """Participation 统一 adapter — wraps sr_event_factor_lab.compute_sr_factor_lab。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 open/high/low/close 列
        其他参数: 透传 LabConfig

    Returns:
        pd.DataFrame: 含 pvt_high_confirm/pvt_low_confirm/resistance_active/support_active 等列
    """
    from app.strategy_assets.algorithms.features.sr_event_factor_lab import (
        LabConfig,
        compute_sr_factor_lab,
    )

    _require_bars(bars, "participation")
    _require_columns(bars, "participation", ["open", "high", "low", "close"])
    cfg = LabConfig(
        pivot_len=pivot_len,
        use_prev_confirmed_level=use_prev_confirmed_level,
        low_zone_thresholds=low_zone_thresholds,
        low_zone_windows=low_zone_windows,
        horizons=horizons,
        cluster_lookback=cluster_lookback,
        cluster_tolerance_pct=cluster_tolerance_pct,
        cluster_tolerance_atr=cluster_tolerance_atr,
        strong_cluster_count=strong_cluster_count,
        strong_cluster_score=strong_cluster_score,
    )
    return compute_sr_factor_lab(bars, cfg)


# =============================================================================
# SMC (Smart Money Concepts)
# =============================================================================


def compute_smc_adapter(
    bars: pd.DataFrame,
    display_bars: int = 250,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SMC 统一 adapter — compute_smc_indicators + adapt_smc_to_display_dto。

    Pine parity 完整计算 + 视图裁剪二合一：
    1. 从 bars 提取 OHLC + times
    2. 调用 compute_smc_indicators（委托 smc_pine_core.compute_smc_pine）得到完整结果
    3. 调用 adapt_smc_to_display_dto 裁成展示窗口 DTO

    FVG 完全排除（不计算/不返回/不渲染）。

    Args:
        bars: MDAS 返回的 DataFrame，必须含 open/high/low/close 列，
              index 为 DatetimeIndex（用于提取 times）
        display_bars: 展示窗口 bar 数（与 indicators API 的 bars 参数同源，默认 250）
        params: SMC 参数覆盖（None → DEFAULT_PARAMS，已逐项匹配 Pine）

    Returns:
        dict: 裁剪后的展示 DTO（events/order_blocks/equal_highs_lows/trailing/
              swing_bias/pivots/time/params/view）
    """
    from app.services.smc_view_adapter import adapt_smc_to_display_dto
    from app.strategy_assets.algorithms.features.smc_indicator import (
        compute_smc_indicators,
    )

    _require_bars(bars, "smc")
    _require_columns(bars, "smc", ["open", "high", "low", "close"])
    opens, highs, lows, closes = _bars_to_ohlc_lists(bars)
    times = [idx.isoformat() for idx in bars.index]
    full_result = compute_smc_indicators(opens, highs, lows, closes, times, params)
    return adapt_smc_to_display_dto(full_result, display_bars)


# =============================================================================
# Node Cluster / Volume Profile（多 timeframe）
# =============================================================================


def compute_node_cluster_adapter(
    daily_bars: pd.DataFrame,
    bars_15m: pd.DataFrame,
    *,
    adjustment_as_of: str | None = None,
    adj_factor_hash: str | None = None,
) -> Any:
    """Node Cluster 统一 adapter — wraps node_cluster_engine.compute_node_cluster_profile。

    多 timeframe 算法：需要日线 + 15m 两个输入。调用方通过
    CanonicalComputationService.compute() 直接调度（不能用 compute_with_mdas，
    因后者只取单一 timeframe）。

    语义合同（来自 indicator_semantics.py，禁止弱化）：
    - 1d 最近 250 根已完成 qfq 日线决定价格范围
    - 15m 最近 4000 根已完成 qfq bar 分配成交量
    - 三链同 stock/as_of/输入 → profile_hash 必须一致

    Args:
        daily_bars: 日线 OHLCV DataFrame（DatetimeIndex，completed qfq）
        bars_15m: 15m OHLCV DataFrame（DatetimeIndex，completed qfq）
        adjustment_as_of: 复权锚点业务日（ISO 字符串，盘后链传入）
        adj_factor_hash: 复权因子 hash（盘后链传入，用于诊断）

    Returns:
        NodeClusterProfileResult（不可变）
    """
    from app.services.node_cluster_engine import compute_node_cluster_profile

    if daily_bars is None or daily_bars.empty:
        raise ValueError("compute_node_cluster_adapter: daily_bars 为空")
    if bars_15m is None or bars_15m.empty:
        raise ValueError("compute_node_cluster_adapter: bars_15m 为空")
    return compute_node_cluster_profile(
        daily_bars,
        bars_15m,
        adjustment_as_of=adjustment_as_of,
        adj_factor_hash=adj_factor_hash,
    )


# =============================================================================
# DSA (Dynamic Swing Algorithm)
# =============================================================================


def compute_dsa_adapter(
    bars: pd.DataFrame,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """DSA 统一 adapter — wraps dsa_selector.compute_dsa_bundle。

    DSA 统一计算入口，封装 VWAP 计算 + 后处理 + metrics 提取。
    因子与可视化契约分离：factor_per_bar 仅供选股/metrics，
    visual_segments/pivot_labels/anchor 仅供前端渲染。

    Args:
        bars: 日线 OHLCV DataFrame（DatetimeIndex，必须含 open/high/low/close/volume/amount）
        config: 运行时配置字典（None → 默认空 dict，内部使用 DSAConfig/ATRRopeConfig 默认值）

    Returns:
        dict: factor_per_bar/visual_segments/factor_time/pivot_labels/anchor/
              last_row_metrics/per_bar（数据不足时各字段为空）
    """
    from app.strategy.selectors.dsa_selector import compute_dsa_bundle

    _require_bars(bars, "dsa")
    _require_columns(bars, "dsa", ["open", "high", "low", "close", "volume", "amount"])
    return compute_dsa_bundle(bars, config or {})


# =============================================================================
# Structural Features（单周期因子组）
# =============================================================================


def compute_structural_features_adapter(
    bars: pd.DataFrame,
    timeframe: str = "1d",
    *,
    precomputed_node_cluster: Any | None = None,
) -> dict[str, Any]:
    """Structural Features 统一 adapter — wraps structural_factor_service._compute_all_factors_for_bars。

    计算单周期所有因子组（dsa_segment/swing_position/cost_position/volatility_momentum/
    participation），每组独立异常隔离。

    Args:
        bars: K 线 DataFrame（None/empty 时返回空因子结构）
        timeframe: 周期标识（"1d" / "15m" 等）
        precomputed_node_cluster: 预计算的 Node Cluster Profile（仅盘后 primary 1d 链路
            由 feature_snapshot_service 注入）。None 时 cost_position 走单周期 VP。

    Returns:
        dict: dsa_segment/swing_position/cost_position/volatility_momentum/participation
              （每组失败时为 None，并附带 degraded_reasons）
    """
    from app.services.structural_factor_service import _compute_all_factors_for_bars

    if bars is None or bars.empty:
        return {
            "dsa_segment": None,
            "swing_position": None,
            "cost_position": None,
            "volatility_momentum": None,
            "participation": None,
            "degraded_reasons": ["bars is None or empty"],
        }
    degraded_reasons: list[str] = []
    warmup_notes: list[str] = []
    result = _compute_all_factors_for_bars(
        bars,
        timeframe,
        degraded_reasons,
        warmup_notes,
        precomputed_node_cluster=precomputed_node_cluster,
    )
    result["degraded_reasons"] = degraded_reasons
    result["warmup_notes"] = warmup_notes
    return result


# =============================================================================
# Primary-Secondary Relation
# =============================================================================


def compute_primary_secondary_relation_adapter(
    primary_factors: dict[str, Any],
    secondary_factors: dict[str, Any],
) -> dict[str, Any]:
    """Primary-Secondary Relation 统一 adapter — wraps structural_factor_service._compute_relation。

    计算 primary vs secondary 对比关系（V1.8 客观关系字段，不输出事件）。

    Args:
        primary_factors: primary 周期因子组（来自 compute_structural_features_adapter）
        secondary_factors: secondary 周期因子组

    Returns:
        dict: primary_dir/secondary_dir/trend_alignment/primary_swing_position/
              secondary_swing_position/primary_slope_atr/secondary_slope_atr/
              secondary_vs_primary_position_delta/notes
    """
    from app.services.structural_factor_service import _compute_relation

    return _compute_relation(primary_factors, secondary_factors)


# =============================================================================
# Temporal Features（异步编排，多 timeframe）
# =============================================================================


async def compute_temporal_features_adapter(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    as_of: str = "latest",
) -> dict[str, Any]:
    """Temporal Features 统一 adapter — wraps temporal_feature_service.compute_temporal_features。

    异步编排算法：内部自行调 MDAS 获取 primary/secondary bars。
    调用方通过 CanonicalComputationService.compute() 直接调度
    （不能用 compute_with_mdas，因后者只取单一 timeframe 且不传 session）。

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权方式（默认 qfq）
        as_of: 截止时间（V1 只支持 latest）

    Returns:
        dict: daily_context/m15_response/derived_relation/meta
    """
    from app.services.temporal_feature_service import compute_temporal_features

    return await compute_temporal_features(
        session,
        instrument_id,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        as_of=as_of,
    )


# =============================================================================
# Snapshot Derived Features（异步编排，盘后 snapshot）
# =============================================================================


async def compute_snapshot_derived_adapter(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    trade_date: date,
    primary_timeframe: str = "1d",
    secondary_timeframe: str = "15m",
    adj: str = "qfq",
    *,
    primary_bars: pd.DataFrame | None = None,
    secondary_bars: pd.DataFrame | None = None,
    source_run_id: uuid.UUID | None = None,
    _diag_sink: dict[str, Any] | None = None,
) -> Any:
    """Snapshot Derived Features 统一 adapter — wraps feature_snapshot_service.compute_feature_snapshot_for_date。

    异步编排算法：为指定 instrument + trade_date 计算 point-in-time 特征快照。
    内部复用 structural/temporal/bollinger 等算法，不复制公式。

    point-in-time：
    - 1d bars 只用 <= trade_date
    - 15m bars 只用 <= trade_date 当日
    - 禁止使用 trade_date 之后数据

    Args:
        session: 异步 DB 会话
        instrument_id: 标的 UUID
        trade_date: 业务交易日
        primary_timeframe: 主周期（默认 1d）
        secondary_timeframe: 副周期（默认 15m）
        adj: 复权方式（默认 qfq）
        primary_bars: 预加载的日线 bars（可选，不传则从 DB 获取）
        secondary_bars: 预加载的 15m bars（可选，不传则从 DB 获取）
        source_run_id: 关联的 snapshot_run_id（可选）
        _diag_sink: 诊断信息收集 dict（可选）

    Returns:
        StockFeatureSnapshot ORM 对象（未写入 DB）
    """
    from app.services.feature_snapshot_service import compute_feature_snapshot_for_date

    return await compute_feature_snapshot_for_date(
        session,
        instrument_id,
        trade_date,
        primary_timeframe=primary_timeframe,
        secondary_timeframe=secondary_timeframe,
        adj=adj,
        primary_bars=primary_bars,
        secondary_bars=secondary_bars,
        source_run_id=source_run_id,
        _diag_sink=_diag_sink,
    )


# =============================================================================
# 自测入口
# =============================================================================


if __name__ == "__main__":
    print("=" * 60)
    print("Canonical Adapters (canonical_adapters.py)")
    print("=" * 60)

    # 构造测试 DataFrame（30 根模拟日线）
    rng = np.random.default_rng(42)
    prices = 100.0 + np.cumsum(rng.standard_normal(30) * 0.5)
    bars = pd.DataFrame(
        {
            "open": prices - 0.1,
            "high": prices + 0.5,
            "low": prices - 0.5,
            "close": prices,
            "volume": rng.integers(1000, 10000, size=30).astype(float),
            "amount": rng.integers(100000, 1000000, size=30).astype(float),
        },
        index=pd.date_range("2026-06-01", periods=30, freq="B"),
    )

    # 测试 macd adapter
    result = compute_macd_adapter(bars)
    assert isinstance(result, dict)
    assert "macd_dif" in result
    assert len(result["macd_dif"]) == 30
    print(f"macd_adapter OK: dif[-1]={result['macd_dif'][-1]:.4f}")

    # 测试 bollinger adapter
    bb_result = compute_bollinger_adapter(bars)
    assert isinstance(bb_result, dict)
    assert "bb_mid" in bb_result and "bb_upper" in bb_result and "bb_lower" in bb_result
    assert len(bb_result["bb_mid"]) == 30
    print(f"bollinger_adapter OK: mid[-1]={bb_result['bb_mid'][-1]:.4f}")

    # 测试 sqzmom adapter
    sqz_result = compute_sqzmom_adapter(bars)
    assert isinstance(sqz_result, dict)
    assert "val" in sqz_result and "sqzOn" in sqz_result
    print(f"sqzmom_adapter OK: val[-1]={sqz_result['val'][-1]}")

    # 测试 smc adapter
    smc_result = compute_smc_adapter(bars, display_bars=30)
    assert isinstance(smc_result, dict)
    assert "events" in smc_result and "order_blocks" in smc_result
    print(f"smc_adapter OK: events={len(smc_result['events'])} ob={len(smc_result['order_blocks'])}")

    # 测试 dsa adapter
    dsa_result = compute_dsa_adapter(bars)
    assert isinstance(dsa_result, dict)
    assert "factor_per_bar" in dsa_result
    print(f"dsa_adapter OK: factor_per_bar rows={len(dsa_result['factor_per_bar'])}")

    # 测试 structural_features adapter
    sf_result = compute_structural_features_adapter(bars, "1d")
    assert isinstance(sf_result, dict)
    assert "dsa_segment" in sf_result
    print(f"structural_features_adapter OK: keys={list(sf_result.keys())}")

    # 测试空 DataFrame 抛 ValueError
    try:
        compute_macd_adapter(pd.DataFrame())
        raise AssertionError("应抛出 ValueError")
    except ValueError as e:
        print(f"empty guard OK: {e}")

    # 确定性验证：相同输入相同输出
    r1 = compute_macd_adapter(bars)
    r2 = compute_macd_adapter(bars)
    assert r1 == r2, "相同输入应得到相同输出"
    print("determinism OK")

    print("OK")
