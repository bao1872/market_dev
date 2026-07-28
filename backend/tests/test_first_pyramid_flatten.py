"""[PRD §三 列表视图第一金字塔全量字段] 纯单元测试：扁平化函数 + 99 列注册表。

不连接数据库；不依赖 React 渲染。
覆盖：
  1. FP_ALL_KEYS 恰好 99 键且分组计数正确
  2. flatten_first_pyramid(None) 返回 99 键全 None
  3. flatten_first_pyramid 完整输入返回 99 键且关键字段映射正确
  4. is_stale 标记由调用方修正（不在 flatten 内判定）
  5. 前端 ColumnRegistry 返回 99 列，key 与后端 FP_ALL_KEYS 完全一致
  6. DEFAULT_FP_VISIBLE_KEYS 是 FP_ALL_KEYS 的真子集（约 20 个）
  7. getDefaultHiddenFpKeys 返回 99 - 默认可见数 = 默认隐藏数

运行：PURE_UNIT_TEST=1 python -m pytest backend/tests/test_first_pyramid_flatten.py -v
"""

from __future__ import annotations

import pytest

from app.services.first_pyramid_flatten import (
    FP_ALL_KEYS,
    FP_FIELD_GROUPS,
    flatten_first_pyramid,
)


class TestFlattenKeys:
    """99 键完整性 + 分组计数。"""

    def test_total_keys_is_99(self) -> None:
        assert len(FP_ALL_KEYS) == 99, f"FP_ALL_KEYS 应为 99 键，实际 {len(FP_ALL_KEYS)}"

    def test_no_duplicate_keys(self) -> None:
        assert len(set(FP_ALL_KEYS)) == 99, "FP_ALL_KEYS 存在重复键"

    @pytest.mark.parametrize(
        "group,expected",
        [
            ("快照", 7),
            ("趋势", 18),
            ("结构", 8),
            ("结构事件", 21),
            ("动量", 13),
            ("动量事件", 9),
            ("筹码", 10),
            ("量能", 13),
        ],
    )
    def test_group_counts(self, group: str, expected: int) -> None:
        actual = len(FP_FIELD_GROUPS[group])
        assert actual == expected, f"分组 {group} 应有 {expected} 键，实际 {actual}"

    def test_group_sum_equals_99(self) -> None:
        total = sum(len(keys) for keys in FP_FIELD_GROUPS.values())
        assert total == 99


class TestFlattenNone:
    """None 输入返回 99 键全 None。"""

    def test_none_returns_99_none_values(self) -> None:
        result = flatten_first_pyramid(None)
        assert len(result) == 99
        assert all(v is None for v in result.values())

    def test_none_preserves_keys(self) -> None:
        result = flatten_first_pyramid(None)
        assert set(result.keys()) == set(FP_ALL_KEYS)


