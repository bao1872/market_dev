"""ReviewAttributionEngine - 子范围与个股归因（PRD §9）。

PRD §9 归因合同：
- 筛选器只负责发现，归因负责解释
- 子范围贡献：找到父范围的直接子范围和关联概念，计算子范围对父范围
  P/Q/U/C/V 变化的贡献；保留正贡献和负贡献；按绝对贡献排序；
  保存前 N 项，但 API 支持分页读取全部
- 个股贡献：每只成员计算对 P/Q/U/C/V 的贡献、新鲜事件、与板块状态的关系
- 归因不得仅按涨幅排序
- 角色分类与因子状态分开保存，角色必须保留 role_evidence

输入数据由 service 层准备：
- 父范围 flat_list 与 P/Q/U/C/V payload
- 子范围 flat_list 字典：{child_scope_key: flat_list}
- 个股 flat_list 字典（含 symbol/instrument_id）

模块自测：
    python -m app.domain.review.attribution_engine
"""

from __future__ import annotations

from typing import Any

from app.domain.first_pyramid_semantics import Direction, MomentumChange, MomentumDirection
from app.domain.review.metric_engine import (
    _safe_float,
    compute_all_metrics,
)
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter

# =============================================================================
# 子范围贡献
# =============================================================================


def compute_child_scope_contribution(
    parent_metrics: dict[str, dict[str, Any]],
    child_metrics: dict[str, dict[str, Any]],
    *,
    parent_ready_count: int,
    child_ready_count: int,
) -> dict[str, float | None]:
    """计算单个子范围对父范围 P/Q/U/C/V 的贡献。

    贡献定义：child_value * (child_ready / parent_ready) - parent_value * 0
    简化为：child_delta_vs_parent = child_value - parent_value
    加权贡献 = (child_value - parent_value) * (child_ready / parent_ready)

    正值表示子范围优于父范围（正向贡献），负值表示拖累。

    Args:
        parent_metrics: 父范围 P/Q/U/C/V payload
        child_metrics: 子范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        child_ready_count: 子范围有效成员数

    Returns:
        {"P": contribution, "Q": contribution, ...}（None 表示数据不足）
    """
    weight = (
        child_ready_count / parent_ready_count
        if parent_ready_count > 0 else 0.0
    )
    out: dict[str, float | None] = {}
    for code in ("P", "Q", "U", "C", "V"):
        p_val = _safe_float((parent_metrics.get(code) or {}).get("value"))
        c_val = _safe_float((child_metrics.get(code) or {}).get("value"))
        if p_val is None or c_val is None:
            out[code] = None
        else:
            out[code] = (c_val - p_val) * weight
    return out


