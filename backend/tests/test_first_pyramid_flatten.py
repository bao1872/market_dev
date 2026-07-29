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
    FP_CHIP_KEYS,
    FP_FIELD_GROUPS,
    FP_QUERY_FIELD_SPECS,
    FP_SERVER_FILTERABLE_KEYS,
    FP_SERVER_SORTABLE_KEYS,
    FpFilterSpec,
    FpSortSpec,
    flatten_chip_fields,
    flatten_first_pyramid,
)
from app.services.first_pyramid_flatten import (
    parse_fp_filter as _parse_fp_filter,
)
from app.services.first_pyramid_flatten import (
    parse_fp_sort as _parse_fp_sort,
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
                    "regime_strength": 0.85,
                    "trend_strength": 0.8,
                    "segment_start_time": "2026-07-01",
                    "segment_end_time": "2026-07-25",
                    "segment_start_price": 9.8,
                    "segment_end_price": 10.8,
                    "segment_bars": 18,
                    "current_vs_prev_volume_mean_ratio": 1.5,
                    "current_vs_prev_amount_mean_ratio": 1.4,
                    "current_segment_volume_mean": 1000000.0,
                    "current_segment_amount_mean": 10000000.0,
                    "prev_segment_volume_mean": 4800000.0,
                    "prev_segment_amount_mean": 48000000.0,
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
        # [P0-4 修复 2026-07-29 二.4] fp_chip_available 改为 computed，flatten 不再设置
        # 由调用方（list/detail 服务）按严格五元组 chip 表存在性 + chip.available=true 计算
        assert result["fp_chip_available"] is None

    def test_trend_fields(self, complete_first_pyramid: dict) -> None:
        result = flatten_first_pyramid(complete_first_pyramid)
        assert result["fp_trend_direction"] == "上行"  # regime_value=1 > 0
        assert result["fp_trend_bars"] == 5
        assert result["fp_dsa_vwap_dev_pct"] == 2.34
        assert result["fp_segment_change_pct"] == 5.6
        assert result["fp_segment_slope"] == 0.12
        # [CHANGE-20260729-005 二.5] 优先 regime_strength（0.85），非 trend_strength（0.8）
        assert result["fp_trend_strength"] == 0.85
        assert result["fp_segment_start_date"] == "2026-07-01"
        assert result["fp_segment_end_date"] == "2026-07-25"
        assert result["fp_segment_start_price"] == 9.8
        assert result["fp_segment_end_price"] == 10.8
        assert result["fp_segment_bars"] == 18
        # [二.5] mean/mean ratio（非废弃 sum/sum）
        assert result["fp_segment_volume_ratio"] == 1.5
        assert result["fp_segment_amount_ratio"] == 1.4
        assert result["fp_segment_avg_volume"] == 1000000.0
        assert result["fp_segment_avg_amount"] == 10000000.0
        # prev_segment 使用 mean 字段（非废弃 sum 字段）
        assert result["fp_prev_segment_volume"] == 4800000.0
        assert result["fp_prev_segment_amount"] == 48000000.0
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
        # [P0-4 修复 2026-07-29 二.4] fp_chip_available 改为 computed，flatten 不再设置
        assert result["fp_chip_available"] is None

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


# =============================================================================
# [CHANGE-20260729-004 P0-1] FP_QUERY_FIELD_SPECS / fp_filter / fp_sort 单元测试
# 用少量代表字段测试注册表全量能力，不为 99 列各写一套重复测试
# =============================================================================


class TestFpQueryFieldSpecs:
    """FP_QUERY_FIELD_SPECS 规格表完整性。"""

    def test_specs_cover_all_99_keys(self) -> None:
        """规格表必须恰好覆盖 FP_ALL_KEYS 的全部 99 键。"""
        assert set(FP_QUERY_FIELD_SPECS.keys()) == set(FP_ALL_KEYS)
        assert len(FP_QUERY_FIELD_SPECS) == 99

    def test_each_spec_has_source_and_operators(self) -> None:
        """[CHANGE-20260729-005] 每个 spec 必须包含 fp_key / data_type / source / operators。"""
        for key, spec in FP_QUERY_FIELD_SPECS.items():
            assert spec["fp_key"] == key
            assert spec["data_type"] in {
                "text", "number", "percent", "datetime", "boolean", "enum",
            }
            # [P0 收口 2026-07-29] 新增 computed source 类型（is_stale/chip_available 动态计算）
            assert spec["source"] in {"flat", "chip", "column", "literal", "computed"}
            assert isinstance(spec["operators"], frozenset)
            assert len(spec["operators"]) > 0

    def test_all_99_keys_are_filterable_and_sortable(self) -> None:
        """[二.2/二.3] 全部 99 字段均支持服务端 filter/sort（含计算字段和事件字段）。"""
        assert len(FP_SERVER_FILTERABLE_KEYS) == 99
        assert len(FP_SERVER_SORTABLE_KEYS) == 99
        # 计算字段也可筛选（通过 flat 对象）
        assert "fp_structure_alignment" in FP_SERVER_FILTERABLE_KEYS
        assert "fp_distance_to_trailing_top_pct" in FP_SERVER_SORTABLE_KEYS
        # 列表事件字段也可筛选（通过 flat 对象）
        assert "fp_structure_event_type" in FP_SERVER_FILTERABLE_KEYS
        assert "fp_latest_bos_direction" in FP_SERVER_SORTABLE_KEYS

    def test_sortable_equals_filterable(self) -> None:
        assert FP_SERVER_SORTABLE_KEYS == FP_SERVER_FILTERABLE_KEYS

    def test_chip_fields_use_chip_source(self) -> None:
        """[二.4] 筹码字段 source=chip（从独立 chip 表读取）。"""
        from app.services.first_pyramid_flatten import FP_CHIP_KEYS
        assert len(FP_CHIP_KEYS) == 10
        for key in FP_CHIP_KEYS:
            assert FP_QUERY_FIELD_SPECS[key]["source"] == "chip"

    def test_column_source_fields(self) -> None:
        """fp_calculated_at/fp_run_id/fp_trade_date 使用真实列。"""
        assert FP_QUERY_FIELD_SPECS["fp_calculated_at"]["source"] == "column"
        assert FP_QUERY_FIELD_SPECS["fp_calculated_at"]["column"] == "created_at"
        assert FP_QUERY_FIELD_SPECS["fp_run_id"]["source"] == "column"
        assert FP_QUERY_FIELD_SPECS["fp_run_id"]["column"] == "source_run_id"
        # [P0-2 修复 2026-07-29 二.2] fp_trade_date 改用 snapshot.trade_date 真实列
        assert FP_QUERY_FIELD_SPECS["fp_trade_date"]["source"] == "column"
        assert FP_QUERY_FIELD_SPECS["fp_trade_date"]["column"] == "trade_date"

    def test_literal_source_fields(self) -> None:
        """fp_data_source 使用常量。"""
        assert FP_QUERY_FIELD_SPECS["fp_data_source"]["source"] == "literal"
        assert FP_QUERY_FIELD_SPECS["fp_data_source"]["literal_value"] == "feature_snapshot"

    def test_computed_source_fields(self) -> None:
        """[P0 收口 2026-07-29] fp_is_stale/fp_chip_available 使用 computed 表达式。"""
        assert FP_QUERY_FIELD_SPECS["fp_is_stale"]["source"] == "computed"
        assert FP_QUERY_FIELD_SPECS["fp_is_stale"]["computed_kind"] == "is_stale"
        assert FP_QUERY_FIELD_SPECS["fp_chip_available"]["source"] == "computed"
        assert FP_QUERY_FIELD_SPECS["fp_chip_available"]["computed_kind"] == "chip_available"

    def test_operator_mapping_by_data_type(self) -> None:
        """按 data_type 抽样校验操作符映射。"""
        num_spec = FP_QUERY_FIELD_SPECS["fp_trend_bars"]
        assert num_spec["data_type"] == "number"
        assert {"gt", "gte", "lt", "lte", "eq", "between", "empty", "not_empty"} <= num_spec["operators"]
        text_spec = FP_QUERY_FIELD_SPECS["fp_summary"]
        assert text_spec["data_type"] == "text"
        assert {"contains", "not_contains", "empty", "not_empty"} <= text_spec["operators"]
        dt_spec = FP_QUERY_FIELD_SPECS["fp_trade_date"]
        assert dt_spec["data_type"] == "datetime"
        assert {"gte", "gt", "lt", "lte", "between", "empty", "not_empty"} <= dt_spec["operators"]
        bool_spec = FP_QUERY_FIELD_SPECS["fp_volume_ready"]
        assert bool_spec["data_type"] == "boolean"
        assert bool_spec["operators"] == frozenset({"eq", "empty", "not_empty"})
        enum_spec = FP_QUERY_FIELD_SPECS["fp_trend_direction"]
        assert enum_spec["data_type"] == "enum"
        assert enum_spec["operators"] == frozenset({"eq", "empty", "not_empty"})


class TestParseFpFilter:
    """_parse_fp_filter 解析 + 白名单校验。"""

    def test_none_and_empty_return_empty(self) -> None:
        assert _parse_fp_filter(None) == []
        assert _parse_fp_filter("") == []

    def test_single_number_filter(self) -> None:
        specs = _parse_fp_filter("fp_trend_bars:gt:5")
        assert specs == [FpFilterSpec(fp_key="fp_trend_bars", operator="gt", value="5", value2=None)]

    def test_between_filter(self) -> None:
        specs = _parse_fp_filter("fp_trend_bars:between:1;10")
        assert specs == [
            FpFilterSpec(fp_key="fp_trend_bars", operator="between", value="1", value2="10")
        ]

    def test_text_contains_filter(self) -> None:
        specs = _parse_fp_filter("fp_summary:contains:上行")
        assert specs == [FpFilterSpec(fp_key="fp_summary", operator="contains", value="上行", value2=None)]

    def test_empty_operator(self) -> None:
        specs = _parse_fp_filter("fp_summary:empty:")
        assert specs == [FpFilterSpec(fp_key="fp_summary", operator="empty", value=None, value2=None)]

    def test_multiple_filters_semicolon(self) -> None:
        specs = _parse_fp_filter("fp_trend_bars:gt:5;fp_volume:lt:1000000")
        assert len(specs) == 2
        assert specs[0].fp_key == "fp_trend_bars"
        assert specs[1].fp_key == "fp_volume"

    def test_invalid_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Not in FP_QUERY_FIELD_SPECS"):
            _parse_fp_filter("fp_nonexistent:gt:5")

    def test_computed_field_now_filterable(self) -> None:
        """[CHANGE-20260729-005 二.3] 计算字段通过 flat 对象支持筛选，不再拒绝。"""
        specs = _parse_fp_filter("fp_structure_alignment:eq:共振")
        assert len(specs) == 1
        assert specs[0].fp_key == "fp_structure_alignment"
        assert specs[0].operator == "eq"
        assert specs[0].value == "共振"

    def test_event_field_now_filterable(self) -> None:
        """[二.3] 事件字段通过 flat 对象支持筛选。"""
        specs = _parse_fp_filter("fp_latest_bos_direction:eq:up")
        assert len(specs) == 1
        assert specs[0].fp_key == "fp_latest_bos_direction"

    def test_invalid_operator_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid operator"):
            _parse_fp_filter("fp_trend_bars:contains:5")

    def test_between_missing_second_value_raises(self) -> None:
        with pytest.raises(ValueError, match="missing second value"):
            _parse_fp_filter("fp_trend_bars:between:1")

    def test_value_required_for_non_empty_op(self) -> None:
        with pytest.raises(ValueError, match="requires value"):
            _parse_fp_filter("fp_trend_bars:gt:")


class TestParseFpSort:
    """_parse_fp_sort 解析 + 白名单校验。"""

    def test_none_and_empty_return_none(self) -> None:
        assert _parse_fp_sort(None) is None
        assert _parse_fp_sort("") is None

    def test_valid_sort(self) -> None:
        result = _parse_fp_sort("fp_trend_bars:desc")
        assert result == FpSortSpec(fp_key="fp_trend_bars", direction="desc")

    def test_invalid_key_raises(self) -> None:
        with pytest.raises(ValueError, match="Not in FP_QUERY_FIELD_SPECS"):
            _parse_fp_sort("fp_nonexistent:asc")

    def test_computed_field_now_sortable(self) -> None:
        """[CHANGE-20260729-005] 计算字段通过 flat 对象支持排序。"""
        result = _parse_fp_sort("fp_structure_alignment:asc")
        assert result is not None
        assert result.fp_key == "fp_structure_alignment"
        assert result.direction == "asc"

    def test_invalid_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="Invalid fp_sort direction"):
            _parse_fp_sort("fp_trend_bars:up")


