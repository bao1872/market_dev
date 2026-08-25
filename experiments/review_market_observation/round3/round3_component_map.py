"""Round 3 §3 — 冻结 Current Review 真实定义。

严格按 DEV_BASE_SHA=6fc7384228b2e51f13d3cf5af2a6b6a26b2837b0 的真实代码：
- metric_registry.py: 27 component specs（5P/6Q/5U/5C/6V）
- metric_engine.py: derive_fn 实现
- member_fact.py: previous_state_to_flat + ReviewMemberFact.to_metric_input

输出 round3_current_component_map.csv，每个 component 一行，纯事实映射。
"""
from __future__ import annotations

import csv
from dataclasses import asdict
from pathlib import Path
from typing import Any

import sys
# 允许从主仓库 backend 导入（DEV_BASE_SHA 路径在 main repo 中）
_MAIN_REPO = Path(__file__).resolve().parents[5]  # market_dev/
sys.path.insert(0, str(_MAIN_REPO / "backend"))

from app.domain.review.metric_registry import (  # noqa: E402
    DEFAULT_REGISTRY,
    MetricComponentSpec,
)

# 基于 metric_engine._DERIVE_FNS + member_fact + registry 字段来源的事实语义映射
# candidate_observation_dimension 只做事实标签，不评价
_DIMENSION_MAP: dict[str, dict[str, str]] = {
    # ============= P（价格表现强度）=============
    "scope_return_1d": {
        "actual_formula": "成员review_return_1d的等权中位数（优先官方指数回退）",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "日收益率截面中位数 → 范围表现",
        "candidate_observation_dimension": "PRICE",
    },
    "advance_ratio": {
        "actual_formula": "count(review_return_1d > 0) / ready_count",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "上涨成员比例 → 涨跌宽度",
        "candidate_observation_dimension": "BREADTH",
    },
    "trend_price_alignment_ratio": {
        "actual_formula": "count(fp_trend_direction=UP AND review_return_1d>0) / ready",
        "actual_input_fields": "fp_trend_direction + review_return_1d",
        "source_semantics": "趋势与当日价格确认的一致性比例",
        "candidate_observation_dimension": "MIXED",
    },
    "new_high_ratio": {
        "actual_formula": "count(review_price_position >= 0.97) / ready_count",
        "actual_input_fields": "review_price_position (=price_position_120d)",
        "source_semantics": "120日高位区间成员比例 → 位置/动量",
        "candidate_observation_dimension": "PRICE",
    },
    "price_position_median": {
        "actual_formula": "median(review_price_position)",
        "actual_input_fields": "review_price_position",
        "source_semantics": "成员120日价格位置中位数 → 整体水位",
        "candidate_observation_dimension": "PRICE",
    },

    # ============= Q（内部结构质量）=============
    "uptrend_member_ratio": {
        "actual_formula": "count(fp_trend_direction=UP, via SemanticAdapter) / ready",
        "actual_input_fields": "fp_trend_direction (=中文方向标签：上行/下行/震荡)",
        "source_semantics": "上行趋势成员比例 → 趋势状态",
        "candidate_observation_dimension": "STATE",
    },
    "main_structure_up_ratio": {
        "actual_formula": "count(fp_swing_direction=UP) / ready",
        "actual_input_fields": "fp_swing_direction (=中文方向)",
        "source_semantics": "主要结构(swing)向上比例 → 结构状态",
        "candidate_observation_dimension": "STATE",
    },
    "short_structure_up_ratio": {
        "actual_formula": "count(fp_internal_direction=UP) / ready",
        "actual_input_fields": "fp_internal_direction (=中文方向)",
        "source_semantics": "短线结构(internal)向上比例 → 结构状态",
        "candidate_observation_dimension": "STATE",
    },
    "trend_structure_momentum_alignment_ratio": {
        "actual_formula": "count(fp_structure_alignment=共振/ALIGNED) / ready",
        "actual_input_fields": "fp_structure_alignment (=共振/背离)",
        "source_semantics": "多周期结构一致性比例 → 跨周期发散/收敛",
        "candidate_observation_dimension": "DIFFUSION",
    },
    "structure_net_event_rate": {
        "actual_formula": "(bullish_event_count - bearish_event_count) / (ready * 3)",
        "actual_input_fields": "fp_latest_bos_direction + fp_latest_choch_direction + fp_latest_ob_direction",
        "source_semantics": "结构事件净多头率 → 结构变化方向（Transition-like）",
        "candidate_observation_dimension": "TRANSITION",
    },
    "structure_breakdown_diffusion": {
        "actual_formula": "count(bearish BOS OR bearish CHoCH) / ready（反向分量）",
        "actual_input_fields": "fp_latest_bos_direction + fp_latest_choch_direction",
        "source_semantics": "结构破坏扩散率 → 负面事件扩散",
        "candidate_observation_dimension": "DIFFUSION",
    },

    # ============= U（参与范围）=============
    "multi_dim_improving_ratio": {
        "actual_formula": "count(≥2维度日环比改善(trend/swing/internal/mom_dir/mom_chg)) / ready",
        "actual_input_fields": "fp_trend_direction + fp_swing_direction + fp_internal_direction + fp_momentum_direction + fp_momentum_change + review_previous_first_pyramid",
        "source_semantics": "多维同步改善比例 → 改善扩散",
        "candidate_observation_dimension": "DIFFUSION",
    },
    "momentum_enhancing_coverage": {
        "actual_formula": "count(今mom_change > 昨mom_change, via SemanticAdapter score) / ready",
        "actual_input_fields": "fp_momentum_change + review_previous_first_pyramid",
        "source_semantics": "动量增强覆盖 → 动量变化过渡",
        "candidate_observation_dimension": "TRANSITION",
    },
    "fresh_structure_event_coverage": {
        "actual_formula": "count(any freshness non-None: bos/choch/ob) / ready",
        "actual_input_fields": "fp_latest_bos_freshness + fp_latest_choch_freshness + fp_latest_ob_freshness",
        "source_semantics": "新鲜结构事件覆盖率 → 事件参与",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "non_head_participation_ratio": {
        "actual_formula": "上涨比例 in 后70%（按review_return_1d排序去前30%头部）",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "非头部上涨参与比 → 参与分层",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "leader_follower_common_confirm_ratio": {
        "actual_formula": "按fp_volume_ratio20分档：前20%龙头/中60%二线/后20%普通；若三组各自up_ratio≥50%则1.0，否则三组平均up_ratio",
        "actual_input_fields": "fp_volume_ratio20 + review_return_1d",
        "source_semantics": "龙头/二线/普通共同确认比 → 层次参与",
        "candidate_observation_dimension": "PARTICIPATION",
    },

    # ============= C（集中程度）=============
    "top5_price_change_contribution": {
        "actual_formula": "sum(top5 abs(review_return_1d)) / sum(all abs(review_return_1d))",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "前5只绝对价格变化占比 → 价格贡献集中度",
        "candidate_observation_dimension": "CONCENTRATION",
    },
    "top10pct_event_contribution": {
        "actual_formula": "sum(top10% 事件成员的event_count) / total_events",
        "actual_input_fields": "fp_latest_bos_freshness + fp_latest_choch_freshness + fp_latest_ob_freshness",
        "source_semantics": "Top10%成员的结构事件贡献占比 → 事件集中度",
        "candidate_observation_dimension": "CONCENTRATION",
    },
    "member_change_hhi": {
        "actual_formula": "sum( (|ret_i| / sum|ret|) ^ 2 ) → HHI指数",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "成员绝对变化的HHI → 价格贡献分布均匀度",
        "candidate_observation_dimension": "CONCENTRATION",
    },
    "leader_median_diff": {
        "actual_formula": "top1(change_pct) - median(change_pct)",
        "actual_input_fields": "review_return_1d",
        "source_semantics": "龙头与中位数表现差 → 领先差距",
        "candidate_observation_dimension": "CONCENTRATION",
    },
    "top5_amount_contribution": {
        "actual_formula": "sum(top5 review_amount) / sum(all review_amount)",
        "actual_input_fields": "review_amount",
        "source_semantics": "Top5成交额占比 → 资金集中度",
        "candidate_observation_dimension": "CONCENTRATION",
    },

    # ============= V（成交活跃与效率）=============
    "volume_expansion_ratio": {
        "actual_formula": "count(review_volume_ratio20 > 1.5) / ready",
        "actual_input_fields": "review_volume_ratio20 (=当日量/前20日均量)",
        "source_semantics": "放量（>1.5×20日均）成员比例 → 量能参与",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "amount_expansion_ratio": {
        "actual_formula": "count(review_amount_ratio20 > 1.5) / ready",
        "actual_input_fields": "review_amount_ratio20 (=当日额/前20日均额)",
        "source_semantics": "放额（>1.5×20日均）成员比例 → 资金参与",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "volume_percentile20_median": {
        "actual_formula": "median(review_volume_percentile20) / 100",
        "actual_input_fields": "review_volume_percentile20 (=20日量分位)",
        "source_semantics": "成员20日量分位中位数 → 量能水位",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "amount_percentile200_median": {
        "actual_formula": "median(review_amount_percentile200) / 100",
        "actual_input_fields": "review_amount_percentile200 (=200日额分位)",
        "source_semantics": "成员200日额分位中位数 → 长期资金水位",
        "candidate_observation_dimension": "PARTICIPATION",
    },
    "trend_segment_volume_improvement": {
        "actual_formula": "median(fp_segment_volume_ratio = current_vs_prev_volume_mean_ratio)",
        "actual_input_fields": "fp_segment_volume_ratio (=当前DSA段均量/前一段均量)",
        "source_semantics": "趋势段均量相对前一段改善中位数 → 段内参与变化",
        "candidate_observation_dimension": "MIXED",
    },
    "price_amount_efficiency_median": {
        "actual_formula": "median( |review_return_1d| / review_amount_ratio20 )",
        "actual_input_fields": "review_return_1d + review_amount_ratio20",
        "source_semantics": "价格变化/相对成交额的效率 → 用多少资金撬动了多少涨跌",
        "candidate_observation_dimension": "OTHER",
    },
}


def _lookup(name: str, key: str) -> str:
    info = _DIMENSION_MAP.get(name) or {}
    return info.get(key, "")


def generate_component_map_csv(out_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    reg = DEFAULT_REGISTRY
    for family_code in reg.metric_codes:
        metric = reg.get_metric(family_code)
        for comp in metric.components:
            row = {
                "component_name": comp.name,
                "family": family_code,
                "weight": comp.weight,
                "direction": comp.direction,
                "actual_formula": _lookup(comp.name, "actual_formula"),
                "actual_input_fields": _lookup(comp.name, "actual_input_fields"),
                "source_semantics": _lookup(comp.name, "source_semantics"),
                "candidate_observation_dimension": _lookup(comp.name,
                                                          "candidate_observation_dimension"),
                "field_source": comp.field_source,
                "derive_fn": comp.derive_fn or "",
                "extra_fields": "|".join(comp.extra_fields),
            }
            rows.append(row)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else []
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    return rows


def expected_component_count() -> int:
    """测试用：registry components 总数。"""
    reg = DEFAULT_REGISTRY
    return sum(len(reg.get_metric(c).components) for c in reg.metric_codes)


def main() -> int:
    out_dir = Path(__file__).resolve().parents[1] / "out" / "round3"
    out_path = out_dir / "round3_current_component_map.csv"
    rows = generate_component_map_csv(out_path)
    print(f"OK: {len(rows)} components written to {out_path}")
    # 家族分布
    families: dict[str, int] = {}
    for r in rows:
        families[r["family"]] = families.get(r["family"], 0) + 1
    for fam, n in sorted(families.items()):
        print(f"  {fam}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
