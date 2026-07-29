"""共享指标视图枚举 - 贯穿 StrategyEvent / NotificationMessage / Capture /
CaptureJob / 输出文件名 / 缓存键 / 幂等键 / 状态查询 / 前端 URL 参数。

[CHANGE-20260728-010] 盘中监控与个股飞书分享固定使用“结构 + 筹码共识”组合视图：
- 新业务只写 FEISHU_CAPTURE_VIEW="structure_node"
- 历史 node_cluster / smc / bollinger 枚举值保留读取兼容，不再作为新写入路径
- EVENT_TYPE_TO_INDICATOR_VIEW 仅供旧数据回读，新链路不再调用 resolve_indicator_view
- 监控事件类别（结构 / 筹码共识）只决定文字与统计归类，不再决定截图图层

历史 CHANGE-20260720-003 §三 “三类独立飞书图片”已被本轮改造替代。
"""

from __future__ import annotations

from typing import Literal

# 共享枚举：历史指标视图（仅读取兼容，不再作为新业务写入路径）
IndicatorView = Literal["node_cluster", "bollinger", "smc", "structure_node"]

# 历史枚举值集合（运行时校验用，含新组合值）
INDICATOR_VIEW_VALUES: tuple[str, ...] = (
    "node_cluster", "bollinger", "smc", "structure_node",
)

# [CHANGE-20260728-010] 新业务唯一写入值：结构 + 筹码共识组合视图
FEISHU_CAPTURE_VIEW: str = "structure_node"

# 用户可见文案
INDICATOR_VIEW_LABELS: dict[str, str] = {
    "node_cluster": "筹码共识价",
    "bollinger": "布林带",
    "smc": "结构",
    "structure_node": "结构 + 筹码共识",
}

# [CHANGE-20260728-010] 监控事件类别（仅用于文字与统计归类，不再决定截图图层）
# 结构：SMC 的 BOS/CHoCH/EQH/EQL/OB first touch
# 筹码共识：node_cluster_touch
EVENT_CATEGORY_STRUCTURE = "structure"
EVENT_CATEGORY_NODE_CONSENSUS = "node_consensus"

# 事件类型 → 监控事件类别映射（新业务统计归类用）
EVENT_TYPE_TO_CATEGORY: dict[str, str] = {
    # 结构（SMC 五类）
    "smc_bos_retest": EVENT_CATEGORY_STRUCTURE,
    "smc_choch_retest": EVENT_CATEGORY_STRUCTURE,
    "smc_equal_highs_retest": EVENT_CATEGORY_STRUCTURE,
    "smc_equal_lows_retest": EVENT_CATEGORY_STRUCTURE,
    "smc_order_block_first_touch": EVENT_CATEGORY_STRUCTURE,
    # 筹码共识
    "node_cluster_touch": EVENT_CATEGORY_NODE_CONSENSUS,
}

# 用户可见类别文案
EVENT_CATEGORY_LABELS: dict[str, str] = {
    EVENT_CATEGORY_STRUCTURE: "结构",
    EVENT_CATEGORY_NODE_CONSENSUS: "筹码共识",
}

# 历史事件类型 → indicator_view 映射（仅供旧数据回读，新业务不再调用）
# 保留以兼容历史 CaptureJob.indicator_view 读取；新链路使用 FEISHU_CAPTURE_VIEW。
EVENT_TYPE_TO_INDICATOR_VIEW: dict[str, str] = {
    # Bollinger（历史兼容，新业务不再触发）
    "bb_upper_touch": "bollinger",
    "bb_mid_touch": "bollinger",
    "bb_lower_touch": "bollinger",
    # Volume Node
    "node_cluster_touch": "node_cluster",
    # SMC
    "smc_bos_retest": "smc",
    "smc_choch_retest": "smc",
    "smc_equal_highs_retest": "smc",
    "smc_equal_lows_retest": "smc",
    "smc_order_block_first_touch": "smc",
}

# 默认值（历史回读用，事件类型未识别时使用）
DEFAULT_INDICATOR_VIEW: str = "node_cluster"

# [Gate3] 未映射事件类型的错误码（不再回退 node_cluster，改为显式 UNSUPPORTED 跳过）
UNSUPPORTED_INDICATOR_VIEW: str = "UNSUPPORTED_INDICATOR_VIEW"


def get_event_category(event_type: str) -> str | None:
    """事件类型 → 监控事件类别。

    [CHANGE-20260728-010] 新业务统计归类使用。
    未映射事件类型返回 None，调用方应跳过统计。

    Args:
        event_type: 内部事件类型（如 smc_bos_retest / node_cluster_touch）

    Returns:
        类别字符串（structure / node_consensus），未映射时返回 None
    """
    return EVENT_TYPE_TO_CATEGORY.get(event_type)


def get_indicator_view_for_event(event_type: str) -> str:
    """[历史兼容] 事件类型 → indicator_view。

    [CHANGE-20260728-010] 新业务不再调用此函数，新链路直接使用 FEISHU_CAPTURE_VIEW。
    保留此函数仅供读取历史 CaptureJob.indicator_view 数据时使用。

    Args:
        event_type: 内部事件类型（如 bb_upper_touch / smc_bos_retest）

    Returns:
        历史 indicator_view 字符串（node_cluster|bollinger|smc）
    """
    return EVENT_TYPE_TO_INDICATOR_VIEW.get(event_type, DEFAULT_INDICATOR_VIEW)


