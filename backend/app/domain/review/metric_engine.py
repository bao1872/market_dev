"""ReviewMetricEngine - P/Q/U/C/V 聚合变量计算引擎（PRD §7）。

输入：service 层从 stock_core snapshot 读取的成员 first_pyramid_flat 列表
输出：每个 metric 的 payload（value/rawValue/delta1d/delta5d/
       historyPercentile120d/crossSectionPercentile/components/coverage/status）

PRD §7.1 通用结构：
    {
        "value": 63.4,           # 归一化值（0-100）
        "rawValue": 0.572,       # 原始加权值
        "delta1d": -4.1,         # 1 日变化（归一化值差）
        "delta5d": 6.7,          # 5 日变化
        "historyPercentile120d": 78.2,
        "crossSectionPercentile": 84.0,
        "historyObservationCount": 120,
        "components": [...],     # 每个 component 含原始值/方向/分母/字段来源/权重
        "coverage": 0.982,
        "status": "ready"        # ready/insufficient_history/partial/unavailable
    }

PRD §7.1 规范：
- value 范围 0-100
- 默认按该范围自身 120 日历史分位归一化
- 历史少于 60 日时 status=insufficient_history，不得伪造分位
- delta1d/delta5d 使用归一化值变化
- value = available component normalized values 的版本化加权平均

模块自测：
    python -m app.domain.review.metric_engine
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.first_pyramid_semantics import (
    Direction,
    MomentumChange,
    MomentumDirection,
    StructureAlignment,
    VolumeBadge,
)
from app.domain.review.metric_registry import (
    DEFAULT_REGISTRY,
    MetricComponentSpec,
    MetricSpec,
    ReviewMetricComponentRegistry,
)
from app.services.first_pyramid_semantic_adapter import FirstPyramidSemanticAdapter

logger = logging.getLogger("review.metric_engine")

# 默认基线窗口与最低窗口（PRD §0、§7.1）
DEFAULT_BASELINE_WINDOW = 120
MIN_BASELINE_WINDOW = 60

# 数值保护 epsilon（PRD §7.6 所有除法使用明确 epsilon）
_EPSILON = 1e-9

# 历史观测样本不足时返回的 status
STATUS_INSUFFICIENT_HISTORY = "insufficient_history"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"
STATUS_READY = "ready"


# =============================================================================
# 工具函数
# =============================================================================


def _safe_float(v: Any) -> float | None:
    """安全转换为 float，None/非数值/NaN 返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN
            return None
        return f
    except (TypeError, ValueError):
        return None


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """限制到 [lo, hi]。"""
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _percentile_of(value: float, history: list[float]) -> float | None:
    """计算 value 在 history 序列中的百分位（0-100，线性插值）。

    返回 None 表示 history 为空。
    """
    if not history:
        return None
    if len(history) < MIN_BASELINE_WINDOW:
        return None
    s = sorted(history)
    n = len(s)
    # value 小于等于 s 中多少比例
    # 使用线性插值（与 numpy.percentile 默认 linear 一致）
    # 找到 value 在 s 中的位置
    le_count = sum(1 for x in s if x <= value)
    # 边界处理
    if value <= s[0]:
        return 0.0
    if value >= s[-1]:
        return 100.0
    # 线性插值
    # 简化：直接用 le_count / n * 100
    return _clamp(le_count / n * 100.0)


def _cross_section_percentile(value: float, peers: list[float]) -> float | None:
    """Rank within same-day peers; the 60-observation gate is history-only."""
    finite = [item for item in peers if item == item]
    if not finite:
        return None
    below_or_equal = sum(item <= value for item in finite)
    return _clamp(below_or_equal / len(finite) * 100.0)


def _normalize_component(
    raw_value: float | None,
    direction: str,
    history: list[float] | None = None,
) -> float | None:
    """将 component 原始值归一化到 0-100。

    归一化策略（PRD §7.1）：
    - 默认按该范围自身 120 日历史分位归一化
    - 历史不足 60 日时返回 None（status=insufficient_history）
    - 反向 component（direction=negative）：归一化值 = 100 - 正向归一化值
    """
    if raw_value is None:
        return None
    if not history or len(history) < MIN_BASELINE_WINDOW:
        return None
    pct = _percentile_of(raw_value, history)
    if pct is None:
        return None
    if direction == "negative":
        return _clamp(100.0 - pct)
    return _clamp(pct)


def _has_daily_return_data(flat_list: list[dict[str, Any]]) -> bool:
    """Return whether the PIT daily price fact exists for at least one member."""
    return any(_safe_float(f.get("review_return_1d")) is not None for f in flat_list)


# =============================================================================
# 派生 component 计算函数（对应 registry 中的 derive_fn）
# =============================================================================


