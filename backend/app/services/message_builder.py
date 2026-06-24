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