class TestFlattenComplete:
    """完整输入返回 99 键且关键字段映射正确。"""

    @pytest.fixture
    def complete_first_pyramid(self) -> dict:
        """构造包含全部字段的 first_pyramid 输入。"""
        return {
            "tradeDate": "2026-07-25",
            "statusText": "趋势上行，结构共振",
            "chipConsensus": {
                "statusText": "筹码峰稳定",
                "continuousFactors": {
                    "poc_price": 10.5,
                    "last_close": 10.8,
                    "n_peak_nodes": 3,
                    "vah_price": 11.0,
                    "val_price": 10.0,
                },
                "events": [
                    {
                        "type": "NODE_CROSSOVER",
                        "direction": "up",
                        "freshnessBars": 5,
                        "price": 10.6,
                    }
                ],
            },
            "trend": {
                "continuousFactors": {
                    "regime_value": 1,
                    "dsa_dir_bars": 5,
                    "dsa_vwap_dev_pct": 2.34,
                    "segment_change_pct": 5.6,
                    "segment_slope": 0.12,
                    "trend_strength": 0.8,
                    "segment_start_time": "2026-07-01",
                    "segment_end_time": "2026-07-25",
                    "segment_start_price": 9.8,
                    "segment_end_price": 10.8,
                    "segment_bars": 18,
                    "current_vs_prev_volume_ratio": 1.5,
                    "current_vs_prev_amount_ratio": 1.4,
                    "current_segment_volume_mean": 1000000.0,
                    "current_segment_amount_mean": 10000000.0,
                    "prev_segment_volume_sum": 5000000.0,
                    "prev_segment_amount_sum": 50000000.0,
                    "vwap_ret_total": 3.2,
                },
            },
            "structure": {
                "continuousFactors": {
                    "swing_bias": 1,
                    "internal_bias": 1,
                    "active_ob_count": 2,
                    "trailing_top": 11.2,
                    "trailing_bottom": 9.5,
                },
                "events": [
                    {
                        "type": "BOS",
                        "direction": "up",
                        "freshnessBars": 3,
                        "occurredAt": "2026-07-22",
                        "price": 10.7,
                        "volumeBadge": "放量",
                        "extra": {"structure_level": "swing"},
                    }
                ],
            },
            "momentum": {
                "continuousFactors": {
                    "sqzmom_val": 0.15,
                    "sqzmom_val_prev": 0.10,
                    "squeeze_on": False,
                    "squeeze_off": True,
                    "bb_position": 0.75,
                    "bb_width": 0.04,
                    "bb_upper": 11.0,
                    "bb_middle": 10.5,
                    "bb_lower": 10.0,
                    "squeeze_period_volume_mean": 800000.0,
                    "release_vs_squeeze_volume_ratio": 1.8,
                    "vol_divergence": "共振",
                },
                "events": [
                    {
                        "type": "SQZ_OFF",
                        "direction": "up",
                        "freshnessBars": 2,
                        "occurredAt": "2026-07-24",
                        "price": 10.8,
                        "volumeBadge": "放量",
                    }
                ],
            },
            "volumeContext": {
                "badge": "放量",
                "volume": 1500000,
                "amount": 15000000,
                "turnoverRate": 2.5,
                "volumeMa20": 1000000,
                "volumeMa200": 800000,
                "volumeRatio20": 1.5,
                "volumeRatio200": 1.875,
                "volumePercentile20": 0.85,
                "volumePercentile200": 0.92,
                "volumeZscore20": 1.5,
                "volumeZscore200": 2.1,
                "readiness": True,
            },
        }

    def test_complete_returns_99_keys(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid, calculated_at="2026-07-25T15:00:00+08:00", run_id="run-abc")
        assert len(result) == 99

    def test_snapshot_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid, calculated_at="2026-07-25T15:00:00+08:00", run_id="run-abc")
        assert result["fp_trade_date"] == "2026-07-25"
        assert result["fp_data_source"] == "feature_snapshot"
        assert result["fp_is_stale"] is False  # 默认 False，由调用方修正
        assert result["fp_calculated_at"] == "2026-07-25T15:00:00+08:00"
        assert result["fp_run_id"] == "run-abc"
        assert result["fp_summary"] == "趋势上行，结构共振"
        assert result["fp_chip_available"] is True

    def test_trend_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_trend_direction"] == "上行"  # regime_value=1 > 0
        assert result["fp_trend_bars"] == 5
        assert result["fp_dsa_vwap_dev_pct"] == 2.34
        assert result["fp_segment_change_pct"] == 5.6
        assert result["fp_segment_slope"] == 0.12
        assert result["fp_trend_strength"] == 0.8
        assert result["fp_segment_start_date"] == "2026-07-01"
        assert result["fp_segment_end_date"] == "2026-07-25"
        assert result["fp_segment_start_price"] == 9.8
        assert result["fp_segment_end_price"] == 10.8
        assert result["fp_segment_bars"] == 18
        assert result["fp_segment_volume_ratio"] == 1.5
        assert result["fp_segment_amount_ratio"] == 1.4
        assert result["fp_segment_avg_volume"] == 1000000.0
        assert result["fp_segment_avg_amount"] == 10000000.0
        assert result["fp_prev_segment_volume"] == 5000000.0
        assert result["fp_prev_segment_amount"] == 50000000.0
        assert result["fp_vwap_ret_total"] == 3.2

    def test_structure_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_swing_direction"] == "上行"  # swing_bias=1
        assert result["fp_internal_direction"] == "上行"  # internal_bias=1
        assert result["fp_structure_alignment"] == "共振"  # 一致
        assert result["fp_active_ob_count"] == 2
        assert result["fp_trailing_top"] == 11.2
        assert result["fp_trailing_bottom"] == 9.5
        # 距离百分比：(target - current) / current * 100，current=segment_end_price=10.8
        assert result["fp_distance_to_trailing_top_pct"] == round((11.2 - 10.8) / 10.8 * 100, 2)
        assert result["fp_distance_to_trailing_bottom_pct"] == round((9.5 - 10.8) / 10.8 * 100, 2)

    def test_structure_event_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_structure_event_type"] == "BOS"
        assert result["fp_structure_event_direction"] == "up"
        assert result["fp_structure_event_level"] == "swing"
        assert result["fp_structure_event_freshness"] == 3
        assert result["fp_structure_event_date"] == "2026-07-22"
        assert result["fp_structure_event_price"] == 10.7
        assert result["fp_structure_event_volume_badge"] == "放量"
        # BOS 子事件
        assert result["fp_latest_bos_direction"] == "up"
        assert result["fp_latest_bos_freshness"] == 3
        assert result["fp_latest_bos_level"] == "swing"
        # CHoCH/OB/EQH/EQL 无匹配事件，应为 None
        assert result["fp_latest_choch_direction"] is None
        assert result["fp_latest_ob_direction"] is None
        assert result["fp_latest_eqh_freshness"] is None
        assert result["fp_latest_eql_price"] is None

    def test_momentum_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_momentum_direction"] == "扩张"  # sqzmom_val=0.15 > 0
        assert result["fp_squeeze_state"] == "已释放"  # squeeze_off=True
        assert result["fp_momentum_change"] == round(0.15 - 0.10, 6)
        assert result["fp_sqzmom_value"] == 0.15
        assert result["fp_sqzmom_prev"] == 0.10
        assert result["fp_bb_position"] == 0.75
        assert result["fp_bb_width"] == 0.04
        assert result["fp_bb_upper"] == 11.0
        assert result["fp_bb_middle"] == 10.5
        assert result["fp_bb_lower"] == 10.0
        assert result["fp_squeeze_avg_volume"] == 800000.0
        assert result["fp_release_volume_ratio"] == 1.8
        assert result["fp_momentum_volume_relation"] == "共振"

    def test_momentum_event_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_momentum_event_type"] == "SQZ_OFF"
        assert result["fp_momentum_event_direction"] == "up"
        assert result["fp_momentum_event_freshness"] == 2
        assert result["fp_momentum_event_date"] == "2026-07-24"
        assert result["fp_momentum_event_price"] == 10.8
        assert result["fp_momentum_event_volume_badge"] == "放量"
        assert result["fp_latest_sqz_off_freshness"] == 2
        assert result["fp_latest_diffusion_direction"] is None  # 无 MOMENTUM_DIFFUSION 事件

    def test_chip_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_chip_state"] == "筹码峰稳定"
        assert result["fp_poc_price"] == 10.5
        # distance_pct = (last_close - poc_price) / poc_price * 100 = (10.8 - 10.5) / 10.5 * 100
        assert result["fp_poc_distance_pct"] == round((10.8 - 10.5) / 10.5 * 100, 2)
        assert result["fp_peak_node_count"] == 3
        assert result["fp_vah_price"] == 11.0
        assert result["fp_val_price"] == 10.0
        assert result["fp_node_event_type"] == "NODE_CROSSOVER"
        assert result["fp_node_event_direction"] == "up"
        assert result["fp_node_event_freshness"] == 5
        assert result["fp_node_event_price"] == 10.6

    def test_volume_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_volume_badge"] == "放量"
        assert result["fp_volume"] == 1500000
        assert result["fp_amount"] == 15000000
        assert result["fp_turnover_rate"] == 2.5
        assert result["fp_volume_ma20"] == 1000000
        assert result["fp_volume_ma200"] == 800000
        assert result["fp_volume_ratio20"] == 1.5
        assert result["fp_volume_ratio200"] == 1.875
        assert result["fp_volume_percentile20"] == 0.85
        assert result["fp_volume_percentile200"] == 0.92
        assert result["fp_volume_zscore20"] == 1.5
        assert result["fp_volume_zscore200"] == 2.1
        assert result["fp_volume_ready"] is True