def aggregate_child_scope_attributions(
    parent_metrics: dict[str, dict[str, Any]],
    parent_ready_count: int,
    child_scopes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """聚合多个子范围归因结果（PRD §9.1）。

    Args:
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        child_scopes: 每个元素为 {
            "scope_type": str,
            "scope_key": str,
            "scope_name": str,
            "relation_type": str,
            "flat_list": list[dict],
            "ready_count": int,
        }

    Returns:
        归因结果列表（按绝对贡献绝对值降序排序），每个元素含：
        - child_scope_type / child_scope_key / child_scope_name / relation_type
        - contribution_value（综合贡献，按 P/Q/U/C/V 平均）
        - contribution_rank
        - metrics_payload（子范围 P/Q/U/C/V payload）
        - evidence_payload（贡献分解）
        - coverage_ratio
    """
    results: list[dict[str, Any]] = []
    for child in child_scopes:
        flat_list = child.get("flat_list") or []
        ready = child.get("ready_count") or len(flat_list)
        if ready == 0:
            continue
        child_metrics = compute_all_metrics(flat_list, ready_count=ready)
        contribs = compute_child_scope_contribution(
            parent_metrics, child_metrics,
            parent_ready_count=parent_ready_count,
            child_ready_count=ready,
        )
        # 综合贡献 = P/Q/U/C/V 贡献的平均（保留正负号）
        valid_contribs = [v for v in contribs.values() if v is not None]
        composite = (
            sum(valid_contribs) / len(valid_contribs)
            if valid_contribs else None
        )
        results.append({
            "child_scope_type": child["scope_type"],
            "child_scope_key": child["scope_key"],
            "child_scope_name": child["scope_name"],
            "relation_type": child.get("relation_type"),
            "contribution_value": composite,
            "metrics_payload": child_metrics,
            "evidence_payload": {
                "contributions": contribs,
                "weight": (
                    ready / parent_ready_count
                    if parent_ready_count > 0 else 0.0
                ),
                "denominator": parent_ready_count,
                "parent_scope_type": child.get("parent_scope_type"),
                "parent_scope_key": child.get("parent_scope_key"),
                "source_board_snapshot_id": str(child["source_board_snapshot_id"]),
                "relation": child.get("relation_type"),
                "taxonomy_version": child.get("taxonomy_version"),
                "taxonomy_compatibility_key": child.get(
                    "taxonomy_compatibility_key"
                ),
                "membership_version": child.get("membership_version"),
                "child_ready_count": ready,
                "fresh_events": _aggregate_fresh_events(flat_list),
                "board_sync": _classify_child_board_sync(
                    flat_list, parent_metrics,
                ),
                "data_quality": child.get("data_quality") or {},
            },
            "coverage_ratio": child.get("coverage_ratio", 0.0),
            "source_board_snapshot_id": child.get("source_board_snapshot_id"),
            "taxonomy_version": child.get("taxonomy_version"),
            "taxonomy_compatibility_key": child.get("taxonomy_compatibility_key"),
            "membership_version": child.get("membership_version"),
            "eligible_count": child.get("eligible_count"),
            "ready_count": ready,
            "data_quality": child.get("data_quality") or {},
        })

    # 按绝对贡献降序排序（PRD §9.1：不得仅按涨幅排序，使用绝对贡献）
    results.sort(
        key=lambda r: abs(r["contribution_value"])
        if r["contribution_value"] is not None else 0,
        reverse=True,
    )
    # 写入 contribution_rank（1-based）
    for i, r in enumerate(results, start=1):
        r["contribution_rank"] = i
    return results


# =============================================================================
# 个股贡献
# =============================================================================


def compute_instrument_contribution(
    flat: dict[str, Any],
    parent_metrics: dict[str, dict[str, Any]],
    *,
    parent_ready_count: int,
) -> dict[str, float | None]:
    """计算单只成员对 P/Q/U/C/V 的贡献（PRD §9.2）。

    个股贡献定义（简化版本，可由 service 层按需要扩展）：
    - P 贡献：成员 change_pct - 父范围 P.rawValue
    - Q 贡献：成员趋势/结构/动量对齐度（0-3） - 父范围 Q.rawValue * 3
    - U 贡献：成员维度改善数（0-4）/ 4 - 父范围 U.rawValue
    - C 贡献：成员 |change_pct| - 父范围成员中位数 |change_pct|
    - V 贡献：成员 volume_ratio20 - 1（放量扩张程度）

    Args:
        flat: 成员 first_pyramid_flat
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数

    Returns:
        {"P": float|None, "Q": float|None, "U": float|None, "C": float|None, "V": float|None}
    """
    weight = 1.0 / max(1, parent_ready_count)
    out: dict[str, float | None] = {}

    # P 贡献
    chg = _safe_float(flat.get("fp_segment_change_pct"))
    p_raw = _safe_float((parent_metrics.get("P") or {}).get("rawValue"))
    if chg is not None and p_raw is not None:
        out["P"] = (chg - p_raw) * weight
    else:
        out["P"] = None

    # Q 贡献：成员 trend/swing/momentum 三维对齐度
    semantics = FirstPyramidSemanticAdapter(flat)
    score = 0
    if semantics.trend is Direction.UP:
        score += 1
    if semantics.swing is Direction.UP:
        score += 1
    if semantics.momentum_direction is MomentumDirection.EXPANDING:
        score += 1
    q_raw = _safe_float((parent_metrics.get("Q") or {}).get("rawValue"))
    if q_raw is not None:
        out["Q"] = (score / 3.0 - q_raw) * weight
    else:
        out["Q"] = None

    # U 贡献：成员改善维度数 / 4 - 父范围 U.rawValue
    improving = 0
    if semantics.trend is Direction.UP:
        improving += 1
    if semantics.swing is Direction.UP:
        improving += 1
    if semantics.momentum_direction is MomentumDirection.EXPANDING:
        improving += 1
    if semantics.momentum_change is MomentumChange.ENHANCING:
        improving += 1
    u_raw = _safe_float((parent_metrics.get("U") or {}).get("rawValue"))
    if u_raw is not None:
        out["U"] = (improving / 4.0 - u_raw) * weight
    else:
        out["U"] = None

    # C 贡献：成员 |change_pct| - 父范围 rawValue（近似中位数）
    if chg is not None and p_raw is not None:
        out["C"] = (abs(chg) - abs(p_raw)) * weight
    else:
        out["C"] = None

    # V 贡献：成员 volume_ratio20 - 1
    vr = _safe_float(flat.get("fp_volume_ratio20"))
    if vr is not None:
        out["V"] = (vr - 1.0) * weight
    else:
        out["V"] = None

    return out


def classify_instrument_board_role(
    flat: dict[str, Any],
    rank_in_scope: int,
    total_ready: int,
) -> str:
    """分类成员的板块角色（PRD §5.6 board_role 枚举）。

    角色分类必须保留 role_evidence，由相对贡献和历史稳定性生成。

    Args:
        flat: 成员 first_pyramid_flat
        rank_in_scope: 该成员在范围内按贡献排名（1-based，1=最强）
        total_ready: 范围有效成员数

    Returns:
        core / second_line / elasticity / follower / laggard / unclassified
    """
    if total_ready <= 0:
        return "unclassified"

    # 排名百分比（0-100，越小越强）
    pct = rank_in_scope / total_ready * 100
    trend = FirstPyramidSemanticAdapter(flat).trend

    # 龙头判定：排名前 10% 且 trend=up 且 momentum_direction=up
    if pct <= 10 and trend is Direction.UP:
        return "core"

    # 二线：排名前 10-30% 且 trend=up
    if pct <= 30 and trend is Direction.UP:
        return "second_line"

    # 弹性：量能扩张且动量增强；segment change 不是日收益，禁止用于角色判定。
    vr = _safe_float(flat.get("fp_volume_ratio20"))
    momentum_change = FirstPyramidSemanticAdapter(flat).momentum_change
    if vr is not None and vr > 1.5 and momentum_change is MomentumChange.ENHANCING:
        return "elasticity"

    # 跟随：排名 30-70% 且 trend != down
    if pct <= 70 and trend is not Direction.DOWN:
        return "follower"

    # 滞后：trend=down 或排名后 30%
    if trend is Direction.DOWN or pct > 70:
        return "laggard"

    return "unclassified"


def classify_instrument_relation_to_scope(
    flat: dict[str, Any],
    parent_p_value: float | None,
    parent_u_value: float | None,
) -> str:
    """Classify Board synchronization from canonical state, never segment return."""
    semantics = FirstPyramidSemanticAdapter(flat)
    if semantics.trend is None:
        return "unconfirmed"

    instrument_strong = (
        semantics.trend is Direction.UP
        and semantics.momentum_change is MomentumChange.ENHANCING
    )
    instrument_weak = (
        semantics.trend is Direction.DOWN
        or semantics.momentum_change is MomentumChange.WEAKENING
    )
    scope_strong = parent_p_value is not None and parent_p_value >= 60
    scope_weak = parent_p_value is not None and parent_p_value < 40

    if instrument_strong and scope_strong:
        return "synchronized_strengthening"
    if instrument_weak and scope_weak:
        return "synchronized_weakening"
    if instrument_strong and scope_weak:
        return "instrument_leads_scope"
    if instrument_weak and scope_strong:
        return "scope_strong_instrument_lags"
    if instrument_strong and parent_u_value is not None and parent_u_value < 50:
        return "instrument_strong_scope_unsupported"
    return "unconfirmed"


def aggregate_instrument_attributions(
    parent_metrics: dict[str, dict[str, Any]],
    parent_ready_count: int,
    instruments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """聚合多个成员个股的归因结果（PRD §9.2）。

    Args:
        parent_metrics: 父范围 P/Q/U/C/V payload
        parent_ready_count: 父范围有效成员数
        instruments: 每个元素为 {
            "instrument_id": str,
            "symbol": str,
            "name": str,
            "flat": dict,
            "source_snapshot_id": str|None,
        }

    Returns:
        成员归因列表（按综合贡献绝对值降序排序），每个元素含：
        - instrument_id / symbol / name
        - board_role / relation_to_scope
        - contribution_value / contribution_rank
        - first_pyramid_payload（趋势/结构/动量/筹码摘要）
        - fresh_events_payload
        - source_snapshot_id
    """
    parent_p_value = _safe_float((parent_metrics.get("P") or {}).get("value"))
    parent_u_value = _safe_float((parent_metrics.get("U") or {}).get("value"))

    results: list[dict[str, Any]] = []
    for inst in instruments:
        flat = inst.get("flat") or {}
        contribs = compute_instrument_contribution(
            flat, parent_metrics, parent_ready_count=parent_ready_count,
        )
        valid = [v for v in contribs.values() if v is not None]
        composite = sum(valid) / len(valid) if valid else None
        results.append({
            "instrument_id": inst["instrument_id"],
            "symbol": inst["symbol"],
            "name": inst["name"],
            "contribution_value": composite,
            "contribution_breakdown": contribs,
            "flat": flat,
            "source_snapshot_id": inst.get("source_snapshot_id"),
        })

    # 按综合贡献绝对值降序排序（PRD §9.2：不得仅按涨幅排序）
    results.sort(
        key=lambda r: abs(r["contribution_value"])
        if r["contribution_value"] is not None else 0,
        reverse=True,
    )

    # 写入 rank / board_role / relation_to_scope / payload
    for i, r in enumerate(results, start=1):
        r["contribution_rank"] = i
        r["board_role"] = classify_instrument_board_role(
            r["flat"], i, len(results),
        )
        r["relation_to_scope"] = classify_instrument_relation_to_scope(
            r["flat"], parent_p_value, parent_u_value,
        )
        r["first_pyramid_payload"] = _extract_first_pyramid_summary(r["flat"])
        r["fresh_events_payload"] = _extract_fresh_events(r["flat"])
        r["contribution_payload"] = {
            "components": r["contribution_breakdown"],
            "denominator": parent_ready_count,
        }
        r["role_evidence"] = _build_role_evidence(
            r["flat"], i, len(results), r["contribution_breakdown"],
        )

    return results



def _aggregate_fresh_events(flat_list: list[dict[str, Any]]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for flat in flat_list:
        instrument_id = flat.get("_instrument_id")
        for event in _extract_fresh_events(flat)["events"]:
            events.append({**event, "instrument_id": instrument_id})
    return {"events": events, "count": len(events)}


def _classify_child_board_sync(
    flat_list: list[dict[str, Any]],
    parent_metrics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    directions = [FirstPyramidSemanticAdapter(flat).trend for flat in flat_list]
    comparable = [direction for direction in directions if direction is not None]
    if not comparable:
        return {"state": "unconfirmed", "comparable_count": 0}
    up_ratio = sum(direction is Direction.UP for direction in comparable) / len(comparable)
    down_ratio = sum(direction is Direction.DOWN for direction in comparable) / len(comparable)
    parent_p = _safe_float((parent_metrics.get("P") or {}).get("value"))
    if parent_p is None:
        state = "unconfirmed"
    elif (parent_p >= 60 and up_ratio >= 0.6) or (parent_p < 40 and down_ratio >= 0.6):
        state = "synchronized"
    else:
        state = "divergent"
    return {
        "state": state,
        "up_ratio": up_ratio,
        "down_ratio": down_ratio,
        "comparable_count": len(comparable),
    }


def _build_role_evidence(
    flat: dict[str, Any],
    rank: int,
    total: int,
    contributions: dict[str, float | None],
) -> dict[str, Any]:
    semantics = FirstPyramidSemanticAdapter(flat)
    return {
        "rank": rank,
        "total": total,
        "rank_percentile": rank / max(1, total),
        "trend": semantics.trend.value if semantics.trend is not None else None,
        "momentum_change": (
            semantics.momentum_change.value
            if semantics.momentum_change is not None else None
        ),
        "volume_ratio20": _safe_float(flat.get("fp_volume_ratio20")),
        "component_contributions": contributions,
    }

def _extract_first_pyramid_summary(flat: dict[str, Any]) -> dict[str, Any]:
    """提取成员第一金字塔摘要 payload（PRD §14.6 个股验证表字段）。"""
    semantics = FirstPyramidSemanticAdapter(flat)
    return {
        "trend": semantics.trend.value if semantics.trend is not None else None,
        "trend_strength": _safe_float(flat.get("fp_trend_strength")),
        "swing": semantics.swing.value if semantics.swing is not None else None,
        "internal": semantics.internal.value if semantics.internal is not None else None,
        "structure_alignment": (
            semantics.structure_alignment.value
            if semantics.structure_alignment is not None else None
        ),
        "momentum": (
            semantics.momentum_direction.value
            if semantics.momentum_direction is not None else None
        ),
        "momentum_change": (
            semantics.momentum_change.value
            if semantics.momentum_change is not None else None
        ),
        "squeeze_state": (
            semantics.squeeze_state.value
            if semantics.squeeze_state is not None else None
        ),
        "volume_badge": (
            semantics.volume_badge.value
            if semantics.volume_badge is not None else None
        ),
        "volume_ratio20": _safe_float(flat.get("fp_volume_ratio20")),
        "volume_percentile20": _safe_float(flat.get("fp_volume_percentile20")),
    }


def _extract_fresh_events(flat: dict[str, Any]) -> dict[str, Any]:
    """提取成员新鲜结构/动量事件（PRD §9.2 新鲜事件）。"""
    events: list[dict[str, Any]] = []
    for ev_type, dir_field, fresh_field in (
        ("BOS", "fp_latest_bos_direction", "fp_latest_bos_freshness"),
        ("CHoCH", "fp_latest_choch_direction", "fp_latest_choch_freshness"),
        ("OB", "fp_latest_ob_direction", "fp_latest_ob_freshness"),
    ):
        d = FirstPyramidSemanticAdapter(flat).event_direction(dir_field)
        f = flat.get(fresh_field)
        if d is not None and f is not None:
            events.append({
                "type": ev_type,
                "direction": d.value,
                "freshness": f,
            })
    return {"events": events, "count": len(events)}


if __name__ == "__main__":
    # 自测：子范围归因
    parent_metrics = {
        "P": {"value": 60.0, "rawValue": 0.6, "components": []},
        "Q": {"value": 50.0, "rawValue": 0.5, "components": []},
        "U": {"value": 55.0, "rawValue": 0.55, "components": []},
        "C": {"value": 45.0, "rawValue": 0.45, "components": []},
        "V": {"value": 50.0, "rawValue": 0.5, "components": []},
    }
    child_scopes = [
        {
            "scope_type": "industry_l2",
            "scope_key": "optoelectronics",
            "scope_name": "光电子",
            "relation_type": "child_industry",
            "flat_list": [
                {
                    "fp_trend_direction": "up", "fp_swing_direction": "up",
                    "fp_internal_direction": "up", "fp_momentum_direction": "up",
                    "fp_momentum_change": "enhancing",
                    "fp_structure_alignment": "aligned",
                    "fp_segment_change_pct": 3.5, "fp_volume_badge": "放量",
                    "fp_volume_ratio20": 1.8, "fp_volume_percentile20": 85.0,
                    "fp_volume_percentile200": 75.0,
                    "fp_distance_to_trailing_top_pct": 1.5,
                    "fp_latest_bos_direction": "up", "fp_latest_bos_freshness": 1,
                    "fp_latest_choch_direction": None, "fp_latest_choch_freshness": None,
                    "fp_latest_ob_direction": "up", "fp_latest_ob_freshness": 2,
                    "fp_amount": 1e8, "fp_segment_volume_ratio": 1.5,
                    "fp_prev_segment_volume": 1.0,
                }
                for _ in range(10)
            ],
            "ready_count": 10,
        },
    ]
    results = aggregate_child_scope_attributions(parent_metrics, 100, child_scopes)
    assert len(results) == 1
    assert results[0]["contribution_rank"] == 1
    print(
        f"OK: child attribution {results[0]['child_scope_name']} "
        f"contrib={results[0]['contribution_value']:.4f}"
    )

    # 自测：个股归因
    instruments = [
        {
            "instrument_id": "00000000-0000-0000-0000-000000000001",
            "symbol": "000021",
            "name": "深科技",
            "flat": {
                "fp_trend_direction": "up", "fp_swing_direction": "up",
                "fp_internal_direction": "up", "fp_momentum_direction": "up",
                "fp_momentum_change": "enhancing",
                "fp_segment_change_pct": 5.0, "fp_volume_ratio20": 2.0,
                "fp_volume_badge": "放量", "fp_volume_percentile20": 90.0,
                "fp_latest_bos_direction": "up", "fp_latest_bos_freshness": 1,
            },
        },
        {
            "instrument_id": "00000000-0000-0000-0000-000000000002",
            "symbol": "000022",
            "name": "深赤湾A",
            "flat": {
                "fp_trend_direction": "down", "fp_swing_direction": "down",
                "fp_internal_direction": "down", "fp_momentum_direction": "down",
                "fp_momentum_change": "fading",
                "fp_segment_change_pct": -3.0, "fp_volume_ratio20": 0.5,
                "fp_volume_badge": "缩量", "fp_volume_percentile20": 20.0,
            },
        },
    ]
    inst_results = aggregate_instrument_attributions(parent_metrics, 100, instruments)
    assert len(inst_results) == 2
    assert inst_results[0]["contribution_rank"] == 1
    print(
        f"OK: instrument attribution top={inst_results[0]['symbol']} "
        f"role={inst_results[0]['board_role']} "
        f"relation={inst_results[0]['relation_to_scope']}"
    )
    print("OK: attribution_engine verified")
