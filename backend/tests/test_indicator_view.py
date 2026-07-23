"""共享指标视图枚举单元测试 - 验证 indicator_view 贯穿全链的常量与函数行为。

[CHANGE-20260720-003 §三+§四] 三类监控独立飞书图片：
- node_cluster: 筹码共识价（VolumeNodeMonitor）
- bollinger: 布林带（BollingerMonitor）
- smc: 结构（SmcMonitor）

测试覆盖：
1. 共享枚举常量值正确性（INDICATOR_VIEW_VALUES / DEFAULT_INDICATOR_VIEW / INDICATOR_VIEW_LABELS）
2. EVENT_TYPE_TO_INDICATOR_VIEW 映射表完整性（7 个事件 → 3 个视图）
3. get_indicator_view_for_event / resolve_indicator_view / is_valid_indicator_view 行为
4. FEISHU_CAPTURE_PRESETS 三套 Preset 完整性（layers 互斥 / ready_check 存在）
5. get_capture_preset 查询函数（已知/未知 indicator_view）
6. build_monitor_event_text 按 indicator_view 拆分文字卡片字段
7. SendFeishuRequest body schema 归一化校验（None/合法/非法值）
"""
from __future__ import annotations

import pytest

from app.constants.capture import (
    FEISHU_CAPTURE_PRESETS,
    FEISHU_CAPTURE_TIMEFRAME,
    get_capture_preset,
)
from app.constants.indicator_view import (
    DEFAULT_INDICATOR_VIEW,
    EVENT_TYPE_TO_INDICATOR_VIEW,
    INDICATOR_VIEW_LABELS,
    INDICATOR_VIEW_VALUES,
    get_indicator_view_for_event,
    is_valid_indicator_view,
    resolve_indicator_view,
)
from app.services.message_builder import build_monitor_event_text


class TestIndicatorViewConstants:
    """共享枚举常量值校验。"""

    def test_indicator_view_values_tuple(self) -> None:
        """INDICATOR_VIEW_VALUES 应为 3 个固定值的 tuple。"""
        assert INDICATOR_VIEW_VALUES == ("node_cluster", "bollinger", "smc")
        assert isinstance(INDICATOR_VIEW_VALUES, tuple)

    def test_default_indicator_view_is_node_cluster(self) -> None:
        """默认 indicator_view 为 node_cluster（避免 capture 链路 None 渗透）。"""
        assert DEFAULT_INDICATOR_VIEW == "node_cluster"
        assert DEFAULT_INDICATOR_VIEW in INDICATOR_VIEW_VALUES

    def test_indicator_view_labels_complete(self) -> None:
        """INDICATOR_VIEW_LABELS 应覆盖全部 3 个视图，文案非空。"""
        assert set(INDICATOR_VIEW_LABELS.keys()) == set(INDICATOR_VIEW_VALUES)
        assert INDICATOR_VIEW_LABELS["node_cluster"] == "筹码共识价"
        assert INDICATOR_VIEW_LABELS["bollinger"] == "布林带"
        assert INDICATOR_VIEW_LABELS["smc"] == "结构"
        for view, label in INDICATOR_VIEW_LABELS.items():
            assert isinstance(label, str) and label, f"视图 {view} 文案不应为空"


class TestEventTypeToIndicatorViewMapping:
    """事件类型 → indicator_view 映射表完整性。"""

    def test_mapping_covers_all_monitor_events(self) -> None:
        """映射表应覆盖 4 个监控事件（3 BB + 1 Node）。"""
        assert EVENT_TYPE_TO_INDICATOR_VIEW["bb_upper_touch"] == "bollinger"
        assert EVENT_TYPE_TO_INDICATOR_VIEW["bb_mid_touch"] == "bollinger"
        assert EVENT_TYPE_TO_INDICATOR_VIEW["bb_lower_touch"] == "bollinger"
        assert EVENT_TYPE_TO_INDICATOR_VIEW["node_cluster_touch"] == "node_cluster"

    def test_mapping_covers_all_smc_events(self) -> None:
        """映射表应覆盖 3 个 SMC 事件。"""
        assert EVENT_TYPE_TO_INDICATOR_VIEW["smc_bos_retest"] == "smc"
        assert EVENT_TYPE_TO_INDICATOR_VIEW["smc_choch_retest"] == "smc"
        assert EVENT_TYPE_TO_INDICATOR_VIEW["smc_order_block_first_touch"] == "smc"

    def test_mapping_values_only_valid_views(self) -> None:
        """映射表所有 value 必须在 INDICATOR_VIEW_VALUES 集合内。"""
        for event_type, view in EVENT_TYPE_TO_INDICATOR_VIEW.items():
            assert view in INDICATOR_VIEW_VALUES, (
                f"事件 {event_type} 映射到非法 indicator_view: {view!r}"
            )

    def test_mapping_has_nine_entries(self) -> None:
        """映射表应有 9 项（4 监控事件 + 5 SMC 事件：BOS/CHoCH/OB/EQH/EQL）。"""
        assert len(EVENT_TYPE_TO_INDICATOR_VIEW) == 9


