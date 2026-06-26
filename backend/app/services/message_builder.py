"""消息构建器 - 根据消息类型与上下文构建统一 NotificationMessageDTO。

设计：
- build_message(message_type, context): 根据类型与上下文构建 DTO
- 模板版本化：每个 message_type 对应固定的 template_key + template_version
- 渲染：从 context 提取字段填充 DTO

支持的 message_type：
- MONITOR_EVENT: 监控事件（合并通知/单策略事件）
- MONITOR_MEMBER_EVENT: 【仅历史兼容】旧单策略过程事件，仅用于读取历史消息，
  新代码禁止生成（advice.md 第十一节遗留清理）。新消息统一用 MONITOR_EVENT。
- SYSTEM_ALERT: 系统异常
- CHANNEL_ALERT: 渠道异常

模板版本：当前统一为 1.1.0，后续模板变更时升级版本号。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.constants.user_facing_labels import get_event_label, get_field_label
from app.schemas.notification import NotificationMessageDTO

# 模板键与版本映射（message_type -> (template_key, template_version)）
_TEMPLATE_MAP: dict[str, tuple[str, str]] = {
    "MONITOR_EVENT": ("monitor_event", "1.1.0"),
    # 【仅历史兼容】仅用于读取历史消息，新代码禁止生成 MONITOR_MEMBER_EVENT
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


# [飞书两段式投递] - 事件类型文案已迁移至 app.constants.user_facing_labels
# 详见 get_event_label()，service 层不再维护重复 dict（advice.md 第二节通俗化要求）


def elements_to_text(elements: list[dict[str, Any]] | None) -> str:
    """将卡片 elements 数组转换为纯文本（飞书两段式投递文本段使用）。

    [飞书两段式投递] - 合并通知场景：
    - _build_merged_card_dto 构建 elements 后，由此函数生成 text_content
    - send_text_message 在 text_content 为空时回退到此函数兜底

    转换规则：
    - markdown 元素: 取 content 字段
    - hr 元素: 转为 "---" 分隔线
    - note 元素: 取内部 elements[0].content（plain_text）
    - 其他: 跳过

    各段用空行分隔，保证飞书文本消息可读。

    Args:
        elements: 卡片元素数组（可能为 None 或空）

    Returns:
        拼接后的纯文本（空数组/None 返回空字符串）
    """
    if not elements:
        return ""

    parts: list[str] = []
    for el in elements:
        tag = el.get("tag")
        if tag == "markdown":
            content = el.get("content", "")
            if content:
                parts.append(content)
        elif tag == "hr":
            parts.append("---")
        elif tag == "note":
            # note 内部为 plain_text elements 数组
            inner = el.get("elements") or []
            for item in inner:
                if item.get("tag") == "plain_text":
                    text = item.get("content", "")
                    if text:
                        parts.append(text)
        # 其他 tag（lark_md/plain_text 等）按 content 字段兜底
        elif "content" in el:
            content = el.get("content", "")
            if content:
                parts.append(str(content))

    return "\n\n".join(parts)


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

    [advice.md 第二节] - 字段名通俗化：
    - "BB / 上节点 / 下节点 / POC / 位置" → "近期波动上沿/中枢/下沿 + 成交密集区 + 最密集成交价 + 当前区间位置"
    - 事件类型文案来自 user_facing_labels.get_event_label

    模板：
        【自选监控触发】
        {股票名称} {股票代码}
        触发：{触发类型通俗文案}
        触发时间：{HH:MM}
        现价：{current_price}
        近期波动上沿：{bb_upper}
        近期价格中枢：{bb_mid}
        近期波动下沿：{bb_lower}
        上方成交密集区：{upper_node}
        下方成交密集区：{lower_node}
        最密集成交价：{poc_price}
        当前区间位置：{position_0_1}

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

    event_label = get_event_label(event_type)

    def _fmt(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "-"

    def _fmt_pos(v: float | None) -> str:
        return f"{v:.2f}" if v is not None else "-"

    # [advice.md 第二节] - 字段名通俗化（BB/上节点/下节点/POC/位置 → 通俗文案）
    text_lines = [
        "【自选监控触发】",
        f"{stock_name} {symbol}",
        f"触发：{event_label}",
        f"触发时间：{trigger_time}",
        f"现价：{_fmt(current_price)}",
        f"{get_field_label('bb_upper')}：{_fmt(bb_upper)}",
        f"{get_field_label('bb_mid')}：{_fmt(bb_mid)}",
        f"{get_field_label('bb_lower')}：{_fmt(bb_lower)}",
        f"{get_field_label('upper_node')}：{_fmt(upper_node)}",
        f"{get_field_label('lower_node')}：{_fmt(lower_node)}",
        f"{get_field_label('poc')}：{_fmt(poc_price)}",
        f"{get_field_label('position')}：{_fmt_pos(position_0_1)}",
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
    # [advice.md 第二节] - 字段名通俗化后断言（不再出现 BB/上节点/下节点/POC/位置 等专业术语）
    assert "近期波动上沿：79.12" in dto_text.text_content
    assert "近期价格中枢：71.20" in dto_text.text_content
    assert "近期波动下沿：63.28" in dto_text.text_content
    assert "上方成交密集区：77.25" in dto_text.text_content
    assert "下方成交密集区：70.04" in dto_text.text_content
    assert "最密集成交价：38.78" in dto_text.text_content
    assert "当前区间位置：0.80" in dto_text.text_content
    # 事件类型文案应来自 user_facing_labels（"价格回到近期价格中枢"）
    assert "触发：价格回到近期价格中枢" in dto_text.text_content
    # 验证旧专业术语已消除（按行检查独立字段前缀，避免子串误判）
    lines = dto_text.text_content.split("\n")
    assert not any(line.startswith("BB：") for line in lines), "不应有独立的 'BB：' 字段"
    assert not any(line.startswith("上节点：") for line in lines), "不应有独立的 '上节点：' 字段"
    assert not any(line.startswith("下节点：") for line in lines), "不应有独立的 '下节点：' 字段"
    assert not any(line.startswith("POC：") for line in lines), "不应有独立的 'POC：' 字段"
    # "位置：" 是 "当前区间位置：" 的子串，按行首前缀检查独立字段是否已替换
    assert not any(line.startswith("位置：") for line in lines), "不应有独立的 '位置：' 字段（应改为 '当前区间位置：'）"
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
