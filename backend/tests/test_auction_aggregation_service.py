# PURE_UNIT_TEST=1
"""竞价板块/市场聚合服务纯单元测试 - 不连接数据库。

覆盖：
  1. 状态标签分类：full_repricing / leader_driven / initial_diffusion /
     resistance_high_open / support_repair / full_breakdown /
     high_divergence / inconclusive
  2. 置信度：high(valid>=20 且 coverage>=0.8) / medium(valid>=10 且 coverage>=0.6) / low
  3. 小样本不排名：valid_count<10 时 confidence=low
  4. HHI 计算：正确计算 Herfindahl-Hirschman Index
  5. Top3/Top5 贡献度：正确计算
  6. 比例含分子分母：payload 中所有比例都有 numerator/denominator/ratio

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_auction_aggregation_service.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.auction_aggregation_service import (
    AUCTION_AGGREGATION_ALGORITHM_VERSION,
    CONFIDENCE_HIGH_MIN_COVERAGE,
    CONFIDENCE_HIGH_MIN_VALID,
    CONFIDENCE_MEDIUM_MIN_COVERAGE,
    CONFIDENCE_MEDIUM_MIN_VALID,
    LABEL_FULL_BREAKDOWN_DUAL_BREAKDOWN,
    LABEL_FULL_BREAKDOWN_LOW_OPEN,
    LABEL_FULL_REPRICING_DUAL_BREAKOUT,
    LABEL_FULL_REPRICING_HIGH_OPEN,
    LABEL_HIGH_DIVERGENCE_DISPERSION,
    LABEL_HIGH_DIVERGENCE_MEDIAN_MAX,
    LABEL_INITIAL_DIFFUSION_COVERAGE,
    LABEL_INITIAL_DIFFUSION_POS_MIGRATION,
    LABEL_LEADER_DRIVEN_TOP3,
    LABEL_RESISTANCE_HIGH_OPEN,
    LABEL_RESISTANCE_SUPPLY_OB,
    LABEL_SUPPORT_DEMAND_OB,
    LABEL_SUPPORT_REPAIR_LOW_OPEN,
    _build_scope_result,
    _classify_confidence,
    _classify_status_label,
    _compute_scope_metrics,
    _is_abnormal_volume,
    _is_breakdown_structure,
    _is_breakout_structure,
    _is_cross_down_chip,
    _is_cross_up_chip,
    _is_dual_breakdown,
    _is_dual_breakout,
    _is_positive_migration,
    _is_resistance_zone,
    _is_support_zone,
    _is_valid_result,
    _percentile,
    _ratio,
    _ratio_entry,
    _round,
    _safe_float,
    _summarize_metrics,
)

_TRADE_DATE = "2026-07-30"


# =============================================================================
# 辅助构造函数
# =============================================================================


class MockResult:
    """简化 AuctionInstrumentResult mock，仅含被测字段。"""

    def __init__(
        self,
        *,
        instrument_id: uuid.UUID | None = None,
        change_pct: float | None = 0.0,
        amount: float | None = None,
        structure_position: str | None = None,
        chip_position: str | None = None,
        participation_level: str | None = None,
        is_suspended: bool = False,
        relative_volume_median_20d: float | None = None,
    ) -> None:
        # `amount` 参数名与 service docstring 中 _MockResult 示例保持一致；
        # 存储为 `auction_amount` 以匹配 AuctionInstrumentResult.auction_amount 字段
        # （聚合服务通过 r.auction_amount 访问）。
        self.instrument_id = instrument_id or uuid.uuid4()
        self.change_pct = change_pct
        self.auction_amount = amount
        self.structure_position = structure_position
        self.chip_position = chip_position
        self.participation_level = participation_level
        self.is_suspended = is_suspended
        self.relative_volume_median_20d = relative_volume_median_20d


def _make_payload(
    *,
    high_open: float = 0.0,
    low_open: float = 0.0,
    dual_breakout: float = 0.0,
    dual_breakdown: float = 0.0,
    supply_ob: float = 0.0,
    demand_ob: float = 0.0,
    positive_migration: float = 0.0,
) -> dict[str, Any]:
    """构造 _classify_status_label 所需的 payload。"""
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


# =============================================================================
# 测试：常量校验
# =============================================================================


class TestConstants:
    """常量校验。"""

    def test_algorithm_version(self) -> None:
        assert AUCTION_AGGREGATION_ALGORITHM_VERSION == "v1.0.0"

    def test_confidence_thresholds(self) -> None:
        assert CONFIDENCE_HIGH_MIN_VALID == 20
        assert CONFIDENCE_HIGH_MIN_COVERAGE == 0.8
        assert CONFIDENCE_MEDIUM_MIN_VALID == 10
        assert CONFIDENCE_MEDIUM_MIN_COVERAGE == 0.6

    def test_label_thresholds(self) -> None:
        assert LABEL_FULL_REPRICING_HIGH_OPEN == 0.60
        assert LABEL_FULL_REPRICING_DUAL_BREAKOUT == 0.20
        assert LABEL_FULL_BREAKDOWN_LOW_OPEN == 0.60
        assert LABEL_FULL_BREAKDOWN_DUAL_BREAKDOWN == 0.20
        assert LABEL_LEADER_DRIVEN_TOP3 == 0.50
        assert LABEL_INITIAL_DIFFUSION_POS_MIGRATION == 0.40
        assert LABEL_INITIAL_DIFFUSION_COVERAGE == 0.50
        assert LABEL_RESISTANCE_HIGH_OPEN == 0.40
        assert LABEL_RESISTANCE_SUPPLY_OB == 0.20
        assert LABEL_SUPPORT_REPAIR_LOW_OPEN == 0.40
        assert LABEL_SUPPORT_DEMAND_OB == 0.20
        assert LABEL_HIGH_DIVERGENCE_DISPERSION == 2.0
        assert LABEL_HIGH_DIVERGENCE_MEDIAN_MAX == 1.0


# =============================================================================
# 测试：辅助纯函数
# =============================================================================


class TestSafeFloat:
    """_safe_float 安全转换。"""

    def test_none(self) -> None:
        assert _safe_float(None) is None

    def test_invalid(self) -> None:
        assert _safe_float("abc") is None

    def test_valid(self) -> None:
        assert _safe_float("3.14") == 3.14


class TestPercentile:
    """_percentile 分位数计算。"""

    def test_empty(self) -> None:
        assert _percentile([], 50) is None

    def test_single_value(self) -> None:
        assert _percentile([1.0], 50) == 1.0

    def test_median_odd(self) -> None:
        assert _percentile([1.0, 2.0, 3.0], 50) == 2.0

    def test_median_even(self) -> None:
        assert _percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.5

    def test_quartile(self) -> None:
        assert _percentile([1.0, 2.0, 3.0, 4.0], 25) == 1.75


class TestRatio:
    """_ratio / _ratio_entry 安全比例计算。"""

    def test_normal_ratio(self) -> None:
        assert _ratio(1, 2) == 0.5

    def test_zero_denominator(self) -> None:
        assert _ratio(5, 0) == 0.0

    def test_zero_numerator(self) -> None:
        assert _ratio(0, 0) == 0.0

    def test_ratio_entry_structure(self) -> None:
        """_ratio_entry 含 numerator/denominator/ratio。"""
        entry = _ratio_entry(3, 10)
        assert entry["numerator"] == 3
        assert entry["denominator"] == 10
        assert entry["ratio"] == 0.3

    def test_ratio_entry_zero_denominator(self) -> None:
        entry = _ratio_entry(5, 0)
        assert entry["numerator"] == 5
        assert entry["denominator"] == 0
        assert entry["ratio"] == 0.0


class TestRound:
    """_round None 安全四舍五入。"""

    def test_none(self) -> None:
        assert _round(None) is None

    def test_normal(self) -> None:
        assert _round(1.23456789) == 1.234568

    def test_custom_digits(self) -> None:
        assert _round(1.2345, 2) == 1.23


# =============================================================================
# 测试：result 判定函数
# =============================================================================


class TestResultPredicates:
    """_is_* 系列判定函数。"""

    def test_is_valid_result(self) -> None:
        """非停牌 + 有 change_pct → valid。"""
        assert _is_valid_result(MockResult(change_pct=1.0, is_suspended=False)) is True

    def test_is_valid_result_suspended(self) -> None:
        assert _is_valid_result(MockResult(change_pct=1.0, is_suspended=True)) is False

    def test_is_valid_result_no_change(self) -> None:
        assert _is_valid_result(MockResult(change_pct=None, is_suspended=False)) is False

    def test_is_breakout_structure(self) -> None:
        assert _is_breakout_structure(MockResult(structure_position="above_trigger"))
        assert _is_breakout_structure(MockResult(structure_position="above_high"))
        assert not _is_breakout_structure(MockResult(structure_position="normal"))

    def test_is_breakdown_structure(self) -> None:
        assert _is_breakdown_structure(MockResult(structure_position="below_trigger"))
        assert _is_breakdown_structure(MockResult(structure_position="below_low"))
        assert not _is_breakdown_structure(MockResult(structure_position="normal"))

    def test_is_cross_up_chip(self) -> None:
        assert _is_cross_up_chip(MockResult(chip_position="above_upper"))
        assert not _is_cross_up_chip(MockResult(chip_position="between"))

    def test_is_cross_down_chip(self) -> None:
        assert _is_cross_down_chip(MockResult(chip_position="below_lower"))
        assert not _is_cross_down_chip(MockResult(chip_position="between"))

    def test_is_dual_breakout(self) -> None:
        r = MockResult(structure_position="above_high", chip_position="above_upper")
        assert _is_dual_breakout(r) is True

    def test_is_dual_breakout_partial(self) -> None:
        r = MockResult(structure_position="above_high", chip_position="between")
        assert _is_dual_breakout(r) is False

    def test_is_dual_breakdown(self) -> None:
        r = MockResult(structure_position="below_low", chip_position="below_lower")
        assert _is_dual_breakdown(r) is True

    def test_is_resistance_zone(self) -> None:
        assert _is_resistance_zone(MockResult(structure_position="supply_ob"))
        assert not _is_resistance_zone(MockResult(structure_position="demand_ob"))

    def test_is_support_zone(self) -> None:
        assert _is_support_zone(MockResult(structure_position="demand_ob"))
        assert not _is_support_zone(MockResult(structure_position="supply_ob"))

    def test_is_abnormal_volume(self) -> None:
        assert _is_abnormal_volume(MockResult(participation_level="abnormal_high"))
        assert not _is_abnormal_volume(MockResult(participation_level="high"))

    def test_is_positive_migration(self) -> None:
        """正迁移：结构突破或筹码上穿（去重计数）。"""
        assert _is_positive_migration(
            MockResult(structure_position="above_trigger")
        )
        assert _is_positive_migration(
            MockResult(chip_position="above_upper")
        )
        assert _is_positive_migration(
            MockResult(structure_position="above_high", chip_position="above_upper")
        )
        assert not _is_positive_migration(MockResult(structure_position="normal"))


# =============================================================================
# 测试：状态标签分类
# =============================================================================


class TestClassifyStatusLabel:
    """_classify_status_label 状态标签分类。"""

    def test_full_repricing(self) -> None:
        """全面重新定价：高开>60% + 双重突破>20%。"""
        label = _classify_status_label(
            payload=_make_payload(high_open=0.7, dual_breakout=0.3),
            median_change=2.0, leader_change=5.0, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label == "full_repricing"

    def test_full_repricing_boundary(self) -> None:
        """边界值：高开=60%（不含）→ 非 full_repricing。"""
        label = _classify_status_label(
            payload=_make_payload(high_open=0.6, dual_breakout=0.3),
            median_change=2.0, leader_change=5.0, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label != "full_repricing"

    def test_full_breakdown(self) -> None:
        """全面崩溃：低开>60% + 双重破位>20%。"""
        label = _classify_status_label(
            payload=_make_payload(low_open=0.7, dual_breakdown=0.3),
            median_change=-2.0, leader_change=1.0, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label == "full_breakdown"

    def test_leader_driven(self) -> None:
        """龙头驱动：Top3贡献>50% + 龙头正向 + 中位数 < 龙头/2。"""
        label = _classify_status_label(
            payload=_make_payload(),
            median_change=1.0, leader_change=5.0, top3_contribution=0.6,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label == "leader_driven"

    def test_leader_driven_median_too_high(self) -> None:
        """龙头驱动但中位数不低于龙头/2 → 非 leader_driven。"""
        label = _classify_status_label(
            payload=_make_payload(),
            median_change=3.0, leader_change=5.0, top3_contribution=0.6,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label != "leader_driven"

    def test_initial_diffusion(self) -> None:
        """初始扩散：正迁移>40% + 覆盖率>50%。"""
        label = _classify_status_label(
            payload=_make_payload(positive_migration=0.5),
            median_change=0.5, leader_change=1.0, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.6,
        )
        assert label == "initial_diffusion"

    def test_resistance_high_open(self) -> None:
        """阻力位高开：高开>40% + 供应OB>20%。"""
        label = _classify_status_label(
            payload=_make_payload(high_open=0.5, supply_ob=0.3),
            median_change=0.5, leader_change=1.0, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.6,
        )
        assert label == "resistance_high_open"

    def test_support_repair(self) -> None:
        """支撑修复：低开>40% + 需求OB>20%。"""
        label = _classify_status_label(
            payload=_make_payload(low_open=0.5, demand_ob=0.3),
            median_change=-0.5, leader_change=0.5, top3_contribution=0.3,
            dispersion=1.0, coverage_ratio=0.6,
        )
        assert label == "support_repair"

    def test_high_divergence(self) -> None:
        """高离散：离散度>2.0 + |中位数|<1.0。"""
        label = _classify_status_label(
            payload=_make_payload(),
            median_change=0.1, leader_change=0.5, top3_contribution=0.3,
            dispersion=3.0, coverage_ratio=0.6,
        )
        assert label == "high_divergence"

    def test_high_divergence_median_too_high(self) -> None:
        """离散度高但中位数变化大 → 非 high_divergence。"""
        label = _classify_status_label(
            payload=_make_payload(),
            median_change=1.5, leader_change=0.5, top3_contribution=0.3,
            dispersion=3.0, coverage_ratio=0.6,
        )
        assert label != "high_divergence"

    def test_inconclusive(self) -> None:
        """其他情况 → inconclusive。"""
        label = _classify_status_label(
            payload=_make_payload(),
            median_change=0.1, leader_change=0.2, top3_contribution=0.3,
            dispersion=0.5, coverage_ratio=0.6,
        )
        assert label == "inconclusive"

    def test_full_repricing_takes_priority_over_leader_driven(self) -> None:
        """full_repricing 优先级高于 leader_driven。"""
        label = _classify_status_label(
            payload=_make_payload(high_open=0.7, dual_breakout=0.3),
            median_change=1.0, leader_change=5.0, top3_contribution=0.6,
            dispersion=1.0, coverage_ratio=0.9,
        )
        assert label == "full_repricing"


# =============================================================================
# 测试：置信度分类
# =============================================================================


class TestClassifyConfidence:
    """_classify_confidence 置信度分类。"""

    def test_high(self) -> None:
        """valid>=20 且 coverage>=0.8 → high。"""
        assert _classify_confidence(valid_count=25, coverage_ratio=0.9) == "high"

    def test_high_boundary(self) -> None:
        """边界值：valid=20 且 coverage=0.8 → high。"""
        assert _classify_confidence(valid_count=20, coverage_ratio=0.8) == "high"

    def test_medium(self) -> None:
        """valid>=10 且 coverage>=0.6 → medium。"""
        assert _classify_confidence(valid_count=15, coverage_ratio=0.7) == "medium"

    def test_medium_boundary(self) -> None:
        """边界值：valid=10 且 coverage=0.6 → medium。"""
        assert _classify_confidence(valid_count=10, coverage_ratio=0.6) == "medium"

    def test_low_small_sample(self) -> None:
        """小样本（valid<10）→ low，不排名。"""
        assert _classify_confidence(valid_count=5, coverage_ratio=0.5) == "low"

    def test_low_low_coverage(self) -> None:
        """覆盖率不足 → low。"""
        assert _classify_confidence(valid_count=25, coverage_ratio=0.5) == "low"

    def test_low_insufficient_valid(self) -> None:
        """valid 不足 → low。"""
        assert _classify_confidence(valid_count=8, coverage_ratio=0.9) == "low"

    def test_concept_core_coverage_downgrade_high_to_medium(self) -> None:
        """概念核心覆盖率不足 → high 降级为 medium。"""
        result = _classify_confidence(
            valid_count=25, coverage_ratio=0.9, core_coverage=0.2,
        )
        assert result == "medium"

    def test_concept_core_coverage_downgrade_medium_to_low(self) -> None:
        """概念核心覆盖率不足 → medium 降级为 low。"""
        result = _classify_confidence(
            valid_count=15, coverage_ratio=0.7, core_coverage=0.1,
        )
        assert result == "low"

    def test_concept_core_coverage_sufficient(self) -> None:
        """概念核心覆盖率充足 → 不降级。"""
        result = _classify_confidence(
            valid_count=25, coverage_ratio=0.9, core_coverage=0.5,
        )
        assert result == "high"

    def test_non_concept_ignores_core_coverage(self) -> None:
        """非概念（core_coverage=None）→ 不应用核心覆盖率约束。"""
        result = _classify_confidence(
            valid_count=25, coverage_ratio=0.9, core_coverage=None,
        )
        assert result == "high"


# =============================================================================
# 测试：_compute_scope_metrics
# =============================================================================


class TestComputeScopeMetrics:
    """_compute_scope_metrics 聚合指标计算。"""

    def test_empty_results(self) -> None:
        """空样本 → valid_count=0, coverage=0, label=inconclusive, confidence=low。"""
        metrics = _compute_scope_metrics([], total_count=0)
        assert metrics["valid_count"] == 0
        assert metrics["coverage_ratio"] == 0.0
        assert metrics["median_change_pct"] is None
        assert metrics["status_label"] == "inconclusive"
        assert metrics["confidence_level"] == "low"

    def test_basic_metrics(self) -> None:
        """基本聚合指标计算。"""
        iids = [uuid.uuid4() for _ in range(5)]
        results = [
            MockResult(
                instrument_id=iids[0], change_pct=5.0, amount=1000,
                structure_position="above_trigger", chip_position="above_upper",
                participation_level="abnormal_high", relative_volume_median_20d=3.0,
            ),
            MockResult(
                instrument_id=iids[1], change_pct=3.0, amount=800,
                structure_position="above_high", relative_volume_median_20d=2.0,
            ),
            MockResult(
                instrument_id=iids[2], change_pct=-1.0, amount=500,
                structure_position="below_trigger", chip_position="below_lower",
                relative_volume_median_20d=1.0,
            ),
            MockResult(
                instrument_id=iids[3], change_pct=0.5, amount=300,
                structure_position="supply_ob", relative_volume_median_20d=0.5,
            ),
            MockResult(
                instrument_id=iids[4], change_pct=-0.5, amount=200,
                structure_position="demand_ob", relative_volume_median_20d=0.3,
            ),
        ]
        metrics = _compute_scope_metrics(results, total_count=5)

        assert metrics["total_count"] == 5
        assert metrics["valid_count"] == 5
        assert metrics["coverage_ratio"] == 1.0
        # change_pcts = [5, 3, -1, 0.5, -0.5] → high: 3, low: 2
        assert metrics["open_high_count"] == 3
        assert metrics["open_low_count"] == 2
        # 结构/筹码迁移
        assert metrics["structure_breakout_count"] == 2  # above_trigger, above_high
        assert metrics["structure_breakdown_count"] == 1  # below_trigger
        assert metrics["chip_cross_up_count"] == 1  # above_upper
        assert metrics["chip_cross_down_count"] == 1  # below_lower
        assert metrics["dual_breakout_count"] == 1
        assert metrics["dual_breakdown_count"] == 1
        assert metrics["resistance_zone_count"] == 1  # supply_ob
        assert metrics["support_zone_count"] == 1  # demand_ob
        # 小样本 valid=5 < 10 → confidence=low
        assert metrics["confidence_level"] == "low"

    def test_deduplication(self) -> None:
        """全市场统计去重（按 instrument_id，保留第一条）。"""
        iid = uuid.uuid4()
        results = [
            MockResult(instrument_id=iid, change_pct=5.0, amount=1000),
            MockResult(instrument_id=iid, change_pct=3.0, amount=800),  # 重复
        ]
        metrics = _compute_scope_metrics(results, total_count=1)
        assert metrics["valid_count"] == 1

    def test_suspended_excluded(self) -> None:
        """停牌的 result 不参与聚合。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=5.0, amount=1000),
            MockResult(
                instrument_id=uuid.uuid4(), change_pct=None,
                amount=0, is_suspended=True,
            ),
        ]
        metrics = _compute_scope_metrics(results, total_count=2)
        assert metrics["valid_count"] == 1