class TestGetIndicatorViewForEvent:
    """get_indicator_view_for_event 行为校验。"""

    @pytest.mark.parametrize(
        "event_type,expected",
        [
            ("bb_upper_touch", "bollinger"),
            ("bb_mid_touch", "bollinger"),
            ("bb_lower_touch", "bollinger"),
            ("node_cluster_touch", "node_cluster"),
            ("smc_bos_retest", "smc"),
            ("smc_choch_retest", "smc"),
            ("smc_order_block_first_touch", "smc"),
            ("smc_equal_highs_retest", "smc"),
            ("smc_equal_lows_retest", "smc"),
        ],
    )
    def test_known_event_types(self, event_type: str, expected: str) -> None:
        """已知事件类型应映射到对应 indicator_view。"""
        assert get_indicator_view_for_event(event_type) == expected

    def test_unknown_event_falls_back_to_default(self) -> None:
        """未知事件类型应回退到 DEFAULT_INDICATOR_VIEW（避免 None 渗透）。"""
        assert get_indicator_view_for_event("unknown_event") == DEFAULT_INDICATOR_VIEW
        assert get_indicator_view_for_event("") == DEFAULT_INDICATOR_VIEW


class TestResolveIndicatorView:
    """resolve_indicator_view 行为校验（payload 优先 / 事件类型回退）。"""

    def test_payload_indicator_view_takes_priority(self) -> None:
        """payload.indicator_view 优先于事件类型映射。"""
        # bb_upper_touch 默认映射到 bollinger，但 payload 指定 smc 时应取 smc
        assert resolve_indicator_view(
            "bb_upper_touch", {"indicator_view": "smc"}
        ) == "smc"
        assert resolve_indicator_view(
            "smc_bos_retest", {"indicator_view": "node_cluster"}
        ) == "node_cluster"

    def test_payload_missing_falls_back_to_event_type(self) -> None:
        """payload 缺失 indicator_view 时回退到事件类型映射。"""
        assert resolve_indicator_view("bb_upper_touch", {}) == "bollinger"
        assert resolve_indicator_view("bb_upper_touch", {"price": 100.0}) == "bollinger"

    def test_payload_none_falls_back_to_event_type(self) -> None:
        """payload=None 时回退到事件类型映射。"""
        assert resolve_indicator_view("bb_upper_touch", None) == "bollinger"
        assert resolve_indicator_view("smc_bos_retest", None) == "smc"

    def test_payload_invalid_value_falls_back_to_event_type(self) -> None:
        """payload.indicator_view 非法值时回退到事件类型映射。"""
        assert resolve_indicator_view(
            "bb_upper_touch", {"indicator_view": "invalid"}
        ) == "bollinger"
        assert resolve_indicator_view(
            "bb_upper_touch", {"indicator_view": ""}
        ) == "bollinger"

    def test_payload_non_string_value_falls_back_to_event_type(self) -> None:
        """payload.indicator_view 非字符串时回退到事件类型映射。"""
        assert resolve_indicator_view(
            "bb_upper_touch", {"indicator_view": 123}
        ) == "bollinger"
        assert resolve_indicator_view(
            "bb_upper_touch", {"indicator_view": None}
        ) == "bollinger"

    def test_unknown_event_with_payload_indicator_view(self) -> None:
        """未知事件 + payload.indicator_view 合法时应取 payload 值。"""
        assert resolve_indicator_view(
            "unknown_event", {"indicator_view": "smc"}
        ) == "smc"

    def test_unknown_event_without_payload_falls_back_to_default(self) -> None:
        """未知事件 + 无 payload 时回退到 DEFAULT_INDICATOR_VIEW。"""
        assert resolve_indicator_view("unknown_event", None) == DEFAULT_INDICATOR_VIEW
        assert resolve_indicator_view("unknown_event", {}) == DEFAULT_INDICATOR_VIEW


