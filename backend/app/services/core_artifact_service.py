"""CoreComputationArtifact 统一编排入口（compute-once SSOT）。

[CHANGE-20260805-CP4A / P0-03 + P0-04 + P0-05]
本模块是 **canonical 盘后主链** 唯一算法调用入口：
    CoreRunContext
    → compute_core_artifact()
        ├─ canonical daily frame
        ├─ DSA result（compute_dsa_bundle，一次）
        ├─ SMC result（compute_smc_pine，一次）
        ├─ momentum result（Bollinger + SQZMOM，各一次）
        ├─ VolumeContext（一次）
        ├─ FirstPyramidCoreSnapshot（纯 builder，不调用 kernel）
        ├─ DSAProjectionPayload 所需 metrics/visual（从 raw dsa_bundle 提取，不解析中文摘要）
        └─ StateEventCandidates（从 SMC/结构事件提取）

消费者只做投影与持久化，禁止再调用任何算法 kernel：
    CoreComputationArtifact
    ├─ stock_core persistence
    ├─ StrategyResult projection（dsa_projection_service.map_dsa_projection）
    ├─ state events
    └─ API read model

硬约束：
- DSA/SMC/Bollinger/SQZMOM/VolumeContext 在每股只计算一次（ComputeOnceDiagnostics 计数）。
- First Pyramid **禁止**再调用算法 kernel（已收敛为纯 builder）。
- dsa_vwap/regime/anchor 等 DSA 投影字段必须从 raw dsa_bundle 提取并写入 artifact，
  禁止从中文摘要反解析（P0-05 round-trip）。
- 本模块为纯计算，不连接数据库，可纯单元测试。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.services.core_run_context import (
    CORE_ARTIFACT_SCHEMA_VERSION,
    ComputeOnceDiagnostics,
    CoreComputationArtifact,
    CoreRunContext,
)
from app.services.first_pyramid_service import (
    FirstPyramidRawResults,
    build_first_pyramid_core_snapshot,
)

logger = logging.getLogger(__name__)


def _extract_dsa_metrics(dsa_bundle: dict[str, Any]) -> dict[str, Any]:
    """从 raw dsa_bundle 提取 DSA projection 标量指标。

    优先取 last_row_metrics，仅保留 `dsa_projection_service.DSA_PROJECTION_METRIC_KEYS`
    契约内的 keys（与 projection 必需指标门禁对齐），保证 map_dsa_projection 的
    dsa_dir_bars/regime_value/dsa_vwap 等必需键齐全。禁止解析中文摘要。
    """
    from app.services.dsa_projection_service import DSA_PROJECTION_METRIC_KEYS

    last_row = dsa_bundle.get("last_row_metrics") or {}
    if not isinstance(last_row, dict):
        return {}
    return {
        k: _json_safe_value(last_row[k])
        for k in DSA_PROJECTION_METRIC_KEYS if k in last_row
    }


def _json_safe_value(val: Any) -> Any:
    """递归把 numpy 标量/数组等转换为 JSON 可序列化的原生 Python 类型。

    [CHANGE-20260806 / PG-暴露缺陷] 真实盘后链的 payload/visual 会含 numpy int64/float64，
    psycopg JSONB 序列化失败（Object of type int64 is not JSON serializable）。
    """
    if isinstance(val, dict):
        return {k: _json_safe_value(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_safe_value(v) for v in val]
    if isinstance(val, np.integer):
        return int(val)
    if isinstance(val, np.floating):
        f = float(val)
        return f if np.isfinite(f) else None
    if isinstance(val, np.bool_):
        return bool(val)
    if isinstance(val, np.ndarray):
        return [_json_safe_value(v) for v in val.tolist()]
    if isinstance(val, pd.Timestamp):
        return val.isoformat()
    return val


def _extract_dsa_visual(dsa_bundle: dict[str, Any]) -> dict[str, Any]:
    """从 raw dsa_bundle 提取 DSA 图表字段（P0-05 round-trip 直接映射）。"""
    last_row = dsa_bundle.get("last_row_metrics") or {}
    visual: dict[str, Any] = {}
    if "dsa_vwap" in last_row:
        visual["dsa_vwap"] = _json_safe_value(last_row["dsa_vwap"])
    factor_per_bar = dsa_bundle.get("factor_per_bar")
    if isinstance(factor_per_bar, pd.DataFrame) and not factor_per_bar.empty:
        last = factor_per_bar.iloc[-1]
        for col in ("regime_id", "anchor_time", "dsa_vwap"):
            if col in last and pd.notna(last[col]):
                visual[col] = (
                    last[col].isoformat()
                    if isinstance(last[col], pd.Timestamp)
                    else _json_safe_value(last[col])
                )
    if dsa_bundle.get("pivot_labels"):
        visual["pivot_labels"] = _json_safe_value(dsa_bundle["pivot_labels"])
    if dsa_bundle.get("anchor"):
        visual["anchor"] = _json_safe_value(dsa_bundle["anchor"])
    return visual


def _extract_state_events(smc_result: dict[str, Any]) -> list[dict[str, Any]]:
    """从 SMC 结构结果提取结构事件候选（结构完成/破坏）。

    当前作为轻量事件候选，由下游 state-event 消费。若 SMC 结果不含结构化事件，
    返回空列表（不伪造事件）。
    """
    events: list[dict[str, Any]] = []
    if isinstance(smc_result, dict):
        # 优先读取结构化 events（若 kernel 已提供）
        raw_events = smc_result.get("events") or smc_result.get("structure_events")
        if isinstance(raw_events, list):
            for ev in raw_events:
                if isinstance(ev, dict):
                    events.append(dict(ev))
    return events


def compute_core_kernel_bundle(
    daily_frame: pd.DataFrame,
    diagnostics: ComputeOnceDiagnostics | None = None,
) -> Any:
    """计算一次 DSA/SMC/Bollinger/SQZMOM/VolumeContext 的 raw bundle（P0-03 唯一 kernel owner）。

    [CHANGE-20260805-CP4A-CP3] 这是 canonical 主链**唯一**调用算法 kernel 的公开入口，
    structural adapter 与 compute_core_artifact 共享同一份 bundle，禁止上层调用私有
    `_compute_first_pyramid_raw_results`。返回 FirstPyramidRawResults。

    [CHANGE-20260806-005 / Phase 1 / PC-02] 传入 run-scoped diagnostics 时，五类 kernel
    计数在实际调用点（`_compute_first_pyramid_raw_results` 内）递增；不传则保持兼容不计数。
    """
    from app.services.first_pyramid_service import _compute_first_pyramid_raw_results

    return _compute_first_pyramid_raw_results(daily_frame, diagnostics)


def compute_core_artifact(
    *,
    context: CoreRunContext,
    instrument_id: Any,
    symbol: str,
    daily_frame: pd.DataFrame,
    input_hash: str,
    bars_hash: str,
    adj_factor_hash: str,
    precomputed_raw: Any | None = None,
) -> CoreComputationArtifact:
    """单股 core 统一编排：每股各算法只计算一次，产出 CoreComputationArtifact。

    这是 canonical 盘后主链的唯一算法入口。First Pyramid / DSA projection /
    state events 全部消费本函数产出的 raw results，禁止再调用算法 kernel。

    Args:
        context: CoreRunContext（run 级唯一事实源，冻结 universe/config/版本；
            run-scoped compute-once 计数由 context.compute_diagnostics 承载）
        instrument_id: 标的 ID
        symbol: 规范化 6 位股票代码
        daily_frame: canonical 日线 OHLCV DataFrame（DatetimeIndex）
        input_hash / bars_hash / adj_factor_hash: lineage 输入 hash
        precomputed_raw: 可选，已算好的 FirstPyramidRawResults（P0-03 compute-once：
            与 structural factors 共享同一组原始结果时传入，避免 kernel 二次计算）

    Returns:
        CoreComputationArtifact

    Raises:
        ValueError: daily_frame 为空或数据不足，无法计算 core
    """
    # 1. canonical frame 消费计数（run-scoped，来自 context）
    diagnostics = context.compute_diagnostics
    diagnostics.bump("canonical_frame_build")

    if daily_frame is None or daily_frame.empty:
        raise ValueError("compute_core_artifact: daily_frame 为空")

    # 2. 各算法 kernel 调用一次（compute-once）；precomputed_raw 提供时复用不重算
    if precomputed_raw is not None:
        raw = precomputed_raw
    else:
        raw = _compute_raw_results(daily_frame, diagnostics)

    # 3. First Pyramid：纯 builder，不调用 kernel
    n_bars = len(daily_frame)
    last_bar_index = n_bars - 1
    trade_date = (
        daily_frame.index[-1].date().isoformat()
    )
    fp_core = build_first_pyramid_core_snapshot(
        dsa_result=raw.dsa_bundle,
        smc_result=raw.smc_result,
        momentum_result={"bb_df": raw.bb_df, "sqzmom_result": raw.sqzmom_result},
        volume_context=raw.vc_series,
        bars=daily_frame,
        symbol=symbol,
        trade_date=trade_date,
        n_bars=n_bars,
        last_bar_index=last_bar_index,
    )

    # 4. DSA projection metrics/visual（从 raw dsa_bundle 提取，P0-05）
    dsa_metrics = _extract_dsa_metrics(raw.dsa_bundle)
    dsa_visual = _extract_dsa_visual(raw.dsa_bundle)

    # 5. state-event candidates（从 SMC）
    state_events = _extract_state_events(raw.smc_result)

    # 6. 组装 artifact：availability 取各维度 DimensionResult.availability
    #    （available/unavailable），归一化为 quality-gate 的 ready/unavailable 语义
    #    （is_available 要求 ready；DimensionResult 用 available）。
    def _norm_avail(dim: Any) -> str:
        raw = str(getattr(dim, "availability", "unavailable"))
        return "ready" if raw == "available" else raw

    availability: dict[str, str] = {
        "trend": _norm_avail(fp_core.trend),
        "structure": _norm_avail(fp_core.structure),
        "momentum": _norm_avail(fp_core.momentum),
    }

    # lineage：来自 CoreRunContext（run 级冻结）与输入 hash
    algorithm_versions = dict(context.algorithm_versions or {})
    if not algorithm_versions:
        algorithm_versions = {
            "dsa": _ALGO_DSA,
            "smc": _ALGO_SMC,
            "momentum": _ALGO_MOMENTUM,
        }

    # [CHANGE-20260806 / PG-暴露缺陷] 全 payload/visual/events 递归 JSON 安全化：
    # first_pyramid 的 model_dump 与 DSA 提取可能含 numpy/Timestamp 标量，psycopg JSONB
    # 序列化会失败（Object of type int64/Timestamp is not JSON serializable）。
    artifact = CoreComputationArtifact(
        instrument_id=instrument_id,
        trade_date=(
            date.fromisoformat(trade_date)
        ),
        payload=_json_safe_value({
            "first_pyramid": fp_core.model_dump(by_alias=False),
            "dsa": dsa_metrics,
        }),
        visual=_json_safe_value(dsa_visual),
        events=_json_safe_value(state_events),
        availability=availability,
        hashes={
            "input_hash": input_hash,
            "bars_hash": bars_hash,
            "adj_factor_hash": adj_factor_hash,
            "parameter_hash": context.parameter_hash,
        },
        diagnostics=dict(diagnostics.to_dict()),
        source_core_run_id=context.run_id,
        parameter_hash=context.parameter_hash,
        algorithm_versions=algorithm_versions,
        schema_version=CORE_ARTIFACT_SCHEMA_VERSION,
    )
    return artifact


# 版本常量（回退：正常由 CoreRunContext 注入，这里仅用于 context 无算法版本时）
_ALGO_DSA = "dsa-v1"
_ALGO_SMC = "smc-v1"
_ALGO_MOMENTUM = "momentum-v1"


def _compute_raw_results(
    daily_frame: pd.DataFrame,
    diagnostics: ComputeOnceDiagnostics,
) -> FirstPyramidRawResults:
    """调用各算法 kernel 一次，累计 compute-once 计数。

    复用 first_pyramid_service 的 raw-results 计算，并将 run-scoped diagnostics 传入，
    使五类 kernel（volume_context/dsa/smc/bollinger/sqzmom）计数在实际调用点递增，
    避免上层根据结果存在与否“补计数”（P0-03/Step-3 + Phase 1 PC-02）。
    """
    from app.services.first_pyramid_service import _compute_first_pyramid_raw_results

    return _compute_first_pyramid_raw_results(daily_frame, diagnostics)
