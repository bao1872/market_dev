"""竞价板块/市场聚合服务 - 基于个股竞价结果计算板块和全市场层面的聚合指标。

输入：
- AuctionScanRun（status=succeeded/partial）
- AuctionInstrumentResult（每股一条，含位置/事件/参与度标签）

输出：
- AuctionScopeResult（scope_type=market/industry/concept）

聚合内容：
- 开盘分布、变化分布、结构/筹码迁移、双重事件、区域分布
- 参与度、集中度（Top3/Top5/HHI/龙头-中位数差）
- 覆盖（正/负覆盖率、离散度）
- 状态标签（full_repricing/leader_driven/...）与置信度（high/medium/low）

约束：
- 使用 async/await + AsyncSession
- 所有比例在 payload 中同时返回分子和分母
- 概念按样本和核心覆盖率给置信度，小样本不得仅凭高比例排名
- 全市场统计去重（按 instrument_id）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_aggregation_service
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

import numpy as np
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import (
    AuctionInstrumentResult,
    AuctionScanRun,
    AuctionScopeResult,
)
from app.models.market_board import MarketBoard, MarketBoardMembership

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

AUCTION_AGGREGATION_ALGORITHM_VERSION = "v1.0.0"

# 置信度阈值
CONFIDENCE_HIGH_MIN_VALID = 20
CONFIDENCE_HIGH_MIN_COVERAGE = 0.8
CONFIDENCE_MEDIUM_MIN_VALID = 10
CONFIDENCE_MEDIUM_MIN_COVERAGE = 0.6
# 概念额外约束：核心覆盖率不足时降级，避免小样本仅凭高比例排名
CONCEPT_CORE_COVERAGE_HIGH_MIN = 0.4
CONCEPT_CORE_COVERAGE_MEDIUM_MIN = 0.2

# 状态标签阈值
LABEL_FULL_REPRICING_HIGH_OPEN = 0.60
LABEL_FULL_REPRICING_DUAL_BREAKOUT = 0.20
LABEL_LEADER_DRIVEN_TOP3 = 0.50
LABEL_INITIAL_DIFFUSION_POS_MIGRATION = 0.40
LABEL_INITIAL_DIFFUSION_COVERAGE = 0.50
LABEL_RESISTANCE_HIGH_OPEN = 0.40
LABEL_RESISTANCE_SUPPLY_OB = 0.20
LABEL_SUPPORT_REPAIR_LOW_OPEN = 0.40
LABEL_SUPPORT_DEMAND_OB = 0.20
LABEL_FULL_BREAKDOWN_LOW_OPEN = 0.60
LABEL_FULL_BREAKDOWN_DUAL_BREAKDOWN = 0.20
LABEL_HIGH_DIVERGENCE_DISPERSION = 2.0
LABEL_HIGH_DIVERGENCE_MEDIAN_MAX = 1.0  # 无明显趋势：|中位数变化| < 1%

# 异常放量参与度级别（放量 = volume surge）
ABNORMAL_VOLUME_LEVELS = frozenset({"abnormal_high"})


# =============================================================================
# 辅助函数（纯函数）
# =============================================================================


def _safe_float(v: Any) -> float | None:
    """安全转换为 float，None/无效值返回 None。"""
    if v is None:
        return None
    try:
        f = float(v)
        return f if f == f else None  # NaN 检查
    except (TypeError, ValueError):
        return None


def _percentile(values: list[float], p: float) -> float | None:
    """计算分位数（p in [0, 100]），线性插值。空列表返回 None。"""
    if not values:
        return None
    arr = np.asarray(sorted(values), dtype=float)
    if arr.size == 1:
        return float(arr[0])
    rank = (p / 100.0) * (arr.size - 1)
    lo = int(np.floor(rank))
    hi = int(np.ceil(rank))
    if lo == hi:
        return float(arr[lo])
    return float(arr[lo] + (arr[hi] - arr[lo]) * (rank - lo))


def _ratio(numerator: int, denominator: int) -> float:
    """安全比例计算，分母为 0 时返回 0.0。"""
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def _ratio_entry(numerator: int, denominator: int) -> dict[str, Any]:
    """构造比例条目（含分子、分母、比例）。"""
    return {
        "numerator": numerator,
        "denominator": denominator,
        "ratio": round(_ratio(numerator, denominator), 6),
    }


def _round(v: float | None, ndigits: int = 6) -> float | None:
    """None 安全的四舍五入。"""
    return round(v, ndigits) if v is not None else None


def _is_valid_result(r: AuctionInstrumentResult) -> bool:
    """判断 result 是否可用于聚合（有有效竞价数据，非停牌）。"""
    if r.is_suspended:
        return False
    return r.change_pct is not None


def _is_breakout_structure(r: AuctionInstrumentResult) -> bool:
    return r.structure_position in ("above_trigger", "above_high")


def _is_breakdown_structure(r: AuctionInstrumentResult) -> bool:
    return r.structure_position in ("below_trigger", "below_low")


def _is_cross_up_chip(r: AuctionInstrumentResult) -> bool:
    return r.chip_position == "above_upper"


def _is_cross_down_chip(r: AuctionInstrumentResult) -> bool:
    return r.chip_position == "below_lower"


def _is_dual_breakout(r: AuctionInstrumentResult) -> bool:
    return _is_breakout_structure(r) and _is_cross_up_chip(r)


def _is_dual_breakdown(r: AuctionInstrumentResult) -> bool:
    return _is_breakdown_structure(r) and _is_cross_down_chip(r)


def _is_resistance_zone(r: AuctionInstrumentResult) -> bool:
    return r.structure_position == "supply_ob"


def _is_support_zone(r: AuctionInstrumentResult) -> bool:
    return r.structure_position == "demand_ob"


def _is_abnormal_volume(r: AuctionInstrumentResult) -> bool:
    return r.participation_level in ABNORMAL_VOLUME_LEVELS


def _is_positive_migration(r: AuctionInstrumentResult) -> bool:
    """正向迁移：结构突破或筹码上穿（去重计数）。"""
    return _is_breakout_structure(r) or _is_cross_up_chip(r)


# =============================================================================
# 状态标签 & 置信度分类
# =============================================================================


def _classify_status_label(
    *,
    payload: dict[str, Any],
    median_change: float | None,
    leader_change: float | None,
    top3_contribution: float | None,
    dispersion: float | None,
    coverage_ratio: float,
) -> str:
    """根据聚合指标分类状态标签。

    优先级：
    1. full_repricing: 高开比例>60% 且 双重突破率>20%
    2. full_breakdown: 低开比例>60% 且 双重破位率>20%
    3. leader_driven: Top3贡献>50% 且 中位数变化<龙头变化/2
    4. initial_diffusion: 正迁移比例>40% 且 覆盖率>50%
    5. resistance_high_open: 高开比例>40% 且 供应OB比例>20%
    6. support_repair: 低开比例>40% 且 需求OB比例>20%
    7. high_divergence: 离散度>阈值 且 无明显趋势
    8. inconclusive: 其他
    """
    high_open_ratio = payload["open_distribution"]["high_open"]["ratio"]
    low_open_ratio = payload["open_distribution"]["low_open"]["ratio"]
    dual_breakout_ratio = payload["dual_events"]["dual_breakout"]["ratio"]
    dual_breakdown_ratio = payload["dual_events"]["dual_breakdown"]["ratio"]
    supply_ob_ratio = payload["zone_distribution"]["resistance_zone"]["ratio"]
    demand_ob_ratio = payload["zone_distribution"]["support_zone"]["ratio"]
    positive_migration_ratio = payload["positive_migration"]["ratio"]

    # 1. 全面重新定价（强势上行）
    if (
        high_open_ratio > LABEL_FULL_REPRICING_HIGH_OPEN
        and dual_breakout_ratio > LABEL_FULL_REPRICING_DUAL_BREAKOUT
    ):
        return "full_repricing"

    # 2. 全面崩溃（强势下行）
    if (
        low_open_ratio > LABEL_FULL_BREAKDOWN_LOW_OPEN
        and dual_breakdown_ratio > LABEL_FULL_BREAKDOWN_DUAL_BREAKDOWN
    ):
        return "full_breakdown"

    # 3. 龙头驱动（少数龙头带动）
    if top3_contribution is not None and top3_contribution > LABEL_LEADER_DRIVEN_TOP3:
        if (
            leader_change is not None
            and leader_change > 0
            and median_change is not None
            and median_change < leader_change / 2.0
        ):
            return "leader_driven"

    # 4. 初始扩散
    if (
        positive_migration_ratio > LABEL_INITIAL_DIFFUSION_POS_MIGRATION
        and coverage_ratio > LABEL_INITIAL_DIFFUSION_COVERAGE
    ):
        return "initial_diffusion"

    # 5. 阻力位高开（高位遇阻）
    if (
        high_open_ratio > LABEL_RESISTANCE_HIGH_OPEN
        and supply_ob_ratio > LABEL_RESISTANCE_SUPPLY_OB
    ):
        return "resistance_high_open"

    # 6. 支撑修复（低位企稳）
    if (
        low_open_ratio > LABEL_SUPPORT_REPAIR_LOW_OPEN
        and demand_ob_ratio > LABEL_SUPPORT_DEMAND_OB
    ):
        return "support_repair"

    # 7. 高离散（无明显趋势）
    if (
        dispersion is not None
        and dispersion > LABEL_HIGH_DIVERGENCE_DISPERSION
        and median_change is not None
        and abs(median_change) < LABEL_HIGH_DIVERGENCE_MEDIAN_MAX
    ):
        return "high_divergence"

    return "inconclusive"


def _classify_confidence(
    *,
    valid_count: int,
    coverage_ratio: float,
    core_coverage: float | None = None,
) -> str:
    """置信度分类。

    - high: valid_count >= 20 且 coverage >= 0.8
    - medium: valid_count >= 10 且 coverage >= 0.6
    - low: 其他

    概念额外约束：核心覆盖率不足时降级，避免小样本仅凭高比例排名。
    """
    if (
        valid_count >= CONFIDENCE_HIGH_MIN_VALID
        and coverage_ratio >= CONFIDENCE_HIGH_MIN_COVERAGE
    ):
        base = "high"
    elif (
        valid_count >= CONFIDENCE_MEDIUM_MIN_VALID
        and coverage_ratio >= CONFIDENCE_MEDIUM_MIN_COVERAGE
    ):
        base = "medium"
    else:
        base = "low"

    if core_coverage is not None:
        if base == "high" and core_coverage < CONCEPT_CORE_COVERAGE_HIGH_MIN:
            base = "medium"
        if base == "medium" and core_coverage < CONCEPT_CORE_COVERAGE_MEDIUM_MIN:
            base = "low"
    return base


# =============================================================================
# 聚合计算（核心）
# =============================================================================


def _compute_scope_metrics(
    results: list[AuctionInstrumentResult],
    *,
    total_count: int,
    is_concept: bool = False,
) -> dict[str, Any]:
    """对一组 instrument results 计算聚合指标。

    Args:
        results: 该 scope 下的 instrument results（已按成员过滤）
        total_count: 该 scope 的成员总数（用于 coverage）
        is_concept: 是否为概念板块（影响置信度计算，引入核心覆盖率约束）

    Returns:
        包含所有聚合指标和 payload 的 dict，可直接用于构造 AuctionScopeResult。
    """
    # 去重（按 instrument_id，保留第一条）— 全市场统计去重
    seen: set[uuid.UUID] = set()
    deduped: list[AuctionInstrumentResult] = []
    for r in results:
        if r.instrument_id in seen:
            continue
        seen.add(r.instrument_id)
        deduped.append(r)
    results = deduped

    valid_results = [r for r in results if _is_valid_result(r)]
    valid_count = len(valid_results)
    coverage_ratio = _ratio(valid_count, total_count) if total_count > 0 else 0.0

    change_pcts = [float(r.change_pct) for r in valid_results]
    rel_vols = [
        float(r.relative_volume_median_20d)
        for r in valid_results
        if r.relative_volume_median_20d is not None
    ]
    # 成交额加权需对齐 change_pct 与 amount
    weighted_pairs = [
        (float(r.change_pct), float(r.auction_amount))
        for r in valid_results
        if r.auction_amount is not None
    ]
    amounts = [amt for _, amt in weighted_pairs]
    total_amount = sum(amounts) if amounts else 0.0

    # 开盘分布
    open_high = sum(1 for v in change_pcts if v > 0)
    open_flat = sum(1 for v in change_pcts if v == 0)
    open_low = sum(1 for v in change_pcts if v < 0)

    # 变化分布
    median_change = _percentile(change_pcts, 50)
    p25_change = _percentile(change_pcts, 25)
    p75_change = _percentile(change_pcts, 75)
    equal_weight_change = (
        sum(change_pcts) / len(change_pcts) if change_pcts else None
    )
    if total_amount > 0:
        amount_weight_change = sum(p * a for p, a in weighted_pairs) / total_amount
    else:
        amount_weight_change = None

    # 结构迁移
    breakout_count = sum(1 for r in valid_results if _is_breakout_structure(r))
    breakdown_count = sum(1 for r in valid_results if _is_breakdown_structure(r))
    # 筹码迁移
    cross_up_count = sum(1 for r in valid_results if _is_cross_up_chip(r))
    cross_down_count = sum(1 for r in valid_results if _is_cross_down_chip(r))
    # 双重事件
    dual_breakout_count = sum(1 for r in valid_results if _is_dual_breakout(r))
    dual_breakdown_count = sum(1 for r in valid_results if _is_dual_breakdown(r))
    # 区域分布
    resistance_zone_count = sum(1 for r in valid_results if _is_resistance_zone(r))
    support_zone_count = sum(1 for r in valid_results if _is_support_zone(r))
    # 正迁移（结构突破或筹码上穿，去重计数）
    positive_migration_count = sum(
        1 for r in valid_results if _is_positive_migration(r)
    )

    # 参与度
    participation_median = _percentile(rel_vols, 50) if rel_vols else None
    abnormal_volume_count = sum(1 for r in valid_results if _is_abnormal_volume(r))
    abnormal_volume_pct = (
        round(_ratio(abnormal_volume_count, valid_count), 6)
        if valid_count > 0 else None
    )

    # 集中度（基于成交额）
    if amounts and total_amount > 0:
        sorted_amts = sorted(amounts, reverse=True)
        top3_contribution = sum(sorted_amts[:3]) / total_amount
        top5_contribution = sum(sorted_amts[:5]) / total_amount
        hhi = sum((a / total_amount) ** 2 for a in amounts)
    else:
        top3_contribution = None
        top5_contribution = None
        hhi = None

    # 龙头与中位数差（龙头 = 最大变化幅度，正向）
    leader_change = max(change_pcts) if change_pcts else None
    if leader_change is not None and median_change is not None:
        leader_median_gap = leader_change - median_change
    else:
        leader_median_gap = None

    # 覆盖
    positive_coverage = _ratio(open_high, valid_count) if valid_count > 0 else 0.0
    negative_coverage = _ratio(open_low, valid_count) if valid_count > 0 else 0.0
    if len(change_pcts) >= 2:
        dispersion = float(np.std(change_pcts, ddof=1))
    elif len(change_pcts) == 1:
        dispersion = 0.0
    else:
        dispersion = None

    # 核心/边缘成员区分
    # 核心成员：与中位数方向一致且幅度超过 |中位数|
    if median_change is not None and valid_count > 0:
        if median_change >= 0:
            core_count = sum(
                1 for v in change_pcts if v >= 0 and v > abs(median_change)
            )
        else:
            core_count = sum(
                1 for v in change_pcts if v <= 0 and v < -abs(median_change)
            )
    else:
        core_count = 0
    peripheral_count = valid_count - core_count
    core_coverage = _ratio(core_count, total_count) if total_count > 0 else 0.0

    # 构造 payload（所有比例含分子和分母）
    payload: dict[str, Any] = {
        "open_distribution": {
            "high_open": _ratio_entry(open_high, valid_count),
            "flat_open": _ratio_entry(open_flat, valid_count),
            "low_open": _ratio_entry(open_low, valid_count),
        },
        "structure_migration": {
            "breakout": _ratio_entry(breakout_count, valid_count),
            "breakdown": _ratio_entry(breakdown_count, valid_count),
        },
        "chip_migration": {
            "cross_up": _ratio_entry(cross_up_count, valid_count),
            "cross_down": _ratio_entry(cross_down_count, valid_count),
        },
        "dual_events": {
            "dual_breakout": _ratio_entry(dual_breakout_count, valid_count),
            "dual_breakdown": _ratio_entry(dual_breakdown_count, valid_count),
        },
        "zone_distribution": {
            "resistance_zone": _ratio_entry(resistance_zone_count, valid_count),
            "support_zone": _ratio_entry(support_zone_count, valid_count),
        },
        "positive_migration": _ratio_entry(positive_migration_count, valid_count),
        "participation": {
            "abnormal_volume": _ratio_entry(abnormal_volume_count, valid_count),
            "median_relative_volume": _round(participation_median),
        },
        "concentration": {
            "top3_contribution": _round(top3_contribution),
            "top5_contribution": _round(top5_contribution),
            "hhi": _round(hhi),
            "leader_change_pct": _round(leader_change),
            "leader_median_gap": _round(leader_median_gap),
            "total_amount": round(total_amount, 2) if total_amount else 0.0,
        },
        "coverage_detail": {
            "positive_coverage": round(positive_coverage, 6),
            "negative_coverage": round(negative_coverage, 6),
            "dispersion": _round(dispersion),
        },
        "core_peripheral": {
            "core_count": core_count,
            "peripheral_count": peripheral_count,
            "core_coverage": round(core_coverage, 6),
        },
    }

    # 状态标签
    status_label = _classify_status_label(
        payload=payload,
        median_change=median_change,
        leader_change=leader_change,
        top3_contribution=top3_contribution,
        dispersion=dispersion,
        coverage_ratio=coverage_ratio,
    )

    # 置信度（概念引入核心覆盖率约束）
    confidence_level = _classify_confidence(
        valid_count=valid_count,
        coverage_ratio=coverage_ratio,
        core_coverage=core_coverage if is_concept else None,
    )

    return {
        "total_count": total_count,
        "valid_count": valid_count,
        "coverage_ratio": round(coverage_ratio, 6),
        "open_high_count": open_high,
        "open_flat_count": open_flat,
        "open_low_count": open_low,
        "median_change_pct": _round(median_change),
        "p25_change_pct": _round(p25_change),
        "p75_change_pct": _round(p75_change),
        "equal_weight_change_pct": _round(equal_weight_change),
        "amount_weight_change_pct": _round(amount_weight_change),
        "structure_breakout_count": breakout_count,
        "structure_breakdown_count": breakdown_count,
        "chip_cross_up_count": cross_up_count,
        "chip_cross_down_count": cross_down_count,
        "dual_breakout_count": dual_breakout_count,
        "dual_breakdown_count": dual_breakdown_count,
        "resistance_zone_count": resistance_zone_count,
        "support_zone_count": support_zone_count,
        "participation_median": _round(participation_median),
        "abnormal_volume_pct": abnormal_volume_pct,
        "top3_contribution": _round(top3_contribution),
        "top5_contribution": _round(top5_contribution),
        "hhi": _round(hhi),
        "leader_median_gap": _round(leader_median_gap),
        "positive_coverage": round(positive_coverage, 6),
        "negative_coverage": round(negative_coverage, 6),
        "dispersion": _round(dispersion),
        "status_label": status_label,
        "confidence_level": confidence_level,
        "payload": payload,
    }


# =============================================================================
# ORM 构造 & 序列化
# =============================================================================


def _build_scope_result(
    *,
    scan_run_id: uuid.UUID,
    trade_date: date,
    scope_type: str,
    scope_id: uuid.UUID | None,
    scope_name: str | None,
    metrics: dict[str, Any],
) -> AuctionScopeResult:
    """根据 metrics dict 构造 AuctionScopeResult ORM 对象。"""
    return AuctionScopeResult(
        scan_run_id=scan_run_id,
        trade_date=trade_date,
        scope_type=scope_type,
        scope_id=scope_id,
        scope_name=scope_name,
        total_count=metrics["total_count"],
        valid_count=metrics["valid_count"],
        coverage_ratio=metrics["coverage_ratio"],
        open_high_count=metrics["open_high_count"],
        open_flat_count=metrics["open_flat_count"],
        open_low_count=metrics["open_low_count"],
        median_change_pct=metrics["median_change_pct"],
        p25_change_pct=metrics["p25_change_pct"],
        p75_change_pct=metrics["p75_change_pct"],
        equal_weight_change_pct=metrics["equal_weight_change_pct"],
        amount_weight_change_pct=metrics["amount_weight_change_pct"],
        structure_breakout_count=metrics["structure_breakout_count"],
        structure_breakdown_count=metrics["structure_breakdown_count"],
        chip_cross_up_count=metrics["chip_cross_up_count"],
        chip_cross_down_count=metrics["chip_cross_down_count"],
        dual_breakout_count=metrics["dual_breakout_count"],
        dual_breakdown_count=metrics["dual_breakdown_count"],
        resistance_zone_count=metrics["resistance_zone_count"],
        support_zone_count=metrics["support_zone_count"],
        participation_median=metrics["participation_median"],
        abnormal_volume_pct=metrics["abnormal_volume_pct"],
        top3_contribution=metrics["top3_contribution"],
        top5_contribution=metrics["top5_contribution"],
        hhi=metrics["hhi"],
        leader_median_gap=metrics["leader_median_gap"],
        positive_coverage=metrics["positive_coverage"],
        negative_coverage=metrics["negative_coverage"],
        dispersion=metrics["dispersion"],
        status_label=metrics["status_label"],
        confidence_level=metrics["confidence_level"],
        payload=metrics["payload"],
        reason_codes=[],
    )


def _summarize_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    """将 metrics dict 压缩为返回给调用方的摘要（含概念额外字段）。"""
    core_peripheral = metrics["payload"]["core_peripheral"]
    return {
        "total_count": metrics["total_count"],
        "valid_count": metrics["valid_count"],
        "coverage_ratio": metrics["coverage_ratio"],
        "status_label": metrics["status_label"],
        "confidence_level": metrics["confidence_level"],
        "median_change_pct": metrics["median_change_pct"],
        "equal_weight_change_pct": metrics["equal_weight_change_pct"],
        "amount_weight_change_pct": metrics["amount_weight_change_pct"],
        "open_high_count": metrics["open_high_count"],
        "open_low_count": metrics["open_low_count"],
        "dual_breakout_count": metrics["dual_breakout_count"],
        "dual_breakdown_count": metrics["dual_breakdown_count"],
        "top3_contribution": metrics["top3_contribution"],
        "hhi": metrics["hhi"],
        "leader_median_gap": metrics["leader_median_gap"],
        "dispersion": metrics["dispersion"],
        # 概念额外输出：核心/边缘成员区分
        "core_count": core_peripheral["core_count"],
        "peripheral_count": core_peripheral["peripheral_count"],
        "core_coverage": core_peripheral["core_coverage"],
    }


def _serialize_scope(s: AuctionScopeResult) -> dict[str, Any]:
    """将 AuctionScopeResult ORM 序列化为 dict。"""
    return {
        "scope_id": str(s.scope_id) if s.scope_id else None,
        "scope_name": s.scope_name,
        "scope_type": s.scope_type,
        "total_count": s.total_count,
        "valid_count": s.valid_count,
        "coverage_ratio": s.coverage_ratio,
        "status_label": s.status_label,
        "confidence_level": s.confidence_level,
        "median_change_pct": s.median_change_pct,
        "equal_weight_change_pct": s.equal_weight_change_pct,
        "amount_weight_change_pct": s.amount_weight_change_pct,
        "open_high_count": s.open_high_count,
        "open_low_count": s.open_low_count,
        "dual_breakout_count": s.dual_breakout_count,
        "dual_breakdown_count": s.dual_breakdown_count,
        "top3_contribution": s.top3_contribution,
        "hhi": s.hhi,
        "leader_median_gap": s.leader_median_gap,
        "dispersion": s.dispersion,
        "payload": s.payload,
    }


# =============================================================================
# DB 查询
# =============================================================================


async def _load_all_instrument_results(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
) -> dict[uuid.UUID, AuctionInstrumentResult]:
    """加载 scan_run 下所有个股结果，按 instrument_id 索引（去重）。"""
    stmt = select(AuctionInstrumentResult).where(
        AuctionInstrumentResult.scan_run_id == scan_run_id,
    )
    result = await db.execute(stmt)
    mapping: dict[uuid.UUID, AuctionInstrumentResult] = {}
    for r in result.scalars().all():
        # 去重：保留第一条
        if r.instrument_id not in mapping:
            mapping[r.instrument_id] = r
    return mapping


async def _load_boards_grouped(
    db: AsyncSession,
) -> tuple[list[MarketBoard], dict[uuid.UUID, set[uuid.UUID]]]:
    """加载所有板块及其成员，返回 (boards, members_by_board)。

    板块按 type asc, name asc 排序（industry 在前，concept 在后）。
    """
    boards_stmt = select(MarketBoard).order_by(
        MarketBoard.type.asc(), MarketBoard.name.asc(),
    )
    boards = list((await db.execute(boards_stmt)).scalars().all())

    members_stmt = select(
        MarketBoardMembership.boardId, MarketBoardMembership.instrumentId,
    )
    members_by_board: dict[uuid.UUID, set[uuid.UUID]] = {}
    for board_id, inst_id in (await db.execute(members_stmt)).all():
        members_by_board.setdefault(board_id, set()).add(inst_id)

    return boards, members_by_board


# =============================================================================
# 主入口：compute_auction_aggregation
# =============================================================================


async def compute_auction_aggregation(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
) -> dict[str, Any]:
    """主入口：基于个股竞价结果计算板块和市场层面聚合指标。

    流程：
    1. 查询 scan_run 和所有 instrument_results
    2. 计算 market 级聚合（scope_type=market, scope_id=null）
    3. 查询所有行业板块，为每个行业计算聚合（scope_type=industry）
    4. 查询所有概念板块，为每个概念计算聚合（scope_type=concept）
       概念额外输出：成员总数、有效样本数、覆盖率、核心/边缘成员区分、置信度
    5. 所有比例同时返回分子和分母（写入 payload）
    6. 删除旧 scope results 后重新写入（幂等）

    Args:
        db: 异步 DB 会话（caller 控制 commit）
        scan_run_id: 竞价扫描 run ID

    Returns:
        {
            "scan_run_id": str,
            "trade_date": str,
            "market": {...},
            "industries": [...],
            "concepts": [...],
        }

    Raises:
        ValueError: scan_run 不存在
    """
    logger.info("[AuctionAggregation] 开始聚合: scan_run_id=%s", scan_run_id)

    run = await db.get(AuctionScanRun, scan_run_id)
    if run is None:
        raise ValueError(f"AuctionScanRun not found: {scan_run_id}")

    # 1. 加载所有个股结果（按 instrument_id 去重）
    results_by_instrument = await _load_all_instrument_results(db, scan_run_id)
    all_results = list(results_by_instrument.values())

    # 2. 幂等：删除旧 scope results
    await db.execute(
        delete(AuctionScopeResult).where(
            AuctionScopeResult.scan_run_id == scan_run_id,
        )
    )
    await db.flush()

    # 3. market 聚合（total_count = eligible_count）
    market_metrics = _compute_scope_metrics(
        all_results,
        total_count=run.eligible_count,
        is_concept=False,
    )
    market_scope = _build_scope_result(
        scan_run_id=scan_run_id,
        trade_date=run.trade_date,
        scope_type="market",
        scope_id=None,
        scope_name=None,
        metrics=market_metrics,
    )
    db.add(market_scope)

    # 4. 行业 + 概念 聚合
    boards, members_by_board = await _load_boards_grouped(db)

    industry_summaries: list[dict[str, Any]] = []
    concept_summaries: list[dict[str, Any]] = []
    industry_count = 0
    concept_count = 0

    for board in boards:
        member_ids = members_by_board.get(board.id, set())
        board_results = [
            results_by_instrument[iid]
            for iid in member_ids
            if iid in results_by_instrument
        ]
        is_concept = board.type == "concept"
        metrics = _compute_scope_metrics(
            board_results,
            total_count=len(member_ids),
            is_concept=is_concept,
        )
        scope = _build_scope_result(
            scan_run_id=scan_run_id,
            trade_date=run.trade_date,
            scope_type=board.type,
            scope_id=board.id,
            scope_name=board.name,
            metrics=metrics,
        )
        db.add(scope)

        summary: dict[str, Any] = {
            "scope_id": str(board.id),
            "scope_name": board.name,
            "scope_type": board.type,
            **_summarize_metrics(metrics),
        }
        if is_concept:
            concept_summaries.append(summary)
            concept_count += 1
        else:
            industry_summaries.append(summary)
            industry_count += 1

    await db.flush()

    logger.info(
        "[AuctionAggregation] 聚合完成: scan_run_id=%s trade_date=%s "
        "industries=%d concepts=%d",
        scan_run_id, run.trade_date, industry_count, concept_count,
    )

    return {
        "scan_run_id": str(scan_run_id),
        "trade_date": run.trade_date.isoformat(),
        "algorithm_version": AUCTION_AGGREGATION_ALGORITHM_VERSION,
        "market": {
            "scope_type": "market",
            "scope_id": None,
            "scope_name": None,
            **_summarize_metrics(market_metrics),
        },
        "industries": industry_summaries,
        "concepts": concept_summaries,
    }


# =============================================================================
# 查询入口：get_aggregation_results
# =============================================================================


async def get_aggregation_results(
    db: AsyncSession,
    scan_run_id: uuid.UUID,
) -> dict[str, Any]:
    """查询已计算的竞价聚合结果。

    Args:
        db: 异步 DB 会话
        scan_run_id: 竞价扫描 run ID

    Returns:
        {
            "scan_run_id": str,
            "trade_date": str,
            "market": {...} | None,
            "industries": [...],
            "concepts": [...],
        }

    Raises:
        ValueError: scan_run 不存在
    """
    run = await db.get(AuctionScanRun, scan_run_id)
    if run is None:
        raise ValueError(f"AuctionScanRun not found: {scan_run_id}")

    stmt = (
        select(AuctionScopeResult)
        .where(AuctionScopeResult.scan_run_id == scan_run_id)
        .order_by(
            AuctionScopeResult.scope_type.asc(),
            AuctionScopeResult.scope_name.asc(),
        )
    )
    scopes = list((await db.execute(stmt)).scalars().all())

    market: dict[str, Any] | None = None
    industries: list[dict[str, Any]] = []
    concepts: list[dict[str, Any]] = []
    for s in scopes:
        entry = _serialize_scope(s)
        if s.scope_type == "market":
            market = entry
        elif s.scope_type == "industry":
            industries.append(entry)
        elif s.scope_type == "concept":
            concepts.append(entry)

    return {
        "scan_run_id": str(scan_run_id),
        "trade_date": run.trade_date.isoformat(),
        "algorithm_version": AUCTION_AGGREGATION_ALGORITHM_VERSION,
        "market": market,
        "industries": industries,
        "concepts": concepts,
    }


# =============================================================================
# 模块自测（纯函数，不连 DB）
# =============================================================================


if __name__ == "__main__":
    print("auction_aggregation_service 自测...")

    # _percentile
    assert _percentile([], 50) is None
    assert _percentile([1.0], 50) == 1.0
    assert _percentile([1.0, 2.0, 3.0], 50) == 2.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5
    assert _percentile([1.0, 2.0, 3.0, 4.0], 25) == 1.75

    # _ratio / _ratio_entry
    assert _ratio(1, 2) == 0.5
    assert _ratio(0, 0) == 0.0
    assert _ratio(5, 0) == 0.0
    entry = _ratio_entry(3, 10)
    assert entry["numerator"] == 3
    assert entry["denominator"] == 10
    assert entry["ratio"] == 0.3

    # _safe_float
    assert _safe_float(None) is None
    assert _safe_float("abc") is None
    assert _safe_float("3.14") == 3.14

    # _round
    assert _round(None) is None
    assert _round(1.23456789) == 1.234568

    # _classify_confidence
    assert _classify_confidence(valid_count=25, coverage_ratio=0.9) == "high"
    assert _classify_confidence(valid_count=15, coverage_ratio=0.7) == "medium"
    assert _classify_confidence(valid_count=5, coverage_ratio=0.5) == "low"
    # 概念核心覆盖率不足降级
    assert _classify_confidence(
        valid_count=25, coverage_ratio=0.9, core_coverage=0.2,
    ) == "medium"
    assert _classify_confidence(
        valid_count=15, coverage_ratio=0.7, core_coverage=0.1,
    ) == "low"
    # 概念核心覆盖率充足不降级
    assert _classify_confidence(
        valid_count=25, coverage_ratio=0.9, core_coverage=0.5,
    ) == "high"

    # _classify_status_label — 构造测试 payload
    def _make_payload(
        *, high_open=0.0, low_open=0.0, dual_breakout=0.0, dual_breakdown=0.0,
        supply_ob=0.0, demand_ob=0.0, positive_migration=0.0,
    ) -> dict[str, Any]:
        return {
            "open_distribution": {
                "high_open": {"ratio": high_open},
                "low_open": {"ratio": low_open},
            },
            "dual_events": {
                "dual_breakout": {"ratio": dual_breakout},
                "dual_breakdown": {"ratio": dual_breakdown},
            },
            "zone_distribution": {
                "resistance_zone": {"ratio": supply_ob},
                "support_zone": {"ratio": demand_ob},
            },
            "positive_migration": {"ratio": positive_migration},
        }

    # full_repricing
    assert _classify_status_label(
        payload=_make_payload(high_open=0.7, dual_breakout=0.3),
        median_change=2.0, leader_change=5.0, top3_contribution=0.3,
        dispersion=1.0, coverage_ratio=0.9,
    ) == "full_repricing"

    # full_breakdown
    assert _classify_status_label(
        payload=_make_payload(low_open=0.7, dual_breakdown=0.3),
        median_change=-2.0, leader_change=1.0, top3_contribution=0.3,
        dispersion=1.0, coverage_ratio=0.9,
    ) == "full_breakdown"

    # leader_driven
    assert _classify_status_label(
        payload=_make_payload(),
        median_change=1.0, leader_change=5.0, top3_contribution=0.6,
        dispersion=1.0, coverage_ratio=0.9,
    ) == "leader_driven"

    # initial_diffusion
    assert _classify_status_label(
        payload=_make_payload(positive_migration=0.5),
        median_change=0.5, leader_change=1.0, top3_contribution=0.3,
        dispersion=1.0, coverage_ratio=0.6,
    ) == "initial_diffusion"

    # resistance_high_open
    assert _classify_status_label(
        payload=_make_payload(high_open=0.5, supply_ob=0.3),
        median_change=0.5, leader_change=1.0, top3_contribution=0.3,
        dispersion=1.0, coverage_ratio=0.6,
    ) == "resistance_high_open"

    # support_repair
    assert _classify_status_label(
        payload=_make_payload(low_open=0.5, demand_ob=0.3),
        median_change=-0.5, leader_change=0.5, top3_contribution=0.3,
        dispersion=1.0, coverage_ratio=0.6,
    ) == "support_repair"

    # high_divergence
    assert _classify_status_label(
        payload=_make_payload(),
        median_change=0.1, leader_change=0.5, top3_contribution=0.3,
        dispersion=3.0, coverage_ratio=0.6,
    ) == "high_divergence"

    # inconclusive
    assert _classify_status_label(
        payload=_make_payload(),
        median_change=0.1, leader_change=0.2, top3_contribution=0.3,
        dispersion=0.5, coverage_ratio=0.6,
    ) == "inconclusive"

    # _compute_scope_metrics — 空样本
    empty_metrics = _compute_scope_metrics([], total_count=0)
    assert empty_metrics["valid_count"] == 0
    assert empty_metrics["coverage_ratio"] == 0.0
    assert empty_metrics["median_change_pct"] is None
    assert empty_metrics["status_label"] == "inconclusive"
    assert empty_metrics["confidence_level"] == "low"
    assert empty_metrics["payload"]["open_distribution"]["high_open"]["denominator"] == 0

    # _compute_scope_metrics — 构造 mock results
    class _MockResult:
        def __init__(
            self, instrument_id, change_pct, amount=None,
            structure_position=None, chip_position=None,
            participation_level=None, is_suspended=False,
            relative_volume_median_20d=None,
        ):
            self.instrument_id = instrument_id
            self.change_pct = change_pct
            self.auction_amount = amount
            self.structure_position = structure_position
            self.chip_position = chip_position
            self.participation_level = participation_level
            self.is_suspended = is_suspended
            self.relative_volume_median_20d = relative_volume_median_20d

    iids = [uuid.uuid4() for _ in range(5)]
    mock_results = [
        _MockResult(iids[0], 5.0, amount=1000, structure_position="above_trigger",
                    chip_position="above_upper", participation_level="abnormal_high",
                    relative_volume_median_20d=3.0),  # dual_breakout, abnormal
        _MockResult(iids[1], 3.0, amount=800, structure_position="above_high",
                    relative_volume_median_20d=2.0),  # breakout
        _MockResult(iids[2], -1.0, amount=500, structure_position="below_trigger",
                    chip_position="below_lower",
                    relative_volume_median_20d=1.0),  # dual_breakdown
        _MockResult(iids[3], 0.5, amount=300, structure_position="supply_ob",
                    relative_volume_median_20d=0.5),  # resistance_zone
        _MockResult(iids[4], -0.5, amount=200, structure_position="demand_ob",
                    relative_volume_median_20d=0.3),  # support_zone
    ]
    metrics = _compute_scope_metrics(mock_results, total_count=5)
    assert metrics["total_count"] == 5
    assert metrics["valid_count"] == 5
    assert metrics["coverage_ratio"] == 1.0
    assert metrics["open_high_count"] == 3  # 5, 3, 0.5 → 3 个 > 0... wait 0.5>0 yes
    # change_pcts = [5, 3, -1, 0.5, -0.5] → high: 5,3,0.5 = 3; low: -1,-0.5 = 2
    assert metrics["open_low_count"] == 2
    assert metrics["structure_breakout_count"] == 2  # above_trigger, above_high
    assert metrics["structure_breakdown_count"] == 1  # below_trigger
    assert metrics["chip_cross_up_count"] == 1  # above_upper
    assert metrics["chip_cross_down_count"] == 1  # below_lower
    assert metrics["dual_breakout_count"] == 1
    assert metrics["dual_breakdown_count"] == 1
    assert metrics["resistance_zone_count"] == 1  # supply_ob
    assert metrics["support_zone_count"] == 1  # demand_ob
    assert metrics["abnormal_volume_pct"] == 0.2  # 1/5
    # top3 contribution = (1000+800+500)/2800
    assert abs(metrics["top3_contribution"] - (2300 / 2800)) < 1e-6
    assert metrics["positive_coverage"] == 0.6  # 3/5
    assert metrics["negative_coverage"] == 0.4  # 2/5
    # payload ratio entries
    assert metrics["payload"]["open_distribution"]["high_open"]["numerator"] == 3
    assert metrics["payload"]["open_distribution"]["high_open"]["denominator"] == 5
    assert metrics["payload"]["dual_events"]["dual_breakout"]["numerator"] == 1
    # 置信度：valid=5 < 10 → low
    assert metrics["confidence_level"] == "low"

    # 概念置信度降级测试
    big_iids = [uuid.uuid4() for _ in range(25)]
    big_results = [
        _MockResult(iid, 0.1, amount=100, relative_volume_median_20d=1.0)
        for iid in big_iids
    ]
    big_metrics = _compute_scope_metrics(big_results, total_count=25, is_concept=True)
    # valid=25, coverage=1.0, 但 core_count=0（所有 change=0.1，median=0.1，无 >0.1 的）
    # core_coverage=0 → 概念降级到 low
    assert big_metrics["confidence_level"] == "low"

    # 去重测试
    dup_results = [mock_results[0], mock_results[0]]
    dup_metrics = _compute_scope_metrics(dup_results, total_count=1)
    assert dup_metrics["valid_count"] == 1  # 去重后只剩 1 条

    print("OK")