# =============================================================================
# 测试：HHI 计算
# =============================================================================


class TestHHICalculation:
    """HHI（Herfindahl-Hirschman Index）正确计算。"""

    def test_hhi_uniform_distribution(self) -> None:
        """均匀分布：4 个相同额度的 result → HHI = 4 * (0.25)^2 = 0.25。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=250),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=250),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=250),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=250),
        ]
        metrics = _compute_scope_metrics(results, total_count=4)
        # HHI = 4 * (250/1000)^2 = 4 * 0.0625 = 0.25
        assert metrics["hhi"] is not None
        assert abs(metrics["hhi"] - 0.25) < 1e-6

    def test_hhi_monopoly(self) -> None:
        """垄断：1 个 result 占全部额度 → HHI = 1.0。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=1000),
        ]
        metrics = _compute_scope_metrics(results, total_count=1)
        assert metrics["hhi"] == 1.0

    def test_hhi_concentrated(self) -> None:
        """集中：1 个占 80%，4 个各占 5% → HHI = 0.64 + 4*0.0025 = 0.65。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=800),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=50),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=50),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=50),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=50),
        ]
        metrics = _compute_scope_metrics(results, total_count=5)
        expected_hhi = 0.8**2 + 4 * 0.05**2  # 0.64 + 0.01 = 0.65
        assert abs(metrics["hhi"] - expected_hhi) < 1e-6

    def test_hhi_no_amounts(self) -> None:
        """无额度数据 → HHI=None。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=None),
        ]
        metrics = _compute_scope_metrics(results, total_count=1)
        assert metrics["hhi"] is None