def resolve_indicator_view(
    event_type: str,
    payload: dict[str, object] | None = None,
) -> str:
    """[历史兼容] 从 payload.indicator_view 优先解析，缺失时回退到事件类型映射。

    [CHANGE-20260728-010] 新业务不再调用此函数，新链路直接使用 FEISHU_CAPTURE_VIEW。
    保留此函数仅供读取历史 StrategyEvent.payload["indicator_view"] 时使用。
    """
    if payload is not None:
        iv = payload.get("indicator_view")
        if isinstance(iv, str) and iv in INDICATOR_VIEW_VALUES:
            return iv
    return get_indicator_view_for_event(event_type)


def is_valid_indicator_view(value: str | None) -> bool:
    """校验是否为合法 indicator_view（含新组合值 structure_node）。"""
    return value in INDICATOR_VIEW_VALUES


def is_supported_event_type(event_type: str, payload: dict[str, object] | None = None) -> bool:
    """[CHANGE-20260728-010] 检查事件类型是否属于本轮保留的两类触发。

    - 结构：smc_bos_retest / smc_choch_retest / smc_equal_highs_retest /
            smc_equal_lows_retest / smc_order_block_first_touch
    - 筹码共识：node_cluster_touch

    BB 事件类型已不再触发，显式返回 False（跳过截图与统计）。
    payload 中显式指定 indicator_view 时仍视为已支持（兼容历史数据回读）。

    Args:
        event_type: 事件类型
        payload: 可能含 indicator_view 字段

    Returns:
        True: 属于本轮保留的两类触发；False: 应跳过
    """
    if event_type in EVENT_TYPE_TO_CATEGORY:
        return True
    # 兼容历史 payload 显式指定 indicator_view 的场景（仅读取，不再新写入）
    if payload is not None:
        iv = payload.get("indicator_view")
        if isinstance(iv, str) and iv in INDICATOR_VIEW_VALUES:
            return True
    return False


if __name__ == "__main__":
    # 自测入口：验证映射 + 兜底
    assert INDICATOR_VIEW_VALUES == ("node_cluster", "bollinger", "smc", "structure_node")
    assert FEISHU_CAPTURE_VIEW == "structure_node"
    assert INDICATOR_VIEW_LABELS["structure_node"] == "结构 + 筹码共识"

    # 事件类别映射
    assert get_event_category("smc_bos_retest") == EVENT_CATEGORY_STRUCTURE
    assert get_event_category("smc_choch_retest") == EVENT_CATEGORY_STRUCTURE
    assert get_event_category("smc_equal_highs_retest") == EVENT_CATEGORY_STRUCTURE
    assert get_event_category("smc_equal_lows_retest") == EVENT_CATEGORY_STRUCTURE
    assert get_event_category("smc_order_block_first_touch") == EVENT_CATEGORY_STRUCTURE
    assert get_event_category("node_cluster_touch") == EVENT_CATEGORY_NODE_CONSENSUS
    assert get_event_category("bb_upper_touch") is None
    assert get_event_category("unknown_event") is None
    print(f"EVENT_TYPE_TO_CATEGORY ({len(EVENT_TYPE_TO_CATEGORY)} 项) ✓")

    # is_supported_event_type：本轮两类触发返回 True，BB 返回 False
    assert is_supported_event_type("smc_bos_retest") is True
    assert is_supported_event_type("node_cluster_touch") is True
    assert is_supported_event_type("bb_upper_touch") is False
    assert is_supported_event_type("bb_mid_touch") is False
    assert is_supported_event_type("bb_lower_touch") is False
    assert is_supported_event_type("unknown_event") is False
    # payload 显式指定 indicator_view 仍视为支持（历史兼容）
    assert is_supported_event_type("unknown_event", {"indicator_view": "smc"}) is True
    print("is_supported_event_type ✓")

    # 历史回读兼容
    assert get_indicator_view_for_event("bb_upper_touch") == "bollinger"
    assert get_indicator_view_for_event("node_cluster_touch") == "node_cluster"
    assert get_indicator_view_for_event("smc_bos_retest") == "smc"
    assert resolve_indicator_view("bb_upper_touch", {}) == "bollinger"
    print("历史回读兼容 ✓")

    # 校验函数
    assert is_valid_indicator_view("node_cluster") is True
    assert is_valid_indicator_view("bollinger") is True
    assert is_valid_indicator_view("smc") is True
    assert is_valid_indicator_view("structure_node") is True
    assert is_valid_indicator_view("invalid") is False
    assert is_valid_indicator_view(None) is False

    print(f"INDICATOR_VIEW_VALUES={INDICATOR_VIEW_VALUES}")
    print(f"FEISHU_CAPTURE_VIEW={FEISHU_CAPTURE_VIEW}")
    print(f"INDICATOR_VIEW_LABELS ({len(INDICATOR_VIEW_LABELS)} 项) ✓")
    print("OK")
