"""ReviewMetricComponentRegistry - P/Q/U/C/V component 字段映射注册表（PRD §7）。

权威约束（PRD §7.1）：
- 所有字段映射必须通过本 registry 引用现有权威扁平字段（fp_*）
- 禁止在业务代码中散落 JSON path
- 初始权重可全部为 1，但必须在 registry 中显式配置并写入 algorithm_version
- 每个 component 必须保留：原始值、方向、分母、字段来源和权重

PRD §7.2-§7.6 定义的初始 components：
- P（价格表现强度）：scope_return_1d / advance_ratio / trend_price_alignment_ratio /
                   new_high_ratio / price_position_median
- Q（内部结构质量）：uptrend_member_ratio / main_structure_up_ratio /
                   short_structure_up_ratio / trend_structure_momentum_alignment_ratio /
                   structure_net_event_rate / structure_breakdown_diffusion（反向）
- U（参与范围）：multi_dim_improving_ratio / momentum_enhancing_coverage /
                fresh_structure_event_coverage / non_head_participation_ratio /
                leader_follower_common_confirm_ratio
- C（集中程度）：top5_price_change_contribution / top10pct_event_contribution /
                member_change_hhi / leader_median_diff / top5_amount_contribution
- V（成交活跃与效率）：volume_expansion_ratio / amount_expansion_ratio /
                     volume_percentile20_median / amount_percentile200_median /
                     trend_segment_volume_improvement / price_amount_efficiency_median

每个 component spec 包含：
- name: component 名称
- field_source: 权威扁平字段名（fp_*）或派生字段名
- direction: positive（正向贡献）/ negative（反向贡献，值越大 metric 越差）
- weight: 权重（默认 1.0）
- description: 中文说明

模块自测：
    python -m app.domain.review.metric_registry
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MetricComponentSpec:
    """P/Q/U/C/V 单个 component 规格（PRD §7.1）。

    不变量：
    - name 在同一 metric 内唯一
    - field_source 必须是权威扁平字段名（fp_*）或显式派生字段名
    - direction: positive=正向贡献，negative=反向贡献（如结构破坏扩散率）
    - weight >= 0
    """

    name: str
    field_source: str
    direction: str = "positive"  # positive / negative
    weight: float = 1.0
    description: str = ""
    # 派生字段需要的计算函数标识（None=直接读 field_source 值）
    # 派生字段命名以 derive_ 开头，由 metric_engine 在计算时识别
    derive_fn: str | None = None
    # 派生字段需要的额外字段（用于 engine 在派生时取数）
    extra_fields: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.direction not in ("positive", "negative"):
            raise ValueError(
                f"MetricComponentSpec(name={self.name}) direction 非法: "
                f"{self.direction}（必须 positive/negative）"
            )
        if self.weight < 0:
            raise ValueError(
                f"MetricComponentSpec(name={self.name}) weight 不能为负: {self.weight}"
            )


@dataclass(frozen=True)
class MetricSpec:
    """单个聚合变量（P/Q/U/C/V）规格。"""

    code: str  # P / Q / U / C / V
    name: str  # 中文全称
    description: str
    components: tuple[MetricComponentSpec, ...]

    def component_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components)

    def get_component(self, name: str) -> MetricComponentSpec | None:
        for c in self.components:
            if c.name == name:
                return c
        return None


# =============================================================================
# 初始 components 定义（PRD §7.2-§7.6）
# =============================================================================

# P：价格表现强度（PRD §7.2）
_P_COMPONENTS: tuple[MetricComponentSpec, ...] = (
    MetricComponentSpec(
        name="scope_return_1d",
        field_source="derive_scope_return_1d",
        direction="positive",
        description="范围 1 日收益率（优先官方指数，回退成员等权中位数）",
        derive_fn="scope_return_1d",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="advance_ratio",
        field_source="derive_advance_ratio",
        direction="positive",
        description="当日上涨成员比例（change_pct > 0 的成员数 / ready_count）",
        derive_fn="advance_ratio",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="trend_price_alignment_ratio",
        field_source="derive_trend_price_alignment_ratio",
        direction="positive",
        description="趋势向上且当日上涨成员数 / ready_count",
        derive_fn="trend_price_alignment_ratio",
        extra_fields=("fp_trend_direction", "review_return_1d"),
    ),
    MetricComponentSpec(
        name="new_high_ratio",
        field_source="derive_new_high_ratio",
        direction="positive",
        description="进入近期高位区间的成员比例",
        derive_fn="new_high_ratio",
        extra_fields=("review_price_position",),
    ),
    MetricComponentSpec(
        name="price_position_median",
        field_source="derive_price_position_median",
        direction="positive",
        description="成员价格在自身滚动区间的位置中位数",
        derive_fn="price_position_median",
        extra_fields=("review_price_position",),
    ),
)

# Q：内部结构质量（PRD §7.3）
_Q_COMPONENTS: tuple[MetricComponentSpec, ...] = (
    MetricComponentSpec(
        name="uptrend_member_ratio",
        field_source="fp_trend_direction",
        direction="positive",
        description="上行趋势成员比例",
    ),
    MetricComponentSpec(
        name="main_structure_up_ratio",
        field_source="fp_swing_direction",
        direction="positive",
        description="主要结构向上比例",
    ),
    MetricComponentSpec(
        name="short_structure_up_ratio",
        field_source="fp_internal_direction",
        direction="positive",
        description="短线结构向上比例",
    ),
    MetricComponentSpec(
        name="trend_structure_momentum_alignment_ratio",
        field_source="fp_structure_alignment",
        direction="positive",
        description="趋势、结构、动量一致性比例",
    ),
    MetricComponentSpec(
        name="structure_net_event_rate",
        field_source="derive_structure_net_event_rate",
        direction="positive",
        description="bullish 结构事件率 - bearish 结构事件率",
        derive_fn="structure_net_event_rate",
        extra_fields=(
            "fp_latest_bos_direction",
            "fp_latest_choch_direction",
            "fp_latest_ob_direction",
        ),
    ),
    MetricComponentSpec(
        name="structure_breakdown_diffusion",
        field_source="derive_structure_breakdown_diffusion",
        direction="negative",  # 反向 component：扩散率越大 Q 越差
        description="结构破坏扩散率（反向 component）",
        derive_fn="structure_breakdown_diffusion",
        extra_fields=(
            "fp_latest_bos_direction",
            "fp_latest_choch_direction",
        ),
    ),
)

# U：参与范围（PRD §7.4）
_U_COMPONENTS: tuple[MetricComponentSpec, ...] = (
    MetricComponentSpec(
        name="multi_dim_improving_ratio",
        field_source="derive_multi_dim_improving_ratio",
        direction="positive",
        description="至少两个核心维度同步改善的成员比例",
        derive_fn="multi_dim_improving_ratio",
        extra_fields=(
            "fp_trend_direction",
            "fp_swing_direction",
            "fp_momentum_direction",
            "fp_momentum_change",
        ),
    ),
    MetricComponentSpec(
        name="momentum_enhancing_coverage",
        field_source="derive_momentum_enhancing_coverage",
        direction="positive",
        description="相对前一交易日动量增强覆盖率",
        derive_fn="momentum_enhancing_coverage",
        extra_fields=("fp_momentum_change", "review_previous_first_pyramid"),
    ),
    MetricComponentSpec(
        name="fresh_structure_event_coverage",
        field_source="derive_fresh_structure_event_coverage",
        direction="positive",
        description="新鲜结构事件覆盖率",
        derive_fn="fresh_structure_event_coverage",
        extra_fields=(
            "fp_latest_bos_freshness",
            "fp_latest_choch_freshness",
            "fp_latest_ob_freshness",
        ),
    ),
    MetricComponentSpec(
        name="non_head_participation_ratio",
        field_source="derive_non_head_participation_ratio",
        direction="positive",
        description="非头部成员参与比例",
        derive_fn="non_head_participation_ratio",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="leader_follower_common_confirm_ratio",
        field_source="derive_leader_follower_common_confirm_ratio",
        direction="positive",
        description="龙头、二线与普通成员共同确认比例",
        derive_fn="leader_follower_common_confirm_ratio",
        extra_fields=("review_return_1d", "review_amount"),
    ),
)

# C：集中程度（PRD §7.5，C 越高表示越集中，不表示越好）
_C_COMPONENTS: tuple[MetricComponentSpec, ...] = (
    MetricComponentSpec(
        name="top5_price_change_contribution",
        field_source="derive_top5_price_change_contribution",
        direction="positive",
        description="绝对价格变化贡献 Top5 占比",
        derive_fn="top5_contribution",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="top10pct_event_contribution",
        field_source="derive_top10pct_event_contribution",
        direction="positive",
        description="事件贡献 Top10% 成员占比",
        derive_fn="top10pct_event_contribution",
        extra_fields=(
            "fp_latest_bos_freshness",
            "fp_latest_choch_freshness",
            "fp_latest_ob_freshness",
        ),
    ),
    MetricComponentSpec(
        name="member_change_hhi",
        field_source="derive_member_change_hhi",
        direction="positive",
        description="成员绝对变化贡献 HHI（赫芬达尔指数）",
        derive_fn="member_change_hhi",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="leader_median_diff",
        field_source="derive_leader_median_diff",
        direction="positive",
        description="龙头与成员中位数表现差",
        derive_fn="leader_median_diff",
        extra_fields=("review_return_1d",),
    ),
    MetricComponentSpec(
        name="top5_amount_contribution",
        field_source="derive_top5_amount_contribution",
        direction="positive",
        description="有可靠成交额数据时 Top5 成交额占比",
        derive_fn="top5_amount_contribution",
        extra_fields=("review_amount",),
    ),
)

# V：成交活跃与效率（PRD §7.6）
_V_COMPONENTS: tuple[MetricComponentSpec, ...] = (
    MetricComponentSpec(
        name="volume_expansion_ratio",
        field_source="derive_volume_expansion_ratio",
        direction="positive",
        description="放量成员比例",
        derive_fn="volume_expansion_ratio",
        extra_fields=("review_volume_ratio20",),
    ),
    MetricComponentSpec(
        name="amount_expansion_ratio",
        field_source="derive_amount_expansion_ratio",
        direction="positive",
        description="成交额相对 20 日均值扩张成员比例",
        derive_fn="amount_expansion_ratio",
        extra_fields=("review_amount_ratio20",),
    ),
    MetricComponentSpec(
        name="volume_percentile20_median",
        field_source="review_volume_percentile20",
        direction="positive",
        description="成员 20 日成交量分位中位数",
    ),
    MetricComponentSpec(
        name="amount_percentile200_median",
        field_source="review_amount_percentile200",
        direction="positive",
        description="成员 200 日成交额分位中位数",
    ),
    MetricComponentSpec(
        name="trend_segment_volume_improvement",
        field_source="derive_trend_segment_volume_improvement",
        direction="positive",
        description="趋势段平均量相对前段改善比例",
        derive_fn="trend_segment_volume_improvement",
        extra_fields=(
            "fp_segment_volume_ratio",
            "fp_prev_segment_volume",
        ),
    ),
    MetricComponentSpec(
        name="price_amount_efficiency_median",
        field_source="derive_price_amount_efficiency_median",
        direction="positive",
        description="价格变化 / 相对成交额的效率中位数",
        derive_fn="price_amount_efficiency_median",
        extra_fields=("review_return_1d", "review_amount_ratio20"),
    ),
)


# =============================================================================
# Registry
# =============================================================================


class ReviewMetricComponentRegistry:
    """P/Q/U/C/V component 字段映射注册表（PRD §7.1）。

    使用方式：
        registry = ReviewMetricComponentRegistry()
        p_spec = registry.get_metric("P")
        for comp in p_spec.components:
            print(comp.name, comp.field_source, comp.direction, comp.weight)

    设计：
    - 不可变（构造后不允许修改 components），保证 algorithm_version 一致性
    - 通过版本号绑定 algorithm_version，components 变化必须升级 algorithm_version
    - 提供 get_metric / get_component / get_all_field_sources 等查询方法
    """

    def __init__(self, *, version: str = "review-1.0.0") -> None:
        self._version = version
        self._metrics: dict[str, MetricSpec] = {
            "P": MetricSpec(
                code="P",
                name="价格表现强度",
                description="PRD §7.2：P 只描述表面表现，不等价于内部质量",
                components=_P_COMPONENTS,
            ),
            "Q": MetricSpec(
                code="Q",
                name="内部结构质量",
                description="PRD §7.3：结构事件必须使用已落库事件和新鲜度",
                components=_Q_COMPONENTS,
            ),
            "U": MetricSpec(
                code="U",
                name="参与范围",
                description="PRD §7.4：U 表示宽度，不使用成交额权重替代成员参与",
                components=_U_COMPONENTS,
            ),
            "C": MetricSpec(
                code="C",
                name="集中程度",
                description="PRD §7.5：C 越高表示越集中，不表示越好",
                components=_C_COMPONENTS,
            ),
            "V": MetricSpec(
                code="V",
                name="成交活跃与效率",
                description="PRD §7.6：所有除法使用明确 epsilon 并过滤异常值",
                components=_V_COMPONENTS,
            ),
        }

    @property
    def version(self) -> str:
        return self._version

    @property
    def metric_codes(self) -> tuple[str, ...]:
        return tuple(self._metrics.keys())

    def get_metric(self, code: str) -> MetricSpec:
        """获取指定 metric 规格（P/Q/U/C/V）。"""
        if code not in self._metrics:
            raise KeyError(
                f"未知 metric code: {code}（合法值: {self.metric_codes}）"
            )
        return self._metrics[code]

    def get_component(self, metric_code: str, component_name: str) -> MetricComponentSpec:
        """获取指定 metric + component 规格。"""
        metric = self.get_metric(metric_code)
        comp = metric.get_component(component_name)
        if comp is None:
            raise KeyError(
                f"未知 component: metric={metric_code} name={component_name}"
            )
        return comp

    def get_all_field_sources(self) -> set[str]:
        """返回 registry 引用的所有权威扁平字段名（fp_* 或 derive_*）。"""
        sources: set[str] = set()
        for metric in self._metrics.values():
            for comp in metric.components:
                sources.add(comp.field_source)
                sources.update(comp.extra_fields)
        return sources

    def to_metadata(self) -> dict[str, Any]:
        """导出 registry 元数据（用于写入 run.metadata_json）。"""
        return {
            "version": self._version,
            "metrics": {
                code: {
                    "name": m.name,
                    "description": m.description,
                    "components": [
                        {
                            "name": c.name,
                            "field_source": c.field_source,
                            "direction": c.direction,
                            "weight": c.weight,
                            "description": c.description,
                            "derive_fn": c.derive_fn,
                            "extra_fields": list(c.extra_fields),
                        }
                        for c in m.components
                    ],
                }
                for code, m in self._metrics.items()
            },
        }


# 默认全局 registry 实例（与 REVIEW_ALGORITHM_VERSION 一致）
DEFAULT_REGISTRY = ReviewMetricComponentRegistry(version="review-1.0.0")


if __name__ == "__main__":
    reg = DEFAULT_REGISTRY
    print(f"version={reg.version} metrics={reg.metric_codes}")
    for code in reg.metric_codes:
        m = reg.get_metric(code)
        print(f"  {code} {m.name}: {len(m.components)} components")
        for c in m.components:
            print(
                f"    - {c.name} dir={c.direction} w={c.weight} "
                f"src={c.field_source}"
            )
    sources = reg.get_all_field_sources()
    assert "fp_trend_direction" in sources
    assert "derive_scope_return_1d" in sources
    print(f"OK: {len(sources)} field sources registered")
    print("OK: metric_registry verified")