# =============================================================================
# 测试：Top3/Top5 贡献度
# =============================================================================


class TestTopContribution:
    """Top3/Top5 贡献度正确计算。"""

    def test_top3_contribution(self) -> None:
        """Top3 = 前3大额度 / 总额度。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=1000),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=800),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=500),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=300),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=200),
        ]
        metrics = _compute_scope_metrics(results, total_count=5)
        total = 1000 + 800 + 500 + 300 + 200  # 2800
        top3 = (1000 + 800 + 500) / total
        assert abs(metrics["top3_contribution"] - top3) < 1e-6

    def test_top5_contribution(self) -> None:
        """Top5 = 前5大额度 / 总额度。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=200),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=300),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=400),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=500),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=600),
        ]
        metrics = _compute_scope_metrics(results, total_count=6)
        total = 100 + 200 + 300 + 400 + 500 + 600  # 2100
        top5 = (600 + 500 + 400 + 300 + 200) / total
        assert abs(metrics["top5_contribution"] - top5) < 1e-6

    def test_top3_fewer_than_three(self) -> None:
        """少于 3 个 result → Top3 = 全部额度 / 总额度 = 1.0。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100),
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=200),
        ]
        metrics = _compute_scope_metrics(results, total_count=2)
        assert metrics["top3_contribution"] == 1.0

    def test_top_contribution_no_amounts(self) -> None:
        """无额度 → Top3/Top5=None。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=None),
        ]
        metrics = _compute_scope_metrics(results, total_count=1)
        assert metrics["top3_contribution"] is None
        assert metrics["top5_contribution"] is None


