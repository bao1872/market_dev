"""飞书卡片构建器 - 将 NotificationMessageDTO 转换为飞书 interactive card JSON。

设计：
- dto_to_feishu_card(dto): 统一 DTO → 飞书卡片 JSON（msg_type=interactive）
- 网页预览与真实投递共享同一渲染逻辑（spec 要求"网页预览与真实投递共享同一 DTO"）
- 卡片格式对齐飞书开放平台 interactive card 规范

卡片结构：
- header: 标题 + 颜色主题（优先使用 resource_refs.header_severity，回退到 message_type 映射）
- elements: 摘要 / 关键事实 / 时间线 / 条目列表 / 操作按钮 / 免责声明

颜色映射：
- MONITOR_EVENT → 由 resource_refs.header_severity 决定（danger=red, warn=orange, info=green）
- MONITOR_MEMBER_EVENT → 由 resource_refs.header_severity 决定（【仅历史兼容】历史消息渲染，
  新代码禁止生成 MONITOR_MEMBER_EVENT，advice.md 第十一节遗留清理）
- SYSTEM_ALERT → red
- CHANNEL_ALERT → orange
"""

from __future__ import annotations

from typing import Any

from app.schemas.notification import NotificationMessageDTO

# message_type → 飞书卡片头部颜色模板（默认映射，可被 resource_refs.header_severity 覆盖）
_HEADER_TEMPLATE_MAP: dict[str, str] = {
    "MONITOR_EVENT": "turquoise",
    # 【仅历史兼容】历史消息渲染兼容，新代码禁止生成 MONITOR_MEMBER_EVENT
    "MONITOR_MEMBER_EVENT": "turquoise",
    "SYSTEM_ALERT": "red",
    "CHANNEL_ALERT": "orange",
}

# header_severity → 飞书卡片头部颜色（监控事件专用）
_SEVERITY_TO_TEMPLATE: dict[str, str] = {
    "danger": "red",
    "warn": "orange",
    "info": "green",
}


def _format_fact(fact: dict[str, Any]) -> str:
    """格式化单条事实为 markdown 行。

    Args:
        fact: {"key": ..., "label": ..., "value": ...}

    Returns:
        markdown 文本行（如 "**组合逻辑**: ALL"）
    """
    label = fact.get("label", fact.get("key", ""))
    value = fact.get("value", "")
    return f"- **{label}**: {value}"


def _format_timeline_entry(entry: dict[str, Any]) -> str:
    """格式化单条时间线条目为 markdown 行。

    Args:
        entry: {"time": ..., "label": ...}

    Returns:
        markdown 文本行（如 "- 10:18 Node 碰触 POC"）
    """
    time_str = entry.get("time", "")
    label = entry.get("label", "")
    # 截取时间部分（ISO8601 取 HH:MM）
    if "T" in time_str:
        time_part = time_str.split("T")[1][:5]
    else:
        time_part = time_str[:5] if len(time_str) >= 5 else time_str
    return f"- {time_part} {label}"


def _format_item(item: dict[str, Any]) -> str:
    """格式化单条条目为 markdown 行。

    Args:
        item: {"name": ..., "rank_value": ..., ...}

    Returns:
        markdown 文本行
    """
    name = item.get("name", item.get("symbol", ""))
    rank = item.get("rank_value")
    if rank is not None:
        return f"- {name}（排名: {rank:.2f}）"
    return f"- {name}"


def _format_action(action: dict[str, Any]) -> dict[str, Any]:
    """格式化操作按钮为飞书 action 元素。

    Args:
        action: {"label": ..., "url": ...}

    Returns:
        飞书 action button 元素
    """
    return {
        "tag": "action",
        "actions": [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": action.get("label", "查看")},
                "url": action.get("url", ""),
                "type": "default",
            }
        ],
    }


def dto_to_feishu_card(dto: NotificationMessageDTO) -> dict[str, Any]:
    """将统一消息 DTO 转换为飞书 interactive card JSON。

    网页预览与真实投递共享此渲染逻辑。

    Args:
        dto: 统一通知消息 DTO

    Returns:
        飞书卡片 JSON（可直接作为 msg_type=interactive 的 card 字段）
    """
    # header 颜色：优先使用 resource_refs.header_severity，回退到 message_type 映射
    header_template = _HEADER_TEMPLATE_MAP.get(dto.message_type, "blue")
    severity = dto.resource_refs.get("header_severity") if dto.resource_refs else None
    if severity and severity in _SEVERITY_TO_TEMPLATE:
        header_template = _SEVERITY_TO_TEMPLATE[severity]

    elements: list[dict[str, Any]] = []

    # 1. 摘要（markdown）
    elements.append({
        "tag": "markdown",
        "content": dto.summary,
    })

    # 2. 关键事实（markdown 列表）
    if dto.facts:
        elements.append({"tag": "hr"})
        facts_text = "**关键事实**\n" + "\n".join(
            _format_fact(f) for f in dto.facts
        )
        elements.append({"tag": "markdown", "content": facts_text})

    # 3. 时间线（markdown 列表）
    if dto.timeline:
        elements.append({"tag": "hr"})
        timeline_text = "**时间线**\n" + "\n".join(
            _format_timeline_entry(t) for t in dto.timeline
        )
        elements.append({"tag": "markdown", "content": timeline_text})

    # 4. 条目列表：含 tag 键的结构化元素直接透传，否则走 _format_item 格式化
    if dto.items:
        has_structured = any("tag" in item for item in dto.items)
        if has_structured:
            # 结构化 items：直接作为卡片元素透传（合并卡片场景）
            for item in dto.items:
                elements.append(item)
        else:
            # 普通条目列表
            elements.append({"tag": "hr"})
            items_text = "**命中标的**\n" + "\n".join(
                _format_item(i) for i in dto.items
            )
            elements.append({"tag": "markdown", "content": items_text})

    # 5. 操作按钮（action 元素）
    if dto.actions:
        elements.append({"tag": "hr"})
        for action in dto.actions:
            elements.append(_format_action(action))

    # 6. 免责声明（markdown，灰色提示）
    if dto.disclaimer:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"<font color='grey'>{dto.disclaimer}</font>",
        })

    # 7. 数据时间
    elements.append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": f"数据时间: {dto.data_time}",
            }
        ],
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {
                "tag": "plain_text",
                "content": dto.title,
            },
            "template": header_template,
        },
        "elements": elements,
    }

    return card