class TestIsValidIndicatorView:
    """is_valid_indicator_view 行为校验。"""

    @pytest.mark.parametrize("view", ["node_cluster", "bollinger", "smc"])
    def test_valid_views(self, view: str) -> None:
        """合法 indicator_view 应返回 True。"""
        assert is_valid_indicator_view(view) is True

    @pytest.mark.parametrize(
        "view", ["", "invalid", "NODE_CLUSTER", "Bollinger", None, "smc ", " smc"]
    )
    def test_invalid_views(self, view: object) -> None:
        """非法 indicator_view（含 None/空串/大小写错误）应返回 False。"""
        assert is_valid_indicator_view(view) is False  # type: ignore[arg-type]


class TestCapturePresets:
    """FEISHU_CAPTURE_PRESETS 三套 Preset 完整性校验。"""

    def test_three_presets_exist(self) -> None:
        """应有 3 套 Preset：node_cluster / bollinger / smc。"""
        assert set(FEISHU_CAPTURE_PRESETS.keys()) == set(INDICATOR_VIEW_VALUES)
        assert len(FEISHU_CAPTURE_PRESETS) == 3

    @pytest.mark.parametrize("view", INDICATOR_VIEW_VALUES)
    def test_preset_required_fields(self, view: str) -> None:
        """每个 Preset 应含 indicator_view/timeframe/chart_version/layers/ready_check。"""
        preset = FEISHU_CAPTURE_PRESETS[view]
        assert preset["indicator_view"] == view
        assert preset["timeframe"] == FEISHU_CAPTURE_TIMEFRAME == "1d"
        assert preset["chart_version"] == "v1"
        assert isinstance(preset["layers"], list) and len(preset["layers"]) > 0
        assert "ready_check" in preset
        # 所有 preset 必须含 candlestick 基础图层
        assert "candlestick" in preset["layers"]

    def test_layers_mutually_exclusive_except_candlestick(self) -> None:
        """三视图 layers 互斥（除共享的 candlestick 外）。"""
        nc_layers = {
            layer for layer in FEISHU_CAPTURE_PRESETS["node_cluster"]["layers"]
            if layer != "candlestick"
        }
        bb_layers = {
            layer for layer in FEISHU_CAPTURE_PRESETS["bollinger"]["layers"]
            if layer != "candlestick"
        }
        smc_layers = {
            layer for layer in FEISHU_CAPTURE_PRESETS["smc"]["layers"]
            if layer != "candlestick"
        }
        assert nc_layers & bb_layers == set(), (
            f"node_cluster 与 bollinger layers 重叠: {nc_layers & bb_layers}"
        )
        assert nc_layers & smc_layers == set(), (
            f"node_cluster 与 smc layers 重叠: {nc_layers & smc_layers}"
        )
        assert bb_layers & smc_layers == set(), (
            f"bollinger 与 smc layers 重叠: {bb_layers & smc_layers}"
        )

    def test_node_cluster_preset_layers(self) -> None:
        """node_cluster Preset 应含 profile/poc/peak_node/trigger_node 图层。"""
        layers = FEISHU_CAPTURE_PRESETS["node_cluster"]["layers"]
        for required in ("candlestick", "volume", "profile", "poc", "peak_node", "trigger_node"):
            assert required in layers, f"node_cluster 缺少图层: {required}"

    def test_bollinger_preset_layers(self) -> None:
        """bollinger Preset 应含 bb_upper/bb_mid/bb_lower/trigger_band 图层。"""
        layers = FEISHU_CAPTURE_PRESETS["bollinger"]["layers"]
        for required in ("candlestick", "bb_upper", "bb_mid", "bb_lower", "trigger_band"):
            assert required in layers, f"bollinger 缺少图层: {required}"

    def test_smc_preset_layers(self) -> None:
        """smc Preset 应含 bos/choch/ob/eqh_eql/strong_weak/trigger_entity 图层。"""
        layers = FEISHU_CAPTURE_PRESETS["smc"]["layers"]
        for required in ("candlestick", "bos", "choch", "ob", "eqh_eql", "strong_weak", "trigger_entity"):
            assert required in layers, f"smc 缺少图层: {required}"

    @pytest.mark.parametrize("view", INDICATOR_VIEW_VALUES)
    def test_preset_ready_check_structure(self, view: str) -> None:
        """每个 Preset 的 ready_check 应含 field + condition。"""
        ready_check = FEISHU_CAPTURE_PRESETS[view]["ready_check"]
        assert "field" in ready_check
        assert "condition" in ready_check