# =============================================================================
# 测试：小样本不排名
# =============================================================================


class TestSmallSampleConfidence:
    """小样本（valid_count<10）confidence=low。"""

    def test_small_sample_low_confidence(self) -> None:
        """valid_count=5 → low（即使覆盖率高）。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100)
            for _ in range(5)
        ]
        metrics = _compute_scope_metrics(results, total_count=5)
        assert metrics["valid_count"] == 5
        assert metrics["confidence_level"] == "low"

    def test_medium_sample(self) -> None:
        """valid_count=10 → medium（coverage>=0.6）。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100)
            for _ in range(10)
        ]
        metrics = _compute_scope_metrics(results, total_count=10)
        assert metrics["valid_count"] == 10
        assert metrics["confidence_level"] == "medium"

    def test_large_sample(self) -> None:
        """valid_count=20 → high（coverage>=0.8）。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100)
            for _ in range(20)
        ]
        metrics = _compute_scope_metrics(results, total_count=20)
        assert metrics["valid_count"] == 20
        assert metrics["confidence_level"] == "high"


# =============================================================================
# 测试：payload 中所有比例含分子分母
# =============================================================================


class TestPayloadRatioEntries:
    """payload 中所有比例都有 numerator/denominator/ratio。"""

    _RATIO_PATHS = [
        ("open_distribution", "high_open"),
        ("open_distribution", "flat_open"),
        ("open_distribution", "low_open"),
        ("structure_migration", "breakout"),
        ("structure_migration", "breakdown"),
        ("chip_migration", "cross_up"),
        ("chip_migration", "cross_down"),
        ("dual_events", "dual_breakout"),
        ("dual_events", "dual_breakdown"),
        ("zone_distribution", "resistance_zone"),
        ("zone_distribution", "support_zone"),
        ("positive_migration",),
        ("participation", "abnormal_volume"),
    ]

    def test_all_ratio_entries_have_numerator_denominator_ratio(self) -> None:
        """所有比例条目都含 numerator/denominator/ratio 三个键。"""
        iids = [uuid.uuid4() for _ in range(3)]
        results = [
            MockResult(
                instrument_id=iids[0], change_pct=5.0, amount=1000,
                structure_position="above_trigger", chip_position="above_upper",
            ),
            MockResult(
                instrument_id=iids[1], change_pct=-3.0, amount=500,
                structure_position="below_trigger", chip_position="below_lower",
            ),
            MockResult(
                instrument_id=iids[2], change_pct=0.0, amount=300,
                structure_position="normal",
            ),
        ]
        metrics = _compute_scope_metrics(results, total_count=3)
        payload = metrics["payload"]

        for path in self._RATIO_PATHS:
            entry = payload
            for key in path:
                assert key in entry, f"路径 {path} 缺少键 {key}"
                entry = entry[key]
            assert "numerator" in entry, f"路径 {path} 缺少 numerator"
            assert "denominator" in entry, f"路径 {path} 缺少 denominator"
            assert "ratio" in entry, f"路径 {path} 缺少 ratio"
            # 分母应等于 valid_count
            assert entry["denominator"] == 3, f"路径 {path} 分母 != valid_count"
            # ratio = numerator / denominator
            expected_ratio = round(entry["numerator"] / entry["denominator"], 6)
            assert entry["ratio"] == expected_ratio

    def test_empty_results_ratio_entries(self) -> None:
        """空样本时比例条目分母为 0，ratio=0.0。"""
        metrics = _compute_scope_metrics([], total_count=0)
        payload = metrics["payload"]
        high_open = payload["open_distribution"]["high_open"]
        assert high_open["numerator"] == 0
        assert high_open["denominator"] == 0
        assert high_open["ratio"] == 0.0


# =============================================================================
# 测试：_build_scope_result 和 _summarize_metrics
# =============================================================================


class TestBuildScopeResult:
    """_build_scope_result 构造 AuctionScopeResult。"""

    def test_build_scope_result_market(self) -> None:
        """market scope 构造正确。"""
        metrics = _compute_scope_metrics(
            [MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100)],
            total_count=1,
        )
        scope = _build_scope_result(
            scan_run_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,  # type: ignore[arg-type]
            scope_type="market",
            scope_id=None,
            scope_name=None,
            metrics=metrics,
        )
        assert scope.scope_type == "market"
        assert scope.scope_id is None
        assert scope.scope_name is None
        assert scope.total_count == 1
        assert scope.valid_count == 1
        assert scope.payload == metrics["payload"]

    def test_build_scope_result_industry(self) -> None:
        """industry scope 构造正确。"""
        metrics = _compute_scope_metrics([], total_count=0)
        scope = _build_scope_result(
            scan_run_id=uuid.uuid4(),
            trade_date=_TRADE_DATE,  # type: ignore[arg-type]
            scope_type="industry",
            scope_id=uuid.uuid4(),
            scope_name="半导体",
            metrics=metrics,
        )
        assert scope.scope_type == "industry"
        assert scope.scope_name == "半导体"
        assert scope.reason_codes == []


class TestSummarizeMetrics:
    """_summarize_metrics 压缩摘要。"""

    def test_summary_contains_all_fields(self) -> None:
        """摘要包含所有必要字段。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=1.0, amount=100),
        ]
        metrics = _compute_scope_metrics(results, total_count=1)
        summary = _summarize_metrics(metrics)
        expected_keys = {
            "total_count", "valid_count", "coverage_ratio",
            "status_label", "confidence_level",
            "median_change_pct", "equal_weight_change_pct",
            "amount_weight_change_pct",
            "open_high_count", "open_low_count",
            "dual_breakout_count", "dual_breakdown_count",
            "top3_contribution", "hhi", "leader_median_gap", "dispersion",
            "core_count", "peripheral_count", "core_coverage",
        }
        assert set(summary.keys()) == expected_keys

    def test_summary_core_peripheral(self) -> None:
        """摘要中 core/peripheral 区分正确。"""
        results = [
            MockResult(instrument_id=uuid.uuid4(), change_pct=5.0, amount=100),
            MockResult(instrument_id=uuid.uuid4(), change_pct=0.1, amount=100),
            MockResult(instrument_id=uuid.uuid4(), change_pct=0.1, amount=100),
        ]
        metrics = _compute_scope_metrics(results, total_count=3)
        summary = _summarize_metrics(metrics)
        # change_pcts = [5, 0.1, 0.1], median = 0.1
        # core_count: v > 0.1 → only 5.0 → core_count=1
        assert summary["core_count"] + summary["peripheral_count"] == 3