def mask_webhook_url(url: str) -> str:
    """脱敏 Webhook URL（用于预览/API 返回）。

    保留协议+域名，路径部分用 *** 替代。

    Args:
        url: 完整 Webhook URL

    Returns:
        脱敏后的 URL（如 "https://open.feishu.cn/***"）
    """
    if not url:
        return ""
    # 保留协议和域名
    if "://" in url:
        protocol, rest = url.split("://", 1)
        if "/" in rest:
            domain = rest.split("/", 1)[0]
            return f"{protocol}://{domain}/***"
        return f"{protocol}://{rest}/***"
    return "***"


if __name__ == "__main__":
    # 自测入口：验证卡片构建
    from app.schemas.notification import NotificationMessageDTO

    dto = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.1.0",
        title="监控事件｜贵州茅台",
        summary="3/3 个策略在 15 分钟内完成确认",
        facts=[
            {"key": "current_price", "label": "当前价格", "value": 1502.3},
            {"key": "confirmed", "label": "确认进度", "value": "3/3"},
        ],
        timeline=[
            {"time": "2026-06-18T10:18:00+08:00", "label": "Node 碰触 POC"},
            {"time": "2026-06-18T10:25:00+08:00", "label": "DSA 方向翻多"},
        ],
        items=[{"name": "贵州茅台(600519)", "rank_value": 0.95}],
        actions=[{"label": "查看个股详情", "url": "/stock-detail?symbol=600519"}],
        resource_refs={"instrument_id": "600519.SH", "plan_id": "monitor_plan_001"},
        data_time="2026-06-18T10:28:00+08:00",
    )

    card = dto_to_feishu_card(dto)
    print(f"header.template={card['header']['template']}")
    print(f"header.title={card['header']['title']['content']}")
    print(f"elements count={len(card['elements'])}")
    assert card["header"]["template"] == "turquoise"
    assert card["header"]["title"]["content"] == "监控事件｜贵州茅台"
    assert len(card["elements"]) > 0

    # 测试 header_severity 动态颜色
    dto_danger = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_merged_event",
        template_version="2.0.0",
        title="BB+节点监控 10:15",
        summary="自选股 17 只 | 触发 1 只",
        resource_refs={"header_severity": "danger"},
        data_time="2026-06-23T10:15:00+08:00",
    )
    card_danger = dto_to_feishu_card(dto_danger)
    assert card_danger["header"]["template"] == "red", f"Expected red, got {card_danger['header']['template']}"
    print(f"header_severity=danger → template={card_danger['header']['template']} ✓")

    dto_warn = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_merged_event",
        template_version="2.0.0",
        title="BB+节点监控 10:20",
        summary="自选股 17 只 | 触发 1 只",
        resource_refs={"header_severity": "warn"},
        data_time="2026-06-23T10:20:00+08:00",
    )
    card_warn = dto_to_feishu_card(dto_warn)
    assert card_warn["header"]["template"] == "orange", f"Expected orange, got {card_warn['header']['template']}"
    print(f"header_severity=warn → template={card_warn['header']['template']} ✓")

    # 测试结构化 items 透传
    dto_struct = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_merged_event",
        template_version="2.0.0",
        title="BB+节点监控 10:15",
        summary="自选股 17 只 | 触发 1 只",
        items=[
            {"tag": "markdown", "content": "**平安银行 000001**"},
            {"tag": "hr"},
            {"tag": "markdown", "content": "🔴 价格触及近期波动上沿"},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": "数据时间: 2026-06-23 10:15"}]},
        ],
        resource_refs={"header_severity": "info"},
        data_time="2026-06-23T10:15:00+08:00",
    )
    card_struct = dto_to_feishu_card(dto_struct)
    # 结构化 items 应直接透传，不包含 "命中标的" 标题
    element_tags = [e.get("tag") for e in card_struct["elements"]]
    assert "hr" in element_tags, f"Expected hr in structured items, got {element_tags}"
    assert "note" in element_tags, f"Expected note in structured items, got {element_tags}"
    print(f"结构化 items 透传: tags={element_tags} ✓")

    # 测试脱敏
    masked = mask_webhook_url("https://open.feishu.cn/open-apis/bot/v2/hook/xxxxx")
    print(f"masked_url={masked}")
    assert masked == "https://open.feishu.cn/***"

    print("OK")
