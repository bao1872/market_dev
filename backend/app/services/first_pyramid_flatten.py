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

from typing import Any

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
# [CHANGE-20260729-004 P0-1] 第一金字塔查询字段规格表
# =============================================================================
# 唯一字段规格表，供 /market/stocks 服务端 fp_filter/fp_sort 使用：
#   - 字段白名单（禁止前端传任意 SQL 字段或 JSON 路径）
#   - 数据类型（text/number/percent/datetime/boolean/enum）
#   - JSON 路径：summary_payload.first_pyramid.<path>（用 SQLAlchemy JSON 路径表达式）
#   - 允许的筛选操作符
#   - 排序表达式（默认基于 JSON 路径取值）
# 所有排序 asc/desc 均 NULLS LAST，第二排序键固定为 Instrument.symbol（在 _build_order_by 中追加）
#
# JSON 路径字段说明（相对 summary_payload.first_pyramid）：
#   "tradeDate" / "statusText" / "chipConsensus" / "volumeContext" / "trend" / "structure" / "momentum"
#   嵌套字段通过点号路径表示，例如 "trend.continuousFactors.regime_value"
#
# 数据类型与操作符映射：
#   text     → contains / not_contains / eq / empty / not_empty
#   number   → gt / gte / lt / lte / eq / between / empty / not_empty
#   percent  → 同 number（百分比已转 float，按数值比较）
#   datetime → gte / gt / lt / lte / between / empty / not_empty（ISO 字符串字典序比较）
#   boolean  → eq / empty / not_empty
#   enum     → eq / empty / not_empty
# =============================================================================

# 操作符集合
_OP_TEXT = {"contains", "not_contains", "eq", "empty", "not_empty"}
_OP_NUMBER = {"gt", "gte", "lt", "lte", "eq", "between", "empty", "not_empty"}
_OP_DATETIME = {"gte", "gt", "lt", "lte", "between", "empty", "not_empty"}
_OP_BOOLEAN = {"eq", "empty", "not_empty"}
_OP_ENUM = {"eq", "empty", "not_empty"}


def _spec(
    fp_key: str,
    data_type: str,
    json_path: tuple[str, ...],
    *,
    operators: set[str] | None = None,
) -> dict[str, Any]:
    """构造字段规格 dict。

    Args:
        fp_key: fp_ 前缀字段名
        data_type: text/number/percent/datetime/boolean/enum
        json_path: 相对 summary_payload.first_pyramid 的 JSON 路径分段
        operators: 允许的操作符集合；None 时按 data_type 自动推导
    """
    if operators is None:
        operators = {
            "text": _OP_TEXT,
            "number": _OP_NUMBER,
            "percent": _OP_NUMBER,
            "datetime": _OP_DATETIME,
            "boolean": _OP_BOOLEAN,
            "enum": _OP_ENUM,
        }[data_type]
    return {
        "fp_key": fp_key,
        "data_type": data_type,
        "json_path": json_path,
        "operators": frozenset(operators),
    }


