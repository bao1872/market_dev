"""消息构建器 - 根据消息类型与上下文构建统一 NotificationMessageDTO。

设计：
- build_message(message_type, context): 根据类型与上下文构建 DTO
- 模板版本化：每个 message_type 对应固定的 template_key + template_version
- 渲染：从 context 提取字段填充 DTO

支持的 message_type：
- MONITOR_EVENT: 监控事件（合并通知/单策略事件）
- MONITOR_MEMBER_EVENT: 单策略过程事件（迁移兼容，逐步废弃）
- SYSTEM_ALERT: 系统异常
- CHANNEL_ALERT: 渠道异常

模板版本：当前统一为 1.1.0，后续模板变更时升级版本号。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.schemas.notification import NotificationMessageDTO

# 模板键与版本映射（message_type -> (template_key, template_version)）
_TEMPLATE_MAP: dict[str, tuple[str, str]] = {
    "MONITOR_EVENT": ("monitor_event", "1.1.0"),
    "MONITOR_MEMBER_EVENT": ("monitor_member_event", "1.1.0"),
    "SYSTEM_ALERT": ("system_alert", "1.1.0"),
    "CHANNEL_ALERT": ("channel_alert", "1.1.0"),
}

DEFAULT_DISCLAIMER = "仅展示规则触发与历史数据，不构成投资建议。"


class MessageBuilderError(ValueError):
    """消息构建错误。"""


def build_message(
    message_type: str,
    context: dict[str, Any],
    locale: str = "zh-CN",
) -> NotificationMessageDTO:
    """构建统一通知消息 DTO。

    Args:
        message_type: 消息类型（MONITOR_EVENT 等）
        context: 上下文字典，包含渲染所需字段
            通用字段：
            - title: 标题（必填）
            - summary: 摘要（必填）
            - resource_refs: 资源引用（必填）
            - data_time: 数据时间 ISO8601（必填）
            - facts: 关键事实列表（可选）
            - timeline: 时间线（可选）
            - items: 条目列表（可选）
            - actions: 操作按钮（可选）
            - disclaimer: 免责声明（可选，有默认值）
        locale: 语言区域

    Returns:
        NotificationMessageDTO

    Raises:
        MessageBuilderError: message_type 不支持或必填字段缺失
    """
    if message_type not in _TEMPLATE_MAP:
        raise MessageBuilderError(
            f"不支持的消息类型: {message_type}，支持: {list(_TEMPLATE_MAP.keys())}"
        )

    template_key, template_version = _TEMPLATE_MAP[message_type]

    # 校验必填字段
    required_fields = ["title", "summary", "resource_refs", "data_time"]
    missing = [f for f in required_fields if f not in context or context[f] is None]
    if missing:
        raise MessageBuilderError(
            f"构建消息缺少必填字段: {missing} (message_type={message_type})"
        )

    dto = NotificationMessageDTO(
        message_type=message_type,
        template_key=template_key,
        template_version=template_version,
        locale=locale,
        title=context["title"],
        summary=context["summary"],
        facts=context.get("facts", []),
        timeline=context.get("timeline", []),
        items=context.get("items", []),
        actions=context.get("actions", []),
        resource_refs=context["resource_refs"],
        data_time=context["data_time"],
        disclaimer=context.get("disclaimer", DEFAULT_DISCLAIMER),
    )
    dto.validate_message_type()
    return dto



def build_monitor_event(
    stock_name: str,
    event_type: str,
    event_time: str,
    member_name: str,
    role: str,
    summary_text: str,
    resource_refs: dict[str, Any],
    actions: list[dict[str, Any]] | None = None,
) -> NotificationMessageDTO:
    """构建监控事件消息（便捷方法）。

    用于 INDEPENDENT 模式下单成员触发，或 ANY 模式首个成员触发。

    Args:
        stock_name: 股票名称
        event_type: 事件类型（如 evt_dsa_dir_flip_up）
        event_time: 事件时间 ISO8601
        member_name: 成员策略名称
        role: 成员角色（TRIGGER/CONFIRM/VETO/OBSERVE）
        summary_text: 事件摘要文本
        resource_refs: 资源引用（instrument_id, plan_id, event_id）
        actions: 操作按钮
    """
    context = {
        "title": f"策略事件｜{stock_name}",
        "summary": summary_text,
        "facts": [
            {"key": "event_type", "label": "事件类型", "value": event_type},
            {"key": "member", "label": "策略", "value": member_name},
            {"key": "role", "label": "角色", "value": role},
        ],
        "actions": actions or [
            {"label": "查看个股详情", "url": f"/stock-detail?symbol={resource_refs.get('instrument_id', '')}"}
        ],
        "resource_refs": resource_refs,
        "data_time": event_time,
    }
    return build_message("MONITOR_EVENT", context)


def build_system_alert(
    alert_type: str,
    message: str,
    resource_refs: dict[str, Any],
    data_time: str | None = None,
    facts: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> NotificationMessageDTO:
    """构建系统异常消息（便捷方法）。

    用于数据过期、策略失败、服务异常等系统级告警。

    Args:
        alert_type: 告警类型（如 DATA_STALE/STRATEGY_FAILED/SERVICE_ERROR）
        message: 告警消息
        resource_refs: 资源引用（如 strategy_key, service_name）
        data_time: 数据时间（默认当前时间）
        facts: 关键事实
        actions: 操作按钮
    """
    if data_time is None:
        data_time = datetime.now(UTC).isoformat()

    context = {
        "title": f"系统告警｜{alert_type}",
        "summary": message,
        "facts": facts or [
            {"key": "alert_type", "label": "告警类型", "value": alert_type},
        ],
        "actions": actions or [],
        "resource_refs": resource_refs,
        "data_time": data_time,
    }
    return build_message("SYSTEM_ALERT", context)


def build_channel_alert(
    channel_name: str,
    error_code: str,
    error_message: str,
    resource_refs: dict[str, Any],
    data_time: str | None = None,
    actions: list[dict[str, Any]] | None = None,
) -> NotificationMessageDTO:
    """构建渠道异常消息（便捷方法）。

    用于渠道投递失败、渠道失效等渠道级告警。

    Args:
        channel_name: 渠道名称
        error_code: 错误码（如 WEBHOOK_INVALID/SIGN_ERROR/RATE_LIMIT）
        error_message: 错误信息
        resource_refs: 资源引用（如 channel_id, message_id）
        data_time: 数据时间（默认当前时间）
        actions: 操作按钮
    """
    if data_time is None:
        data_time = datetime.now(UTC).isoformat()

    context = {
        "title": f"渠道异常｜{channel_name}",
        "summary": f"投递失败: {error_message}",
        "facts": [
            {"key": "channel", "label": "渠道", "value": channel_name},
            {"key": "error_code", "label": "错误码", "value": error_code},
        ],
        "actions": actions or [],
        "resource_refs": resource_refs,
        "data_time": data_time,
    }
    return build_message("CHANNEL_ALERT", context)


# [飞书两段式投递] - 事件类型 → 中文标签（纯文本消息用）
_EVENT_TYPE_TEXT_LABEL: dict[str, str] = {
    "bb_upper_touch": "BB上轨穿越",
    "bb_mid_touch": "BB中轨穿越",
    "bb_lower_touch": "BB下轨穿越",
    "node_cluster_touch": "节点集群穿越",
}


def build_monitor_event_text(
    stock_name: str,
    symbol: str,
    event_type: str,
    event_time: str,
    current_price: float | None = None,
    bb_upper: float | None = None,
    bb_mid: float | None = None,
    bb_lower: float | None = None,
    upper_node: float | None = None,
    lower_node: float | None = None,
    poc_price: float | None = None,
    position_0_1: float | None = None,
    resource_refs: dict[str, Any] | None = None,
) -> NotificationMessageDTO:
    """构建监控事件纯文本消息（飞书两段式投递 - 文本段）。

    按 advice.md 模板生成纯文本，只保留一个时间字段"触发时间"，
    不出现"数据时间"/"更新时间"/"发送时间"。

    模板：
        【自选监控触发】
        {股票名称} {股票代码}
        触发：{触发类型中文}
        触发时间：{HH:MM}
        现价：{current_price}
        BB：{bb_upper} / {bb_mid} / {bb_lower}
        上节点：{upper_node}
        下节点：{lower_node}
        POC：{poc_price}
        位置：{position_0_1}

    Args:
        stock_name: 股票名称
        symbol: 股票代码
        event_type: 事件类型（如 bb_upper_touch）
        event_time: 事件时间 ISO8601（仅用于提取 HH:MM 作为触发时间）
        current_price: 现价
        bb_upper / bb_mid / bb_lower: BB 三轨
        upper_node / lower_node: 上下节点
        poc_price: POC 价格
        position_0_1: 节点位置 0~1
        resource_refs: 资源引用（instrument_id/symbol/event_id 等）

    Returns:
        NotificationMessageDTO（text_content 字段填充纯文本）
    """
    from zoneinfo import ZoneInfo

    _CST = ZoneInfo("Asia/Shanghai")

    # 解析 event_time -> HH:MM（北京时间）
    try:
        from datetime import datetime as _dt

        dt = _dt.fromisoformat(event_time)
        if dt.tzinfo is None:
            from datetime import UTC

            dt = dt.replace(tzinfo=UTC)
        trigger_time = dt.astimezone(_CST).strftime("%H:%M")
    except (ValueError, TypeError):
        trigger_time = "--:--"

    event_label = _EVENT_TYPE_TEXT_LABEL.get(event_type, event_type)

    def _fmt(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "-"

    def _fmt_pos(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "-"

    text_lines = [
        "【自选监控触发】",
        f"{stock_name} {symbol}",
        f"触发：{event_label}",
        f"触发时间：{trigger_time}",
        f"现价：{_fmt(current_price)}",
        f"BB：{_fmt(bb_upper)} / {_fmt(bb_mid)} / {_fmt(bb_lower)}",
        f"上节点：{_fmt(upper_node)}",
        f"下节点：{_fmt(lower_node)}",
        f"POC：{_fmt(poc_price)}",
        f"位置：{_fmt_pos(position_0_1)}",
    ]
    text_content = "\n".join(text_lines)

    refs = resource_refs or {}
    return NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_event_text",
        template_version="1.1.0",
        title=f"监控触发｜{stock_name} {symbol}",
        summary=text_content,
        text_content=text_content,
        resource_refs={
            "instrument_id": refs.get("instrument_id", ""),
            "symbol": symbol,
            "event_type": event_type,
            **refs,
        },
        data_time=event_time,
        primary_instrument={
            "instrument_id": refs.get("instrument_id", ""),
            "symbol": symbol,
            "name": stock_name,
        },
        event_summary=event_label,
    )


if __name__ == "__main__":
    # 自测入口：验证消息构建
    print("测试监控事件消息:")
    dto1 = build_monitor_event(
        stock_name="贵州茅台",
        event_type="evt_dsa_dir_flip_up",
        event_time="2026-06-18T10:18:00+08:00",
        member_name="DSA选股",
        role="TRIGGER",
        summary_text="DSA 方向翻多",
        resource_refs={"instrument_id": "600519.SH", "plan_id": "monitor_plan_001"},
    )
    print(f"  title={dto1.title}")
    print(f"  template_key={dto1.template_key}, version={dto1.template_version}")
    assert dto1.message_type == "MONITOR_EVENT"

    print("测试监控事件纯文本消息:")
    dto_text = build_monitor_event_text(
        stock_name="鼎阳科技",
        symbol="688112",
        event_type="bb_mid_touch",
        event_time="2026-06-18T14:48:00+08:00",
        current_price=71.13,
        bb_upper=79.12,
        bb_mid=71.20,
        bb_lower=63.28,
        upper_node=77.25,
        lower_node=70.04,
        poc_price=38.78,
        position_0_1=0.80,
        resource_refs={"instrument_id": "688112.SH", "event_id": "evt-001"},
    )
    print(f"  title={dto_text.title}")
    print(f"  text_content:\n{dto_text.text_content}")
    assert dto_text.text_content is not None
    assert "【自选监控触发】" in dto_text.text_content
    assert "触发时间：14:48" in dto_text.text_content
    assert "现价：71.13" in dto_text.text_content
    assert "BB：79.12 / 71.20 / 63.28" in dto_text.text_content
    assert "上节点：77.25" in dto_text.text_content
    assert "下节点：70.04" in dto_text.text_content
    assert "POC：38.78" in dto_text.text_content
    assert "位置：0.80" in dto_text.text_content
    # 验证只有一个时间字段"触发时间"，不出现"数据时间"
    assert "数据时间" not in dto_text.text_content
    assert "更新时间" not in dto_text.text_content
    print("  纯文本消息字段验证通过 ✓")

    print("测试系统告警消息:")
    dto2 = build_system_alert(
        alert_type="DATA_STALE",
        message="日线行情数据已过期 30 分钟",
        resource_refs={"service": "bars_daily"},
    )
    print(f"  title={dto2.title}")
    assert dto2.message_type == "SYSTEM_ALERT"

    print("测试渠道异常消息:")
    dto3 = build_channel_alert(
        channel_name="飞书Webhook",
        error_code="WEBHOOK_INVALID",
        error_message="Webhook URL 返回 404",
        resource_refs={"channel_id": "ch_001"},
    )
    print(f"  title={dto3.title}")
    assert dto3.message_type == "CHANNEL_ALERT"

    print("测试不支持的消息类型:")
    try:
        build_message("INVALID_TYPE", {"title": "t", "summary": "s", "resource_refs": {}, "data_time": "2026-06-18"})
    except MessageBuilderError as e:
        print(f"  预期错误: {e}")

    print("测试缺少必填字段:")
    try:
        build_message("SYSTEM_ALERT", {"title": "t"})  # 缺少 summary 等
    except MessageBuilderError as e:
        print(f"  预期错误: {e}")

    print("OK")