def _derive_scope_return_1d(flat_list: list[dict[str, Any]]) -> float | None:
    """范围 1 日收益率（成员等权中位数）。

    PRD §7.2：优先官方指数；无官方序列时使用成员等权中位数，
    并记录 price_source=member_equal_weight。
    """
    returns = [_safe_float(f.get("review_return_1d")) for f in flat_list]
    returns = [r for r in returns if r is not None]
    if not returns:
        return None
    returns.sort()
    n = len(returns)
    if n % 2 == 1:
        return returns[n // 2]
    return (returns[n // 2 - 1] + returns[n // 2]) / 2.0  # type: ignore[operator, index]


def _derive_advance_ratio(flat_list: list[dict[str, Any]]) -> float | None:
    """当日上涨成员比例（PRD §7.2）。"""
    if not flat_list:
        return None
    ready = 0
    up = 0
    for f in flat_list:
        chg = _safe_float(f.get("review_return_1d"))
        if chg is None:
            continue
        ready += 1
        if chg > 0:
            up += 1
    if ready == 0:
        return None
    return up / ready


def _derive_trend_price_alignment_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """趋势向上且当日上涨成员数 / ready_count（PRD §7.2）。"""
    if not flat_list:
        return None
    ready = 0
    aligned = 0
    for f in flat_list:
        chg = _safe_float(f.get("review_return_1d"))
        td = FirstPyramidSemanticAdapter(f).trend
        if chg is None or td is None:
            continue
        ready += 1
        if td is Direction.UP and chg > 0:
            aligned += 1
    if ready == 0:
        return None
    return aligned / ready


def _derive_new_high_ratio(flat_list: list[dict[str, Any]]) -> float | None:
    """进入近期高位区间的成员比例（PRD §7.2）。

    近期高位定义：当日收盘位于自身 120 日高低区间顶部 3%。
    """
    if not flat_list:
        return None
    ready = 0
    near_high = 0
    for f in flat_list:
        position = _safe_float(f.get("review_price_position"))
        if position is None:
            continue
        ready += 1
        if position >= 0.97:
            near_high += 1
    if ready == 0:
        return None
    return near_high / ready


def _derive_price_position_median(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """成员价格在自身滚动区间的位置中位数（PRD §7.2）。

    位置由成员事实层用 point-in-time 日线计算。
    """
    positions: list[float] = []
    for f in flat_list:
        position = _safe_float(f.get("review_price_position"))
        if position is None:
            continue
        positions.append(max(0.0, min(1.0, position)))
    if not positions:
        return None
    positions.sort()
    n = len(positions)
    if n % 2 == 1:
        return positions[n // 2]
    return (positions[n // 2 - 1] + positions[n // 2]) / 2.0


def _derive_structure_net_event_rate(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """bullish 结构事件率 - bearish 结构事件率（PRD §7.3）。

    结构事件来源：BOS / CHoCH / OB（direction=up/down）。
    rate = (bullish_count - bearish_count) / ready_count，结果范围 [-1, 1]。
    """
    if not flat_list:
        return None
    ready = 0
    bull = 0
    bear = 0
    for f in flat_list:
        semantics = FirstPyramidSemanticAdapter(f)
        if semantics.trend is None:
            continue
        ready += 1
        for ev_field in (
            "fp_latest_bos_direction",
            "fp_latest_choch_direction",
            "fp_latest_ob_direction",
        ):
            d = semantics.event_direction(ev_field)
            if d is Direction.UP:
                bull += 1
            elif d is Direction.DOWN:
                bear += 1
    if ready == 0:
        return None
    return (bull - bear) / (ready * 3)  # 3 个事件来源


def _derive_structure_breakdown_diffusion(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """结构破坏扩散率（反向 component，PRD §7.3）。

    定义：出现 bearish CHoCH 或 bearish BOS 的成员比例（值越大 Q 越差）。
    """
    if not flat_list:
        return None
    ready = 0
    broken = 0
    for f in flat_list:
        semantics = FirstPyramidSemanticAdapter(f)
        if semantics.trend is None:
            continue
        ready += 1
        bos_d = semantics.event_direction("fp_latest_bos_direction")
        choch_d = semantics.event_direction("fp_latest_choch_direction")
        if bos_d is Direction.DOWN or choch_d is Direction.DOWN:
            broken += 1
    if ready == 0:
        return None
    return broken / ready


def _derive_multi_dim_improving_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Share of members improving in at least two dimensions day over day."""
    if not flat_list:
        return None
    ready = 0
    multi = 0
    for f in flat_list:
        current = FirstPyramidSemanticAdapter(f)
        previous_payload = f.get("review_previous_first_pyramid")
        if current.trend is None or not isinstance(previous_payload, dict):
            continue
        previous = FirstPyramidSemanticAdapter(previous_payload)
        ready += 1
        improvements = sum(
            (
                _direction_score(current.trend) > _direction_score(previous.trend),
                _direction_score(current.swing) > _direction_score(previous.swing),
                _direction_score(current.internal) > _direction_score(previous.internal),
                _momentum_direction_score(current.momentum_direction)
                > _momentum_direction_score(previous.momentum_direction),
                _momentum_change_score(current.momentum_change)
                > _momentum_change_score(previous.momentum_change),
            )
        )
        if improvements >= 2:
            multi += 1
    if ready == 0:
        return None
    return multi / ready


def _direction_score(value: Direction | None) -> int:
    if value is None:
        return 0
    return {Direction.DOWN: -1, Direction.SIDEWAYS: 0, Direction.UP: 1}[value]


def _momentum_direction_score(value: MomentumDirection | None) -> int:
    if value is None:
        return 0
    return {
        MomentumDirection.CONTRACTING: -1,
        MomentumDirection.FLAT: 0,
        MomentumDirection.EXPANDING: 1,
    }[value]


def _momentum_change_score(value: MomentumChange | None) -> int:
    if value is None:
        return 0
    return {
        MomentumChange.WEAKENING: -1,
        MomentumChange.FLAT: 0,
        MomentumChange.ENHANCING: 1,
    }[value]


def _derive_momentum_enhancing_coverage(
    flat_list: list[dict[str, Any]],
) -> float | None:
    ready = 0
    enhancing = 0
    for flat in flat_list:
        previous_payload = flat.get("review_previous_first_pyramid")
        if not isinstance(previous_payload, dict):
            continue
        current = FirstPyramidSemanticAdapter(flat).momentum_change
        previous = FirstPyramidSemanticAdapter(previous_payload).momentum_change
        if current is None or previous is None:
            continue
        ready += 1
        if _momentum_change_score(current) > _momentum_change_score(previous):
            enhancing += 1
    return enhancing / ready if ready else None


def _derive_fresh_structure_event_coverage(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """新鲜结构事件覆盖率（PRD §7.4）。

    定义：BOS/CHoCH/OB freshness 非 None 的成员比例。
    """
    if not flat_list:
        return None
    ready = 0
    has_event = 0
    for f in flat_list:
        if FirstPyramidSemanticAdapter(f).trend is None:
            continue
        ready += 1
        if (
            f.get("fp_latest_bos_freshness") is not None
            or f.get("fp_latest_choch_freshness") is not None
            or f.get("fp_latest_ob_freshness") is not None
        ):
            has_event += 1
    if ready == 0:
        return None
    return has_event / ready


def _derive_non_head_participation_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """非头部成员参与比例（PRD §7.4）。

    定义：当日上涨且非头部（change_pct 处于后 70%）的成员比例。
    """
    if not flat_list:
        return None
    # 按 change_pct 排序，前 30% 视为头部
    valid = [
        (f, _safe_float(f.get("review_return_1d")))
        for f in flat_list
        if _safe_float(f.get("review_return_1d")) is not None
        and FirstPyramidSemanticAdapter(f).trend is not None
    ]
    if not valid:
        return None
    valid.sort(key=lambda x: x[1], reverse=True)  # type: ignore[arg-type, return-value]
    n = len(valid)
    head_count = max(1, int(n * 0.3))
    non_head = valid[head_count:]
    if not non_head:
        return None
    up = sum(1 for _, chg in non_head if chg > 0)  # type: ignore[misc, operator]
    return up / len(non_head)


def _derive_leader_follower_common_confirm_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """龙头、二线与普通成员共同确认比例（PRD §7.4）。

    简化定义：按 volume_ratio20 排序，前 20% 为龙头，中 60% 为二线，后 20% 为普通；
    若三组各自上涨比例均 >= 50%，返回 1.0，否则返回三组平均上涨比例。

    无日收益数据时无法判断方向确认，结果应为 None。
    """
    if not flat_list:
        return None
    # 前置检查：若全部成员日收益为空，直接返回 None
    has_any_change = any(
        _safe_float(f.get("review_return_1d")) is not None
        for f in flat_list
    )
    if not has_any_change:
        return None
    valid = [
        (f, _safe_float(f.get("fp_volume_ratio20")))
        for f in flat_list
        if _safe_float(f.get("fp_volume_ratio20")) is not None
        and FirstPyramidSemanticAdapter(f).trend is not None
    ]
    if len(valid) < 5:
        return None
    valid.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)
    n = len(valid)
    leader = valid[: max(1, n // 5)]
    normal = valid[-(max(1, n // 5)):]
    second_line = valid[max(1, n // 5): n - max(1, n // 5)] or valid
    if not second_line:
        return None

    def _up_ratio(group: list[tuple[dict[str, Any], float | None]]) -> float:
        if not group:
            return 0.0
        up = 0
        for f, _ in group:
            chg = _safe_float(f.get("review_return_1d"))
            if chg is not None and chg > 0:
                up += 1
        return up / len(group)

    leader_up = _up_ratio(leader)
    second_up = _up_ratio(second_line)
    normal_up = _up_ratio(normal)
    if leader_up >= 0.5 and second_up >= 0.5 and normal_up >= 0.5:
        return 1.0
    return (leader_up + second_up + normal_up) / 3.0


def _derive_top5_contribution(flat_list: list[dict[str, Any]]) -> float | None:
    """绝对价格变化贡献 Top5 占比（PRD §7.5）。"""
    changes = [
        (f, _safe_float(f.get("review_return_1d")))
        for f in flat_list
        if _safe_float(f.get("review_return_1d")) is not None
    ]
    if not changes:
        return None
    abs_changes = [(f, abs(chg)) for f, chg in changes]  # type: ignore[arg-type]
    abs_changes.sort(key=lambda x: x[1], reverse=True)
    top5 = abs_changes[:5]
    total = sum(c for _, c in abs_changes)
    if total < _EPSILON:
        return None
    return sum(c for _, c in top5) / total


def _derive_top10pct_event_contribution(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """事件贡献 Top10% 成员占比（PRD §7.5）。"""
    event_counts: list[tuple[dict[str, Any], int]] = []
    for f in flat_list:
        cnt = 0
        if f.get("fp_latest_bos_freshness") is not None:
            cnt += 1
        if f.get("fp_latest_choch_freshness") is not None:
            cnt += 1
        if f.get("fp_latest_ob_freshness") is not None:
            cnt += 1
        event_counts.append((f, cnt))
    if not event_counts:
        return None
    event_counts.sort(key=lambda x: x[1], reverse=True)
    n = len(event_counts)
    top10pct_count = max(1, int(n * 0.1))
    top = event_counts[:top10pct_count]
    total_events = sum(c for _, c in event_counts)
    if total_events == 0:
        return 0.0
    return sum(c for _, c in top) / total_events


def _derive_member_change_hhi(flat_list: list[dict[str, Any]]) -> float | None:
    """成员绝对变化贡献 HHI（赫芬达尔指数，PRD §7.5）。

    HHI = sum((abs_change_i / total_abs_change)^2)，范围 [0, 1]。
    """
    changes = [
        _safe_float(f.get("review_return_1d"))
        for f in flat_list
        if _safe_float(f.get("review_return_1d")) is not None
    ]
    if not changes:
        return None
    abs_changes = [abs(c) for c in changes]  # type: ignore[arg-type]
    total = sum(abs_changes)
    if total < _EPSILON:
        return None
    return sum((c / total) ** 2 for c in abs_changes)


def _derive_leader_median_diff(flat_list: list[dict[str, Any]]) -> float | None:
    """龙头与成员中位数表现差（PRD §7.5）。

    简化：top1 成员 change_pct - 中位数 change_pct。
    """
    changes = [
        _safe_float(f.get("review_return_1d"))
        for f in flat_list
        if _safe_float(f.get("review_return_1d")) is not None
    ]
    if len(changes) < 3:
        return None
    changes.sort(reverse=True)
    top1 = changes[0]
    n = len(changes)
    if n % 2 == 1:
        median = changes[n // 2]
    else:
        median = (changes[n // 2 - 1] + changes[n // 2]) / 2.0  # type: ignore[operator, index]
    return top1 - median  # type: ignore[operator]


def _derive_top5_amount_contribution(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Top5 成交额占比（PRD §7.5）。"""
    amounts = [
        (f, _safe_float(f.get("review_amount")))
        for f in flat_list
        if _safe_float(f.get("review_amount")) is not None
    ]
    if len(amounts) < 5:
        return None
    amounts.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)
    total = sum(a for _, a in amounts)  # type: ignore[misc]
    if total < _EPSILON:
        return None
    return sum(a for _, a in amounts[:5]) / total  # type: ignore[misc]


def _derive_volume_expansion_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Members whose daily volume exceeds 1.5 times the prior 20-day mean."""
    if not flat_list:
        return None
    ready = 0
    high = 0
    for f in flat_list:
        ratio = _safe_float(f.get("review_volume_ratio20"))
        if ratio is None:
            continue
        ready += 1
        if ratio > 1.5:
            high += 1
    if ready == 0:
        return None
    return high / ready


def _derive_amount_expansion_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Members whose amount exceeds 1.5 times the prior 20-day mean."""
    if not flat_list:
        return None
    ready = 0
    expand = 0
    for f in flat_list:
        ratio = _safe_float(f.get("review_amount_ratio20"))
        if ratio is None:
            continue
        ready += 1
        if ratio > 1.5:
            expand += 1
    if ready == 0:
        return None
    return expand / ready


def _derive_trend_segment_volume_improvement(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Median current-segment mean volume / previous-segment mean volume."""
    ratios: list[float] = []
    for f in flat_list:
        ratio = _safe_float(f.get("fp_segment_volume_ratio"))
        if ratio is None:
            continue
        ratios.append(ratio)
    if not ratios:
        return None
    ratios.sort()
    n = len(ratios)
    if n % 2 == 1:
        return ratios[n // 2]
    return (ratios[n // 2 - 1] + ratios[n // 2]) / 2.0


def _derive_price_amount_efficiency_median(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """价格变化 / 相对成交额的效率中位数（PRD §7.6）。

    efficiency = abs(return_1d) / max(amount_ratio20, epsilon)
    """
    effs: list[float] = []
    for f in flat_list:
        chg = _safe_float(f.get("review_return_1d"))
        amount_ratio = _safe_float(f.get("review_amount_ratio20"))
        if chg is None or amount_ratio is None or amount_ratio < _EPSILON:
            continue
        effs.append(abs(chg) / amount_ratio)
    if not effs:
        return None
    effs.sort()
    n = len(effs)
    if n % 2 == 1:
        return effs[n // 2]
    return (effs[n // 2 - 1] + effs[n // 2]) / 2.0


# derive_fn 调度表
_DERIVE_FNS: dict[str, Any] = {
    "scope_return_1d": _derive_scope_return_1d,
    "advance_ratio": _derive_advance_ratio,
    "trend_price_alignment_ratio": _derive_trend_price_alignment_ratio,
    "new_high_ratio": _derive_new_high_ratio,
    "price_position_median": _derive_price_position_median,
    "structure_net_event_rate": _derive_structure_net_event_rate,
    "structure_breakdown_diffusion": _derive_structure_breakdown_diffusion,
    "multi_dim_improving_ratio": _derive_multi_dim_improving_ratio,
    "momentum_enhancing_coverage": _derive_momentum_enhancing_coverage,
    "fresh_structure_event_coverage": _derive_fresh_structure_event_coverage,
    "non_head_participation_ratio": _derive_non_head_participation_ratio,
    "leader_follower_common_confirm_ratio": _derive_leader_follower_common_confirm_ratio,
    "top5_contribution": _derive_top5_contribution,
    "top10pct_event_contribution": _derive_top10pct_event_contribution,
    "member_change_hhi": _derive_member_change_hhi,
    "leader_median_diff": _derive_leader_median_diff,
    "top5_amount_contribution": _derive_top5_amount_contribution,
    "volume_expansion_ratio": _derive_volume_expansion_ratio,
    "amount_expansion_ratio": _derive_amount_expansion_ratio,
    "trend_segment_volume_improvement": _derive_trend_segment_volume_improvement,
    "price_amount_efficiency_median": _derive_price_amount_efficiency_median,
}


# =============================================================================
# 单 component 原始值计算
# =============================================================================

# 方向类字段 → 是否计为"积极方向"的统一语义判断。
# [C2] 禁止直接比较 "up"/"enhancing"/"aligned"（中文"上行/共振/扩张"等值无法命中），
# 统一使用 FirstPyramidSemanticAdapter 将中文/英文/数字/枚举规范化为 canonical 类型。
# 关键修复：
#   - 中文"上行" → Direction.UP（计为积极）
#   - 中文"下行" → Direction.DOWN（不计为积极）
#   - "共振" → StructureAlignment.ALIGNED（计为积极）；"背离" → DIVERGENT（不计）
#   - fp_momentum_change 数值：>0 → ENHANCING（计为积极），<0 → WEAKENING（不计）
_SEMANTIC_POSITIVE: dict[str, tuple[str, Any]] = {
    "fp_trend_direction": ("direction", Direction.UP),
    "fp_swing_direction": ("direction", Direction.UP),
    "fp_internal_direction": ("direction", Direction.UP),
    "fp_structure_alignment": ("alignment", "aligned"),
    "fp_momentum_direction": ("momentum_direction", "expanding"),
    "fp_momentum_change": ("momentum_change", "enhancing"),
    "fp_volume_badge": ("volume_badge", "high"),
}


def _is_positive_semantic_direction(src: str, val: Any) -> bool:
    """按字段类型用 FirstPyramidSemanticAdapter 判定 val 是否属"积极方向"。

    未知字段返回 False（不把 truthy 值误判为积极方向）。
    """
    mapping = _SEMANTIC_POSITIVE.get(src)
    if mapping is None:
        return False
    kind, positive = mapping
    adapter = FirstPyramidSemanticAdapter()
    if kind == "direction":
        return adapter.direction(val) is Direction.UP
    if kind == "alignment":
        return adapter.alignment(val) is StructureAlignment.ALIGNED
    if kind == "momentum_direction":
        return adapter.momentum_direction_value(val) is MomentumDirection.EXPANDING
    if kind == "momentum_change":
        return adapter.momentum_change_value(val) is MomentumChange.ENHANCING
    if kind == "volume_badge":
        return adapter.volume_badge_value(val) is VolumeBadge.HIGH
    return False


def _compute_component_raw(
    spec: MetricComponentSpec,
    flat_list: list[dict[str, Any]],
) -> float | None:
    """计算单个 component 的原始值。

    - 若 spec.derive_fn 指定，调用对应派生函数
    - 否则按 field_source 直接对成员取值聚合（ratio 类按方向匹配）
    """
    if spec.derive_fn is not None:
        fn = _DERIVE_FNS.get(spec.derive_fn)
        if fn is None:
            logger.warning("未知 derive_fn: %s（component=%s）", spec.derive_fn, spec.name)
            return None
        return fn(flat_list)

    # 直接读 field_source：按字段值匹配方向
    if not flat_list:
        return None
    src = spec.field_source
    ready = 0
    matched = 0.0
    for f in flat_list:
        val = f.get(src)
        if val is None:
            continue
        ready += 1
        # 方向匹配：[C2] 统一用 FirstPyramidSemanticAdapter 判定积极方向，
        # 不再直接比较 "up"/"enhancing"/"aligned"（中文"上行/共振/扩张"与数值
        # 动量方向须经语义规范化，避免"下行/背离/走弱"被 truthy 误判为积极）。
        if src in _SEMANTIC_POSITIVE:
            if _is_positive_semantic_direction(src, val):
                matched += 1
        # 分位类字段（0-100）：取中位数 / 100
        elif src in (
            "fp_volume_percentile20",
            "fp_volume_percentile200",
            "review_volume_percentile20",
            "review_amount_percentile200",
        ):
            try:
                matched += float(val) / 100.0
            except (TypeError, ValueError):
                pass
        else:
            # 布尔型或其他：默认按 truthy 计数
            if val:
                matched += 1
    if ready == 0:
        return None
    return matched / ready


def _build_component_payload(
    spec: MetricComponentSpec,
    flat_list: list[dict[str, Any]],
    ready_count: int,
    *,
    history_map: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """构建单个 component 的 payload（PRD §7.1 components 元素）。

    [P0-6 2026-07-30] 新增 readiness 字段，区分 raw_ready / normalized_ready：
    - raw_ready: rawValue 已计算（上游字段有值）
    - normalized_ready: normalizedValue 已计算（历史 >= MIN_BASELINE_WINDOW）
    冷启动时 raw_ready=True 但 normalized_ready=False，
    提示操作者运行 bootstrap 补历史而非伪造分位。
    """
    raw_value = _compute_component_raw(spec, flat_list)
    history = (history_map or {}).get(spec.name) if history_map else None
    normalized = _normalize_component(raw_value, spec.direction, history or [])

    # status 判定
    # [P0 2026-07-30] history is None（未传）也判为 insufficient_history，
    # 否则 orchestrator 不传 history 时 component 错误标为 ready，
    # 掩盖 P/Q/U/C/V normalizedValue=None 的真实根因
    if raw_value is None:
        status = STATUS_UNAVAILABLE
    elif history is None or len(history) < MIN_BASELINE_WINDOW:
        status = STATUS_INSUFFICIENT_HISTORY
    else:
        status = STATUS_READY

    # [P0-6] granular readiness
    raw_ready = raw_value is not None
    normalized_ready = normalized is not None
    if not raw_ready:
        reason = f"upstream field '{spec.field_source}' returned None for all members"
    elif not normalized_ready:
        hist_len = len(history) if history else 0
        reason = (
            f"history insufficient: {hist_len} < {MIN_BASELINE_WINDOW} "
            f"(run bootstrap from canonical FP history, bars, and PIT membership)"
        )
    else:
        reason = None

    coverage = (
        ready_count / max(1, len(flat_list)) if flat_list else 0.0
    )

    return {
        "name": spec.name,
        "rawValue": raw_value,
        "normalizedValue": normalized,
        "direction": spec.direction,
        "denominator": ready_count,
        "fieldSource": spec.field_source,
        "weight": spec.weight,
        "weightMode": _weight_mode(flat_list),
        "coverage": coverage,
        "status": status,
        "readiness": {
            "raw_ready": raw_ready,
            "normalized_ready": normalized_ready,
            "reason": reason,
        },
        "extra": (
            {"derive_fn": spec.derive_fn, "extra_fields": list(spec.extra_fields)}
            if spec.derive_fn else None
        ),
    }


def _weight_mode(flat_list: list[dict[str, Any]]) -> str:
    modes = {
        str(flat.get("review_weight_mode"))
        for flat in flat_list
        if flat.get("review_weight_mode")
    }
    return modes.pop() if len(modes) == 1 else "mixed" if modes else "equal_weight"


# =============================================================================
# Metric 计算
# =============================================================================


def compute_metric_payload(
    metric_code: str,
    flat_list: list[dict[str, Any]],
    *,
    ready_count: int | None = None,
    history_map: dict[str, list[float]] | None = None,
    prev_value: float | None = None,
    prev5d_value: float | None = None,
    cross_section_values: list[float] | None = None,
    registry: ReviewMetricComponentRegistry = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """计算单个聚合变量（P/Q/U/C/V）的完整 payload。

    Args:
        metric_code: P / Q / U / C / V
        flat_list: 成员 first_pyramid_flat 列表（service 层从 stock_core snapshot 读取）
        ready_count: 有效成员数（None 时取 len(flat_list)）
        history_map: {component_name: [历史 raw_value 序列]}，用于历史分位归一化
        prev_value: 前一交易日的 value（用于 delta1d）
        prev5d_value: 前 5 交易日的 value（用于 delta5d）
        cross_section_values: 当日所有 scope 的 value 序列（用于横截面分位）
        registry: component 注册表

    Returns:
        PRD §7.1 通用结构 payload dict
    """
    spec: MetricSpec = registry.get_metric(metric_code)
    if ready_count is None:
        ready_count = len(flat_list)

    # 1. 计算 components
    component_payloads: list[dict[str, Any]] = []
    for comp_spec in spec.components:
        component_payloads.append(
            _build_component_payload(
                comp_spec, flat_list, ready_count, history_map=history_map,
            ),
        )

    # 2. value = available component normalized values 的版本化加权平均
    available_norms: list[tuple[float, float]] = []  # (normalized, weight)
    for cp in component_payloads:
        if cp["normalizedValue"] is not None and cp["status"] != STATUS_UNAVAILABLE:
            available_norms.append((cp["normalizedValue"], cp["weight"]))

    if available_norms:
        total_weight = sum(w for _, w in available_norms)
        if total_weight > _EPSILON:
            value = _clamp(sum(n * w for n, w in available_norms) / total_weight)
        else:
            value = None
    else:
        value = None

    # P 的核心 component 依赖同日行情事实；DSA segment 累计涨跌不得替代。
    p_daily_return_unavailable = (
        metric_code == "P" and not _has_daily_return_data(flat_list)
    )
    if p_daily_return_unavailable:
        value = None

    # 3. rawValue = 原始加权平均（归一化前）
    available_raws: list[tuple[float, float]] = []
    for cp in component_payloads:
        if cp["rawValue"] is not None:
            available_raws.append((cp["rawValue"], cp["weight"]))
    if available_raws:
        total_w = sum(w for _, w in available_raws)
        raw_value = (
            sum(r * w for r, w in available_raws) / total_w
            if total_w > _EPSILON else None
        )
    else:
        raw_value = None

    # 4. delta1d / delta5d
    delta_1d = (value - prev_value) if (value is not None and prev_value is not None) else None
    delta_5d = (
        (value - prev5d_value)
        if (value is not None and prev5d_value is not None) else None
    )

    # 5. historyPercentile120d：基于 value 的历史序列
    #    service 层通过 history_map 传入 value_history（key="_metric_value"）
    value_history = (history_map or {}).get("_metric_value") if history_map else None
    if value is not None and value_history and len(value_history) >= MIN_BASELINE_WINDOW:
        history_pct = _percentile_of(value, value_history)
    else:
        history_pct = None
    history_obs = len(value_history) if value_history else 0

    # 6. crossSectionPercentile
    cross_pct: float | None = None
    if value is not None and cross_section_values:
        cross_pct = _cross_section_percentile(value, cross_section_values)

    # 7. coverage
    coverage = ready_count / max(1, len(flat_list)) if flat_list else 0.0

    # 8. status - [P0-6 2026-07-30] 修正冷启动状态判定
    # 旧 BUG: value=None（因历史不足无法归一化）时直接判 UNAVAILABLE，
    #         掩盖了"raw 数据可用但历史不足"的真实状态，导致 publish gate 报告
    #         "value 为空"而非"历史不足，请运行 bootstrap"。
    # 修复: 区分 raw_ready / normalized_ready，历史不足时标 insufficient_history。
    comp_unavailable = sum(
        1 for cp in component_payloads if cp["status"] == STATUS_UNAVAILABLE
    )
    comp_insufficient = sum(
        1 for cp in component_payloads
        if cp["status"] == STATUS_INSUFFICIENT_HISTORY
    )
    raw_ready = raw_value is not None
    normalized_ready = value is not None

    # P 指标日收益全空属于上游关键数据缺失，
    # 即使部分 component（new_high_ratio/price_position_median）的 rawValue
    # 非 None，P 的语义已无法成立 → UNAVAILABLE，优先级最高。
    if p_daily_return_unavailable:
        status = STATUS_UNAVAILABLE
    elif not raw_ready:
        # 所有 component rawValue 均为 None（上游数据缺失）
        status = STATUS_UNAVAILABLE
    elif not normalized_ready:
        # raw 可用但无法归一化（历史不足）→ insufficient_history 而非 unavailable
        status = STATUS_INSUFFICIENT_HISTORY
    elif comp_insufficient > 0 or comp_unavailable > 0:
        # 部分归一化值可用，部分不可用或历史不足
        status = STATUS_PARTIAL
    else:
        status = STATUS_READY

    # [P0-6] metric-level readiness（供 publish gate 和操作者诊断）
    if p_daily_return_unavailable:
        readiness_reason = (
            "P metric unavailable: review_return_1d is None for all "
            "members; core components (scope_return_1d / advance_ratio / "
            "trend_price_alignment_ratio) require point-in-time daily bars"
        )
    elif not raw_ready:
        readiness_reason = (
            f"all {len(component_payloads)} components rawValue=None "
            f"(upstream stock_core flat_list fields missing)"
        )
    elif not normalized_ready:
        insufficient_comps = [
            cp["name"] for cp in component_payloads
            if cp["status"] == STATUS_INSUFFICIENT_HISTORY
        ]
        readiness_reason = (
            f"history insufficient for normalization: "
            f"{len(insufficient_comps)}/{len(component_payloads)} components "
            f"need >= {MIN_BASELINE_WINDOW} observations "
            f"(run review_bootstrap_service from canonical FP history, "
            f"bars, and PIT membership)"
        )
    elif comp_insufficient > 0 or comp_unavailable > 0:
        readiness_reason = (
            f"partial: {comp_unavailable} unavailable, "
            f"{comp_insufficient} insufficient_history "
            f"out of {len(component_payloads)} components"
        )
    else:
        readiness_reason = None

    # 缺少日收益时强制 raw_ready/normalized_ready=False，
    # 使 publish gate 的 readiness 四态判定落入 unavailable 分支。
    if p_daily_return_unavailable:
        raw_ready = False
        normalized_ready = False

    return {
        "value": value,
        "rawValue": raw_value,
        "delta1d": delta_1d,
        "delta5d": delta_5d,
        "historyPercentile120d": history_pct,
        "crossSectionPercentile": cross_pct,
        "historyObservationCount": history_obs,
        "components": component_payloads,
        "coverage": coverage,
        "status": status,
        "readiness": {
            "raw_ready": raw_ready,
            "normalized_ready": normalized_ready,
            "status": status,
            "reason": readiness_reason,
            "history_observations": history_obs,
            "min_required": MIN_BASELINE_WINDOW,
        },
    }


def compute_all_metrics(
    flat_list: list[dict[str, Any]],
    *,
    ready_count: int | None = None,
    history_maps: dict[str, dict[str, list[float]]] | None = None,
    prev_values: dict[str, float] | None = None,
    prev5d_values: dict[str, float] | None = None,
    cross_section_values: dict[str, list[float]] | None = None,
    registry: ReviewMetricComponentRegistry = DEFAULT_REGISTRY,
) -> dict[str, dict[str, Any]]:
    """一次性计算 P/Q/U/C/V 全部 payload。

    Returns:
        {"P": payload, "Q": payload, "U": payload, "C": payload, "V": payload}
    """
    out: dict[str, dict[str, Any]] = {}
    for code in registry.metric_codes:
        out[code] = compute_metric_payload(
            code,
            flat_list,
            ready_count=ready_count,
            history_map=(history_maps or {}).get(code),
            prev_value=(prev_values or {}).get(code),
            prev5d_value=(prev5d_values or {}).get(code),
            cross_section_values=(cross_section_values or {}).get(code),
            registry=registry,
        )
    return out


if __name__ == "__main__":
    # 自测：构造最小 flat_list
    fake_flat: list[dict[str, Any]] = [
        {
            "fp_trend_direction": "up",
            "fp_swing_direction": "up",
            "fp_internal_direction": "up",
            "fp_momentum_direction": "up",
            "fp_momentum_change": "enhancing",
            "fp_structure_alignment": "aligned",
            "review_return_1d": 2.5,
            "review_price_position": 0.98,
            "review_volume_ratio20": 1.8,
            "review_amount_ratio20": 1.4,
            "review_volume_percentile20": 80.0,
            "review_amount_percentile200": 70.0,
            "review_amount": 1.0e8,
            "review_previous_first_pyramid": {
                "fp_trend_direction": "sideways",
                "fp_swing_direction": "sideways",
                "fp_internal_direction": "sideways",
                "fp_momentum_direction": "flat",
                "fp_momentum_change": "flat",
            },
            "fp_volume_badge": "放量",
            "fp_volume_ratio20": 1.8,
            "fp_volume_percentile20": 80.0,
            "fp_volume_percentile200": 70.0,
            "fp_distance_to_trailing_top_pct": 2.0,
            "fp_latest_bos_direction": "up",
            "fp_latest_bos_freshness": 1,
            "fp_latest_choch_direction": None,
            "fp_latest_choch_freshness": None,
            "fp_latest_ob_direction": "up",
            "fp_latest_ob_freshness": 2,
            "fp_amount": 1.0e8,
            "fp_segment_volume_ratio": 1.5,
            "fp_prev_segment_volume": 1.0,
        }
        for _ in range(20)
    ]

    payloads = compute_all_metrics(fake_flat)
    for code, p in payloads.items():
        print(f"  {code}: value={p['value']} status={p['status']} components={len(p['components'])}")
    assert "P" in payloads
    assert "Q" in payloads
    assert payloads["P"]["status"] in (
        STATUS_READY, STATUS_INSUFFICIENT_HISTORY, STATUS_PARTIAL, STATUS_UNAVAILABLE,
    )

    # P 指标日收益全空 → unavailable
    fake_flat_no_change = [
        {**f, "review_return_1d": None} for f in fake_flat
    ]
    payloads_no_change = compute_all_metrics(fake_flat_no_change)
    p_payload = payloads_no_change["P"]
    print(
        f"  P (no change_pct): value={p_payload['value']} "
        f"status={p_payload['status']}"
    )
    assert p_payload["value"] is None, "P value 必须为 None"
    assert p_payload["status"] == STATUS_UNAVAILABLE, "P status 必须为 unavailable"
    readiness = p_payload.get("readiness") or {}
    assert readiness.get("raw_ready") is False
    assert readiness.get("normalized_ready") is False
    assert "review_return_1d" in (readiness.get("reason") or "")
    print("OK: P unavailable when review_return_1d all None")

    print("OK: metric_engine verified")
