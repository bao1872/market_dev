"""History-v3 projection owner.

[CHANGE-20260826-001 History-v3] Daily AfterClose 不得重新计算 First Pyramid History。
History-v3 是 canonical Core computation artifact 的 **PURE / DETERMINISTIC 投影**，
只从「已计算一次」的 Core 事实（`StockFeatureSnapshot.summary_payload["first_pyramid_flat"]`
+ Core events）派生 review-history-v3 的 state/event 契约，**绝不**重新运行
DSA / SMC / Bollinger / SQZMOM / VolumeContext kernel。

核心不变量：
    ONE instrument + ONE trade_date + ONE canonical Core input
    = ONE DSA/SMC/BB/SQZMOM/VolumeContext compute
    History-v3 只能投影、不能运行 kernel。

本模块为纯函数（无 IO、无 DB、无 numpy 副作用之外的可变状态），便于：
- 单元测试（spy gate 验证无 kernel 调用）；
- crash/resume（durable artifact 重放得到相同投影）；
- projection parity（同一 Core artifact → stock_core flatten 与 History-v3 重叠字段一致）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.first_pyramid_service import REVIEW_HISTORY_V3_CONTRACT_VERSION

# ---------------------------------------------------------------------------
# Field RTM：Core fp_ flat key → History-v3 canonical state key + adapter
# ---------------------------------------------------------------------------
# 仅列出用户明确点名的字段；其余 fp_ 键以原值透传（passthrough），保证
# 全字段 RTM 一次完整实现，不丢字段。
#
# adapter 形式：
#   None            → 直接透传（字符串/数值/None）
#   "momentum_dir"  → fp_momentum_change 数值 → enhancing / weakening / flat
#   "squeeze_enum"  → fp_squeeze_state 等 Core display → History canonical enum
#   "pct"           → 已是百分比数值，透传
_V3_FIELD_RTM: dict[str, dict[str, Any]] = {
    # regime
    "regime_value": {"src": "fp_regime_value"},
    "regime_strength": {"src": "fp_regime_strength"},
    # dsa
    "dsa_dir_bars": {"src": "fp_dsa_dir_bars"},
    "dsa_vwap_dev_pct": {"src": "fp_dsa_vwap_dev_pct"},
    # segment
    "segment_id": {"src": "fp_segment_id"},
    "segment_direction": {"src": "fp_segment_direction"},
    "segment_bars": {"src": "fp_segment_bars"},
    "segment_change_pct": {"src": "fp_segment_change_pct"},
    "segment_slope": {"src": "fp_segment_slope"},
    "current_vs_prev_volume_mean_ratio": {"src": "fp_segment_volume_ratio"},
    "current_vs_prev_amount_mean_ratio": {"src": "fp_segment_amount_ratio"},
    "current_segment_volume_mean": {"src": "fp_segment_avg_volume"},
    "prev_segment_volume_mean": {"src": "fp_prev_segment_volume"},
    # bias
    "swing_bias": {"src": "fp_swing_direction"},
    "internal_bias": {"src": "fp_internal_direction"},
    # momentum / sqzmom
    "sqzmom_val": {"src": "fp_sqzmom_value"},
    "sqzmom_delta": {"src": "fp_sqzmom_prev", "adapter": "sqzmom_delta"},
    "momentum_direction": {"src": "fp_momentum_direction"},
    "momentum_change": {"src": "fp_momentum_change", "adapter": "momentum_dir"},
    # volume context
    "volume_ratio_20": {"src": "fp_volume_ratio20"},
    "volume_ratio_200": {"src": "fp_volume_ratio200"},
    "volume_percentile_20": {"src": "fp_volume_percentile20"},
    "volume_percentile_200": {"src": "fp_volume_percentile200"},
    "volume_zscore_20": {"src": "fp_volume_zscore20"},
    "volume_zscore_200": {"src": "fp_volume_zscore200"},
    # squeeze volume facts（Core SSOT 已计算一次）
    "squeeze_period_volume_mean": {"src": "fp_squeeze_avg_volume"},
    "release_volume_ratio": {"src": "fp_release_volume_ratio"},
    # momentum_volume_relation（Core vol_divergence owner 已算）
    "momentum_volume_relation": {"src": "fp_momentum_volume_relation"},
}


def _adapt_sqzmom_delta(value: Any) -> str:
    """fp_sqzmom_prev 是「上一根 sqzmom 值」；History-v3 canonical 表达为相对变化语义。

    这里保留原始数值语义，并以 enhancing/weakening/flat 表达方向（纯 adapter，不重算）。
    """
    if value is None:
        return "flat"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "flat"
    if v > 0:
        return "enhancing"
    if v < 0:
        return "weakening"
    return "flat"


def _adapt_momentum_dir(value: Any) -> str:
    """fp_momentum_change 数值 → History canonical momentum delta 语义。"""
    if value is None:
        return "flat"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "flat"
    if v > 0:
        return "enhancing"
    if v < 0:
        return "weakening"
    return "flat"


_ADAPTERS = {
    "momentum_dir": _adapt_momentum_dir,
    "sqzmom_delta": _adapt_sqzmom_delta,
}


def _project_state_payload(core_flat: dict[str, Any]) -> dict[str, Any]:
    """把 Core fp_ flat 投影成 v3 state_payload（全字段 RTM）。"""
    state: dict[str, Any] = {}
    for v3_key, spec in _V3_FIELD_RTM.items():
        src_key = spec["src"]
        raw = core_flat.get(src_key)
        adapter = spec.get("adapter")
        if adapter is not None:
            raw = _ADAPTERS[adapter](raw)
        state[v3_key] = raw
    # 透传其余 fp_ 键（不丢字段；Core canonical 事实全量投影）
    for k, v in core_flat.items():
        if k.startswith("fp_") and k not in {s["src"] for s in _V3_FIELD_RTM.values()}:
            state.setdefault(k, v)
    return state


# ---------------------------------------------------------------------------
# Event projection：Core event → History-v3 event
# ---------------------------------------------------------------------------
# Core 事件来源（first_pyramid_flat 的 fp_*_event_type / fp_structure_event_* /
# fp_momentum_event_* / fp_node_event_*）。History-v3 仅投影，不重判结构事件。
_CORE_EVENT_TYPE_MAP = {
    "BOS": "BOS",
    "CHoCH": "CHoCH",
    "OB_CREATED": "OB_CREATED",
    "OB_ENTERED": "OB_ENTERED",
    "OB_MITIGATED": "OB_MITIGATED",
    "EQH": "EQH",
    "EQL": "EQL",
    "SQZ_RELEASE": "SQZ_RELEASE",
    "ZERO_CROSS_UP": "ZERO_CROSS_UP",
    "ZERO_CROSS_DOWN": "ZERO_CROSS_DOWN",
}


def _project_event_payloads(core_flat: dict[str, Any], trade_date: Any) -> list[dict[str, Any]]:
    """投影 Core canonical 事件为 v3 event_payload 列表。

    仅投影：不运行 build_momentum_history / SMC / DSA 重新生成。
    若 CoreArtifact 在某日缺某事件 → 该事件本就不出现在列表（合法零事件）。
    """
    events: list[dict[str, Any]] = []

    # 结构事件（fp_structure_event_*）
    struct_type = core_flat.get("fp_structure_event_type")
    if struct_type:
        events.append({
            "event_type": _CORE_EVENT_TYPE_MAP.get(struct_type, struct_type),
            "direction": core_flat.get("fp_structure_event_direction"),
            "level": core_flat.get("fp_structure_event_level"),
            "event_date": core_flat.get("fp_structure_event_date") or str(trade_date),
            "price": core_flat.get("fp_structure_event_price"),
            "freshness": core_flat.get("fp_structure_event_freshness"),
            "volume_badge": core_flat.get("fp_structure_event_volume_badge"),
            "source": "core_structure_event",
        })

    # 动量事件（fp_momentum_event_*）
    mom_type = core_flat.get("fp_momentum_event_type")
    if mom_type:
        events.append({
            "event_type": _CORE_EVENT_TYPE_MAP.get(mom_type, mom_type),
            "direction": core_flat.get("fp_momentum_event_direction"),
            "event_date": core_flat.get("fp_momentum_event_date") or str(trade_date),
            "price": core_flat.get("fp_momentum_event_price"),
            "freshness": core_flat.get("fp_momentum_event_freshness"),
            "volume_badge": core_flat.get("fp_momentum_event_volume_badge"),
            "source": "core_momentum_event",
        })

    # 节点事件（fp_node_event_*，chip/participation 模块；投影透传）
    node_type = core_flat.get("fp_node_event_type")
    if node_type:
        events.append({
            "event_type": _CORE_EVENT_TYPE_MAP.get(node_type, node_type),
            "direction": core_flat.get("fp_node_event_direction"),
            "event_date": str(trade_date),
            "price": core_flat.get("fp_node_event_price"),
            "freshness": core_flat.get("fp_node_event_freshness"),
            "source": "core_node_event",
        })

    # SQZ_RELEASE 由 squeeze state 推导（fp_squeeze_state 已释放 + freshness）
    squeeze_state = core_flat.get("fp_squeeze_state")
    if squeeze_state == "已释放":
        events.append({
            "event_type": "SQZ_RELEASE",
            "freshness": core_flat.get("fp_latest_sqz_off_freshness"),
            "event_date": str(trade_date),
            "source": "core_squeeze_state",
        })
    return events


def _compute_lineage_hash(
    instrument_id: str,
    trade_date: Any,
    core_run_id: str | None,
    state: dict[str, Any],
    events: list[dict[str, Any]],
) -> str:
    payload = json.dumps(
        {
            "instrument_id": str(instrument_id),
            "trade_date": str(trade_date),
            "core_run_id": str(core_run_id) if core_run_id else None,
            "state": state,
            "events": events,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_history_v3_projection(
    *,
    core_flat: dict[str, Any],
    instrument_id: str,
    trade_date: Any,
    core_run_id: str | None = None,
) -> dict[str, Any]:
    """纯投影：Core fp_ flat → review-history-v3 projection。

    Args:
        core_flat: StockFeatureSnapshot.summary_payload["first_pyramid_flat"]（Core 已算一次）。
        instrument_id: 标的 ID（str UUID）。
        trade_date: 业务交易日。
        core_run_id: 来源 Core run id（lineage）。

    Returns:
        {
          "contract_version": "review-history-v3",
          "trade_date": str,
          "instrument_id": str,
          "source_core_run_id": str | None,
          "state_payload": {...},
          "event_payloads": [...],
          "availability": {...},
          "lineage": {"hash": ..., "core_run_id": ...},
        }

    纯函数：无 IO、无 kernel 调用。
    """
    if not isinstance(core_flat, dict):
        raise ValueError("core_flat must be a dict (first_pyramid_flat)")

    state_payload = _project_state_payload(core_flat)
    event_payloads = _project_event_payloads(core_flat, trade_date)

    # availability：基于 Core 字段是否缺失/None 推断（投影语义，不重算）
    availability = {
        "core_flat_present": True,
        "squeeze_facts_ready": core_flat.get("fp_squeeze_avg_volume") is not None
        or core_flat.get("fp_release_volume_ratio") is not None
        or core_flat.get("fp_squeeze_state") is not None,
        "momentum_ready": core_flat.get("fp_momentum_direction") is not None,
        "volume_context_ready": core_flat.get("fp_volume_ratio20") is not None,
    }

    lineage_hash = _compute_lineage_hash(
        instrument_id, trade_date, core_run_id, state_payload, event_payloads
    )

    return {
        "contract_version": REVIEW_HISTORY_V3_CONTRACT_VERSION,
        "trade_date": str(trade_date),
        "instrument_id": str(instrument_id),
        "source_core_run_id": str(core_run_id) if core_run_id else None,
        "state_payload": state_payload,
        "event_payloads": event_payloads,
        "availability": availability,
        "lineage": {
            "hash": lineage_hash,
            "core_run_id": str(core_run_id) if core_run_id else None,
        },
    }


def to_history_result_shape(projection: dict[str, Any]) -> dict[str, Any]:
    """把 projection 包装成 ``_persist_history_result`` 期望的 history 结果形状。

    复用既有 daily_state upsert + events immutable insert 写入路径，
    history_contract_version = review-history-v3。
    """
    return {
        "daily_state": [
            {
                "time": projection["trade_date"],
                "state_payload": projection["state_payload"],
                "event_payloads": projection["event_payloads"],
            }
        ],
        "events": projection["event_payloads"],
        "meta": {
            "input_hash": projection["lineage"]["hash"],
            "contract_version": projection["contract_version"],
            "source_core_run_id": projection["source_core_run_id"],
        },
    }
