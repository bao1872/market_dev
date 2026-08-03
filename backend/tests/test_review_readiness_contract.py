"""Review P/Q/U/C/V 就绪状态与历史门槛合同测试。

覆盖：
- 59 条历史：raw_ready=true, normalized_ready=false, status=insufficient_history
- 60 条历史：normalized_ready=true, status=ready
- 历史不足时 rawValue 保留（非 null）
- 缺单一指标只影响该指标，不令整只股票 P/Q/U/C/V 全空
- P 缺日收益（review_return_1d 全空）→ status=unavailable, raw_ready=false
- coverage 基于 ready_count 计算
- all-null 结果触发质量保护（readiness 明确标记）

运行：
    cd backend && PURE_UNIT_TEST=1 python -m pytest tests/test_review_readiness_contract.py -v
"""
from __future__ import annotations

import pytest

from app.domain.review.metric_engine import (
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_READY,
    STATUS_UNAVAILABLE,
    compute_all_metrics,
)

MIN_WINDOW = 60


def _member() -> dict:
    return {
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


def _flat_list(n: int = 20) -> list[dict]:
    return [_member() for _ in range(n)]


def _history_map_for(
    code: str, n_obs: int, component_names: list[str] | None = None
) -> dict[str, dict[str, list[float]]]:
    """构造 metric_code 的历史观测：每个 component 有 n_obs 条 raw_value。

    component 归一化用 history_map[component_name]；metric 级 value 用 _metric_value。
    """
    from app.domain.review.metric_registry import DEFAULT_REGISTRY

    if component_names is None:
        component_names = [c.name for c in DEFAULT_REGISTRY.get_metric(code).components]
    hist = {"_metric_value": [float(i) for i in range(n_obs)]}
    for name in component_names:
        hist[name] = [float(i) for i in range(n_obs)]
    return {code: hist}


class TestReadinessContract:
    def test_59_history_raw_ready_normalized_not(self):
        """59 条历史：raw_ready=true, normalized_ready=false, insufficient_history。"""
        payloads = compute_all_metrics(
            _flat_list(),
            history_maps=_history_map_for("Q", MIN_WINDOW - 1),
        )
        q = payloads["Q"]
        assert q["readiness"]["raw_ready"] is True
        assert q["readiness"]["normalized_ready"] is False
        assert q["status"] == STATUS_INSUFFICIENT_HISTORY
        # rawValue 保留（历史不足不丢弃 raw）
        assert q["rawValue"] is not None
        assert q["value"] is None

    def test_60_history_normalized_ready(self):
        """60 条历史：normalized_ready=true, status=ready（无 off-by-one）。"""
        payloads = compute_all_metrics(
            _flat_list(),
            history_maps=_history_map_for("Q", MIN_WINDOW),
        )
        q = payloads["Q"]
        assert q["readiness"]["normalized_ready"] is True
        assert q["status"] == STATUS_READY

    def test_59_60_boundary_exact(self):
        """59 条不足，60 条就绪的精确边界。"""
        p59 = compute_all_metrics(_flat_list(), history_maps=_history_map_for("U", 59))
        p60 = compute_all_metrics(_flat_list(), history_maps=_history_map_for("U", 60))
        assert p59["U"]["readiness"]["normalized_ready"] is False
        assert p59["U"]["status"] == STATUS_INSUFFICIENT_HISTORY
        assert p60["U"]["readiness"]["normalized_ready"] is True
        assert p60["U"]["status"] == STATUS_READY

    def test_raw_value_kept_when_insufficient(self):
        """历史不足时 rawValue 保留、normalized value 为 None（不得用 0 填充）。"""
        payloads = compute_all_metrics(_flat_list(), history_maps=_history_map_for("V", 30))
        v = payloads["V"]
        assert v["readiness"]["raw_ready"] is True
        assert v["status"] == STATUS_INSUFFICIENT_HISTORY
        assert v["rawValue"] is not None
        assert v["value"] is None

    def test_single_missing_metric_only_affects_itself(self):
        """缺单一来源只影响该指标，不令整只股票 P/Q/U/C/V 全空。"""
        # 去掉 fp_latest_choch 相关（只影响 Q 的 structure_breakdown_diffusion）
        flat_no_choch = [dict(m, fp_latest_choch_direction=None) for m in _flat_list()]
        payloads = compute_all_metrics(
            flat_no_choch, history_maps=_history_map_for("Q", MIN_WINDOW)
        )
        q = payloads["Q"]
        # Q 仍应有 ready/partial（其他 component 有值）
        assert q["status"] in (STATUS_READY, STATUS_INSUFFICIENT_HISTORY, "partial")
        # P（依赖 review_return_1d）不受影响
        p = compute_all_metrics(flat_no_choch, history_maps=_history_map_for("P", MIN_WINDOW))["P"]
        assert p["rawValue"] is not None

    def test_p_missing_daily_return_unavailable(self):
        """P 缺日收益（review_return_1d 全空）→ 语义不可用：value=None, raw_ready=false。"""
        flat_no_return = [dict(m, review_return_1d=None) for m in _flat_list()]
        payloads = compute_all_metrics(flat_no_return)
        p = payloads["P"]
        # P 语义不可用：readiness 强制 raw_ready=false（即使个别 component 有值）
        assert p["readiness"]["raw_ready"] is False
        assert p["readiness"]["normalized_ready"] is False
        # 不伪造归一化值
        assert p["value"] is None
        # status 为 unavailable（日收益缺失优先级最高）或 insufficient_history
        assert p["status"] in (STATUS_UNAVAILABLE, STATUS_INSUFFICIENT_HISTORY)

    def test_all_null_trigger_quality_guard(self):
        """all-null 结果（全部成员缺日收益）不被视为有效 ready：value 为空。"""
        flat_all_null = [dict(m, review_return_1d=None) for m in _flat_list()]
        payloads = compute_all_metrics(flat_all_null)
        for code in ("P",):
            p = payloads[code]
            # value（归一化）必须为空，绝不产生有效 ready 结论
            assert p["value"] is None
            assert p["readiness"]["raw_ready"] is False

    def test_coverage_based_on_ready_count(self):
        """coverage 基于 ready_count/成员数计算，不按结果行数伪装。"""
        payloads = compute_all_metrics(
            _flat_list(n=20), ready_count=18, history_maps=_history_map_for("Q", MIN_WINDOW)
        )
        q = payloads["Q"]
        assert q["coverage"] == pytest.approx(18 / 20)
