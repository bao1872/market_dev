"""ReviewFilterEngine - 筛选器执行引擎（PRD §8）。

职责：
- 注册 filter_definitions 中的特殊评估器（A1/B1/B2/C1/C2/C3 复合条件）
- 接收 scope 的 P/Q/U/C/V payload 与历史上下文，评估命中哪些筛选器
- 输出命中的信号 trigger/baseline/evidence payload，供 signal_service 持久化

输入上下文（context dict）字段：
- "P" / "Q" / "U" / "C" / "V": 各 metric payload（含 value/delta1d/historyPercentile120d/...）
- "coverage": scope 级 coverage_ratio
- "ready_count": 有效成员数
- "_pq_diff_history_pct": (P.value - Q.value) 的历史分位（由 service 预计算注入）
- "_q_delta1d_history_pct": Q.delta1d 的历史分位
- "_u_delta1d_history_pct": U.delta1d 的历史分位
- "_structure_breakdown_not_rising": 结构破坏扩散率不再上升（1/0）
- "_v_delta1d_history_pct": V.delta1d 的历史分位
- "_u_delta1d_history_pct": U.delta1d 的历史分位（与 _u_delta1d_history_pct 一致）
- "_c_history_pct": C 历史分位（与 C.historyPercentile120d 一致，冗余便于评估器）
- "_c_rising": C 继续上升（1/0）
- "_c_high_anomaly": C 处于异常高位（1/0）

模块自测：
    python -m app.domain.review.filter_engine
"""

from __future__ import annotations

from typing import Any

from app.domain.review.filter_definitions import (
    DEFAULT_FILTERS,
    REVIEW_FILTER_VERSION,
    ComparisonOp,
    FilterCondition,
    FilterDefinition,
    FilterFamily,
    build_rank_key,
    compare_rank_keys,
    set_evaluator,
)


def _get_float(context: dict[str, Any], path: str) -> float | None:
    """从 context 中按路径取 float（与 FilterCondition.evaluate 同源）。"""
    parts = path.split(".")
    if len(parts) == 1:
        v = context.get(parts[0])
        return _to_float(v)
    if len(parts) == 2:
        metric_code, field = parts
        metric = context.get(metric_code)
        if not isinstance(metric, dict):
            return None
        return _to_float(metric.get(field))
    return None


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:
            return None
        return f
    except (TypeError, ValueError):
        return None


# =============================================================================
# 特殊评估器（A1/B1/B2/C1/C2/C3 复合条件）
# =============================================================================


