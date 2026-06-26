"""用户可见文案常量 - 把内部事件类型/字段名映射为普通用户能看懂的中文文案。

设计目的（advice.md 第二节）：
- 飞书消息和消息中心原文案过于专业（"BB上轨穿越"/"POC"/"上节点"等），普通用户难理解
- 统一收敛到本模块，作为后端唯一权威实现，禁止在 service 层再散落 dict

提供：
- EVENT_LABELS: 事件类型 → 通俗文案
- FIELD_LABELS: 字段名 → 通俗文案
- get_event_label(event_type): 查询事件文案，未知返回原值
- get_field_label(field): 查询字段文案，未知返回原值

注意：
- 内部 event_type 值（如 bb_upper_touch）保持不变，仅改展示文案
- emoji 与文案分离，emoji 仍在 monitor_batch_service._EVENT_EMOJI 维护

用法：
    from app.constants.user_facing_labels import get_event_label, get_field_label
    label = get_event_label("bb_upper_touch")  # "价格触及近期波动上沿"

模块自测：
    python -m app.constants.user_facing_labels
"""

from __future__ import annotations

# 事件类型 → 通俗文案（用户在飞书/消息中心看到的名称）
# [user_facing_labels] - 描述: 事件类型权威文案表，service 层禁止重复定义
EVENT_LABELS: dict[str, str] = {
    "bb_upper_touch": "价格触及近期波动上沿",
    "bb_mid_touch": "价格回到近期价格中枢",
    "bb_lower_touch": "价格触及近期波动下沿",
    "node_cluster_touch": "价格触及成交密集区",
    # [StockDetailFeishu] - 个股快照主动分享（不暴露内部 manual_send 代码）
    "STOCK_SNAPSHOT_SHARE": "个股快照",
}

# 字段名 → 通俗文案（飞书正文/合并卡片中各数据行的标签）
# [user_facing_labels] - 描述: 字段名权威文案表，替代 "BB上轨/上节点/POC" 等专业术语
FIELD_LABELS: dict[str, str] = {
    "bb_upper": "近期波动上沿",
    "bb_mid": "近期价格中枢",
    "bb_lower": "近期波动下沿",
    "upper_node": "上方成交密集区",
    "lower_node": "下方成交密集区",
    "poc": "最密集成交价",
    "position": "当前区间位置",
    # 概览行用的简称（保持单行紧凑可读）
    "bb_upper_short": "波动上沿",
    "bb_mid_short": "价格中枢",
    "bb_lower_short": "波动下沿",
    "node_cluster_short": "密集区",
}


def get_event_label(event_type: str) -> str:
    """查询事件类型对应的通俗文案，未知则返回原 event_type。

    Args:
        event_type: 内部事件类型（如 bb_upper_touch）

    Returns:
        用户可见文案（如 "价格触及近期波动上沿"）；未知返回原值
    """
    return EVENT_LABELS.get(event_type, event_type)


def get_field_label(field: str) -> str:
    """查询字段名对应的通俗文案，未知则返回原 field。

    Args:
        field: 内部字段名（如 bb_upper / upper_node / poc / position）

    Returns:
        用户可见文案（如 "近期波动上沿"）；未知返回原值
    """
    return FIELD_LABELS.get(field, field)


if __name__ == "__main__":
    # 自测入口：验证映射完整性 + 查询函数行为（无副作用）
    # 1. 事件类型映射
    assert get_event_label("bb_upper_touch") == "价格触及近期波动上沿"
    assert get_event_label("bb_mid_touch") == "价格回到近期价格中枢"
    assert get_event_label("bb_lower_touch") == "价格触及近期波动下沿"
    assert get_event_label("node_cluster_touch") == "价格触及成交密集区"
    assert get_event_label("STOCK_SNAPSHOT_SHARE") == "个股快照"
    # 未知 event_type 原样返回
    assert get_event_label("unknown_event") == "unknown_event"
    print(f"EVENT_LABELS ({len(EVENT_LABELS)} 项) ✓")

    # 2. 字段名映射
    assert get_field_label("bb_upper") == "近期波动上沿"
    assert get_field_label("bb_mid") == "近期价格中枢"
    assert get_field_label("bb_lower") == "近期波动下沿"
    assert get_field_label("upper_node") == "上方成交密集区"
    assert get_field_label("lower_node") == "下方成交密集区"
    assert get_field_label("poc") == "最密集成交价"
    assert get_field_label("position") == "当前区间位置"
    # 概览行简称
    assert get_field_label("bb_upper_short") == "波动上沿"
    assert get_field_label("bb_mid_short") == "价格中枢"
    assert get_field_label("bb_lower_short") == "波动下沿"
    assert get_field_label("node_cluster_short") == "密集区"
    # 未知 field 原样返回
    assert get_field_label("unknown_field") == "unknown_field"
    print(f"FIELD_LABELS ({len(FIELD_LABELS)} 项) ✓")

    # 3. 与 advice.md 第二节映射表对齐校验
    assert EVENT_LABELS["bb_upper_touch"].startswith("价格触及")
    assert EVENT_LABELS["bb_mid_touch"].startswith("价格回到")
    assert EVENT_LABELS["bb_lower_touch"].startswith("价格触及")
    assert EVENT_LABELS["node_cluster_touch"].startswith("价格触及")
    print("advice.md 第二节映射表对齐 ✓")

    print("OK")
