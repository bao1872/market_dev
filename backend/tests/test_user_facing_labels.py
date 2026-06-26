"""user_facing_labels 单元测试 - 验证 advice.md 第二节通俗化映射正确性。

测试覆盖：
1. EVENT_LABELS 事件类型映射与 advice.md 第二节映射表对齐
2. FIELD_LABELS 字段名映射与 advice.md 第二节映射表对齐
3. get_event_label / get_field_label 查询函数行为（已知/未知 key）
4. 边界：空字符串、None 安全（未知 key 原样返回）
5. 一致性：事件文案与字段文案不冲突（事件文案含"价格"动词前缀，字段文案为名词）
"""

from __future__ import annotations

import pytest

from app.constants.user_facing_labels import (
    EVENT_LABELS,
    FIELD_LABELS,
    get_event_label,
    get_field_label,
)


class TestEventLabels:
    """事件类型 → 通俗文案映射（advice.md 第二节映射表）。"""

    def test_bb_upper_touch_label(self) -> None:
        assert EVENT_LABELS["bb_upper_touch"] == "价格触及近期波动上沿"
        assert get_event_label("bb_upper_touch") == "价格触及近期波动上沿"

    def test_bb_mid_touch_label(self) -> None:
        assert EVENT_LABELS["bb_mid_touch"] == "价格回到近期价格中枢"
        assert get_event_label("bb_mid_touch") == "价格回到近期价格中枢"

    def test_bb_lower_touch_label(self) -> None:
        assert EVENT_LABELS["bb_lower_touch"] == "价格触及近期波动下沿"
        assert get_event_label("bb_lower_touch") == "价格触及近期波动下沿"

    def test_node_cluster_touch_label(self) -> None:
        assert EVENT_LABELS["node_cluster_touch"] == "价格触及成交密集区"
        assert get_event_label("node_cluster_touch") == "价格触及成交密集区"

    def test_stock_snapshot_share_label(self) -> None:
        """个股快照分享事件文案应为"个股快照"（非"个股快照分享"）。"""
        assert EVENT_LABELS["STOCK_SNAPSHOT_SHARE"] == "个股快照"
        assert get_event_label("STOCK_SNAPSHOT_SHARE") == "个股快照"

    def test_unknown_event_type_returned_as_is(self) -> None:
        """未知 event_type 应原样返回，不抛异常。"""
        assert get_event_label("unknown_event") == "unknown_event"
        assert get_event_label("") == ""

    def test_event_labels_count(self) -> None:
        """事件类型映射应覆盖 5 项（4 个监控事件 + 1 个快照分享）。"""
        assert len(EVENT_LABELS) == 5

    def test_event_labels_start_with_price_verb(self) -> None:
        """advice.md 第二节要求：监控事件文案以"价格"开头（动词前缀，便于用户理解）。"""
        for event_type in ("bb_upper_touch", "bb_mid_touch", "bb_lower_touch", "node_cluster_touch"):
            label = EVENT_LABELS[event_type]
            assert label.startswith("价格"), f"{event_type} 文案应以'价格'开头: {label}"


class TestFieldLabels:
    """字段名 → 通俗文案映射（advice.md 第二节映射表）。"""

    def test_bb_fields(self) -> None:
        assert FIELD_LABELS["bb_upper"] == "近期波动上沿"
        assert FIELD_LABELS["bb_mid"] == "近期价格中枢"
        assert FIELD_LABELS["bb_lower"] == "近期波动下沿"
        assert get_field_label("bb_upper") == "近期波动上沿"
        assert get_field_label("bb_mid") == "近期价格中枢"
        assert get_field_label("bb_lower") == "近期波动下沿"

    def test_node_fields(self) -> None:
        assert FIELD_LABELS["upper_node"] == "上方成交密集区"
        assert FIELD_LABELS["lower_node"] == "下方成交密集区"
        assert get_field_label("upper_node") == "上方成交密集区"
        assert get_field_label("lower_node") == "下方成交密集区"

    def test_poc_field(self) -> None:
        assert FIELD_LABELS["poc"] == "最密集成交价"
        assert get_field_label("poc") == "最密集成交价"

    def test_position_field(self) -> None:
        assert FIELD_LABELS["position"] == "当前区间位置"
        assert get_field_label("position") == "当前区间位置"

    def test_short_labels_for_overview(self) -> None:
        """概览行简称（保持单行紧凑可读）。"""
        assert FIELD_LABELS["bb_upper_short"] == "波动上沿"
        assert FIELD_LABELS["bb_mid_short"] == "价格中枢"
        assert FIELD_LABELS["bb_lower_short"] == "波动下沿"
        assert FIELD_LABELS["node_cluster_short"] == "密集区"

    def test_unknown_field_returned_as_is(self) -> None:
        """未知 field 应原样返回，不抛异常。"""
        assert get_field_label("unknown_field") == "unknown_field"
        assert get_field_label("") == ""


class TestConsistency:
    """事件文案与字段文案一致性校验。"""

    def test_event_label_differs_from_field_label(self) -> None:
        """事件文案含"价格"动词前缀，字段文案为纯名词，二者不应相同。"""
        # bb_upper_touch 事件文案 vs bb_upper 字段文案
        assert get_event_label("bb_upper_touch") != get_field_label("bb_upper")
        assert get_event_label("bb_mid_touch") != get_field_label("bb_mid")
        assert get_event_label("bb_lower_touch") != get_field_label("bb_lower")

    def test_no_legacy_professional_terms_in_event_labels(self) -> None:
        """advice.md 第二节要求：事件文案不应再出现旧专业术语。"""
        legacy_terms = ["BB上轨穿越", "BB中轨穿越", "BB下轨穿越", "节点集群穿越",
                        "布林上轨穿越", "布林中轨穿越", "布林下轨穿越"]
        for label in EVENT_LABELS.values():
            for term in legacy_terms:
                assert term not in label, f"事件文案不应包含旧术语 '{term}': {label}"

    def test_no_legacy_professional_terms_in_field_labels(self) -> None:
        """advice.md 第二节要求：字段文案不应再出现旧专业术语（BB/上节点/下节点/POC/位置）。"""
        # 排除"位置"——它在"当前区间位置"中是合法子串
        legacy_terms = ["BB上轨", "BB中轨", "BB下轨", "上节点", "下节点"]
        # POC 作为独立字段标签不应出现（应为"最密集成交价"）
        for field, label in FIELD_LABELS.items():
            for term in legacy_terms:
                assert term not in label, f"字段 {field} 文案不应包含旧术语 '{term}': {label}"
            # POC 不应作为独立标签出现（除非是 poc 字段本身的旧值，但已改为"最密集成交价"）
            if field != "poc":
                assert label != "POC", f"字段 {field} 不应为 'POC'"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
