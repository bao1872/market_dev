"""共享指标视图枚举 - 贯穿 StrategyEvent / NotificationMessage / Capture /
CaptureJob / 输出文件名 / 缓存键 / 幂等键 / 状态查询 / 前端 URL 参数。

[CHANGE-20260720-003 §三] 三类监控独立飞书图片：
- node_cluster: 筹码共识价（VolumeNodeMonitor）
- bollinger: 布林带（BollingerMonitor）
- smc: SMC 结构（SmcMonitor）

禁止三类指标叠在同一张图：每张截图只渲染一个 indicator_view 对应的图层。
"""

from __future__ import annotations

from typing import Literal

# 共享枚举：指标视图
IndicatorView = Literal["node_cluster", "bollinger", "smc"]

# 枚举值集合（运行时校验用）
INDICATOR_VIEW_VALUES: tuple[str, ...] = ("node_cluster", "bollinger", "smc")

# 默认值（事件类型未识别时使用，避免 capture 链路 None 渗透）
DEFAULT_INDICATOR_VIEW: str = "node_cluster"

# 事件类型 → indicator_view 映射（监控自动发送时使用）
# 监控事件写入 StrategyEvent.payload["indicator_view"]，capture 链路从 payload 读取，
# payload 缺失时回退到本映射表。
EVENT_TYPE_TO_INDICATOR_VIEW: dict[str, str] = {
    # Bollinger
    "bb_upper_touch": "bollinger",
    "bb_mid_touch": "bollinger",
    "bb_lower_touch": "bollinger",
    # Volume Node
    "node_cluster_touch": "node_cluster",
    # SMC
    "smc_bos_retest": "smc",
    "smc_choch_retest": "smc",
    "smc_order_block_first_touch": "smc",
}

# 用户可见文案（前端弹窗单选项 + 飞书卡片标题）
INDICATOR_VIEW_LABELS: dict[str, str] = {
    "node_cluster": "筹码共识价",
    "bollinger": "布林带",
    "smc": "SMC结构",
}


def get_indicator_view_for_event(event_type: str) -> str:
    """事件类型 → indicator_view。

    未知事件类型回退到 DEFAULT_INDICATOR_VIEW（node_cluster），
    避免 capture 链路收到 None 导致缓存键/文件名异常。

    Args:
        event_type: 内部事件类型（如 bb_upper_touch / smc_bos_retest）

    Returns:
        indicator_view 字符串（node_cluster|bollinger|smc）
    """
    return EVENT_TYPE_TO_INDICATOR_VIEW.get(event_type, DEFAULT_INDICATOR_VIEW)


def resolve_indicator_view(
    event_type: str,
    payload: dict[str, object] | None = None,
) -> str:
    """从 payload.indicator_view 优先解析，缺失时回退到事件类型映射。

    监控事件写入时已填充 payload["indicator_view"]；历史事件 payload 缺失时
    使用 EVENT_TYPE_TO_INDICATOR_VIEW 兜底，保证旧事件也能映射到正确视图。

    Args:
        event_type: 事件类型
        payload: StrategyEvent.payload（可能含 indicator_view 字段）

    Returns:
        indicator_view 字符串
    """
    if payload is not None:
        iv = payload.get("indicator_view")
        if isinstance(iv, str) and iv in INDICATOR_VIEW_VALUES:
            return iv
    return get_indicator_view_for_event(event_type)


def is_valid_indicator_view(value: str | None) -> bool:
    """校验是否为合法 indicator_view。"""
    return value in INDICATOR_VIEW_VALUES


if __name__ == "__main__":
    # 自测入口：验证映射 + 兜底
    assert INDICATOR_VIEW_VALUES == ("node_cluster", "bollinger", "smc")
    assert DEFAULT_INDICATOR_VIEW == "node_cluster"

    # 事件类型映射
    assert get_indicator_view_for_event("bb_upper_touch") == "bollinger"
    assert get_indicator_view_for_event("bb_mid_touch") == "bollinger"
    assert get_indicator_view_for_event("bb_lower_touch") == "bollinger"
    assert get_indicator_view_for_event("node_cluster_touch") == "node_cluster"
    assert get_indicator_view_for_event("smc_bos_retest") == "smc"
    assert get_indicator_view_for_event("smc_choch_retest") == "smc"
    assert get_indicator_view_for_event("smc_order_block_first_touch") == "smc"
    # 未知事件类型回退默认
    assert get_indicator_view_for_event("unknown_event") == DEFAULT_INDICATOR_VIEW

    # payload 优先
    assert resolve_indicator_view("bb_upper_touch", {"indicator_view": "smc"}) == "smc"
    # payload 缺失时回退事件类型映射
    assert resolve_indicator_view("bb_upper_touch", {}) == "bollinger"
    assert resolve_indicator_view("bb_upper_touch", None) == "bollinger"
    # payload 非法值时回退事件类型映射
    assert resolve_indicator_view("bb_upper_touch", {"indicator_view": "invalid"}) == "bollinger"
    assert resolve_indicator_view("unknown_event", None) == DEFAULT_INDICATOR_VIEW

    # 校验函数
    assert is_valid_indicator_view("node_cluster") is True
    assert is_valid_indicator_view("bollinger") is True
    assert is_valid_indicator_view("smc") is True
    assert is_valid_indicator_view("invalid") is False
    assert is_valid_indicator_view(None) is False

    # 用户可见文案
    assert INDICATOR_VIEW_LABELS["node_cluster"] == "筹码共识价"
    assert INDICATOR_VIEW_LABELS["bollinger"] == "布林带"
    assert INDICATOR_VIEW_LABELS["smc"] == "SMC结构"

    print(f"INDICATOR_VIEW_VALUES={INDICATOR_VIEW_VALUES}")
    print(f"EVENT_TYPE_TO_INDICATOR_VIEW ({len(EVENT_TYPE_TO_INDICATOR_VIEW)} 项) ✓")
    print(f"INDICATOR_VIEW_LABELS ({len(INDICATOR_VIEW_LABELS)} 项) ✓")
    print("OK")
