"""第一金字塔扁平化函数 - 将嵌套 FirstPyramidSnapshot dict 转为 99 个扁平 fp_ 键。

PRD §三 列表视图第一金字塔全量字段要求：
- 99 个键必须全部可选，不能少、不能改名
- 分组：快照7 / 趋势18 / 结构8 / 结构事件21 / 动量13 / 动量事件9 / 筹码10 / 量能13
- null 统一返回 None（前端显示 "—"，不得补 0）
- 纯函数，不连接数据库，可独立单元测试

用法：
    from app.services.first_pyramid_flatten import flatten_first_pyramid

    flat = flatten_first_pyramid(
        summary_payload.get("first_pyramid"),
        calculated_at=snapshot.created_at,
        run_id=snapshot.source_run_id,
        is_stale=is_stale,
    )
    # flat["fp_trade_date"], flat["fp_trend_direction"], ...（共 99 键）

模块自测：
    python -m app.services.first_pyramid_flatten
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.filter_operators import canonicalize_filter_operator
from app.domain.first_pyramid_semantics import (
    MomentumDirection,
    SqueezeState,
    StructureAlignment,
    alignment_display,
    direction_display,
    direction_from_regime,
    momentum_direction_display,
    squeeze_display,
)

# 99 个键的分组定义（用于前端 ColumnRegistry 和后端验证）
FP_FIELD_GROUPS: dict[str, list[str]] = {
    "快照": [
        "fp_trade_date", "fp_data_source", "fp_is_stale", "fp_calculated_at",
        "fp_run_id", "fp_summary", "fp_chip_available",
    ],
    "趋势": [
        "fp_trend_direction", "fp_trend_bars", "fp_dsa_vwap_dev_pct",
        "fp_segment_change_pct", "fp_segment_slope", "fp_trend_strength",
        "fp_segment_start_date", "fp_segment_end_date",
        "fp_segment_start_price", "fp_segment_end_price", "fp_segment_bars",
        "fp_segment_volume_ratio", "fp_segment_amount_ratio",
        "fp_segment_avg_volume", "fp_segment_avg_amount",
        "fp_prev_segment_volume", "fp_prev_segment_amount",
        "fp_vwap_ret_total",
    ],
    "结构": [
        "fp_swing_direction", "fp_internal_direction", "fp_structure_alignment",
        "fp_active_ob_count", "fp_trailing_top", "fp_trailing_bottom",
        "fp_distance_to_trailing_top_pct", "fp_distance_to_trailing_bottom_pct",
    ],
    "结构事件": [
        "fp_structure_event_type", "fp_structure_event_direction",
        "fp_structure_event_level", "fp_structure_event_freshness",
        "fp_structure_event_date", "fp_structure_event_price",
        "fp_structure_event_volume_badge",
        "fp_latest_bos_direction", "fp_latest_bos_freshness", "fp_latest_bos_level",
        "fp_latest_choch_direction", "fp_latest_choch_freshness", "fp_latest_choch_level",
        "fp_latest_ob_direction", "fp_latest_ob_freshness",
        "fp_latest_ob_high", "fp_latest_ob_low",
        "fp_latest_eqh_freshness", "fp_latest_eqh_price",
        "fp_latest_eql_freshness", "fp_latest_eql_price",
    ],
    "动量": [
        "fp_momentum_direction", "fp_squeeze_state", "fp_momentum_change",
        "fp_sqzmom_value", "fp_sqzmom_prev",
        "fp_bb_position", "fp_bb_width",
        "fp_bb_upper", "fp_bb_middle", "fp_bb_lower",
        "fp_squeeze_avg_volume", "fp_release_volume_ratio",
        "fp_momentum_volume_relation",
    ],
    "动量事件": [
        "fp_momentum_event_type", "fp_momentum_event_direction",
        "fp_momentum_event_freshness", "fp_momentum_event_date",
        "fp_momentum_event_price", "fp_momentum_event_volume_badge",
        "fp_latest_sqz_off_freshness",
        "fp_latest_diffusion_direction", "fp_latest_diffusion_freshness",
    ],
    "筹码": [
        "fp_chip_state", "fp_poc_price", "fp_poc_distance_pct",
        "fp_peak_node_count", "fp_vah_price", "fp_val_price",
        "fp_node_event_type", "fp_node_event_direction",
        "fp_node_event_freshness", "fp_node_event_price",
    ],
    "量能": [
        "fp_volume_badge", "fp_volume", "fp_amount", "fp_turnover_rate",
        "fp_volume_ma20", "fp_volume_ma200",
        "fp_volume_ratio20", "fp_volume_ratio200",
        "fp_volume_percentile20", "fp_volume_percentile200",
        "fp_volume_zscore20", "fp_volume_zscore200",
        "fp_volume_ready",
    ],
}

# 扁平化后的全部 99 键（展开 FP_FIELD_GROUPS）
FP_ALL_KEYS: list[str] = [k for keys in FP_FIELD_GROUPS.values() for k in keys]

# 断言恰好 99 键
assert len(FP_ALL_KEYS) == 99, f"FP_ALL_KEYS 应为 99 键，实际 {len(FP_ALL_KEYS)}"


# =============================================================================
# [CHANGE-20260729-005] 99字段全部真实支持筛选排序
# =============================================================================
# 数据源类型（source）决定查询时从哪里取值：
#   "flat"     → summary_payload.first_pyramid_flat.<fp_key>（扁平对象，写入时生成）
#   "chip"     → stock_chip_consensus_snapshots.chip_payload.chip_flat.<fp_key>（独立筹码表）
#   "column"   → StockFeatureSnapshot 真实列（trade_date/source_run_id/created_at）
#   "literal"  → 常量（如 fp_data_source 固定为 "feature_snapshot"）
#   "computed" → SQL 计算字段（如 fp_is_stale/fp_chip_available 动态判定）
#
# [P0 收口 2026-07-29] 二.2/二.3/二.4 要求：
# - fp_trade_date 改用 snapshot.trade_date 真实列（不再读 first_pyramid.tradeDate）
# - fp_is_stale 改为 computed：latest_snap.trade_date < MAX(bars_daily.trade_date)
#   数据库筛选/排序/页面展示必须同口径
# - fp_chip_available 改为 computed：只在存在与当前 core 严格匹配（五元组）
#   且 chip 维度 available=true 的 succeeded 记录时为 true，不从 review-core.chipConsensus 读取
# - 筹码字段（10 个）不得从 review-core 的 first_pyramid.chipConsensus 读取；
#   改为关联 stock_chip_consensus_snapshots 表（chip_flat 扁平对象）。
#
# 所有 99 字段均有 queryable source，禁止因 json_path 为空返回 422。
# =============================================================================

# [CHANGE-20260730-013] 操作符合同（严格按 data_type）
# text: contains, not_contains, eq, neq, empty, not_empty
# enum: eq, neq, in, not_in, empty, not_empty
# boolean: eq, empty, not_empty
# number/percent: eq, neq, gt, gte, lt, lte, between, empty, not_empty
# datetime: date_eq, before, after, between, empty, not_empty
# multi_enum: has_any, has_all, not_has_any, empty, not_empty
_OP_TEXT = {"contains", "not_contains", "eq", "neq", "empty", "not_empty"}
_OP_NUMBER = {"eq", "neq", "gt", "gte", "lt", "lte", "between", "empty", "not_empty"}
_OP_DATETIME = {"date_eq", "before", "after", "between", "empty", "not_empty"}
_OP_BOOLEAN = {"eq", "empty", "not_empty"}
_OP_ENUM = {"eq", "neq", "in", "not_in", "empty", "not_empty"}
_OP_MULTI_ENUM = {"has_any", "has_all", "not_has_any", "empty", "not_empty"}

# 数据源类型
_SOURCE_FLAT = "flat"
_SOURCE_CHIP = "chip"
_SOURCE_COLUMN = "column"
_SOURCE_LITERAL = "literal"
_SOURCE_COMPUTED = "computed"

# computed 字段子类型
_COMPUTED_IS_STALE = "is_stale"
_COMPUTED_CHIP_AVAILABLE = "chip_available"

# 默认操作符按 data_type 推导
_DEFAULT_OPS: dict[str, frozenset[str]] = {
    "text": frozenset(_OP_TEXT),
    "number": frozenset(_OP_NUMBER),
    "percent": frozenset(_OP_NUMBER),
    "datetime": frozenset(_OP_DATETIME),
    "boolean": frozenset(_OP_BOOLEAN),
    "enum": frozenset(_OP_ENUM),
    "multi_enum": frozenset(_OP_MULTI_ENUM),
}

# [CHANGE-20260730-013] input_control 按 data_type 推导
_DEFAULT_INPUT_CONTROL: dict[str, str] = {
    "text": "text_input",
    "number": "number_input",
    "percent": "number_input",
    "datetime": "date_picker",
    "boolean": "boolean_toggle",
    "enum": "single_select",
    "multi_enum": "multi_select",
}

# [CHANGE-20260730-013] value_normalizer 按 data_type 推导
_DEFAULT_VALUE_NORMALIZER: dict[str, str] = {
    "text": "trim",
    "number": "none",
    "percent": "none",
    "datetime": "trim",
    "boolean": "lower",
    "enum": "none",
    "multi_enum": "none",
}

# [CHANGE-20260730-013] 枚举值定义（从 first_pyramid_service.py / volume_context.py
# / flatten_first_pyramid 实际产出代码中提取）
_ENUM_VALUES_DIRECTION_LABEL = ["上行", "下行", "震荡"]
_ENUM_VALUES_ALIGNMENT = ["共振", "背离"]
_ENUM_VALUES_SQUEEZE_STATE = ["挤压中", "已释放", "无挤压"]
_ENUM_VALUES_MOMENTUM_DIRECTION = ["扩张", "收缩"]
_ENUM_VALUES_STRUCTURE_EVENT_TYPE = [
    "BOS", "CHoCH", "OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "EQH", "EQL",
]
_ENUM_VALUES_MOMENTUM_EVENT_TYPE = ["SQZ_OFF", "MOMENTUM_DIFFUSION"]
_ENUM_VALUES_NODE_EVENT_TYPE = ["node_cluster_touch"]
_ENUM_VALUES_EVENT_DIRECTION = ["up", "down"]
_ENUM_VALUES_STRUCTURE_LEVEL = ["swing", "internal"]
_ENUM_VALUES_VOLUME_BADGE = ["放量", "缩量", "正常", "未知"]
_ENUM_VALUES_MOMENTUM_VOLUME_RELATION = ["共振", "背离"]


def _spec_flat(
    fp_key: str,
    data_type: str,
    *,
    operators: set[str] | None = None,
    enum_values: list[str] | None = None,
) -> dict[str, Any]:
    """flat 源：从 summary_payload.first_pyramid_flat.<fp_key> 读取。"""
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "source": _SOURCE_FLAT,
        "json_path": ("first_pyramid_flat", fp_key),
        "operators": frozenset(operators) if operators else _DEFAULT_OPS[data_type],
        "enum_values": list(enum_values) if enum_values else [],
        "input_control": _DEFAULT_INPUT_CONTROL[data_type],
        "value_normalizer": _DEFAULT_VALUE_NORMALIZER[data_type],
    }


def _spec_chip(
    fp_key: str,
    data_type: str,
    *,
    operators: set[str] | None = None,
    enum_values: list[str] | None = None,
) -> dict[str, Any]:
    """chip 源：从 stock_chip_consensus_snapshots.chip_payload.chip_flat.<fp_key> 读取。"""
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "source": _SOURCE_CHIP,
        "json_path": ("chip_flat", fp_key),  # 相对 chip_payload
        "operators": frozenset(operators) if operators else _DEFAULT_OPS[data_type],
        "enum_values": list(enum_values) if enum_values else [],
        "input_control": _DEFAULT_INPUT_CONTROL[data_type],
        "value_normalizer": _DEFAULT_VALUE_NORMALIZER[data_type],
    }


def _spec_column(
    fp_key: str,
    data_type: str,
    column: str,
    *,
    operators: set[str] | None = None,
    enum_values: list[str] | None = None,
) -> dict[str, Any]:
    """column 源：从 StockFeatureSnapshot 真实列读取。"""
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "source": _SOURCE_COLUMN,
        "column": column,
        "operators": frozenset(operators) if operators else _DEFAULT_OPS[data_type],
        "enum_values": list(enum_values) if enum_values else [],
        "input_control": _DEFAULT_INPUT_CONTROL[data_type],
        "value_normalizer": _DEFAULT_VALUE_NORMALIZER[data_type],
    }


def _spec_literal(
    fp_key: str,
    data_type: str,
    value: Any,
    *,
    operators: set[str] | None = None,
    enum_values: list[str] | None = None,
) -> dict[str, Any]:
    """literal 源：常量值。"""
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "source": _SOURCE_LITERAL,
        "literal_value": value,
        "operators": frozenset(operators) if operators else _DEFAULT_OPS[data_type],
        "enum_values": list(enum_values) if enum_values else [],
        "input_control": _DEFAULT_INPUT_CONTROL[data_type],
        "value_normalizer": _DEFAULT_VALUE_NORMALIZER[data_type],
    }


def _spec_computed(
    fp_key: str,
    data_type: str,
    computed_kind: str,
    *,
    operators: set[str] | None = None,
    enum_values: list[str] | None = None,
) -> dict[str, Any]:
    """computed 源：SQL 计算字段（如 fp_is_stale, fp_chip_available）。

    [P0 收口 2026-07-29] 不读取存储值，由 SQL 表达式动态计算：
    - is_stale: latest_snap.trade_date < MAX(bars_daily.trade_date)
    - chip_available: chip 严格五元组匹配 AND chip_payload.chip.available=true
    """
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "source": _SOURCE_COMPUTED,
        "computed_kind": computed_kind,
        "operators": frozenset(operators) if operators else _DEFAULT_OPS[data_type],
        "enum_values": list(enum_values) if enum_values else [],
        "input_control": _DEFAULT_INPUT_CONTROL[data_type],
        "value_normalizer": _DEFAULT_VALUE_NORMALIZER[data_type],
    }


# 99 字段规格表（与 FP_ALL_KEYS 严格一一对应）
# [P0 收口 2026-07-29] 全部 99 字段均有 queryable source，支持服务端 filter/sort
# - 84 非 chip 字段：source=flat（从 summary_payload.first_pyramid_flat.<fp_key> 读取）
# - 10 chip 字段：source=chip（从 stock_chip_consensus_snapshots.chip_payload.chip_flat.<fp_key> 读取）
# - 3 元数据字段：source=column（trade_date/created_at/source_run_id 真实列）
# - 2 动态计算字段：source=computed（fp_is_stale, fp_chip_available）
# - 1 常量字段：source=literal（fp_data_source="feature_snapshot"）
FP_QUERY_FIELD_SPECS: dict[str, dict[str, Any]] = {
    # ===== 快照 (7) =====
    # [P0 收口 2026-07-29 二.2] fp_trade_date 改用 snapshot.trade_date 真实列
    "fp_trade_date": _spec_column("fp_trade_date", "datetime", "trade_date"),
    "fp_data_source": _spec_literal("fp_data_source", "text", "feature_snapshot", operators={"eq", "empty", "not_empty"}),
    # [P0-6 修复] fp_is_stale 改为 computed：snap.trade_date < MAX(bar_daily.trade_date)
    # 数据库筛选/排序/页面展示同口径
    "fp_is_stale": _spec_computed("fp_is_stale", "boolean", _COMPUTED_IS_STALE),
    "fp_calculated_at": _spec_column("fp_calculated_at", "datetime", "created_at"),
    "fp_run_id": _spec_column("fp_run_id", "text", "source_run_id", operators={"eq", "empty", "not_empty"}),
    "fp_summary": _spec_flat("fp_summary", "text"),
    # [P0-4 修复] fp_chip_available 改为 computed：只在存在严格匹配（五元组）
    # 且 chip 维度 available=true 的 succeeded 记录时为 true
    "fp_chip_available": _spec_computed(
        "fp_chip_available", "boolean", _COMPUTED_CHIP_AVAILABLE,
        operators={"eq", "empty", "not_empty"},
    ),

    # ===== 趋势 (18) =====
    "fp_trend_direction": _spec_flat("fp_trend_direction", "enum", enum_values=_ENUM_VALUES_DIRECTION_LABEL),
    "fp_trend_bars": _spec_flat("fp_trend_bars", "number"),
    "fp_dsa_vwap_dev_pct": _spec_flat("fp_dsa_vwap_dev_pct", "percent"),
    "fp_segment_change_pct": _spec_flat("fp_segment_change_pct", "percent"),
    "fp_segment_slope": _spec_flat("fp_segment_slope", "number"),
    "fp_trend_strength": _spec_flat("fp_trend_strength", "number"),
    "fp_segment_start_date": _spec_flat("fp_segment_start_date", "datetime"),
    "fp_segment_end_date": _spec_flat("fp_segment_end_date", "datetime"),
    "fp_segment_start_price": _spec_flat("fp_segment_start_price", "number"),
    "fp_segment_end_price": _spec_flat("fp_segment_end_price", "number"),
    "fp_segment_bars": _spec_flat("fp_segment_bars", "number"),
    "fp_segment_volume_ratio": _spec_flat("fp_segment_volume_ratio", "number"),
    "fp_segment_amount_ratio": _spec_flat("fp_segment_amount_ratio", "number"),
    "fp_segment_avg_volume": _spec_flat("fp_segment_avg_volume", "number"),
    "fp_segment_avg_amount": _spec_flat("fp_segment_avg_amount", "number"),
    "fp_prev_segment_volume": _spec_flat("fp_prev_segment_volume", "number"),
    "fp_prev_segment_amount": _spec_flat("fp_prev_segment_amount", "number"),
    "fp_vwap_ret_total": _spec_flat("fp_vwap_ret_total", "percent"),

    # ===== 结构 (8) =====
    "fp_swing_direction": _spec_flat("fp_swing_direction", "enum", enum_values=_ENUM_VALUES_DIRECTION_LABEL),
    "fp_internal_direction": _spec_flat("fp_internal_direction", "enum", enum_values=_ENUM_VALUES_DIRECTION_LABEL),
    "fp_structure_alignment": _spec_flat("fp_structure_alignment", "enum", enum_values=_ENUM_VALUES_ALIGNMENT),
    "fp_active_ob_count": _spec_flat("fp_active_ob_count", "number"),
    "fp_trailing_top": _spec_flat("fp_trailing_top", "number"),
    "fp_trailing_bottom": _spec_flat("fp_trailing_bottom", "number"),
    "fp_distance_to_trailing_top_pct": _spec_flat("fp_distance_to_trailing_top_pct", "percent"),
    "fp_distance_to_trailing_bottom_pct": _spec_flat("fp_distance_to_trailing_bottom_pct", "percent"),

    # ===== 结构事件 (21) - 全部从 flat 读取（写入时已扁平化）=====
    "fp_structure_event_type": _spec_flat("fp_structure_event_type", "enum", enum_values=_ENUM_VALUES_STRUCTURE_EVENT_TYPE),
    "fp_structure_event_direction": _spec_flat("fp_structure_event_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_structure_event_level": _spec_flat("fp_structure_event_level", "enum", enum_values=_ENUM_VALUES_STRUCTURE_LEVEL),
    "fp_structure_event_freshness": _spec_flat("fp_structure_event_freshness", "number"),
    "fp_structure_event_date": _spec_flat("fp_structure_event_date", "datetime"),
    "fp_structure_event_price": _spec_flat("fp_structure_event_price", "number"),
    "fp_structure_event_volume_badge": _spec_flat("fp_structure_event_volume_badge", "enum", enum_values=_ENUM_VALUES_VOLUME_BADGE),
    "fp_latest_bos_direction": _spec_flat("fp_latest_bos_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_latest_bos_freshness": _spec_flat("fp_latest_bos_freshness", "number"),
    "fp_latest_bos_level": _spec_flat("fp_latest_bos_level", "enum", enum_values=_ENUM_VALUES_STRUCTURE_LEVEL),
    "fp_latest_choch_direction": _spec_flat("fp_latest_choch_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_latest_choch_freshness": _spec_flat("fp_latest_choch_freshness", "number"),
    "fp_latest_choch_level": _spec_flat("fp_latest_choch_level", "enum", enum_values=_ENUM_VALUES_STRUCTURE_LEVEL),
    "fp_latest_ob_direction": _spec_flat("fp_latest_ob_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_latest_ob_freshness": _spec_flat("fp_latest_ob_freshness", "number"),
    "fp_latest_ob_high": _spec_flat("fp_latest_ob_high", "number"),
    "fp_latest_ob_low": _spec_flat("fp_latest_ob_low", "number"),
    "fp_latest_eqh_freshness": _spec_flat("fp_latest_eqh_freshness", "number"),
    "fp_latest_eqh_price": _spec_flat("fp_latest_eqh_price", "number"),
    "fp_latest_eql_freshness": _spec_flat("fp_latest_eql_freshness", "number"),
    "fp_latest_eql_price": _spec_flat("fp_latest_eql_price", "number"),

    # ===== 动量 (13) =====
    "fp_momentum_direction": _spec_flat("fp_momentum_direction", "enum", enum_values=_ENUM_VALUES_MOMENTUM_DIRECTION),
    "fp_squeeze_state": _spec_flat("fp_squeeze_state", "enum", enum_values=_ENUM_VALUES_SQUEEZE_STATE),
    "fp_momentum_change": _spec_flat("fp_momentum_change", "number"),
    "fp_sqzmom_value": _spec_flat("fp_sqzmom_value", "number"),
    "fp_sqzmom_prev": _spec_flat("fp_sqzmom_prev", "number"),
    "fp_bb_position": _spec_flat("fp_bb_position", "number"),
    "fp_bb_width": _spec_flat("fp_bb_width", "number"),
    "fp_bb_upper": _spec_flat("fp_bb_upper", "number"),
    "fp_bb_middle": _spec_flat("fp_bb_middle", "number"),
    "fp_bb_lower": _spec_flat("fp_bb_lower", "number"),
    "fp_squeeze_avg_volume": _spec_flat("fp_squeeze_avg_volume", "number"),
    "fp_release_volume_ratio": _spec_flat("fp_release_volume_ratio", "number"),
    "fp_momentum_volume_relation": _spec_flat("fp_momentum_volume_relation", "enum", enum_values=_ENUM_VALUES_MOMENTUM_VOLUME_RELATION),

    # ===== 动量事件 (9) - 全部从 flat 读取 =====
    "fp_momentum_event_type": _spec_flat("fp_momentum_event_type", "enum", enum_values=_ENUM_VALUES_MOMENTUM_EVENT_TYPE),
    "fp_momentum_event_direction": _spec_flat("fp_momentum_event_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_momentum_event_freshness": _spec_flat("fp_momentum_event_freshness", "number"),
    "fp_momentum_event_date": _spec_flat("fp_momentum_event_date", "datetime"),
    "fp_momentum_event_price": _spec_flat("fp_momentum_event_price", "number"),
    "fp_momentum_event_volume_badge": _spec_flat("fp_momentum_event_volume_badge", "enum", enum_values=_ENUM_VALUES_VOLUME_BADGE),
    "fp_latest_sqz_off_freshness": _spec_flat("fp_latest_sqz_off_freshness", "number"),
    "fp_latest_diffusion_direction": _spec_flat("fp_latest_diffusion_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_latest_diffusion_freshness": _spec_flat("fp_latest_diffusion_freshness", "number"),

    # ===== 筹码 (10) - 全部从独立 chip 表读取（二.4 要求）=====
    "fp_chip_state": _spec_chip("fp_chip_state", "text"),
    "fp_poc_price": _spec_chip("fp_poc_price", "number"),
    "fp_poc_distance_pct": _spec_chip("fp_poc_distance_pct", "percent"),
    "fp_peak_node_count": _spec_chip("fp_peak_node_count", "number"),
    "fp_vah_price": _spec_chip("fp_vah_price", "number"),
    "fp_val_price": _spec_chip("fp_val_price", "number"),
    "fp_node_event_type": _spec_chip("fp_node_event_type", "enum", enum_values=_ENUM_VALUES_NODE_EVENT_TYPE),
    "fp_node_event_direction": _spec_chip("fp_node_event_direction", "enum", enum_values=_ENUM_VALUES_EVENT_DIRECTION),
    "fp_node_event_freshness": _spec_chip("fp_node_event_freshness", "number"),
    "fp_node_event_price": _spec_chip("fp_node_event_price", "number"),

    # ===== 量能 (13) =====
    "fp_volume_badge": _spec_flat("fp_volume_badge", "enum", enum_values=_ENUM_VALUES_VOLUME_BADGE),
    "fp_volume": _spec_flat("fp_volume", "number"),
    "fp_amount": _spec_flat("fp_amount", "number"),
    "fp_turnover_rate": _spec_flat("fp_turnover_rate", "percent"),
    "fp_volume_ma20": _spec_flat("fp_volume_ma20", "number"),
    "fp_volume_ma200": _spec_flat("fp_volume_ma200", "number"),
    "fp_volume_ratio20": _spec_flat("fp_volume_ratio20", "number"),
    "fp_volume_ratio200": _spec_flat("fp_volume_ratio200", "number"),
    "fp_volume_percentile20": _spec_flat("fp_volume_percentile20", "number"),
    "fp_volume_percentile200": _spec_flat("fp_volume_percentile200", "number"),
    "fp_volume_zscore20": _spec_flat("fp_volume_zscore20", "number"),
    "fp_volume_zscore200": _spec_flat("fp_volume_zscore200", "number"),
    "fp_volume_ready": _spec_flat("fp_volume_ready", "boolean"),
}

# 断言：规格表必须覆盖全部 99 字段
assert set(FP_QUERY_FIELD_SPECS.keys()) == set(FP_ALL_KEYS), (
    f"FP_QUERY_FIELD_SPECS 必须覆盖全部 99 键，"
    f"缺失：{set(FP_ALL_KEYS) - set(FP_QUERY_FIELD_SPECS.keys())}，"
    f"多余：{set(FP_QUERY_FIELD_SPECS.keys()) - set(FP_ALL_KEYS)}"
)

# [CHANGE-20260729-005] 全部 99 字段均有 queryable source，全部支持服务端 filter/sort
FP_SERVER_FILTERABLE_KEYS: frozenset[str] = frozenset(FP_QUERY_FIELD_SPECS.keys())
FP_SERVER_SORTABLE_KEYS: frozenset[str] = FP_SERVER_FILTERABLE_KEYS

# chip 字段集合（source=chip），用于查询时判断是否需要 JOIN chip 表
FP_CHIP_KEYS: frozenset[str] = frozenset(
    k for k, v in FP_QUERY_FIELD_SPECS.items() if v["source"] == _SOURCE_CHIP
)


def serialize_fp_query_field_specs() -> dict[str, dict[str, Any]]:
    """将 FP_QUERY_FIELD_SPECS 序列化为 JSON 可序列化的 dict（供 API 返回）。

    [CHANGE-20260730-013] 字段元数据 API 使用：
    - frozenset → sorted list
    - tuple → list
    - 只返回前端需要的字段（data_type/operators/enum_values/input_control/value_normalizer）
    - 不暴露内部字段（json_path/column/literal_value/computed_kind）
    """
    result: dict[str, dict[str, Any]] = {}
    for key, spec in FP_QUERY_FIELD_SPECS.items():
        result[key] = {
            "fp_key": spec["fp_key"],
            "data_type": spec["data_type"],
            "operators": sorted(spec["operators"]),
            "enum_values": list(spec.get("enum_values", [])),
            "input_control": spec["input_control"],
            "value_normalizer": spec["value_normalizer"],
        }
    return result


# =============================================================================
# [CHANGE-20260729-005] fp_filter/fp_sort 解析（纯函数，无 DB 依赖）
# 移入本模块以便纯单元测试无需触发 app.db 配置加载。
# URL 编码格式：
#   fp_filter=key1:op1:val1[;val2];key2:op2:val2
#   fp_sort=key:direction
# =============================================================================

_FP_SORT_DIRECTIONS = {"asc", "desc"}


@dataclass(frozen=True)
class FpFilterSpec:
    """单个 fp 筛选条件（已通过白名单校验）。"""

    fp_key: str
    operator: str
    value: str | None  # empty/not_empty 时为 None
    value2: str | None  # between 时的上界


class FpFilterValidationError(ValueError):
    """[CHANGE-20260730-013] fp 筛选校验失败，携带结构化字段供 API 返回 422 detail。

    属性：
        field: 失败的 fp_key
        data_type: 该字段的 data_type
        operator: 被拒绝的 operator
        allowed: 允许的 operator 列表
        message: 人类可读的错误描述
    """

    def __init__(
        self,
        message: str,
        *,
        field: str,
        data_type: str,
        operator: str,
        allowed: list[str],
    ) -> None:
        super().__init__(message)
        self.field = field
        self.data_type = data_type
        self.operator = operator
        self.allowed = allowed

    def to_detail(self) -> dict[str, Any]:
        """转换为 API 422 响应的 detail dict。"""
        return {
            "field": self.field,
            "dataType": self.data_type,
            "operator": self.operator,
            "allowed": self.allowed,
            "message": str(self.args[0]) if self.args else "",
        }


@dataclass(frozen=True)
class FpSortSpec:
    """fp 排序规格（已通过白名单校验）。"""

    fp_key: str
    direction: str  # asc | desc


def parse_fp_filter(fp_filter: str | None) -> list[FpFilterSpec]:
    """解析 fp_filter 字符串为 FpFilterSpec 列表。

    格式：fp_filter=key1:op1:val1[;val2];key2:op2:val2
    - 多个条件用 `;` 分隔
    - 每个条件 `key:op:value`，between 用 `key:between:val1;val2`
    - empty/not_empty 操作符无需 value（`key:empty:`）
    - 非法 key/operator 抛 ValueError（由 API 层转 422）

    [CHANGE-20260729-005] 全部 99 字段均有 queryable source，不再因 json_path 为空拒绝。

    [CHANGE-20260730-013] 类型化筛选：
    - 根据 data_type 校验 operator 是否合法（操作符合同严格按类型）
    - enum 字段值必须匹配 enum_values（in/not_in 校验每个值）
    - 旧 URL 中 enum+contains 若值精确匹配枚举可迁移为 eq，否则返回 422
    - in/not_in 用逗号分隔多值：key:in:val1,val2,val3
    """
    if not fp_filter:
        return []

    specs: list[FpFilterSpec] = []
    tokens = fp_filter.split(";")
    i = 0
    while i < len(tokens):
        token = tokens[i].strip()
        if not token:
            i += 1
            continue
        parts = token.split(":", maxsplit=2)
        if len(parts) < 2:
            raise ValueError(
                f"Invalid fp_filter token '{token}': expected 'key:op[:value]'"
            )
        fp_key = parts[0].strip()
        operator = canonicalize_filter_operator(parts[1].strip())
        value = parts[2] if len(parts) > 2 else None

        if fp_key not in FP_QUERY_FIELD_SPECS:
            raise ValueError(
                f"Invalid fp_filter key '{fp_key}'. Not in FP_QUERY_FIELD_SPECS."
            )
        spec = FP_QUERY_FIELD_SPECS[fp_key]
        data_type = spec["data_type"]
        allowed_ops = spec["operators"]

        # [CHANGE-20260730-013] 旧 URL 兼容：enum+contains 若值精确匹配枚举则迁移为 eq
        if (
            operator == "contains"
            and data_type in ("enum", "multi_enum")
            and value is not None
            and value in spec["enum_values"]
        ):
            operator = "eq"

        if operator not in allowed_ops:
            raise FpFilterValidationError(
                f"Invalid operator '{operator}' for fp key '{fp_key}' "
                f"(data_type={data_type}). "
                f"Allowed: {sorted(allowed_ops)}",
                field=fp_key,
                data_type=data_type,
                operator=operator,
                allowed=sorted(allowed_ops),
            )

        value2: str | None = None
        if operator == "between":
            if value is None:
                raise ValueError(
                    f"fp_filter 'between' requires value; got '{token}'"
                )
            i += 1
            if i >= len(tokens):
                raise ValueError(
                    f"fp_filter 'between' for '{fp_key}' missing second value"
                )
            value2 = tokens[i].strip()
            if not value2:
                raise ValueError(
                    f"fp_filter 'between' for '{fp_key}' missing second value"
                )
        elif operator in ("empty", "not_empty"):
            value = None
        elif value is None or value == "":
            raise ValueError(
                f"fp_filter operator '{operator}' requires value; got '{token}'"
            )

        # [CHANGE-20260730-013] enum 值校验：eq/neq 值必须在 enum_values 中
        if (
            data_type in ("enum", "multi_enum")
            and operator in ("eq", "neq")
            and value is not None
            and spec["enum_values"]
            and value not in spec["enum_values"]
        ):
            raise ValueError(
                f"Invalid enum value '{value}' for fp key '{fp_key}'. "
                f"Allowed: {spec['enum_values']}"
            )

        # [CHANGE-20260730-013] in/not_in 每个值必须在 enum_values 中
        if (
            data_type in ("enum", "multi_enum")
            and operator in ("in", "not_in")
            and value is not None
            and spec["enum_values"]
        ):
            items = [v.strip() for v in value.split(",") if v.strip()]
            invalid = [v for v in items if v not in spec["enum_values"]]
            if invalid:
                raise ValueError(
                    f"Invalid enum values {invalid} for fp key '{fp_key}'. "
                    f"Allowed: {spec['enum_values']}"
                )

        specs.append(FpFilterSpec(fp_key=fp_key, operator=operator, value=value, value2=value2))
        i += 1

    return specs


def parse_fp_sort(fp_sort: str | None) -> FpSortSpec | None:
    """解析 fp_sort 字符串为 FpSortSpec。

    [CHANGE-20260729-005] 全部 99 字段均可排序（均有 queryable source）。
    """
    if not fp_sort:
        return None

    parts = fp_sort.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid fp_sort '{fp_sort}': expected 'key:direction'")
    fp_key = parts[0].strip()
    direction = parts[1].strip().lower()

    if fp_key not in FP_QUERY_FIELD_SPECS:
        raise ValueError(
            f"Invalid fp_sort key '{fp_key}'. Not in FP_QUERY_FIELD_SPECS."
        )
    if direction not in _FP_SORT_DIRECTIONS:
        raise ValueError(
            f"Invalid fp_sort direction '{direction}'. Allowed: asc, desc"
        )

    return FpSortSpec(fp_key=fp_key, direction=direction)


def _safe_float(val: Any) -> float | None:
    """安全转 float，None/异常返回 None。"""
    if val is None:
        return None
    try:
        f = float(val)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    """安全转 int，None/异常返回 None。"""
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _direction_label(val: Any) -> str | None:
    """将方向数值映射为中文标签。"""
    return direction_display(direction_from_regime(val))


def _latest_event_by_type(
    events: list[dict[str, Any]],
    event_types: set[str],
) -> dict[str, Any] | None:
    """从事件列表中取指定类型的最新事件（freshnessBars 最小或列表最后一个）。"""
    candidates = [e for e in events if e.get("type") in event_types]
    if not candidates:
        return None
    # 列表按时间升序，最后一个为最新
    return candidates[-1]


def _structure_alignment(swing: Any, internal: Any) -> str | None:
    """结构对齐：主要结构与短线结构方向是否一致。"""
    s = _safe_int(swing)
    i = _safe_int(internal)
    if s is None or i is None:
        return None
    if s == 0 or i == 0:
        return None
    alignment = (
        StructureAlignment.ALIGNED if s == i else StructureAlignment.DIVERGENT
    )
    return alignment_display(alignment)


def _distance_pct(current: float | None, target: float | None) -> float | None:
    """计算百分比距离：(target - current) / current * 100。"""
    if current is None or target is None or current == 0:
        return None
    return round((target - current) / current * 100, 2)


def flatten_first_pyramid(
    first_pyramid: dict[str, Any] | None,
    *,
    calculated_at: str | None = None,
    run_id: str | None = None,
    is_stale: bool = False,
) -> dict[str, Any]:
    """将嵌套 FirstPyramidSnapshot dict 扁平化为 99 个 fp_ 键。

    Args:
        first_pyramid: FirstPyramidSnapshot.to_dict() 的输出，或 None（无快照）
        calculated_at: 快照计算时间 ISO（来自 snapshot.created_at）
        run_id: 快照归属 run ID（来自 snapshot.source_run_id）
        is_stale: 快照是否过期（trade_date 非最近交易日）

    Returns:
        包含恰好 99 个 fp_ 键的 dict；无数据时所有值为 None
    """
    # 初始化所有 99 键为 None
    result: dict[str, Any] = dict.fromkeys(FP_ALL_KEYS)

    if first_pyramid is None:
        return result

    # ===== 快照 (7) =====
    result["fp_trade_date"] = first_pyramid.get("tradeDate")
    result["fp_data_source"] = "feature_snapshot"
    result["fp_is_stale"] = is_stale
    result["fp_calculated_at"] = calculated_at
    result["fp_run_id"] = str(run_id) if run_id is not None else None
    result["fp_summary"] = first_pyramid.get("statusText")
    # [P0-4 修复 2026-07-29 二.4] fp_chip_available 改为 computed 表达式
    # 不再从 review-core 的 chipConsensus 读取，由调用方（list/detail 服务）按
    # 严格五元组匹配的 chip 表记录存在性 + chip_payload.chip.available=true 计算
    # 此处保留 None 默认值，禁止从 chipConsensus 推断

    # ===== 趋势 (18) =====
    trend = first_pyramid.get("trend") or {}
    trend_cf = trend.get("continuousFactors") or {}
    result["fp_trend_direction"] = _direction_label(trend_cf.get("regime_value"))
    result["fp_trend_bars"] = _safe_int(trend_cf.get("dsa_dir_bars"))
    result["fp_dsa_vwap_dev_pct"] = _safe_float(trend_cf.get("dsa_vwap_dev_pct"))
    result["fp_segment_change_pct"] = _safe_float(trend_cf.get("segment_change_pct"))
    result["fp_segment_slope"] = _safe_float(trend_cf.get("segment_slope"))
    # [CHANGE-20260729-005 二.5] 优先 regime_strength（DSA SSOT 权威字段），trend_strength 仅 fallback
    result["fp_trend_strength"] = _safe_float(
        trend_cf.get("regime_strength", trend_cf.get("trend_strength"))
    )
    result["fp_segment_start_date"] = trend_cf.get("segment_start_time")
    result["fp_segment_end_date"] = trend_cf.get("segment_end_time")
    result["fp_segment_start_price"] = _safe_float(trend_cf.get("segment_start_price"))
    result["fp_segment_end_price"] = _safe_float(trend_cf.get("segment_end_price"))
    result["fp_segment_bars"] = _safe_int(trend_cf.get("segment_bars"))
    # [CHANGE-20260729-005 二.5] DSA 字段修复：mean/mean ratio（禁止消费废弃 sum/sum 口径）
    result["fp_segment_volume_ratio"] = _safe_float(trend_cf.get("current_vs_prev_volume_mean_ratio"))
    result["fp_segment_amount_ratio"] = _safe_float(trend_cf.get("current_vs_prev_amount_mean_ratio"))
    result["fp_segment_avg_volume"] = _safe_float(trend_cf.get("current_segment_volume_mean"))
    result["fp_segment_avg_amount"] = _safe_float(trend_cf.get("current_segment_amount_mean"))
    # prev_segment 使用 mean 字段（废弃 sum 字段仅兼容旧快照）
    result["fp_prev_segment_volume"] = _safe_float(
        trend_cf.get("prev_segment_volume_mean", trend_cf.get("prev_segment_volume_sum"))
    )
    result["fp_prev_segment_amount"] = _safe_float(
        trend_cf.get("prev_segment_amount_mean", trend_cf.get("prev_segment_amount_sum"))
    )
    result["fp_vwap_ret_total"] = _safe_float(trend_cf.get("vwap_ret_total"))

    # ===== 结构 (8) =====
    structure = first_pyramid.get("structure") or {}
    struct_cf = structure.get("continuousFactors") or {}
    swing_dir = struct_cf.get("swing_direction", struct_cf.get("swing_bias"))
    internal_dir = struct_cf.get("internal_direction", struct_cf.get("internal_bias"))
    result["fp_swing_direction"] = _direction_label(swing_dir)
    result["fp_internal_direction"] = _direction_label(internal_dir)
    result["fp_structure_alignment"] = _structure_alignment(swing_dir, internal_dir)
    result["fp_active_ob_count"] = _safe_int(struct_cf.get("active_ob_count"))
    trailing_top = _safe_float(struct_cf.get("trailing_top"))
    trailing_bottom = _safe_float(struct_cf.get("trailing_bottom"))
    result["fp_trailing_top"] = trailing_top
    result["fp_trailing_bottom"] = trailing_bottom
    # 距离百分比用段末价格（≈当前价）计算
    last_price = _safe_float(trend_cf.get("segment_end_price"))
    result["fp_distance_to_trailing_top_pct"] = _distance_pct(last_price, trailing_top)
    result["fp_distance_to_trailing_bottom_pct"] = _distance_pct(last_price, trailing_bottom)

    # ===== 结构事件 (21) =====
    struct_events = structure.get("events") or []
    latest_struct = struct_events[-1] if struct_events else None
    if latest_struct:
        result["fp_structure_event_type"] = latest_struct.get("type")
        result["fp_structure_event_direction"] = latest_struct.get("direction")
        extra = latest_struct.get("extra") or {}
        result["fp_structure_event_level"] = extra.get("structure_level")
        result["fp_structure_event_freshness"] = latest_struct.get("freshnessBars")
        result["fp_structure_event_date"] = latest_struct.get("occurredAt")
        result["fp_structure_event_price"] = _safe_float(latest_struct.get("price"))
        result["fp_structure_event_volume_badge"] = latest_struct.get("volumeBadge")

    bos_evt = _latest_event_by_type(struct_events, {"BOS"})
    if bos_evt:
        result["fp_latest_bos_direction"] = bos_evt.get("direction")
        result["fp_latest_bos_freshness"] = bos_evt.get("freshnessBars")
        result["fp_latest_bos_level"] = (bos_evt.get("extra") or {}).get("structure_level")

    choch_evt = _latest_event_by_type(struct_events, {"CHoCH"})
    if choch_evt:
        result["fp_latest_choch_direction"] = choch_evt.get("direction")
        result["fp_latest_choch_freshness"] = choch_evt.get("freshnessBars")
        result["fp_latest_choch_level"] = (choch_evt.get("extra") or {}).get("structure_level")

    # [P0-3 修复 2026-07-29] SMC OB 生命周期改为 OB_CREATED/OB_ENTERED/OB_MITIGATED 三事件
    # 旧 OB_ENTRY 已废弃，保留读取仅为历史快照兼容
    ob_evt = _latest_event_by_type(
        struct_events, {"OB_CREATED", "OB_ENTERED", "OB_MITIGATED", "OB_ENTRY"},
    )
    if ob_evt:
        result["fp_latest_ob_direction"] = ob_evt.get("direction")
        result["fp_latest_ob_freshness"] = ob_evt.get("freshnessBars")
        ob_extra = ob_evt.get("extra") or {}
        result["fp_latest_ob_high"] = _safe_float(ob_extra.get("ob_high"))
        result["fp_latest_ob_low"] = _safe_float(ob_extra.get("ob_low"))

    eqh_evt = _latest_event_by_type(struct_events, {"EQH", "equal_high"})
    if eqh_evt:
        result["fp_latest_eqh_freshness"] = eqh_evt.get("freshnessBars")
        result["fp_latest_eqh_price"] = _safe_float(eqh_evt.get("price"))

    eql_evt = _latest_event_by_type(struct_events, {"EQL", "equal_low"})
    if eql_evt:
        result["fp_latest_eql_freshness"] = eql_evt.get("freshnessBars")
        result["fp_latest_eql_price"] = _safe_float(eql_evt.get("price"))

    # ===== 动量 (13) =====
    momentum = first_pyramid.get("momentum") or {}
    mom_cf = momentum.get("continuousFactors") or {}
    sqz_val = _safe_float(mom_cf.get("sqzmom_val"))
    sqz_prev = _safe_float(mom_cf.get("sqzmom_val_prev"))
    momentum_direction = (
        MomentumDirection.EXPANDING if sqz_val is not None and sqz_val > 0
        else MomentumDirection.CONTRACTING if sqz_val is not None and sqz_val < 0
        else MomentumDirection.FLAT if sqz_val == 0
        else None
    )
    result["fp_momentum_direction"] = momentum_direction_display(momentum_direction)
    if mom_cf.get("squeeze_on"):
        result["fp_squeeze_state"] = squeeze_display(SqueezeState.SQUEEZE)
    elif mom_cf.get("squeeze_off"):
        result["fp_squeeze_state"] = squeeze_display(SqueezeState.RELEASED)
    elif mom_cf.get("no_squeeze"):
        result["fp_squeeze_state"] = squeeze_display(SqueezeState.NORMAL)
    if sqz_val is not None and sqz_prev is not None:
        result["fp_momentum_change"] = round(sqz_val - sqz_prev, 6)
    result["fp_sqzmom_value"] = sqz_val
    result["fp_sqzmom_prev"] = sqz_prev
    result["fp_bb_position"] = _safe_float(mom_cf.get("bb_position"))
    result["fp_bb_width"] = _safe_float(mom_cf.get("bb_width"))
    result["fp_bb_upper"] = _safe_float(mom_cf.get("bb_upper"))
    result["fp_bb_middle"] = _safe_float(mom_cf.get("bb_middle"))
    result["fp_bb_lower"] = _safe_float(mom_cf.get("bb_lower"))
    result["fp_squeeze_avg_volume"] = _safe_float(mom_cf.get("squeeze_period_volume_mean"))
    result["fp_release_volume_ratio"] = _safe_float(mom_cf.get("release_vs_squeeze_volume_ratio"))
    result["fp_momentum_volume_relation"] = mom_cf.get("vol_divergence")

    # ===== 动量事件 (9) =====
    mom_events = momentum.get("events") or []
    latest_mom = mom_events[-1] if mom_events else None
    if latest_mom:
        result["fp_momentum_event_type"] = latest_mom.get("type")
        result["fp_momentum_event_direction"] = latest_mom.get("direction")
        result["fp_momentum_event_freshness"] = latest_mom.get("freshnessBars")
        result["fp_momentum_event_date"] = latest_mom.get("occurredAt")
        result["fp_momentum_event_price"] = _safe_float(latest_mom.get("price"))
        result["fp_momentum_event_volume_badge"] = latest_mom.get("volumeBadge")

    sqz_off_evt = _latest_event_by_type(mom_events, {"SQZ_OFF"})
    if sqz_off_evt:
        result["fp_latest_sqz_off_freshness"] = sqz_off_evt.get("freshnessBars")

    diff_evt = _latest_event_by_type(mom_events, {"MOMENTUM_DIFFUSION"})
    if diff_evt:
        result["fp_latest_diffusion_direction"] = diff_evt.get("direction")
        result["fp_latest_diffusion_freshness"] = diff_evt.get("freshnessBars")

    # ===== 筹码 (10) =====
    chip = first_pyramid.get("chipConsensus")
    if chip is not None:
        chip_cf = chip.get("continuousFactors") or {}
        result["fp_chip_state"] = chip.get("statusText")
        poc_price = _safe_float(chip_cf.get("poc_price"))
        result["fp_poc_price"] = poc_price
        chip_last_close = _safe_float(chip_cf.get("last_close"))
        if poc_price is not None and poc_price != 0 and chip_last_close is not None:
            result["fp_poc_distance_pct"] = round(
                (chip_last_close - poc_price) / poc_price * 100, 2
            )
        result["fp_peak_node_count"] = _safe_int(chip_cf.get("n_peak_nodes"))
        result["fp_vah_price"] = _safe_float(chip_cf.get("vah_price"))
        result["fp_val_price"] = _safe_float(chip_cf.get("val_price"))
        chip_events = chip.get("events") or []
        latest_chip = chip_events[-1] if chip_events else None
        if latest_chip:
            result["fp_node_event_type"] = latest_chip.get("type")
            result["fp_node_event_direction"] = latest_chip.get("direction")
            result["fp_node_event_freshness"] = latest_chip.get("freshnessBars")
            result["fp_node_event_price"] = _safe_float(latest_chip.get("price"))

    # ===== 量能 (13) =====
    vc = first_pyramid.get("volumeContext")
    if vc is not None:
        result["fp_volume_badge"] = vc.get("badge")
        result["fp_volume"] = _safe_float(vc.get("volume"))
        result["fp_amount"] = _safe_float(vc.get("amount"))
        result["fp_turnover_rate"] = _safe_float(vc.get("turnoverRate"))
        result["fp_volume_ma20"] = _safe_float(vc.get("volumeMa20"))
        result["fp_volume_ma200"] = _safe_float(vc.get("volumeMa200"))
        result["fp_volume_ratio20"] = _safe_float(vc.get("volumeRatio20"))
        result["fp_volume_ratio200"] = _safe_float(vc.get("volumeRatio200"))
        result["fp_volume_percentile20"] = _safe_float(vc.get("volumePercentile20"))
        result["fp_volume_percentile200"] = _safe_float(vc.get("volumePercentile200"))
        result["fp_volume_zscore20"] = _safe_float(vc.get("volumeZscore20"))
        result["fp_volume_zscore200"] = _safe_float(vc.get("volumeZscore200"))
        result["fp_volume_ready"] = vc.get("readiness")

    return result


def flatten_chip_fields(chip_dimension: dict[str, Any] | None) -> dict[str, Any]:
    """将筹码维度扁平化为 10 个 chip fp_ 键，用于写入 chip_payload.chip_flat。

    [CHANGE-20260729-005 二.4] 筹码字段独立存储于 stock_chip_consensus_snapshots 表，
    查询时从 chip_payload.chip_flat.<fp_key> 读取，不依赖 review-core 的 chipConsensus。

    Args:
        chip_dimension: DimensionResult.to_dict() 输出，或 None

    Returns:
        包含 10 个 chip fp_ 键的 dict（无数据时全 None）
    """
    # 构造一个仅含 chipConsensus 的 first_pyramid dict，复用 flatten_first_pyramid
    partial = {"chipConsensus": chip_dimension} if chip_dimension else None
    flat = flatten_first_pyramid(partial)
    # 只返回 chip 字段（10 个）
    return {k: flat[k] for k in FP_CHIP_KEYS}


# =============================================================================
# [C1] 第一金字塔统一读模型组装
# =============================================================================
# 唯一权威组装函数：producer 写入持久化 flat、Market API / Review / 详情 / 导出
# 读取时**必须**复用本函数完成元数据覆盖与 chip 合并，不得各自重复覆盖字段。
# 设计要点：
#   - fp_trade_date 覆盖为 snapshot.trade_date 真实列（不读 first_pyramid.tradeDate）
#   - fp_run_id 覆盖为 snapshot.source_run_id 真实列
#   - fp_calculated_at 覆盖为 snapshot.created_at 真实列
#   - fp_is_stale 动态计算：snapshot.trade_date < max_bar_date（None 时 False）
#   - 合并严格匹配 chip_flat 到 10 个 chip 字段；fp_chip_available 按 chip available 计算
#   - 保留条件性 null，不补 0
# =============================================================================


def assemble_first_pyramid_read_model(
    stored_flat: dict[str, Any] | None,
    snapshot_columns: dict[str, Any] | None = None,
    chip_snapshot: dict[str, Any] | None = None,
    max_bar_date: str | None = None,
) -> dict[str, Any] | None:
    """将已扁平化的 first_pyramid_flat 统一组装为最终读模型（99 键）。

    Args:
        stored_flat: flatten_first_pyramid 的输出（99 键）；None 时返回 None（无快照）
        snapshot_columns: 快照真实列，可用键：
            - trade_date: date|str（业务交易日，覆盖 fp_trade_date）
            - created_at: str|None（快照创建时间 ISO，覆盖 fp_calculated_at）
            - source_run_id: str|None（快照归属 run，覆盖 fp_run_id）
        chip_snapshot: 严格匹配的 chip 快照，可用键：
            - chip_flat: dict（chip_payload.chip_flat，10 个 chip 字段）
            - chip_available: bool（chip 维度 available 是否 true）
            None 或缺省时 chip 字段清为 None、fp_chip_available=False
        max_bar_date: 该股票最新日线 trade_date（ISO YYYY-MM-DD），用于 fp_is_stale；
            None 时 is_stale 保持 False

    Returns:
        组装后的 flat dict（恰好 99 键）；stored_flat 为 None 时返回 None
    """
    if stored_flat is None:
        return None

    cols = snapshot_columns or {}
    chip = chip_snapshot or {}

    # 1. 覆盖元数据字段（真实列优先）
    trade_date = cols.get("trade_date")
    if trade_date is not None:
        stored_flat["fp_trade_date"] = (
            trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        )

    created_at = cols.get("created_at")
    if created_at is not None:
        stored_flat["fp_calculated_at"] = (
            created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        )

    source_run_id = cols.get("source_run_id")
    if source_run_id is not None:
        stored_flat["fp_run_id"] = str(source_run_id)

    # 2. 动态计算 fp_is_stale：snapshot.trade_date < 该股最新日线 trade_date
    if trade_date is not None and max_bar_date is not None:
        td = trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        md = max_bar_date.isoformat() if hasattr(max_bar_date, "isoformat") else str(max_bar_date)
        stored_flat["fp_is_stale"] = td < md

    # 3. 合并 chip_flat / 计算 fp_chip_available
    chip_flat = chip.get("chip_flat") or {}
    chip_available = bool(chip.get("chip_available"))
    for k in FP_CHIP_KEYS:
        if chip_flat and k in chip_flat:
            stored_flat[k] = chip_flat[k]
        else:
            stored_flat[k] = None
    stored_flat["fp_chip_available"] = chip_available

    return stored_flat


if __name__ == "__main__":
    # 自测：验证 99 键完整性
    flat_none = flatten_first_pyramid(None)
    assert len(flat_none) == 99, f"None 输入应返回 99 键，实际 {len(flat_none)}"
    assert all(v is None for v in flat_none.values()), "None 输入所有值应为 None"

    # 验证分组计数
    for group, keys in FP_FIELD_GROUPS.items():
        print(f"{group}: {len(keys)} 键")

    print(f"总计: {len(FP_ALL_KEYS)} 键")
    assert len(FP_ALL_KEYS) == 99
    print("OK")