class TestGetCapturePreset:
    """get_capture_preset 查询函数行为。"""

    @pytest.mark.parametrize("view", INDICATOR_VIEW_VALUES)
    def test_known_view_returns_preset(self, view: str) -> None:
        """已知 indicator_view 应返回对应 Preset。"""
        preset = get_capture_preset(view)
        assert preset["indicator_view"] == view

    def test_unknown_view_raises_value_error(self) -> None:
        """未知 indicator_view 应抛 ValueError。"""
        with pytest.raises(ValueError, match="未知 indicator_view"):
            get_capture_preset("invalid")
        with pytest.raises(ValueError):
            get_capture_preset("")


class TestBuildMonitorEventTextIndicatorViewSplit:
    """build_monitor_event_text 按 indicator_view 拆分文字卡片字段。"""

    _COMMON_KWARGS: dict[str, object] = {
        "stock_name": "测试股票",
        "symbol": "600000",
        "event_type": "bb_upper_touch",
        "event_time": "2026-07-20T10:30:00+08:00",
        "current_price": 25.50,
        "bb_upper": 27.00,
        "bb_mid": 25.00,
        "bb_lower": 23.00,
        "upper_node": 26.00,
        "lower_node": 24.00,
        "poc_price": 25.00,
        "position_0_1": 0.50,
        "resource_refs": {"instrument_id": "600000.SH"},
    }

    def test_none_includes_all_fields(self) -> None:
        """indicator_view=None 应包含全部字段（向后兼容）。"""
        dto = build_monitor_event_text(indicator_view=None, **self._COMMON_KWARGS)
        text = dto.text_content
        assert "近期波动上沿：27.00" in text
        assert "近期价格中枢：25.00" in text
        assert "近期波动下沿：23.00" in text
        assert "上方成交密集区：26.00" in text
        assert "下方成交密集区：24.00" in text
        assert "最密集成交价：25.00" in text
        assert "当前区间位置：0.50" in text

    def test_node_cluster_only_node_fields(self) -> None:
        """indicator_view='node_cluster' 只应展示节点字段，不应出现 BB 字段。"""
        dto = build_monitor_event_text(indicator_view="node_cluster", **self._COMMON_KWARGS)
        text = dto.text_content
        # 应包含节点字段
        assert "上方成交密集区：26.00" in text
        assert "下方成交密集区：24.00" in text
        assert "最密集成交价：25.00" in text
        assert "当前区间位置：0.50" in text
        # 不应包含 BB 字段值行（注意：event_label "价格触及近期波动上沿" 含 "近期波动上沿" 子串，
        # 必须按字段值行而非子串判断）
        assert "近期波动上沿：27.00" not in text
        assert "近期价格中枢：25.00" not in text
        assert "近期波动下沿：23.00" not in text
        # resource_refs 应携带 indicator_view
        assert dto.resource_refs.get("indicator_view") == "node_cluster"

    def test_bollinger_only_bb_fields(self) -> None:
        """indicator_view='bollinger' 只应展示 BB 字段，不应出现节点字段。"""
        dto = build_monitor_event_text(indicator_view="bollinger", **self._COMMON_KWARGS)
        text = dto.text_content
        # 应包含 BB 字段
        assert "近期波动上沿：27.00" in text
        assert "近期价格中枢：25.00" in text
        assert "近期波动下沿：23.00" in text
        # 不应包含节点字段
        assert "上方成交密集区" not in text
        assert "下方成交密集区" not in text
        assert "最密集成交价" not in text
        assert "当前区间位置" not in text
        # resource_refs 应携带 indicator_view
        assert dto.resource_refs.get("indicator_view") == "bollinger"

    def test_smc_only_smc_fields(self) -> None:
        """indicator_view='smc' 只应展示 SMC 字段，不应出现 BB/节点字段。"""
        dto = build_monitor_event_text(
            indicator_view="smc",
            smc_bos_level=24.50,
            smc_choch_level=25.20,
            smc_ob_high=26.00,
            smc_ob_low=25.50,
            smc_swing_bias=1,
            **self._COMMON_KWARGS,
        )
        text = dto.text_content
        # 应包含 SMC 字段
        assert "日线 SMC 破位结构位：24.50" in text
        assert "日线 SMC 趋势反转结构位：25.20" in text
        assert "日线 SMC 订单块区间：25.50 ~ 26.00" in text
        assert "日线 SMC 主趋势方向：上行" in text
        # 不应包含 BB 字段值行（注意：event_label "价格触及近期波动上沿" 含 "近期波动上沿" 子串，
        # 必须按字段值行而非子串判断）
        assert "近期波动上沿：27.00" not in text
        assert "近期价格中枢：25.00" not in text
        assert "近期波动下沿：23.00" not in text
        # 不应包含节点字段
        assert "上方成交密集区：26.00" not in text
        assert "下方成交密集区：24.00" not in text
        # resource_refs 应携带 indicator_view
        assert dto.resource_refs.get("indicator_view") == "smc"

    def test_smc_swing_bias_down(self) -> None:
        """smc_swing_bias=-1 应展示为'下行'。"""
        dto = build_monitor_event_text(
            indicator_view="smc",
            smc_swing_bias=-1,
            **self._COMMON_KWARGS,
        )
        assert "日线 SMC 主趋势方向：下行" in dto.text_content

    def test_smc_swing_bias_neutral(self) -> None:
        """smc_swing_bias=0 应展示为'震荡'。"""
        dto = build_monitor_event_text(
            indicator_view="smc",
            smc_swing_bias=0,
            **self._COMMON_KWARGS,
        )
        assert "日线 SMC 主趋势方向：震荡" in dto.text_content

    def test_smc_swing_bias_none_shows_dash(self) -> None:
        """smc_swing_bias=None 应展示为'-'。"""
        dto = build_monitor_event_text(
            indicator_view="smc",
            smc_swing_bias=None,
            **self._COMMON_KWARGS,
        )
        assert "日线 SMC 主趋势方向：-" in dto.text_content

    def test_none_indicator_view_not_in_resource_refs(self) -> None:
        """indicator_view=None 时 resource_refs 不应包含 indicator_view 键。"""
        dto = build_monitor_event_text(indicator_view=None, **self._COMMON_KWARGS)
        assert "indicator_view" not in dto.resource_refs

    def test_all_views_share_basic_header(self) -> None:
        """所有视图都应包含基础头部（标题/触发/时间/现价）。"""
        for view in INDICATOR_VIEW_VALUES:
            dto = build_monitor_event_text(indicator_view=view, **self._COMMON_KWARGS)
            text = dto.text_content
            assert "【自选监控触发】" in text, f"视图 {view} 缺少标题"
            assert "测试股票 600000" in text, f"视图 {view} 缺少股票信息"
            assert "触发：" in text, f"视图 {view} 缺少触发字段"
            assert "触发时间：10:30" in text, f"视图 {view} 缺少触发时间"
            assert "现价：25.50" in text, f"视图 {view} 缺少现价"


