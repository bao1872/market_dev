"""A/B/C 三类偏差筛选器 Pydantic schema 与初始配置（PRD §8）。

PRD §8 筛选器合同：
- 必须由版本化配置驱动（初始工程默认值写入代码常量，后续可迁移到 yaml）
- 不得把阈值散落在多个 service
- 初始工程默认值仅用于形成可运行基线，上线前必须用历史回放校准
- 配置变化必须升级 filter_version

三类筛选器：
- A 类：表面表现与内部质量偏差
  - A1 surface_strong_internal_weak
  - A2 surface_weak_internal_improving
- B 类：当前状态与变化速度偏差
  - B1 high_level_slowing
  - B2 low_level_repair
- C 类：成交、参与与集中度偏差
  - C1 volume_without_breadth
  - C2 breadth_without_volume
  - C3 synchronized_expansion

PRD §8.4 信号排序（不生成综合黑箱分）：
- 偏差历史分位；当日变化分位；持续日数；coverage；
- scope_type 固定优先级；scope_name 稳定第二键。

模块自测：
    python -m app.domain.review.filter_definitions
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# 筛选器版本（配置变化必须升级；初始为 filters-1.0.0）
# [P0-7 2026-07-30] 升级到 filters-1.1.0：新增 D 族（第二金字塔维度偏差筛选器）
REVIEW_FILTER_VERSION = "filters-1.1.0"


class FilterFamily(StrEnum):
    """筛选器族（PRD §8）。"""

    A = "A"  # 表面表现与内部质量偏差
    B = "B"  # 当前状态与变化速度偏差
    C = "C"  # 成交、参与与集中度偏差
    D = "D"  # 第二金字塔维度偏差（PRD §24：迁移/新鲜度/覆盖率/集中度/相对变化）


class ComparisonOp(StrEnum):
    """比较操作符。"""

    GTE = ">="  # 大于等于
    LTE = "<="  # 小于等于
    GT = ">"
    LT = "<"
    EQ = "=="
    NEQ = "!="


class FilterCondition(BaseModel):
    """单个筛选条件（PRD §8 阈值表达式）。

    字段路径语法：
    - "P.value" → P payload 的 value 字段
    - "P.historyPercentile120d" → P 的 120 日历史分位
    - "P.delta1d" → P 的 1 日变化
    - "Q.components.structure_net_event_rate" → Q 的指定 component rawValue
    - "coverage" → scope 级 coverage_ratio
    """

    field: str = Field(..., description="字段路径（如 P.historyPercentile120d）")
    op: ComparisonOp = Field(..., description="比较操作符")
    value: float = Field(..., description="阈值")

    def evaluate(self, context: dict[str, Any]) -> bool:
        """评估条件是否满足。

        Args:
            context: 包含 P/Q/U/C/V payload 和 coverage 的上下文 dict

        Returns:
            True 表示条件满足
        """
        val = _resolve_field(self.field, context)
        if val is None:
            return False
        try:
            v = float(val)
        except (TypeError, ValueError):
            return False
        if self.op == ComparisonOp.GTE:
            return v >= self.value
        if self.op == ComparisonOp.LTE:
            return v <= self.value
        if self.op == ComparisonOp.GT:
            return v > self.value
        if self.op == ComparisonOp.LT:
            return v < self.value
        if self.op == ComparisonOp.EQ:
            return abs(v - self.value) < 1e-9
        if self.op == ComparisonOp.NEQ:
            return abs(v - self.value) >= 1e-9
        return False


class FilterDefinition(BaseModel):
    """单个筛选器定义（PRD §8）。

    命中条件：所有 conditions 满足（AND 语义）。
    多组 conditions 之间通过 any_of_groups 实现 OR 语义（PRD §8.1 A1 / §8.2 B1）。

    - conditions: AND 条件列表（全部满足才命中）
    - any_of_groups: 多组条件列表（任一组全部满足即命中），与 conditions 二选一
    - min_match_count: 配合 any_of_groups，要求至少 N 个 group 命中
    """

    signal_type: str = Field(..., description="信号类型（如 surface_strong_internal_weak）")
    family: FilterFamily = Field(..., description="筛选器族 A/B/C")
    description: str = Field("", description="中文说明")
    conditions: list[FilterCondition] = Field(
        default_factory=list,
        description="AND 条件列表（全部满足才命中）",
    )
    any_of_groups: list[list[FilterCondition]] | None = Field(
        None,
        description="多组条件（任一组全部满足即命中），与 conditions 二选一",
    )
    min_match_count: int | None = Field(
        None,
        description="配合 any_of_groups：至少 N 个 group 命中（None=1）",
    )
    confirmation_rule: dict[str, Any] = Field(
        default_factory=dict,
        description="确认规则（PRD §10.1 confirmed 状态触发条件）",
    )
    invalidation_rule: dict[str, Any] = Field(
        default_factory=dict,
        description="失效规则（PRD §10.1 invalidated 状态触发条件）",
    )
    # 特殊评估器标识（用于 B/C 类复合条件，如"至少 2 项历史分位 >= 70"）
    # 命名以 eval_ 开头，由 filter_engine 在评估时识别。
    # None=使用 conditions / any_of_groups 标准评估。
    evaluator: str | None = Field(
        None,
        description="特殊评估器标识（如 eval_b1_high_level_slowing）",
    )

    @model_validator(mode="after")
    def _check_conditions_xor(self) -> FilterDefinition:
        # evaluator 模式下允许 conditions/any_of_groups 都为空
        if self.evaluator:
            return self
        if not self.conditions and not self.any_of_groups:
            raise ValueError(
                f"FilterDefinition(signal_type={self.signal_type}) "
                "必须指定 conditions / any_of_groups / evaluator 之一"
            )
        if self.conditions and self.any_of_groups:
            raise ValueError(
                f"FilterDefinition(signal_type={self.signal_type}) "
                "conditions 与 any_of_groups 互斥"
            )
        return self

    def evaluate(self, context: dict[str, Any]) -> bool:
        """评估筛选器是否命中。

        evaluator 模式下委托给 filter_engine 中对应的评估函数；
        filter_engine 会通过 set_evaluator 注入特殊评估器。
        """
        if self.evaluator:
            fn = _EVALUATOR_REGISTRY.get(self.evaluator)
            if fn is None:
                return False
            return fn(self, context)
        if self.conditions:
            return all(c.evaluate(context) for c in self.conditions)
        if self.any_of_groups:
            min_n = self.min_match_count or 1
            matched = sum(
                1 for grp in self.any_of_groups
                if all(c.evaluate(context) for c in grp)
            )
            return matched >= min_n
        return False


# 特殊评估器注册表（由 filter_engine 注入，避免循环依赖）
_EVALUATOR_REGISTRY: dict[str, Any] = {}


def set_evaluator(name: str, fn: Any) -> None:
    """注册特殊评估器函数（供 filter_engine 调用）。"""
    _EVALUATOR_REGISTRY[name] = fn


# =============================================================================
# 字段路径解析
# =============================================================================


def _resolve_field(path: str, context: dict[str, Any]) -> float | None:
    """从 context 中解析字段路径。

    支持的路径：
    - "P.value" / "P.historyPercentile120d" / "P.delta1d" / "P.coverage"
    - "Q.components.<name>" → component 的 rawValue
    - "coverage" → scope 级 coverage_ratio
    - "ready_count" → 有效成员数
    """
    parts = path.split(".")
    if len(parts) == 1:
        # 顶层字段
        v = context.get(parts[0])
        return _to_float(v)
    if len(parts) == 2:
        metric_code, field = parts
        metric = context.get(metric_code)
        if not isinstance(metric, dict):
            return None
        return _to_float(metric.get(field))
    if len(parts) == 3 and parts[1] == "components":
        # P.components.<name> → component rawValue
        metric_code, _, comp_name = parts
        metric = context.get(metric_code)
        if not isinstance(metric, dict):
            return None
        comps = metric.get("components") or []
        for c in comps:
            if c.get("name") == comp_name:
                return _to_float(c.get("rawValue"))
        return None
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
# 初始筛选器配置（PRD §8.1-§8.3 初始工程默认值）
# =============================================================================

# A1 surface_strong_internal_weak（PRD §8.1）
FILTER_A1_SURFACE_STRONG_INTERNAL_WEAK = FilterDefinition(
    signal_type="surface_strong_internal_weak",
    family=FilterFamily.A,
    description=(
        "A1：表面表现强但内部质量弱。P 历史分位 >= 70，"
        "(P-Q) 历史分位 >= 90，Q 或 U 1 日变化 <= 0，coverage >= 0.95"
    ),
    evaluator="eval_a1_surface_strong_internal_weak",
    confirmation_rule={
        "description": "连续 3 个交易日命中且 Q 历史分位持续下降",
        "min_consecutive_days": 3,
        "extra_conditions": ["Q.historyPercentile120d_declining"],
    },
    invalidation_rule={
        "description": "Q.value 上升超过 10 或 P.historyPercentile120d < 50",
        "conditions": [
            "Q.delta1d > 10",
            "P.historyPercentile120d < 50",
        ],
    },
)

# A2 surface_weak_internal_improving（PRD §8.1）
FILTER_A2_SURFACE_WEAK_INTERNAL_IMPROVING = FilterDefinition(
    signal_type="surface_weak_internal_improving",
    family=FilterFamily.A,
    description=(
        "A2：表面表现弱但内部改善。P 历史分位 <= 40，"
        "Q.delta1d 历史分位 >= 70，U.delta1d 历史分位 >= 60"
    ),
    conditions=[
        FilterCondition(field="P.historyPercentile120d", op=ComparisonOp.LTE, value=40),
        FilterCondition(
            field="_q_delta1d_history_pct", op=ComparisonOp.GTE, value=70,
        ),
        FilterCondition(
            field="_u_delta1d_history_pct", op=ComparisonOp.GTE, value=60,
        ),
        FilterCondition(field="coverage", op=ComparisonOp.GTE, value=0.95),
    ],
    confirmation_rule={
        "description": "连续 3 个交易日命中且 Q.value 上升",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "P.historyPercentile120d > 60 或 Q.delta1d < 0",
        "conditions": ["P.historyPercentile120d > 60", "Q.delta1d < 0"],
    },
)

# B1 high_level_slowing（PRD §8.2）
FILTER_B1_HIGH_LEVEL_SLOWING = FilterDefinition(
    signal_type="high_level_slowing",
    family=FilterFamily.B,
    description=(
        "B1：高位减速。P/Q/U/V 中至少 2 项历史分位 >= 70，"
        "Q/U/V 中至少 2 项 1 日变化分位 <= 30"
    ),
    evaluator="eval_b1_high_level_slowing",
    confirmation_rule={
        "description": "连续 5 个交易日命中",
        "min_consecutive_days": 5,
    },
    invalidation_rule={
        "description": "Q/U/V 任一 1 日变化分位 >= 70",
        "conditions": ["Q.delta1d >= 70", "U.delta1d >= 70", "V.delta1d >= 70"],
    },
)

# B2 low_level_repair（PRD §8.2）
FILTER_B2_LOW_LEVEL_REPAIR = FilterDefinition(
    signal_type="low_level_repair",
    family=FilterFamily.B,
    description=(
        "B2：低位修复。P/Q/U 中至少 2 项历史分位 <= 40，"
        "Q 与 U 的 1 日变化分位 >= 70，结构破坏扩散率不再继续上升"
    ),
    evaluator="eval_b2_low_level_repair",
    confirmation_rule={
        "description": "连续 3 个交易日命中且 P.historyPercentile120d 上升",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "P.historyPercentile120d > 60 或 Q.delta1d < 0",
        "conditions": ["P.historyPercentile120d > 60", "Q.delta1d < 0"],
    },
)

# C1 volume_without_breadth（PRD §8.3）
FILTER_C1_VOLUME_WITHOUT_BREADTH = FilterDefinition(
    signal_type="volume_without_breadth",
    family=FilterFamily.C,
    description=(
        "C1：放量无广度。V 历史分位 >= 70 或 V 变化分位 >= 70，"
        "U 变化分位 <= 40，C 历史分位 >= 70 或 C 继续上升"
    ),
    evaluator="eval_c1_volume_without_breadth",
    confirmation_rule={
        "description": "连续 3 个交易日命中",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "U.delta1d > 0 或 V.historyPercentile120d < 50",
        "conditions": ["U.delta1d > 0", "V.historyPercentile120d < 50"],
    },
)

# C2 breadth_without_volume（PRD §8.3）
FILTER_C2_BREADTH_WITHOUT_VOLUME = FilterDefinition(
    signal_type="breadth_without_volume",
    family=FilterFamily.C,
    description=(
        "C2：广度无放量。U 变化分位 >= 70，"
        "V 历史分位 <= 50 或 V 变化分位 <= 50"
    ),
    evaluator="eval_c2_breadth_without_volume",
    confirmation_rule={
        "description": "连续 3 个交易日命中",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "V.delta1d > 0 且 V.historyPercentile120d > 50",
        "conditions": ["V.delta1d > 0", "V.historyPercentile120d > 50"],
    },
)

# C3 synchronized_expansion（PRD §8.3）
FILTER_C3_SYNCHRONIZED_EXPANSION = FilterDefinition(
    signal_type="synchronized_expansion",
    family=FilterFamily.C,
    description=(
        "C3：同步扩张。U 变化分位 >= 70，V 变化分位 >= 70，"
        "C 未处于异常高位或未继续上升"
    ),
    evaluator="eval_c3_synchronized_expansion",
    confirmation_rule={
        "description": "连续 2 个交易日命中",
        "min_consecutive_days": 2,
    },
    invalidation_rule={
        "description": "U.delta1d < 0 或 V.delta1d < 0",
        "conditions": ["U.delta1d < 0", "V.delta1d < 0"],
    },
)


# =============================================================================
# D 族：第二金字塔维度偏差筛选器（PRD §24）
#
# Slice 4A4 — consumer cutover：
# - D2（事件新鲜度）读取 canonical ``scope_observation.freshness``（Review owner）；
# - D4（集中度）读取 canonical
#   ``scope_observation.structure.current_state.technical_state.concentration``
#   （technical-state concentration，Review owner）；
# - D1 / D3 / D5 仍读取 legacy ``context["pyramid_v2"]``。
#
# 维度（PRD §24.1）：
# - 状态迁移（state migration）：D1 pyramid_v2.diffusion（legacy）
# - 事件新鲜度（event freshness）：D2 scope_observation.freshness（canonical）
# - 覆盖率/宽度（breadth）：D3 pyramid_v2.diffusion.participation_coverage（legacy）
# - 集中度（concentration）：D4 scope_observation...technical_state.concentration（canonical）
# - 相对强度（relative strength）：D5 pyramid_v2.relative_strength（legacy）
#
# D 族筛选器只在对应输入数据可用时评估；market/major_index/style 等
# 无数据来源的 scope 不命中 D 族（评估器返回 False）。
# =============================================================================

# D1 state_migration_positive（PRD §24.1 状态迁移）
FILTER_D1_STATE_MIGRATION_POSITIVE = FilterDefinition(
    signal_type="state_migration_positive",
    family=FilterFamily.D,
    description=(
        "D1：正向状态迁移占优。positive_migration_count >= 5，"
        "positive_ratio >= 0.6，negative_migration_count <= positive_migration_count"
    ),
    evaluator="eval_d1_state_migration_positive",
    confirmation_rule={
        "description": "连续 2 个交易日命中",
        "min_consecutive_days": 2,
    },
    invalidation_rule={
        "description": "negative_migration_count > positive_migration_count",
        "conditions": [],
    },
)

# D2 event_freshness_high（PRD §24.1 事件新鲜度）
FILTER_D2_EVENT_FRESHNESS_HIGH = FilterDefinition(
    signal_type="event_freshness_high",
    family=FilterFamily.D,
    description=(
        "D2：事件新鲜度高。decay_weighted_density >= 0.3，"
        "today_count >= 1 或 last_5d_count >= 3"
    ),
    evaluator="eval_d2_event_freshness_high",
    confirmation_rule={
        "description": "连续 2 个交易日命中",
        "min_consecutive_days": 2,
    },
    invalidation_rule={
        "description": "decay_weighted_density < 0.1",
        "conditions": [],
    },
)

# D3 breadth_expansion（PRD §24.1 宽度/覆盖率）
FILTER_D3_BREADTH_EXPANSION = FilterDefinition(
    signal_type="breadth_expansion",
    family=FilterFamily.D,
    description=(
        "D3：宽度扩张。participation_coverage >= 0.3，"
        "total_migration_count >= 5"
    ),
    evaluator="eval_d3_breadth_expansion",
    confirmation_rule={
        "description": "连续 2 个交易日命中",
        "min_consecutive_days": 2,
    },
    invalidation_rule={
        "description": "participation_coverage < 0.1",
        "conditions": [],
    },
)

# D4 concentration_high（PRD §24.1 集中度）
FILTER_D4_CONCENTRATION_HIGH = FilterDefinition(
    signal_type="concentration_high",
    family=FilterFamily.D,
    description=(
        "D4：集中度高。hhi >= 0.1 或 top5_contribution >= 0.4，"
        "leader_median_gap > 0"
    ),
    evaluator="eval_d4_concentration_high",
    confirmation_rule={
        "description": "连续 3 个交易日命中",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "hhi < 0.05 且 top5_contribution < 0.2",
        "conditions": [],
    },
)

# D5 relative_strength_strong（PRD §24.1 相对强度）
FILTER_D5_RELATIVE_STRENGTH_STRONG = FilterDefinition(
    signal_type="relative_strength_strong",
    family=FilterFamily.D,
    description=(
        "D5：相对强度强。vs_market.ratio >= 1.1，"
        "equal_weight_diff > 0"
    ),
    evaluator="eval_d5_relative_strength_strong",
    confirmation_rule={
        "description": "连续 3 个交易日命中",
        "min_consecutive_days": 3,
    },
    invalidation_rule={
        "description": "vs_market.ratio < 1.0",
        "conditions": [],
    },
)


# 初始筛选器列表（按 PRD §8 顺序）
DEFAULT_FILTERS: list[FilterDefinition] = [
    FILTER_A1_SURFACE_STRONG_INTERNAL_WEAK,
    FILTER_A2_SURFACE_WEAK_INTERNAL_IMPROVING,
    FILTER_B1_HIGH_LEVEL_SLOWING,
    FILTER_B2_LOW_LEVEL_REPAIR,
    FILTER_C1_VOLUME_WITHOUT_BREADTH,
    FILTER_C2_BREADTH_WITHOUT_VOLUME,
    FILTER_C3_SYNCHRONIZED_EXPANSION,
    # [P0-7] D 族：第二金字塔维度偏差（PRD §24）
    FILTER_D1_STATE_MIGRATION_POSITIVE,
    FILTER_D2_EVENT_FRESHNESS_HIGH,
    FILTER_D3_BREADTH_EXPANSION,
    FILTER_D4_CONCENTRATION_HIGH,
    FILTER_D5_RELATIVE_STRENGTH_STRONG,
]


# =============================================================================
# 排序键（PRD §8.4）
# =============================================================================

# scope_type 固定优先级（数字越小越靠前）
SCOPE_TYPE_PRIORITY: dict[str, int] = {
    "market": 1,
    "major_index": 2,
    "style": 3,
    "industry_l1": 4,
    "industry_l2": 5,
    "industry_l3": 6,
    "concept": 7,
    "instrument": 8,
}


def build_rank_key(
    *,
    bias_history_pct: float | None,
    delta1d_pct: float | None,
    duration_days: int,
    coverage: float | None,
    scope_type: str,
    scope_name: str,
) -> dict[str, Any]:
    """构建信号排序键（PRD §8.4）。

    排序顺序（数字越小越靠前）：
    1. 偏差历史分位（越大越靠前）
    2. 当日变化分位（越大越靠前）
    3. 持续日数（越大越靠前）
    4. coverage（越大越靠前）
    5. scope_type 固定优先级（数字越小越靠前）
    6. scope_name 稳定第二键（字典序升序）
    """
    return {
        "bias_history_pct": bias_history_pct,
        "delta1d_pct": delta1d_pct,
        "duration_days": duration_days,
        "coverage": coverage,
        "scope_type_priority": SCOPE_TYPE_PRIORITY.get(scope_type, 99),
        "scope_name": scope_name,
    }


def compare_rank_keys(a: dict[str, Any], b: dict[str, Any]) -> int:
    """比较两个 rank_key（a 在前返回 -1，b 在前返回 1，相等返回 0）。

    PRD §8.4：偏差历史分位 > 当日变化分位 > 持续日数 > coverage >
              scope_type 优先级 > scope_name 字典序
    """
    # 偏差历史分位（越大越靠前，None 视为 -1）
    a_bias = a.get("bias_history_pct") or -1
    b_bias = b.get("bias_history_pct") or -1
    if a_bias > b_bias:
        return -1
    if a_bias < b_bias:
        return 1

    # 当日变化分位（越大越靠前）
    a_delta = a.get("delta1d_pct") or -1
    b_delta = b.get("delta1d_pct") or -1
    if a_delta > b_delta:
        return -1
    if a_delta < b_delta:
        return 1

    # 持续日数（越大越靠前）
    a_dur = a.get("duration_days") or 0
    b_dur = b.get("duration_days") or 0
    if a_dur > b_dur:
        return -1
    if a_dur < b_dur:
        return 1

    # coverage（越大越靠前）
    a_cov = a.get("coverage") or 0
    b_cov = b.get("coverage") or 0
    if a_cov > b_cov:
        return -1
    if a_cov < b_cov:
        return 1

    # scope_type 优先级（数字越小越靠前）
    a_pri = a.get("scope_type_priority") or 99
    b_pri = b.get("scope_type_priority") or 99
    if a_pri < b_pri:
        return -1
    if a_pri > b_pri:
        return 1

    # scope_name 字典序（升序）
    a_name = a.get("scope_name") or ""
    b_name = b.get("scope_name") or ""
    if a_name < b_name:
        return -1
    if a_name > b_name:
        return 1

    return 0


if __name__ == "__main__":
    assert len(DEFAULT_FILTERS) == 12  # A(2) + B(2) + C(3) + D(5)
    families = {f.family.value for f in DEFAULT_FILTERS}
    assert families == {"A", "B", "C", "D"}
    print(f"OK: {len(DEFAULT_FILTERS)} filters loaded, families={families}")
    print(f"OK: filter_version={REVIEW_FILTER_VERSION}")

    # 测试 rank_key 比较
    k1 = build_rank_key(
        bias_history_pct=90, delta1d_pct=80, duration_days=5,
        coverage=0.98, scope_type="market", scope_name="全市场",
    )
    k2 = build_rank_key(
        bias_history_pct=80, delta1d_pct=90, duration_days=10,
        coverage=0.99, scope_type="industry_l1", scope_name="电子",
    )
    # k1 bias > k2 bias，k1 应在前
    assert compare_rank_keys(k1, k2) == -1
    print("OK: rank_key comparison verified")