def _eval_a1_surface_strong_internal_weak(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """A1：表面强但内部弱（PRD §8.1）。

    P.historyPercentile120d >= 70
    (P.value - Q.value) 的自身历史分位 >= 90  [_pq_diff_history_pct]
    Q.delta1d <= 0 或 U.delta1d <= 0
    coverage >= 0.95
    """
    p_pct = _get_float(context, "P.historyPercentile120d")
    pq_diff_pct = _get_float(context, "_pq_diff_history_pct")
    q_delta = _get_float(context, "Q.delta1d")
    u_delta = _get_float(context, "U.delta1d")
    coverage = _get_float(context, "coverage")

    if p_pct is None or p_pct < 70:
        return False
    if pq_diff_pct is None or pq_diff_pct < 90:
        return False
    if coverage is None or coverage < 0.95:
        return False
    # Q 或 U 1 日变化 <= 0
    q_ok = q_delta is not None and q_delta <= 0
    u_ok = u_delta is not None and u_delta <= 0
    return q_ok or u_ok


def _eval_b1_high_level_slowing(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """B1：高位减速（PRD §8.2）。

    P/Q/U/V 中至少 2 项历史分位 >= 70
    Q/U/V 中至少 2 项 1 日变化分位 <= 30  [使用 _*_delta1d_history_pct]
    """
    p_pct = _get_float(context, "P.historyPercentile120d")
    q_pct = _get_float(context, "Q.historyPercentile120d")
    u_pct = _get_float(context, "U.historyPercentile120d")
    v_pct = _get_float(context, "V.historyPercentile120d")

    high_count = sum(
        1 for x in (p_pct, q_pct, u_pct, v_pct) if x is not None and x >= 70
    )
    if high_count < 2:
        return False

    q_delta_pct = _get_float(context, "_q_delta1d_history_pct")
    u_delta_pct = _get_float(context, "_u_delta1d_history_pct")
    v_delta_pct = _get_float(context, "_v_delta1d_history_pct")
    slow_count = sum(
        1 for x in (q_delta_pct, u_delta_pct, v_delta_pct)
        if x is not None and x <= 30
    )
    return slow_count >= 2


def _eval_b2_low_level_repair(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """B2：低位修复（PRD §8.2）。

    P/Q/U 中至少 2 项历史分位 <= 40
    Q 与 U 的 1 日变化分位 >= 70  [_q_delta1d_history_pct / _u_delta1d_history_pct]
    结构破坏扩散率不再继续上升  [_structure_breakdown_not_rising=1]
    """
    p_pct = _get_float(context, "P.historyPercentile120d")
    q_pct = _get_float(context, "Q.historyPercentile120d")
    u_pct = _get_float(context, "U.historyPercentile120d")

    low_count = sum(
        1 for x in (p_pct, q_pct, u_pct) if x is not None and x <= 40
    )
    if low_count < 2:
        return False

    q_delta_pct = _get_float(context, "_q_delta1d_history_pct")
    u_delta_pct = _get_float(context, "_u_delta1d_history_pct")
    if q_delta_pct is None or q_delta_pct < 70:
        return False
    if u_delta_pct is None or u_delta_pct < 70:
        return False

    not_rising = _get_float(context, "_structure_breakdown_not_rising")
    return not_rising is not None and not_rising >= 1


def _eval_c1_volume_without_breadth(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """C1：放量无广度（PRD §8.3）。

    V 历史分位 >= 70 或 V 变化分位 >= 70
    U 变化分位 <= 40
    C 历史分位 >= 70 或 C 继续上升
    """
    v_pct = _get_float(context, "V.historyPercentile120d")
    v_delta_pct = _get_float(context, "_v_delta1d_history_pct")
    v_high = (v_pct is not None and v_pct >= 70) or (
        v_delta_pct is not None and v_delta_pct >= 70
    )
    if not v_high:
        return False

    u_delta_pct = _get_float(context, "_u_delta1d_history_pct")
    if u_delta_pct is None or u_delta_pct > 40:
        return False

    c_pct = _get_float(context, "C.historyPercentile120d")
    c_rising = _get_float(context, "_c_rising")
    c_high = (c_pct is not None and c_pct >= 70) or (
        c_rising is not None and c_rising >= 1
    )
    return c_high


def _eval_c2_breadth_without_volume(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """C2：广度无放量（PRD §8.3）。

    U 变化分位 >= 70
    V 历史分位 <= 50 或 V 变化分位 <= 50
    """
    u_delta_pct = _get_float(context, "_u_delta1d_history_pct")
    if u_delta_pct is None or u_delta_pct < 70:
        return False

    v_pct = _get_float(context, "V.historyPercentile120d")
    v_delta_pct = _get_float(context, "_v_delta1d_history_pct")
    v_low = (v_pct is not None and v_pct <= 50) or (
        v_delta_pct is not None and v_delta_pct <= 50
    )
    return v_low


def _eval_c3_synchronized_expansion(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """C3：同步扩张（PRD §8.3）。

    U 变化分位 >= 70
    V 变化分位 >= 70
    C 未处于异常高位或未继续上升
    """
    u_delta_pct = _get_float(context, "_u_delta1d_history_pct")
    if u_delta_pct is None or u_delta_pct < 70:
        return False

    v_delta_pct = _get_float(context, "_v_delta1d_history_pct")
    if v_delta_pct is None or v_delta_pct < 70:
        return False

    c_high = _get_float(context, "_c_high_anomaly")
    c_rising = _get_float(context, "_c_rising")
    # C 未处于异常高位 或 未继续上升 → not (c_high AND c_rising)
    c_high_bool = c_high is not None and c_high >= 1
    c_rising_bool = c_rising is not None and c_rising >= 1
    return not (c_high_bool and c_rising_bool)


# 注册所有特殊评估器（在 import 时完成）
set_evaluator("eval_a1_surface_strong_internal_weak", _eval_a1_surface_strong_internal_weak)
set_evaluator("eval_b1_high_level_slowing", _eval_b1_high_level_slowing)
set_evaluator("eval_b2_low_level_repair", _eval_b2_low_level_repair)
set_evaluator("eval_c1_volume_without_breadth", _eval_c1_volume_without_breadth)
set_evaluator("eval_c2_breadth_without_volume", _eval_c2_breadth_without_volume)
set_evaluator("eval_c3_synchronized_expansion", _eval_c3_synchronized_expansion)


# =============================================================================
# D 族评估器：第二金字塔维度偏差（PRD §24）
#
# 输入：context["pyramid_v2"]，来自 board_analysis_snapshots.payload["pyramid_v2"]
# 结构：
#   pyramid_v2 = {
#       "state_transitions": {...},      # 状态迁移原始计数
#       "freshness": {...},              # 事件新鲜度密度
#       "diffusion": {                   # 扩散度（基于 state_transitions）
#           "positive_migration_count": int,
#           "negative_migration_count": int,
#           "total_migration_count": int,
#           "positive_ratio": {"numerator": int, "denominator": int},
#           "negative_ratio": {"numerator": int, "denominator": int},
#           "participation_coverage": {"numerator": int, "denominator": int},
#       },
#       "concentration": {               # 集中度
#           "top3_contribution": {"numerator": float, "denominator": float},
#           "top5_contribution": {"numerator": float, "denominator": float},
#           "hhi": float,
#           "leader_median_gap": float | None,
#           ...
#       },
#       "dispersion": {...},
#       "relative_strength": {
#           "vs_market": {"ratio": float | None, "label": str | None, "diff": float | None},
#           "vs_parent": {"ratio": float | None, "label": str | None, "diff": float | None},
#           "equal_weight_diff": float | None,
#       },
#   }
#
# D 族数据的 owner（Slice 4A4 / 4A4R 后不再统一依赖 pyramid_v2）：
# - D2（event_freshness_high）：读取 canonical context["scope_observation"].freshness，
#   可 canonical-only 命中；canonical 缺失时不回退到 pyramid_v2。
# - D4（concentration_high）：读取 canonical
#   context["scope_observation"].structure.current_state.technical_state.concentration，
#   可 canonical-only 命中；canonical 缺失时不回退到 pyramid_v2。
# - D1 / D3 / D5：仍读取 legacy context["pyramid_v2"]。
# =============================================================================


def _get_pyramid_v2(context: dict[str, Any]) -> dict[str, Any] | None:
    """从 context 中安全提取 pyramid_v2 payload (legacy Board ``pyramid_v2``)."""
    pv2 = context.get("pyramid_v2")
    if not isinstance(pv2, dict):
        return None
    return pv2


def _get_scope_observation(context: dict[str, Any]) -> dict[str, Any] | None:
    """从 context 中安全提取 canonical Review ``scope_observation``。

    Slice 4A4 — Board-derived filter consumer cutover.  D2 / D4 read their inputs
    from the canonical ``scope_observation`` top-level (single source of truth),
    NOT from the legacy Board ``pyramid_v2``.  Absent / non-dict canonical payload
    -> ``None`` (D2 / D4 return False with the exact same missing semantics as
    their pre-cutover behavior).
    """
    obs = context.get("scope_observation")
    if not isinstance(obs, dict):
        return None
    return obs


def _ratio_value(ratio_obj: Any) -> float | None:
    """从 {numerator, denominator} 结构计算比值。"""
    if not isinstance(ratio_obj, dict):
        return None
    num = _to_float(ratio_obj.get("numerator"))
    den = _to_float(ratio_obj.get("denominator"))
    if num is None or den is None or abs(den) < 1e-9:
        return None
    return num / den


def _eval_d1_state_migration_positive(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """D1：正向状态迁移占优（PRD §24.1 状态迁移）。

    positive_migration_count >= 5
    positive_ratio >= 0.6
    negative_migration_count <= positive_migration_count
    """
    pv2 = _get_pyramid_v2(context)
    if pv2 is None:
        return False
    diffusion = pv2.get("diffusion") or {}
    pos_count = _to_float(diffusion.get("positive_migration_count"))
    neg_count = _to_float(diffusion.get("negative_migration_count"))
    if pos_count is None or pos_count < 5:
        return False
    pos_ratio = _ratio_value(diffusion.get("positive_ratio"))
    if pos_ratio is None or pos_ratio < 0.6:
        return False
    if neg_count is not None and neg_count > pos_count:
        return False
    return True


def _eval_d2_event_freshness_high(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """D2：事件新鲜度高（PRD §24.1 事件新鲜度）。

    decay_weighted_density >= 0.3
    today_count >= 1 或 last_5d_count >= 3

    Slice 4A4 — consumer cutover: reads from canonical ``scope_observation.freshness``
    (Review is now the owner).  MUST NOT fall back to ``pyramid_v2.freshness``.
    """
    obs = _get_scope_observation(context)
    if obs is None:
        return False
    freshness = obs.get("freshness")
    if not isinstance(freshness, dict):
        return False
    density = _to_float(freshness.get("decay_weighted_density"))
    if density is None or density < 0.3:
        return False
    today = _to_float(freshness.get("today_count")) or 0.0
    last_5d = _to_float(freshness.get("last_5d_count")) or 0.0
    return today >= 1 or last_5d >= 3


def _eval_d3_breadth_expansion(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """D3：宽度扩张（PRD §24.1 宽度/覆盖率）。

    participation_coverage >= 0.3
    total_migration_count >= 5
    """
    pv2 = _get_pyramid_v2(context)
    if pv2 is None:
        return False
    diffusion = pv2.get("diffusion") or {}
    coverage = _ratio_value(diffusion.get("participation_coverage"))
    if coverage is None or coverage < 0.3:
        return False
    total = _to_float(diffusion.get("total_migration_count"))
    if total is None or total < 5:
        return False
    return True


def _eval_d4_concentration_high(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """D4：集中度高（PRD §24.1 集中度）。

    hhi >= 0.1 或 top5_contribution >= 0.4
    leader_median_gap > 0

    Slice 4A4 — consumer cutover: reads from canonical
    ``scope_observation.structure.current_state.technical_state.concentration``
    (this is the technical-state concentration, NOT price/amount concentration;
    Review is now the owner).  MUST NOT fall back to ``pyramid_v2.concentration``.
    """
    obs = _get_scope_observation(context)
    if obs is None:
        return False
    structure = obs.get("structure")
    if not isinstance(structure, dict):
        return False
    current_state = structure.get("current_state")
    if not isinstance(current_state, dict):
        return False
    technical_state = current_state.get("technical_state")
    if not isinstance(technical_state, dict):
        return False
    conc = technical_state.get("concentration")
    if not isinstance(conc, dict):
        return False
    hhi = _to_float(conc.get("hhi"))
    top5 = _ratio_value(conc.get("top5_contribution"))
    hhi_high = hhi is not None and hhi >= 0.1
    top5_high = top5 is not None and top5 >= 0.4
    if not (hhi_high or top5_high):
        return False
    gap = _to_float(conc.get("leader_median_gap"))
    if gap is None or gap <= 0:
        return False
    return True


def _eval_d5_relative_strength_strong(
    filt: FilterDefinition, context: dict[str, Any],
) -> bool:
    """D5：相对强度强（PRD §24.1 相对强度）。

    vs_market.ratio >= 1.1
    equal_weight_diff > 0
    """
    pv2 = _get_pyramid_v2(context)
    if pv2 is None:
        return False
    rs = pv2.get("relative_strength") or {}
    vs_market = rs.get("vs_market") or {}
    ratio = _to_float(vs_market.get("ratio"))
    if ratio is None or ratio < 1.1:
        return False
    diff = _to_float(rs.get("equal_weight_diff"))
    if diff is None or diff <= 0:
        return False
    return True


set_evaluator("eval_d1_state_migration_positive", _eval_d1_state_migration_positive)
set_evaluator("eval_d2_event_freshness_high", _eval_d2_event_freshness_high)
set_evaluator("eval_d3_breadth_expansion", _eval_d3_breadth_expansion)
set_evaluator("eval_d4_concentration_high", _eval_d4_concentration_high)
set_evaluator("eval_d5_relative_strength_strong", _eval_d5_relative_strength_strong)


# =============================================================================
# 筛选器执行
# =============================================================================


def evaluate_filters(
    context: dict[str, Any],
    *,
    filters: list[FilterDefinition] | None = None,
) -> list[FilterDefinition]:
    """评估所有筛选器，返回命中的筛选器列表。

    Args:
        context: P/Q/U/C/V payload + coverage + 历史分位注入字段
        filters: 筛选器列表（None=使用 DEFAULT_FILTERS）

    Returns:
        命中的 FilterDefinition 列表（按 DEFAULT_FILTERS 顺序）
    """
    if filters is None:
        filters = DEFAULT_FILTERS
    return [f for f in filters if f.evaluate(context)]


def build_signal_payloads(
    filt: FilterDefinition,
    context: dict[str, Any],
    *,
    duration_days: int = 0,
    scope_type: str = "",
    scope_name: str = "",
) -> dict[str, Any]:
    """构建命中信号的 trigger/baseline/evidence/rank_key payload（PRD §12.3、§14.4）。

    Args:
        filt: 命中的筛选器
        context: 评估 context
        duration_days: 持续日数（service 层注入）
        scope_type: 范围类型
        scope_name: 范围名称

    Returns:
        {
            "trigger_payload": {...},
            "baseline_payload": {...},
            "evidence_payload": {...},
            "confirmation_rule": {...},
            "invalidation_rule": {...},
            "rank_key": {...},
        }
    """
    # trigger_payload：触发条件的当前值快照
    trigger: dict[str, Any] = {
        "signal_type": filt.signal_type,
        "family": filt.family.value,
        "metrics": {
            code: {
                "value": _get_float(context, f"{code}.value"),
                "delta1d": _get_float(context, f"{code}.delta1d"),
                "historyPercentile120d": _get_float(
                    context, f"{code}.historyPercentile120d",
                ),
            }
            for code in ("P", "Q", "U", "C", "V")
        },
        "coverage": _get_float(context, "coverage"),
    }

    # baseline_payload：基线参考值（PRD §8.4 偏差历史分位）
    baseline: dict[str, Any] = {
        "pq_diff_history_pct": _get_float(context, "_pq_diff_history_pct"),
        "q_delta1d_history_pct": _get_float(context, "_q_delta1d_history_pct"),
        "u_delta1d_history_pct": _get_float(context, "_u_delta1d_history_pct"),
        "v_delta1d_history_pct": _get_float(context, "_v_delta1d_history_pct"),
        "structure_breakdown_not_rising": _get_float(
            context, "_structure_breakdown_not_rising",
        ),
    }

    # evidence_payload：结构化解释（PRD §14.5 模板化解释，禁止黑箱分）
    evidence: dict[str, Any] = {
        "description": filt.description,
        "filter_version": REVIEW_FILTER_VERSION,
        "components_evidence": _collect_components_evidence(context),
    }
    # [P0-7] D 族信号附加维度证据（PRD §24）
    # Slice 4A4R — evidence 按具体 D filter 的各自 owner 选择，而非整族一刀切：
    #   D2 的 freshness            ← canonical scope_observation
    #   D4 的 concentration        ← canonical scope_observation
    #   其余维度（diffusion / relative_strength / 未切源的 freshness、concentration）
    #                              ← legacy pyramid_v2
    #   D1 / D3 / D5 的 evidence 与 4A4 之前完全一致（全部 legacy）。
    # canonical 缺失时不得回退到 pyramid_v2；canonical-only 也允许生成相关
    # canonical evidence，其余 section 保持 None（不 fallback）。
    if filt.family == FilterFamily.D:
        pv2 = _get_pyramid_v2(context)
        obs = _get_scope_observation(context)
        if pv2 is not None or obs is not None:
            if filt.signal_type == "event_freshness_high":
                # D2：freshness 由 canonical Review 提供；concentration 仍取 legacy
                fresh_raw = (obs.get("freshness") or {}) if obs is not None else {}
                conc_raw = (
                    (pv2.get("concentration") or {}) if pv2 is not None else {}
                )
            elif filt.signal_type == "concentration_high":
                # D4：technical-state concentration 由 canonical Review 提供
                # （注意是 technical-state，不是 price/amount 集中度）；freshness 仍取 legacy
                fresh_raw = (pv2.get("freshness") or {}) if pv2 is not None else {}
                conc_raw = (
                    ((obs.get("structure") or {}).get("current_state") or {})
                    .get("technical_state") or {}
                ).get("concentration") or {}
            else:
                # D1 / D3 / D5：全部 legacy（与 4A4 之前完全一致）
                fresh_raw = (pv2.get("freshness") or {}) if pv2 is not None else {}
                conc_raw = (
                    (pv2.get("concentration") or {}) if pv2 is not None else {}
                )
            evidence["pyramid_v2_evidence"] = {
                "diffusion": pv2.get("diffusion") if pv2 is not None else None,
                "freshness": {
                    k: fresh_raw.get(k)
                    for k in (
                        "today_count", "last_5d_count", "last_10d_count",
                        "decay_weighted_density",
                    )
                },
                "concentration": {
                    k: conc_raw.get(k)
                    for k in ("hhi", "leader_median_gap", "leader_symbol")
                },
                "relative_strength": (
                    pv2.get("relative_strength") if pv2 is not None else None
                ),
            }

    # rank_key（PRD §8.4）
    bias_pct = _pick_bias_history_pct(filt, context)
    delta1d_pct = _pick_delta1d_pct(filt, context)
    coverage = _get_float(context, "coverage")
    rank_key = build_rank_key(
        bias_history_pct=bias_pct,
        delta1d_pct=delta1d_pct,
        duration_days=duration_days,
        coverage=coverage,
        scope_type=scope_type,
        scope_name=scope_name,
    )

    return {
        "trigger_payload": trigger,
        "baseline_payload": baseline,
        "evidence_payload": evidence,
        "confirmation_rule": filt.confirmation_rule,
        "invalidation_rule": filt.invalidation_rule,
        "rank_key": rank_key,
    }


def _collect_components_evidence(context: dict[str, Any]) -> dict[str, Any]:
    """收集 P/Q/U/C/V 各 component 的证据（PRD §14.8 EvidenceDrawer）。"""
    out: dict[str, Any] = {}
    for code in ("P", "Q", "U", "C", "V"):
        metric = context.get(code)
        if not isinstance(metric, dict):
            continue
        comps = metric.get("components") or []
        out[code] = {
            "value": metric.get("value"),
            "status": metric.get("status"),
            "components": [
                {
                    "name": c.get("name"),
                    "rawValue": c.get("rawValue"),
                    "direction": c.get("direction"),
                    "fieldSource": c.get("fieldSource"),
                    "weight": c.get("weight"),
                }
                for c in comps
            ],
        }
    return out


def _pick_bias_history_pct(
    filt: FilterDefinition, context: dict[str, Any],
) -> float | None:
    """根据筛选器类型选择最具代表性的偏差历史分位（PRD §8.4 排序键 1）。"""
    if filt.family == FilterFamily.A:
        return _get_float(context, "_pq_diff_history_pct")
    if filt.family == FilterFamily.B:
        # B 类按历史分位偏离衡量：取 P/Q/U/V 中 |pct - 50| 最大者
        pcts = [
            _get_float(context, f"{c}.historyPercentile120d")
            for c in ("P", "Q", "U", "V")
        ]
        valid = [p for p in pcts if p is not None]
        if not valid:
            return None
        return max(abs(p - 50) for p in valid)
    if filt.family == FilterFamily.D:
        # D 族：第二金字塔无历史分位，用 relative_strength.vs_market.ratio 作为
        # 偏差代理（>1 表示强于市场）；无 pyramid_v2 时返回 None。
        pv2 = _get_pyramid_v2(context)
        if pv2 is None:
            return None
        rs = pv2.get("relative_strength") or {}
        vs_market = rs.get("vs_market") or {}
        ratio = _to_float(vs_market.get("ratio"))
        return ratio
    # C 类：取 V 或 U 的变化分位
    v_pct = _get_float(context, "_v_delta1d_history_pct")
    u_pct = _get_float(context, "_u_delta1d_history_pct")
    candidates = [p for p in (v_pct, u_pct) if p is not None]
    return max(candidates) if candidates else None


def _pick_delta1d_pct(
    filt: FilterDefinition, context: dict[str, Any],
) -> float | None:
    """根据筛选器类型选择最具代表性的当日变化分位（PRD §8.4 排序键 2）。"""
    if filt.family == FilterFamily.A:
        return _get_float(context, "_q_delta1d_history_pct")
    if filt.family == FilterFamily.B:
        cands = [
            _get_float(context, f"_{c.lower()}_delta1d_history_pct")
            for c in ("Q", "U", "V")
        ]
        valid = [c for c in cands if c is not None]
        return min(valid) if valid else None  # B 类关注减速，取最小变化分位
    if filt.family == FilterFamily.D:
        # D 族：第二金字塔无当日变化分位概念，返回 None（rank_key 排序时
        # 与其他 D 族信号同等对待）
        return None
    # C 类
    cands = [
        _get_float(context, "_v_delta1d_history_pct"),
        _get_float(context, "_u_delta1d_history_pct"),
    ]
    valid = [c for c in cands if c is not None]
    return max(valid) if valid else None


def sort_signals_by_rank(
    signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 PRD §8.4 排序信号列表（rank_key 升序，rank_key 自带优先级语义）。

    Args:
        signals: 每个元素须含 "rank_key" 字段

    Returns:
        排序后的 signals（最优先在前）
    """
    def _key(sig: dict[str, Any]) -> tuple:
        rk = sig.get("rank_key") or {}
        # 排序键：越大越靠前的取负数；越小越靠前的取正数
        return (
            -float(rk.get("bias_history_pct") or -1),
            -float(rk.get("delta1d_pct") or -1),
            -int(rk.get("duration_days") or 0),
            -float(rk.get("coverage") or 0),
            int(rk.get("scope_type_priority") or 99),
            str(rk.get("scope_name") or ""),
        )

    return sorted(signals, key=_key)


# 显式重导出 compare_rank_keys / build_rank_key，便于 service 层导入
__all__ = [
    "REVIEW_FILTER_VERSION",
    "DEFAULT_FILTERS",
    "FilterDefinition",
    "FilterCondition",
    "FilterFamily",
    "ComparisonOp",
    "evaluate_filters",
    "build_signal_payloads",
    "sort_signals_by_rank",
    "build_rank_key",
    "compare_rank_keys",
]


if __name__ == "__main__":
    # 自测：构造命中 A1 的 context
    ctx = {
        "P": {
            "value": 80, "rawValue": 0.8, "delta1d": -1.0,
            "historyPercentile120d": 85, "components": [],
            "coverage": 0.98, "status": "ready",
        },
        "Q": {
            "value": 60, "rawValue": 0.6, "delta1d": -2.0,
            "historyPercentile120d": 50, "components": [],
            "coverage": 0.98, "status": "ready",
        },
        "U": {
            "value": 55, "rawValue": 0.55, "delta1d": 1.0,
            "historyPercentile120d": 50, "components": [],
            "coverage": 0.98, "status": "ready",
        },
        "C": {
            "value": 50, "rawValue": 0.5, "delta1d": 0,
            "historyPercentile120d": 50, "components": [],
            "coverage": 0.98, "status": "ready",
        },
        "V": {
            "value": 50, "rawValue": 0.5, "delta1d": 0,
            "historyPercentile120d": 50, "components": [],
            "coverage": 0.98, "status": "ready",
        },
        "coverage": 0.98,
        "_pq_diff_history_pct": 95,
        "_q_delta1d_history_pct": 20,
        "_u_delta1d_history_pct": 30,
        "_v_delta1d_history_pct": 50,
        "_structure_breakdown_not_rising": 1,
        "_c_rising": 0,
        "_c_high_anomaly": 0,
    }

    hits = evaluate_filters(ctx)
    hit_types = {f.signal_type for f in hits}
    print(f"hits: {hit_types}")
    assert "surface_strong_internal_weak" in hit_types, "A1 应命中"

    payload = build_signal_payloads(
        hits[0], ctx, duration_days=1, scope_type="market", scope_name="全市场",
    )
    assert payload["trigger_payload"]["signal_type"] == "surface_strong_internal_weak"
    assert payload["rank_key"]["scope_type_priority"] == 1
    print(f"OK: filter_engine hits={len(hits)} rank_key={payload['rank_key']}")

    # [P0-7] D 族筛选器自测：canonical scope_observation（D2/D4）+ pyramid_v2（D1/D3/D5）
    ctx_d = {
        "P": {"value": 50, "status": "ready", "components": []},
        "Q": {"value": 50, "status": "ready", "components": []},
        "U": {"value": 50, "status": "ready", "components": []},
        "C": {"value": 50, "status": "ready", "components": []},
        "V": {"value": 50, "status": "ready", "components": []},
        "coverage": 0.98,
        # Slice 4A4 — D2/D4 must read from canonical scope_observation.
        "scope_observation": {
            "freshness": {
                "today_count": 2,
                "last_5d_count": 5,
                "decay_weighted_density": 0.45,
            },
            "structure": {
                "current_state": {
                    "technical_state": {
                        "concentration": {
                            "hhi": 0.15,
                            "top5_contribution": {"numerator": 0.5, "denominator": 1.0},
                            "leader_median_gap": 3.5,
                            "leader_symbol": "000001",
                        },
                    },
                },
            },
        },
        # D1 / D3 / D5 remain on legacy pyramid_v2 (this Slice only cuts D2/D4).
        "pyramid_v2": {
            "diffusion": {
                "positive_migration_count": 8,
                "negative_migration_count": 3,
                "total_migration_count": 11,
                "positive_ratio": {"numerator": 8, "denominator": 11},
                "negative_ratio": {"numerator": 3, "denominator": 11},
                "participation_coverage": {"numerator": 15, "denominator": 40},
            },
            "relative_strength": {
                "vs_market": {"ratio": 1.25, "label": "strong", "diff": 0.15},
                "equal_weight_diff": 0.15,
            },
        },
    }
    d_hits = evaluate_filters(ctx_d)
    d_types = {f.signal_type for f in d_hits}
    print(f"D-family hits: {d_types}")
    assert "state_migration_positive" in d_types, "D1 应命中"
    assert "event_freshness_high" in d_types, "D2 应命中（canonical freshness）"
    assert "breadth_expansion" in d_types, "D3 应命中（coverage=0.375>=0.3, total=11>=5）"
    assert "concentration_high" in d_types, "D4 应命中（canonical technical-state concentration）"
    assert "relative_strength_strong" in d_types, "D5 应命中"

    # 无 canonical scope_observation（且无 pyramid_v2）时 D 族不命中
    ctx_no_pv2 = {"P": {}, "Q": {}, "U": {}, "C": {}, "V": {}, "coverage": 0.98}
    d_hits_empty = evaluate_filters(ctx_no_pv2)
    d_types_empty = {f.signal_type for f in d_hits_empty}
    assert not any(f.family.value == "D" for f in d_hits_empty), "无 canonical/旧 payload 时 D 族不应命中"
    print(f"OK: no-D-inputs D-family hits: {d_types_empty}")

    print("OK: filter_engine verified")