class TestSendFeishuRequestBodySchema:
    """SendFeishuRequest body schema 归一化校验（PROMPT.md §四）。"""

    def test_default_indicator_view_is_none(self) -> None:
        """SendFeishuRequest 默认 indicator_view 应为 None。"""
        from app.api.stock_detail_feishu import SendFeishuRequest

        req = SendFeishuRequest()
        assert req.indicator_view is None
        assert req.normalized_indicator_view() is None

    @pytest.mark.parametrize("view", INDICATOR_VIEW_VALUES)
    def test_valid_indicator_view_normalizes_to_self(self, view: str) -> None:
        """合法 indicator_view 应归一化为自身。"""
        from app.api.stock_detail_feishu import SendFeishuRequest

        req = SendFeishuRequest(indicator_view=view)
        assert req.normalized_indicator_view() == view

    @pytest.mark.parametrize(
        "invalid_view", ["", "invalid", "NODE_CLUSTER", "Bollinger", "smc ", " smc"]
    )
    def test_invalid_indicator_view_normalizes_to_none(self, invalid_view: str) -> None:
        """非法 indicator_view 应归一化为 None（向后兼容降级）。"""
        from app.api.stock_detail_feishu import SendFeishuRequest

        req = SendFeishuRequest(indicator_view=invalid_view)
        assert req.normalized_indicator_view() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