# 99 字段规格表（与 FP_ALL_KEYS 严格一一对应）
FP_QUERY_FIELD_SPECS: dict[str, dict[str, Any]] = {
    # ===== 快照 (7) =====
    "fp_trade_date": _spec("fp_trade_date", "datetime", ("tradeDate",)),
    "fp_data_source": _spec("fp_data_source", "text", (), operators={"eq", "empty", "not_empty"}),
    "fp_is_stale": _spec("fp_is_stale", "boolean", ()),  # 特殊：由后端计算，不在 JSON 中
    "fp_calculated_at": _spec("fp_calculated_at", "datetime", ()),  # 来自 snapshot.created_at
    "fp_run_id": _spec("fp_run_id", "text", (), operators={"eq", "empty", "not_empty"}),
    "fp_summary": _spec("fp_summary", "text", ("statusText",), operators={"contains", "not_contains", "empty", "not_empty"}),
    "fp_chip_available": _spec("fp_chip_available", "boolean", ()),  # 特殊：chipConsensus is not None

    # ===== 趋势 (18) =====
    "fp_trend_direction": _spec("fp_trend_direction", "enum", ("trend", "continuousFactors", "regime_value")),
    "fp_trend_bars": _spec("fp_trend_bars", "number", ("trend", "continuousFactors", "dsa_dir_bars")),
    "fp_dsa_vwap_dev_pct": _spec("fp_dsa_vwap_dev_pct", "percent", ("trend", "continuousFactors", "dsa_vwap_dev_pct")),
    "fp_segment_change_pct": _spec("fp_segment_change_pct", "percent", ("trend", "continuousFactors", "segment_change_pct")),
    "fp_segment_slope": _spec("fp_segment_slope", "number", ("trend", "continuousFactors", "segment_slope")),
    "fp_trend_strength": _spec("fp_trend_strength", "number", ("trend", "continuousFactors", "trend_strength")),
    "fp_segment_start_date": _spec("fp_segment_start_date", "datetime", ("trend", "continuousFactors", "segment_start_time")),
    "fp_segment_end_date": _spec("fp_segment_end_date", "datetime", ("trend", "continuousFactors", "segment_end_time")),
    "fp_segment_start_price": _spec("fp_segment_start_price", "number", ("trend", "continuousFactors", "segment_start_price")),
    "fp_segment_end_price": _spec("fp_segment_end_price", "number", ("trend", "continuousFactors", "segment_end_price")),
    "fp_segment_bars": _spec("fp_segment_bars", "number", ("trend", "continuousFactors", "segment_bars")),
    "fp_segment_volume_ratio": _spec("fp_segment_volume_ratio", "number", ("trend", "continuousFactors", "current_vs_prev_volume_ratio")),
    "fp_segment_amount_ratio": _spec("fp_segment_amount_ratio", "number", ("trend", "continuousFactors", "current_vs_prev_amount_ratio")),
    "fp_segment_avg_volume": _spec("fp_segment_avg_volume", "number", ("trend", "continuousFactors", "current_segment_volume_mean")),
    "fp_segment_avg_amount": _spec("fp_segment_avg_amount", "number", ("trend", "continuousFactors", "current_segment_amount_mean")),
    "fp_prev_segment_volume": _spec("fp_prev_segment_volume", "number", ("trend", "continuousFactors", "prev_segment_volume_sum")),
    "fp_prev_segment_amount": _spec("fp_prev_segment_amount", "number", ("trend", "continuousFactors", "prev_segment_amount_sum")),
    "fp_vwap_ret_total": _spec("fp_vwap_ret_total", "percent", ("trend", "continuousFactors", "vwap_ret_total")),

    # ===== 结构 (8) =====
    "fp_swing_direction": _spec("fp_swing_direction", "enum", ("structure", "continuousFactors", "swing_direction")),
    "fp_internal_direction": _spec("fp_internal_direction", "enum", ("structure", "continuousFactors", "internal_direction")),
    "fp_structure_alignment": _spec("fp_structure_alignment", "enum", ()),  # 计算字段
    "fp_active_ob_count": _spec("fp_active_ob_count", "number", ("structure", "continuousFactors", "active_ob_count")),
    "fp_trailing_top": _spec("fp_trailing_top", "number", ("structure", "continuousFactors", "trailing_top")),
    "fp_trailing_bottom": _spec("fp_trailing_bottom", "number", ("structure", "continuousFactors", "trailing_bottom")),
    "fp_distance_to_trailing_top_pct": _spec("fp_distance_to_trailing_top_pct", "percent", ()),  # 计算字段
    "fp_distance_to_trailing_bottom_pct": _spec("fp_distance_to_trailing_bottom_pct", "percent", ()),  # 计算字段

    # ===== 结构事件 (21) =====
    # 结构事件类字段均通过 structure.events 列表取最新一项，无法直接用 JSON 路径索引
    # 因此这些字段不参与服务端 fp_filter/fp_sort（保留 sortable=false/filterable=false 语义在服务端拒绝）
    # 但仍记录在规格表中以支持前端列设置统一性
    "fp_structure_event_type": _spec("fp_structure_event_type", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_structure_event_direction": _spec("fp_structure_event_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_structure_event_level": _spec("fp_structure_event_level", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_structure_event_freshness": _spec("fp_structure_event_freshness", "number", ()),
    "fp_structure_event_date": _spec("fp_structure_event_date", "datetime", ()),
    "fp_structure_event_price": _spec("fp_structure_event_price", "number", ()),
    "fp_structure_event_volume_badge": _spec("fp_structure_event_volume_badge", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_bos_direction": _spec("fp_latest_bos_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_bos_freshness": _spec("fp_latest_bos_freshness", "number", ()),
    "fp_latest_bos_level": _spec("fp_latest_bos_level", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_choch_direction": _spec("fp_latest_choch_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_choch_freshness": _spec("fp_latest_choch_freshness", "number", ()),
    "fp_latest_choch_level": _spec("fp_latest_choch_level", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_ob_direction": _spec("fp_latest_ob_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_ob_freshness": _spec("fp_latest_ob_freshness", "number", ()),
    "fp_latest_ob_high": _spec("fp_latest_ob_high", "number", ()),
    "fp_latest_ob_low": _spec("fp_latest_ob_low", "number", ()),
    "fp_latest_eqh_freshness": _spec("fp_latest_eqh_freshness", "number", ()),
    "fp_latest_eqh_price": _spec("fp_latest_eqh_price", "number", ()),
    "fp_latest_eql_freshness": _spec("fp_latest_eql_freshness", "number", ()),
    "fp_latest_eql_price": _spec("fp_latest_eql_price", "number", ()),

    # ===== 动量 (13) =====
    "fp_momentum_direction": _spec("fp_momentum_direction", "enum", ()),  # 计算字段
    "fp_squeeze_state": _spec("fp_squeeze_state", "enum", ()),  # 计算字段
    "fp_momentum_change": _spec("fp_momentum_change", "number", ()),  # 计算字段
    "fp_sqzmom_value": _spec("fp_sqzmom_value", "number", ("momentum", "continuousFactors", "sqzmom_val")),
    "fp_sqzmom_prev": _spec("fp_sqzmom_prev", "number", ("momentum", "continuousFactors", "sqzmom_val_prev")),
    "fp_bb_position": _spec("fp_bb_position", "number", ("momentum", "continuousFactors", "bb_position")),
    "fp_bb_width": _spec("fp_bb_width", "number", ("momentum", "continuousFactors", "bb_width")),
    "fp_bb_upper": _spec("fp_bb_upper", "number", ("momentum", "continuousFactors", "bb_upper")),
    "fp_bb_middle": _spec("fp_bb_middle", "number", ("momentum", "continuousFactors", "bb_middle")),
    "fp_bb_lower": _spec("fp_bb_lower", "number", ("momentum", "continuousFactors", "bb_lower")),
    "fp_squeeze_avg_volume": _spec("fp_squeeze_avg_volume", "number", ("momentum", "continuousFactors", "squeeze_period_volume_mean")),
    "fp_release_volume_ratio": _spec("fp_release_volume_ratio", "number", ("momentum", "continuousFactors", "release_vs_squeeze_volume_ratio")),
    "fp_momentum_volume_relation": _spec("fp_momentum_volume_relation", "enum", ("momentum", "continuousFactors", "vol_divergence"), operators={"eq", "empty", "not_empty"}),

    # ===== 动量事件 (9) =====
    "fp_momentum_event_type": _spec("fp_momentum_event_type", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_momentum_event_direction": _spec("fp_momentum_event_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_momentum_event_freshness": _spec("fp_momentum_event_freshness", "number", ()),
    "fp_momentum_event_date": _spec("fp_momentum_event_date", "datetime", ()),
    "fp_momentum_event_price": _spec("fp_momentum_event_price", "number", ()),
    "fp_momentum_event_volume_badge": _spec("fp_momentum_event_volume_badge", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_sqz_off_freshness": _spec("fp_latest_sqz_off_freshness", "number", ()),
    "fp_latest_diffusion_direction": _spec("fp_latest_diffusion_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_latest_diffusion_freshness": _spec("fp_latest_diffusion_freshness", "number", ()),

    # ===== 筹码 (10) =====
    "fp_chip_state": _spec("fp_chip_state", "text", ("chipConsensus", "statusText")),
    "fp_poc_price": _spec("fp_poc_price", "number", ("chipConsensus", "continuousFactors", "poc_price")),
    "fp_poc_distance_pct": _spec("fp_poc_distance_pct", "percent", ()),  # 计算字段
    "fp_peak_node_count": _spec("fp_peak_node_count", "number", ("chipConsensus", "continuousFactors", "n_peak_nodes")),
    "fp_vah_price": _spec("fp_vah_price", "number", ("chipConsensus", "continuousFactors", "vah_price")),
    "fp_val_price": _spec("fp_val_price", "number", ("chipConsensus", "continuousFactors", "val_price")),
    "fp_node_event_type": _spec("fp_node_event_type", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_node_event_direction": _spec("fp_node_event_direction", "enum", (), operators={"eq", "empty", "not_empty"}),
    "fp_node_event_freshness": _spec("fp_node_event_freshness", "number", ()),
    "fp_node_event_price": _spec("fp_node_event_price", "number", ()),

    # ===== 量能 (13) =====
    "fp_volume_badge": _spec("fp_volume_badge", "enum", ("volumeContext", "badge"), operators={"eq", "empty", "not_empty"}),
    "fp_volume": _spec("fp_volume", "number", ("volumeContext", "volume")),
    "fp_amount": _spec("fp_amount", "number", ("volumeContext", "amount")),
    "fp_turnover_rate": _spec("fp_turnover_rate", "percent", ("volumeContext", "turnoverRate")),
    "fp_volume_ma20": _spec("fp_volume_ma20", "number", ("volumeContext", "volumeMa20")),
    "fp_volume_ma200": _spec("fp_volume_ma200", "number", ("volumeContext", "volumeMa200")),
    "fp_volume_ratio20": _spec("fp_volume_ratio20", "number", ("volumeContext", "volumeRatio20")),
    "fp_volume_ratio200": _spec("fp_volume_ratio200", "number", ("volumeContext", "volumeRatio200")),
    "fp_volume_percentile20": _spec("fp_volume_percentile20", "number", ("volumeContext", "volumePercentile20")),
    "fp_volume_percentile200": _spec("fp_volume_percentile200", "number", ("volumeContext", "volumePercentile200")),
    "fp_volume_zscore20": _spec("fp_volume_zscore20", "number", ("volumeContext", "volumeZscore20")),
    "fp_volume_zscore200": _spec("fp_volume_zscore200", "number", ("volumeContext", "volumeZscore200")),
    "fp_volume_ready": _spec("fp_volume_ready", "boolean", ("volumeContext", "readiness"), operators={"eq", "empty", "not_empty"}),
}

# 断言：规格表必须覆盖全部 99 字段
assert set(FP_QUERY_FIELD_SPECS.keys()) == set(FP_ALL_KEYS), (
    f"FP_QUERY_FIELD_SPECS 必须覆盖全部 99 键，"
    f"缺失：{set(FP_ALL_KEYS) - set(FP_QUERY_FIELD_SPECS.keys())}，"
    f"多余：{set(FP_QUERY_FIELD_SPECS.keys()) - set(FP_ALL_KEYS)}"
)

# 支持服务端 filter/sort 的字段集合：仅包含有 JSON 路径的字段
# （计算字段、列表事件字段无 JSON 路径，服务端拒绝）
FP_SERVER_FILTERABLE_KEYS: frozenset[str] = frozenset(
    k for k, v in FP_QUERY_FIELD_SPECS.items() if v["json_path"]
)
FP_SERVER_SORTABLE_KEYS: frozenset[str] = FP_SERVER_FILTERABLE_KEYS


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
    v = _safe_int(val)
    if v is None:
        return None
    if v > 0:
        return "上行"
    if v < 0:
        return "下行"
    return "震荡"


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
    return "共振" if s == i else "背离"


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
    result["fp_chip_available"] = first_pyramid.get("chipConsensus") is not None

    # ===== 趋势 (18) =====
    trend = first_pyramid.get("trend") or {}
    trend_cf = trend.get("continuousFactors") or {}
    result["fp_trend_direction"] = _direction_label(trend_cf.get("regime_value"))
    result["fp_trend_bars"] = _safe_int(trend_cf.get("dsa_dir_bars"))
    result["fp_dsa_vwap_dev_pct"] = _safe_float(trend_cf.get("dsa_vwap_dev_pct"))
    result["fp_segment_change_pct"] = _safe_float(trend_cf.get("segment_change_pct"))
    result["fp_segment_slope"] = _safe_float(trend_cf.get("segment_slope"))
    result["fp_trend_strength"] = _safe_float(trend_cf.get("trend_strength"))
    result["fp_segment_start_date"] = trend_cf.get("segment_start_time")
    result["fp_segment_end_date"] = trend_cf.get("segment_end_time")
    result["fp_segment_start_price"] = _safe_float(trend_cf.get("segment_start_price"))
    result["fp_segment_end_price"] = _safe_float(trend_cf.get("segment_end_price"))
    result["fp_segment_bars"] = _safe_int(trend_cf.get("segment_bars"))
    result["fp_segment_volume_ratio"] = _safe_float(trend_cf.get("current_vs_prev_volume_ratio"))
    result["fp_segment_amount_ratio"] = _safe_float(trend_cf.get("current_vs_prev_amount_ratio"))
    result["fp_segment_avg_volume"] = _safe_float(trend_cf.get("current_segment_volume_mean"))
    result["fp_segment_avg_amount"] = _safe_float(trend_cf.get("current_segment_amount_mean"))
    result["fp_prev_segment_volume"] = _safe_float(trend_cf.get("prev_segment_volume_sum"))
    result["fp_prev_segment_amount"] = _safe_float(trend_cf.get("prev_segment_amount_sum"))
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
    result["fp_momentum_direction"] = (
        "扩张" if sqz_val is not None and sqz_val > 0
        else "收缩" if sqz_val is not None and sqz_val < 0
        else None
    )
    if mom_cf.get("squeeze_on"):
        result["fp_squeeze_state"] = "挤压中"
    elif mom_cf.get("squeeze_off"):
        result["fp_squeeze_state"] = "已释放"
    elif mom_cf.get("no_squeeze"):
        result["fp_squeeze_state"] = "无挤压"
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