# =============================================================================
# [CHANGE-20260729-005 二.4] flatten_chip_fields 单元测试
# 筹码字段独立存储：写入 chip_payload.chip_flat，查询从独立 chip 表读取
# =============================================================================


class TestFlattenChipFields:
    """flatten_chip_fields：将筹码维度扁平化为 10 个 chip fp_ 键。"""

    def test_none_returns_10_none_values(self) -> None:
        """None 输入返回 10 个 chip 键，值全为 None。"""
        result = flatten_chip_fields(None)
        assert len(result) == len(FP_CHIP_KEYS)
        assert set(result.keys()) == set(FP_CHIP_KEYS)
        assert all(v is None for v in result.values())

    @pytest.fixture
    def chip_dimension(self) -> dict:
        """构造筹码维度输入（DimensionResult.to_dict() 格式）。"""
        return {
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
        }

    def test_chip_fields_mapped(self, chip_dimension: dict) -> None:
        """完整筹码维度输入返回正确映射的 10 个 chip 键。"""
        result = flatten_chip_fields(chip_dimension)
        assert len(result) == len(FP_CHIP_KEYS)
        assert set(result.keys()) == set(FP_CHIP_KEYS)
        # 非筹码键不应出现
        assert "fp_trade_date" not in result
        assert "fp_trend_direction" not in result
        assert "fp_volume_badge" not in result
        # 筹码字段映射正确
        assert result["fp_chip_state"] == "筹码峰稳定"
        assert result["fp_poc_price"] == 10.5
        assert result["fp_peak_node_count"] == 3
        assert result["fp_vah_price"] == 11.0
        assert result["fp_val_price"] == 10.0
        assert result["fp_node_event_type"] == "NODE_CROSSOVER"
        assert result["fp_node_event_direction"] == "up"
        assert result["fp_node_event_freshness"] == 5
        assert result["fp_node_event_price"] == 10.6
        # poc_distance_pct = (last_close - poc_price) / poc_price * 100
        assert result["fp_poc_distance_pct"] == round((10.8 - 10.5) / 10.5 * 100, 2)

    def test_empty_dict_returns_10_none(self) -> None:
        """空 dict 输入返回 10 个 chip 键，值全为 None。"""
        result = flatten_chip_fields({})
        assert len(result) == len(FP_CHIP_KEYS)
        assert all(v is None for v in result.values())

    def test_partial_chip_preserves_10_keys(self, chip_dimension: dict) -> None:
        """部分字段输入仍返回 10 个 chip 键。"""
        del chip_dimension["events"]
        result = flatten_chip_fields(chip_dimension)
        assert len(result) == len(FP_CHIP_KEYS)
        # 事件字段为 None，但其他筹码字段有值
        assert result["fp_node_event_type"] is None
        assert result["fp_poc_price"] == 10.5

    def test_chip_keys_count_is_10(self) -> None:
        """[二.4] 筹码字段恰好 10 个（source=chip）。"""
        assert len(FP_CHIP_KEYS) == 10
        # 校验全部 chip 键都在筹码分组中
        chip_group_keys = set(FP_FIELD_GROUPS["筹码"])
        assert set(FP_CHIP_KEYS) == chip_group_keys