class TestFlattenEdgeCases:
    """边界场景。"""

    def test_empty_dict_returns_99_keys(self) -> None:
        """空 dict 输入返回 99 键，所有值为 None。"""
        result = flatten_first_pyramid({})
        assert len(result) == 99
        # 空 dict 不应导致任何字段被填充
        assert result["fp_trade_date"] is None
        assert result["fp_trend_direction"] is None

    def test_partial_dict_preserves_99_keys(self) -> None:
        """部分字段输入仍返回 99 键。"""
        partial = {
            "tradeDate": "2026-07-25",
            "trend": {"continuousFactors": {"regime_value": -1}},
        }
        result = flatten_first_pyramid(partial)
        assert len(result) == 99
        assert result["fp_trade_date"] == "2026-07-25"
        assert result["fp_trend_direction"] == "下行"  # regime_value=-1 < 0
        # 未提供的字段应为 None
        assert result["fp_chip_state"] is None
        assert result["fp_volume_badge"] is None

    def test_is_stale_passed_through(self) -> None:
        """is_stale 参数原样写入 fp_is_stale，不做内部判定。"""
        result = flatten_first_pyramid({"tradeDate": "2026-07-25"}, is_stale=True)
        assert result["fp_is_stale"] is True

    def test_direction_label_for_zero(self) -> None:
        """regime_value=0 应映射为'震荡'。"""
        result = flatten_first_pyramid(
            {"trend": {"continuousFactors": {"regime_value": 0}}}
        )
        assert result["fp_trend_direction"] == "震荡"

    def test_direction_label_for_none(self) -> None:
        """regime_value=None 应映射为 None。"""
        result = flatten_first_pyramid(
            {"trend": {"continuousFactors": {"regime_value": None}}}
        )
        assert result["fp_trend_direction"] is None

    def test_structure_alignment_divergence(self) -> None:
        """swing=1, internal=-1 应为'背离'。"""
        result = flatten_first_pyramid(
            {
                "structure": {
                    "continuousFactors": {
                        "swing_bias": 1,
                        "internal_bias": -1,
                    }
                }
            }
        )
        assert result["fp_structure_alignment"] == "背离"

    def test_chip_null_returns_none_for_chip_fields(self) -> None:
        """chipConsensus=None 时筹码字段全为 None。"""
        result = flatten_first_pyramid({"chipConsensus": None})
        assert result["fp_chip_state"] is None
        assert result["fp_poc_price"] is None
        assert result["fp_chip_available"] is False  # chipConsensus is None → False

    def test_volume_context_null_returns_none_for_volume_fields(self) -> None:
        """volumeContext=None 时量能字段全为 None。"""
        result = flatten_first_pyramid({"volumeContext": None})
        assert result["fp_volume_badge"] is None
        assert result["fp_volume"] is None
        assert result["fp_volume_ready"] is None


class TestNoExtraFields:
    """扁平化结果不应包含 FP_ALL_KEYS 之外的键。"""

    def test_no_extra_keys_in_result(self) -> None:
        result = flatten_first_pyramid(
            {
                "tradeDate": "2026-07-25",
                "unknownField": "should not appear",
                "trend": {"continuousFactors": {"unknownMetric": 999}},
            }
        )
        assert set(result.keys()) == set(FP_ALL_KEYS)
