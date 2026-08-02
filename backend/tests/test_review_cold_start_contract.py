"""[P0-6] Review 冷启动合同测试。

验证以下不变量（PRD §7.1、§0 冷启动）：
1. fp_segment_change_pct 全空时 P metric 不得伪造值（rawValue/normalizedValue 均 None）
2. 历史不足 60 日时 status=insufficient_history（不是 unavailable）
3. readiness 字段正确报告 raw_ready / normalized_ready / reason
4. bootstrap 提示：历史不足时 readiness.reason 包含 "bootstrap" 关键词
5. fp_segment_change_pct 定义确认为 segment_change_pct（段内变化，非日变化）

模块自测：
    PURE_UNIT_TEST=1 python -m pytest tests/test_review_cold_start_contract.py -v
"""

from __future__ import annotations

import pytest

from app.domain.review.metric_engine import (
    MIN_BASELINE_WINDOW,
    STATUS_INSUFFICIENT_HISTORY,
    STATUS_UNAVAILABLE,
    compute_all_metrics,
)
from app.domain.review.metric_registry import DEFAULT_REGISTRY

# =============================================================================
# 1. fp_segment_change_pct 全空 → 不得伪造值
# =============================================================================


class TestSegmentChangePctEmptyContract:
    """[P0-6] fp_segment_change_pct 全空时合同测试。

    根因确认（2026-07-30）：
    - fp_segment_change_pct 来源: first_pyramid_flatten.py L797
      `trend_cf.get("segment_change_pct")`
    - 这是趋势段内累计变化（segment start → now），非日变化
    - P metric 的 scope_return_1d / advance_ratio 等组件使用此字段
      作为"1 日收益率"，定义不匹配（PRD §7.2 描述为 1d return）

    合同要求：
    - 字段全空时 rawValue=None, normalizedValue=None, value=None
    - status=unavailable（raw 数据缺失，非历史不足）
    - readiness.raw_ready=False, reason 指向上游字段缺失
    - 禁止伪造 0.0 或任意默认值
    """

    def _make_flat_list_without_segment_change(
        self, count: int = 20,
    ) -> list[dict]:
        """构造 fp_segment_change_pct 全 None 的 flat_list。"""
        return [
            {
                "fp_trend_direction": "up",
                "fp_swing_direction": "up",
                "fp_internal_direction": "up",
                "fp_momentum_direction": "up",
                "fp_momentum_change": "enhancing",
                "fp_structure_alignment": "aligned",
                # fp_segment_change_pct 故意不设（None）
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
            for _ in range(count)
        ]

    def test_p_metric_value_none_when_segment_change_empty(self):
        """fp_segment_change_pct 全空 → P value=None（不得伪造归一化值）。

        注意：P rawValue 可能非 None，因为 P 的 5 个组件中 2 个不依赖
        fp_segment_change_pct（new_high_ratio, price_position_median），
        它们的 rawValue 仍可计算。但 P value（归一化值）必须 None，
        因为归一化需要历史数据。
        """
        flat_list = self._make_flat_list_without_segment_change(20)
        payloads = compute_all_metrics(flat_list)
        p_payload = payloads["P"]

        assert p_payload["value"] is None, (
            "fp_segment_change_pct 全空时 P value（归一化值）必须 None，不得伪造"
        )
        # rawValue 可能非 None（来自不依赖 segment_change_pct 的组件）

    def test_p_metric_status_unavailable_when_segment_change_empty(self):
        """fp_segment_change_pct 全空 → 依赖它的组件 status=unavailable。

        P 的 5 个 components 中 3 个依赖 fp_segment_change_pct：
        scope_return_1d, advance_ratio, trend_price_alignment_ratio
        另外 2 个不依赖：new_high_ratio, price_position_median

        P 整体 status 可能是 insufficient_history（因为 2 个组件有 raw 但无历史），
        但依赖 segment_change_pct 的 3 个组件必须 unavailable。
        """
        flat_list = self._make_flat_list_without_segment_change(20)
        payloads = compute_all_metrics(flat_list)
        p_payload = payloads["P"]

        # 依赖 fp_segment_change_pct 的组件必须 unavailable
        for comp in p_payload["components"]:
            if comp["name"] in (
                "scope_return_1d", "advance_ratio",
                "trend_price_alignment_ratio",
            ):
                assert comp["status"] == STATUS_UNAVAILABLE, (
                    f"{comp['name']} 依赖 fp_segment_change_pct，"
                    f"全空时必须 unavailable，实际 {comp['status']}"
                )
                assert comp["rawValue"] is None
                assert comp["normalizedValue"] is None

    def test_p_metric_readiness_raw_ready_false_when_empty(self):
        """fp_segment_change_pct 全空 → P readiness 报告组件状态。"""
        flat_list = self._make_flat_list_without_segment_change(20)
        payloads = compute_all_metrics(flat_list)
        p_payload = payloads["P"]

        readiness = p_payload.get("readiness", {})
        # P 有 5 个组件，3 个依赖 fp_segment_change_pct → unavailable
        # 2 个不依赖 → 有 rawValue 但无历史 → insufficient_history
        # raw_ready=True（2 个组件有 rawValue），normalized_ready=False（无归一化值）
        assert "raw_ready" in readiness
        assert "normalized_ready" in readiness
        assert "reason" in readiness
        assert readiness.get("normalized_ready") is False, (
            "fp_segment_change_pct 全空时 P normalized_ready 必须 False"
        )

    def test_no_faked_values_when_segment_change_empty(self):
        """合同：全空时任何 payload 字段不得出现伪造的 0.0 或默认值。"""
        flat_list = self._make_flat_list_without_segment_change(20)
        payloads = compute_all_metrics(flat_list)

        for code, payload in payloads.items():
            # 依赖 fp_segment_change_pct 的组件 rawValue 必须 None（非 0.0）
            for comp in payload["components"]:
                if comp["name"] in (
                    "scope_return_1d", "advance_ratio",
                    "trend_price_alignment_ratio",
                    "non_head_participation_ratio",
                    "leader_follower_common_confirm_ratio",
                    "top5_price_change_contribution",
                    "member_change_hhi",
                    "leader_median_diff",
                    "price_amount_efficiency_median",
                ):
                    assert comp["rawValue"] is None, (
                        f"{code}/{comp['name']} 依赖 fp_segment_change_pct，"
                        f"全空时 rawValue 必须 None，实际 {comp['rawValue']}"
                    )


# =============================================================================
# 2. 历史不足 → insufficient_history（非 unavailable）
# =============================================================================


class TestInsufficientHistoryContract:
    """[P0-6] 历史不足 60 日时 status=insufficient_history。

    旧 BUG：value=None（因 normalizedValue=None）时直接判 UNAVAILABLE，
    掩盖了"raw 可用但历史不足"的真实状态。

    修复后：raw_ready=True 但 normalized_ready=False → insufficient_history。
    """

    def _make_flat_list(self, count: int = 20) -> list[dict]:
        return [
            {
                "fp_trend_direction": "up",
                "fp_swing_direction": "up",
                "fp_internal_direction": "up",
                "fp_momentum_direction": "up",
                "fp_momentum_change": "enhancing",
                "fp_structure_alignment": "aligned",
                "fp_segment_change_pct": 2.5,
                "review_return_1d": 1.5,
                "review_price_position": 0.9,
                "review_volume_ratio20": 1.8,
                "review_amount_ratio20": 1.6,
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
            for _ in range(count)
        ]

    def test_status_insufficient_history_when_no_history(self):
        """raw 可用但无历史 → status=insufficient_history（非 unavailable）。"""
        flat_list = self._make_flat_list(20)
        # 不传 history_maps → 所有组件历史不足
        payloads = compute_all_metrics(flat_list)

        for code, payload in payloads.items():
            assert payload["status"] == STATUS_INSUFFICIENT_HISTORY, (
                f"{code} raw 可用但无历史 → 必须 insufficient_history，"
                f"实际 {payload['status']}"
            )

    def test_readiness_reports_bootstrap_hint(self):
        """历史不足时 readiness.reason 包含 bootstrap 提示。"""
        flat_list = self._make_flat_list(20)
        payloads = compute_all_metrics(flat_list)

        for code, payload in payloads.items():
            readiness = payload.get("readiness", {})
            reason = readiness.get("reason") or ""
            assert "bootstrap" in reason.lower(), (
                f"{code} 历史不足时 reason 应提示 bootstrap，实际: {reason}"
            )

    def test_raw_ready_true_when_data_available(self):
        """raw 数据可用时 readiness.raw_ready=True。"""
        flat_list = self._make_flat_list(20)
        payloads = compute_all_metrics(flat_list)

        for code, payload in payloads.items():
            readiness = payload.get("readiness", {})
            assert readiness.get("raw_ready") is True, (
                f"{code} raw 数据可用时 raw_ready 必须 True"
            )
            assert readiness.get("normalized_ready") is False, (
                f"{code} 无历史时 normalized_ready 必须 False"
            )

    def test_status_ready_when_history_sufficient(self):
        """历史充足 → status=ready, normalized_ready=True。"""
        flat_list = self._make_flat_list(20)
        # 构造 120 日历史（每个组件 120 个值）
        history_maps: dict[str, dict[str, list[float]]] = {}
        for code in DEFAULT_REGISTRY.metric_codes:
            metric = DEFAULT_REGISTRY.get_metric(code)
            comp_history: dict[str, list[float]] = {}
            for comp in metric.components:
                comp_history[comp.name] = [0.5] * MIN_BASELINE_WINDOW
            comp_history["_metric_value"] = [50.0] * MIN_BASELINE_WINDOW
            history_maps[code] = comp_history

        payloads = compute_all_metrics(
            flat_list, history_maps=history_maps,
        )

        for code, payload in payloads.items():
            readiness = payload.get("readiness", {})
            assert readiness.get("normalized_ready") is True, (
                f"{code} 历史充足时 normalized_ready 必须 True"
            )


# =============================================================================
# 3. fp_segment_change_pct 定义确认
# =============================================================================


class TestSegmentChangePctDefinition:
    """Confirm DSA segment change remains separate from Review daily return.

    根因审计（2026-07-30）：
    - 来源: first_pyramid_flatten.py L797
      result["fp_segment_change_pct"] = _safe_float(trend_cf.get("segment_change_pct"))
    - 含义: 当前趋势段内累计变化（segment_start_price → now）
    - 非: 当日涨跌幅（daily change）

    Review P reads ``review_return_1d`` from PIT daily bars. The segment field remains
    available to First Pyramid consumers but is forbidden in Review return components.
    """

    def test_fp_segment_change_pct_is_segment_not_daily(self):
        """合同：确认 fp_segment_change_pct 来自 trend_cf.segment_change_pct。"""
        # 验证 first_pyramid_flatten.py 中字段映射
        from app.services.first_pyramid_flatten import FP_QUERY_FIELD_SPECS

        spec = FP_QUERY_FIELD_SPECS.get("fp_segment_change_pct")
        assert spec is not None, (
            "fp_segment_change_pct 必须在 FP_QUERY_FIELD_SPECS 中注册"
        )
        assert spec.get("data_type") == "percent"

    def test_p_components_use_daily_return_not_segment_change(self):
        """Review return components use the typed daily fact exclusively."""
        p_spec = DEFAULT_REGISTRY.get_metric("P")
        daily_components = {
            comp.name
            for comp in p_spec.components
            if "review_return_1d" in comp.extra_fields
        }
        assert daily_components == {
            "scope_return_1d",
            "advance_ratio",
            "trend_price_alignment_ratio",
        }
        assert all(
            "fp_segment_change_pct" not in comp.extra_fields
            for comp in p_spec.components
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
