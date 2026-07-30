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

from app.domain.review.metric_registry import (
    DEFAULT_REGISTRY,
    MetricComponentSpec,
    MetricSpec,
    ReviewMetricComponentRegistry,
)

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


# =============================================================================
# 派生 component 计算函数（对应 registry 中的 derive_fn）
# =============================================================================


def _derive_scope_return_1d(flat_list: list[dict[str, Any]]) -> float | None:
    """范围 1 日收益率（成员等权中位数）。

    PRD §7.2：优先官方指数；无官方序列时使用成员等权中位数，
    并记录 price_source=member_equal_weight。
    """
    returns = [_safe_float(f.get("fp_segment_change_pct")) for f in flat_list]
    returns = [r for r in returns if r is not None]
    if not returns:
        return None
    returns.sort()
    n = len(returns)
    if n % 2 == 1:
        return returns[n // 2]
    return (returns[n // 2 - 1] + returns[n // 2]) / 2.0


def _derive_advance_ratio(flat_list: list[dict[str, Any]]) -> float | None:
    """当日上涨成员比例（PRD §7.2）。"""
    if not flat_list:
        return None
    ready = 0
    up = 0
    for f in flat_list:
        chg = _safe_float(f.get("fp_segment_change_pct"))
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
        chg = _safe_float(f.get("fp_segment_change_pct"))
        td = f.get("fp_trend_direction")
        if chg is None or td is None:
            continue
        ready += 1
        if td == "up" and chg > 0:
            aligned += 1
    if ready == 0:
        return None
    return aligned / ready


def _derive_new_high_ratio(flat_list: list[dict[str, Any]]) -> float | None:
    """进入近期高位区间的成员比例（PRD §7.2）。

    近期高位定义：distance_to_trailing_top_pct <= 3%（距阶段顶部 3% 以内）。
    """
    if not flat_list:
        return None
    ready = 0
    near_high = 0
    for f in flat_list:
        dist = _safe_float(f.get("fp_distance_to_trailing_top_pct"))
        if dist is None:
            continue
        ready += 1
        if dist <= 3.0:
            near_high += 1
    if ready == 0:
        return None
    return near_high / ready


def _derive_price_position_median(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """成员价格在自身滚动区间的位置中位数（PRD §7.2）。

    位置 = (current - trailing_bottom) / (trailing_top - trailing_bottom)
    使用 distance 字段反推：position = 1 - distance_to_top_pct/100
    """
    positions: list[float] = []
    for f in flat_list:
        dist_top = _safe_float(f.get("fp_distance_to_trailing_top_pct"))
        if dist_top is None:
            continue
        # 简化：位置 = 1 - dist_top/100（距顶部越近位置越高）
        positions.append(max(0.0, min(1.0, 1.0 - dist_top / 100.0)))
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
        td = f.get("fp_trend_direction")
        if td is None:
            continue
        ready += 1
        for ev_field in (
            "fp_latest_bos_direction",
            "fp_latest_choch_direction",
            "fp_latest_ob_direction",
        ):
            d = f.get(ev_field)
            if d == "up":
                bull += 1
            elif d == "down":
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
        td = f.get("fp_trend_direction")
        if td is None:
            continue
        ready += 1
        bos_d = f.get("fp_latest_bos_direction")
        choch_d = f.get("fp_latest_choch_direction")
        if bos_d == "down" or choch_d == "down":
            broken += 1
    if ready == 0:
        return None
    return broken / ready


def _derive_multi_dim_improving_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """至少两个核心维度同步改善的成员比例（PRD §7.4）。

    维度：trend=up, structure(swing)=up, momentum=up, momentum_change=enhancing。
    任一成员满足其中 >=2 个即计入。
    """
    if not flat_list:
        return None
    ready = 0
    multi = 0
    for f in flat_list:
        td = f.get("fp_trend_direction")
        if td is None:
            continue
        ready += 1
        score = 0
        if f.get("fp_trend_direction") == "up":
            score += 1
        if f.get("fp_swing_direction") == "up":
            score += 1
        if f.get("fp_momentum_direction") == "up":
            score += 1
        if f.get("fp_momentum_change") == "enhancing":
            score += 1
        if score >= 2:
            multi += 1
    if ready == 0:
        return None
    return multi / ready


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
        td = f.get("fp_trend_direction")
        if td is None:
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
        (f, _safe_float(f.get("fp_segment_change_pct")))
        for f in flat_list
        if _safe_float(f.get("fp_segment_change_pct")) is not None
        and f.get("fp_trend_direction") is not None
    ]
    if not valid:
        return None
    valid.sort(key=lambda x: x[1], reverse=True)
    n = len(valid)
    head_count = max(1, int(n * 0.3))
    non_head = valid[head_count:]
    if not non_head:
        return None
    up = sum(1 for _, chg in non_head if chg > 0)
    return up / len(non_head)


def _derive_leader_follower_common_confirm_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """龙头、二线与普通成员共同确认比例（PRD §7.4）。

    简化定义：按 volume_ratio20 排序，前 20% 为龙头，中 60% 为二线，后 20% 为普通；
    若三组各自上涨比例均 >= 50%，返回 1.0，否则返回三组平均上涨比例。
    """
    if not flat_list:
        return None
    valid = [
        (f, _safe_float(f.get("fp_volume_ratio20")))
        for f in flat_list
        if _safe_float(f.get("fp_volume_ratio20")) is not None
        and f.get("fp_trend_direction") is not None
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
            chg = _safe_float(f.get("fp_segment_change_pct"))
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
        (f, _safe_float(f.get("fp_segment_change_pct")))
        for f in flat_list
        if _safe_float(f.get("fp_segment_change_pct")) is not None
    ]
    if not changes:
        return None
    abs_changes = [(f, abs(chg)) for f, chg in changes]
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
        _safe_float(f.get("fp_segment_change_pct"))
        for f in flat_list
        if _safe_float(f.get("fp_segment_change_pct")) is not None
    ]
    if not changes:
        return None
    abs_changes = [abs(c) for c in changes]
    total = sum(abs_changes)
    if total < _EPSILON:
        return None
    return sum((c / total) ** 2 for c in abs_changes)


def _derive_leader_median_diff(flat_list: list[dict[str, Any]]) -> float | None:
    """龙头与成员中位数表现差（PRD §7.5）。

    简化：top1 成员 change_pct - 中位数 change_pct。
    """
    changes = [
        _safe_float(f.get("fp_segment_change_pct"))
        for f in flat_list
        if _safe_float(f.get("fp_segment_change_pct")) is not None
    ]
    if len(changes) < 3:
        return None
    changes.sort(reverse=True)
    top1 = changes[0]
    n = len(changes)
    if n % 2 == 1:
        median = changes[n // 2]
    else:
        median = (changes[n // 2 - 1] + changes[n // 2]) / 2.0
    return top1 - median


def _derive_top5_amount_contribution(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """Top5 成交额占比（PRD §7.5）。"""
    amounts = [
        (f, _safe_float(f.get("fp_amount")))
        for f in flat_list
        if _safe_float(f.get("fp_amount")) is not None
    ]
    if len(amounts) < 5:
        return None
    amounts.sort(key=lambda x: x[1] if x[1] is not None else 0, reverse=True)
    total = sum(a for _, a in amounts)
    if total < _EPSILON:
        return None
    return sum(a for _, a in amounts[:5]) / total


def _derive_volume_expansion_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """放量成员比例（PRD §7.6）。"""
    if not flat_list:
        return None
    ready = 0
    high = 0
    for f in flat_list:
        if f.get("fp_volume_badge") is None:
            continue
        ready += 1
        if f.get("fp_volume_badge") == "放量":
            high += 1
    if ready == 0:
        return None
    return high / ready


def _derive_amount_expansion_ratio(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """成交额扩张成员比例（PRD §7.6，volume_ratio20 > 1.5）。"""
    if not flat_list:
        return None
    ready = 0
    expand = 0
    for f in flat_list:
        vr = _safe_float(f.get("fp_volume_ratio20"))
        if vr is None:
            continue
        ready += 1
        if vr > 1.5:
            expand += 1
    if ready == 0:
        return None
    return expand / ready


def _derive_trend_segment_volume_improvement(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """趋势段平均量相对前段改善比例（PRD §7.6）。"""
    improvements: list[float] = []
    for f in flat_list:
        cur = _safe_float(f.get("fp_segment_volume_ratio"))
        prev = _safe_float(f.get("fp_prev_segment_volume"))
        if cur is None or prev is None or prev < _EPSILON:
            continue
        improvements.append((cur - prev) / prev)
    if not improvements:
        return None
    # 返回中位数
    improvements.sort()
    n = len(improvements)
    if n % 2 == 1:
        return improvements[n // 2]
    return (improvements[n // 2 - 1] + improvements[n // 2]) / 2.0


def _derive_price_amount_efficiency_median(
    flat_list: list[dict[str, Any]],
) -> float | None:
    """价格变化 / 相对成交额的效率中位数（PRD §7.6）。

    efficiency = abs(change_pct) / max(volume_ratio20, epsilon)
    """
    effs: list[float] = []
    for f in flat_list:
        chg = _safe_float(f.get("fp_segment_change_pct"))
        vr = _safe_float(f.get("fp_volume_ratio20"))
        if chg is None or vr is None or vr < _EPSILON:
            continue
        effs.append(abs(chg) / vr)
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
    matched = 0
    for f in flat_list:
        val = f.get(src)
        if val is None:
            continue
        ready += 1
        # 方向匹配：trend/structure/momentum 方向类字段
        if src in (
            "fp_trend_direction", "fp_swing_direction",
            "fp_internal_direction", "fp_momentum_change",
        ):
            if val == "up" or val == "enhancing" or val == "aligned":
                matched += 1
        # 分位类字段（0-100）：取中位数 / 100
        elif src in ("fp_volume_percentile20", "fp_volume_percentile200"):
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
    """构建单个 component 的 payload（PRD §7.1 components 元素）。"""
    raw_value = _compute_component_raw(spec, flat_list)
    history = (history_map or {}).get(spec.name) if history_map else None
    normalized = _normalize_component(raw_value, spec.direction, history or [])

    # status 判定
    if raw_value is None:
        status = STATUS_UNAVAILABLE
    elif history is not None and len(history) < MIN_BASELINE_WINDOW:
        status = STATUS_INSUFFICIENT_HISTORY
    else:
        status = STATUS_READY

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
        "coverage": coverage,
        "status": status,
        "extra": (
            {"derive_fn": spec.derive_fn, "extra_fields": list(spec.extra_fields)}
            if spec.derive_fn else None
        ),
    }


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
        cross_pct = _percentile_of(value, cross_section_values)

    # 7. coverage
    coverage = ready_count / max(1, len(flat_list)) if flat_list else 0.0

    # 8. status
    if value is None:
        status = STATUS_UNAVAILABLE
    elif history_obs > 0 and history_obs < MIN_BASELINE_WINDOW:
        status = STATUS_INSUFFICIENT_HISTORY
    elif any(cp["status"] == STATUS_UNAVAILABLE for cp in component_payloads):
        # 部分 component 缺失但仍有可用值
        status = STATUS_PARTIAL
    else:
        status = STATUS_READY

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
            "fp_segment_change_pct": 2.5,
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
    print("OK: metric_engine verified")
