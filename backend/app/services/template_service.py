"""模板服务 - 通知模板版本管理与渲染。

设计：
- get_template(db, key, version, locale): 获取模板（active 优先）
- render_template(template, context): 渲染模板，返回 NotificationMessageDTO 字段

模板版本化：
- template_key + version + locale 唯一
- active 状态不可修改，新文案发布新版本
- 渲染时使用模板 body 中的字段定义，结合 context 填充

当前实现：
- render_template 从 template.body 提取 title/summary/facts 等字段的模板字符串
- 使用 str.format_map 渲染（context 中的键值替换占位符 {key}）
- 后续可扩展为 Jinja2 等模板引擎
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationTemplate


class TemplateNotFoundError(ValueError):
    """模板不存在。"""


class TemplateRenderError(ValueError):
    """模板渲染失败。"""


async def get_template(
    db: AsyncSession,
    key: str,
    version: str | None = None,
    locale: str = "zh-CN",
) -> NotificationTemplate:
    """获取通知模板。

    Args:
        db: 异步会话
        key: 模板键（如 monitoring_plan_confirmed）
        version: 模板版本，None 则返回 active 状态的最新版本
        locale: 语言区域

    Returns:
        NotificationTemplate

    Raises:
        TemplateNotFoundError: 模板不存在
    """
    if version is not None:
        # 指定版本：精确查找
        stmt = select(NotificationTemplate).where(
            NotificationTemplate.template_key == key,
            NotificationTemplate.version == version,
            NotificationTemplate.locale == locale,
        )
    else:
        # 未指定版本：返回 active 状态的模板
        stmt = (
            select(NotificationTemplate)
            .where(
                NotificationTemplate.template_key == key,
                NotificationTemplate.locale == locale,
                NotificationTemplate.status == "active",
            )
            .order_by(NotificationTemplate.version.desc())
            .limit(1)
        )

    result = await db.execute(stmt)
    template = result.scalar_one_or_none()
    if template is None:
        raise TemplateNotFoundError(
            f"模板不存在: key={key}, version={version}, locale={locale}"
        )
    return template


def render_template(
    template: NotificationTemplate,
    context: dict[str, Any],
) -> dict[str, Any]:
    """渲染模板，返回 DTO 字段字典。

    模板 body 结构（JSONB）：
    {
        "title": "监控组合确认｜{stock_name}",
        "summary": "{confirmed}/{total} 个策略在 {window} 分钟内确认",
        "facts": [...],
        "actions": [...],
        "disclaimer": "..."
    }

    渲染规则：
    - 字符串字段使用 str.format_map(context) 替换占位符
    - 列表/字典字段原样返回（由 message_builder 负责填充动态内容）
    - context 中不存在的占位符保留原样（不报错）

    Args:
        template: 通知模板 ORM 对象
        context: 渲染上下文

    Returns:
        渲染后的字段字典（title, summary, facts, actions, disclaimer 等）

    Raises:
        TemplateRenderError: 渲染失败
    """
    body = template.body or {}
    rendered: dict[str, Any] = {}

    for field_name, field_value in body.items():
        if isinstance(field_value, str):
            # 字符串字段：替换占位符
            try:
                rendered[field_name] = _safe_format(field_value, context)
            except (KeyError, IndexError, ValueError) as e:
                raise TemplateRenderError(
                    f"渲染字段 {field_name} 失败: {e} (template={template.template_key} v{template.version})"
                ) from e
        else:
            # 非字符串字段（list/dict）：原样返回
            rendered[field_name] = field_value

    return rendered


def _safe_format(template_str: str, context: dict[str, Any]) -> str:
    """安全格式化字符串，缺失的占位符保留原样。

    使用 FormatPlaceholder 字典子类，缺失键返回 {key} 原样。
    """
    class _SafeDict(dict):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    safe_context = _SafeDict(context)
    return template_str.format_map(safe_context)


async def create_template(
    db: AsyncSession,
    template_key: str,
    version: str,
    locale: str,
    schema: dict[str, Any],
    body: dict[str, Any],
    status: str = "draft",
) -> NotificationTemplate:
    """创建通知模板。

    Args:
        db: 异步会话
        template_key: 模板键
        version: 版本号
        locale: 语言区域
        schema: 模板字段 schema
        body: 模板正文（含占位符的字段定义）
        status: 初始状态（draft/active）

    Returns:
        NotificationTemplate
    """
    template = NotificationTemplate(
        template_key=template_key,
        version=version,
        locale=locale,
        status=status,
        schema=schema,
        body=body,
    )
    db.add(template)
    await db.flush()
    return template


if __name__ == "__main__":
    # 自测入口：验证渲染逻辑（不连接 DB）
    from unittest.mock import MagicMock

    # 构造 mock 模板
    mock_template = MagicMock(spec=NotificationTemplate)
    mock_template.template_key = "monitoring_plan_confirmed"
    mock_template.version = "1.1.0"
    mock_template.body = {
        "title": "监控组合确认｜{stock_name}",
        "summary": "{confirmed}/{total} 个策略在 {window} 分钟内确认",
        "facts": [{"key": "current_price", "label": "当前价格"}],
        "disclaimer": "仅展示规则触发与历史数据，不构成投资建议。",
    }

    context = {
        "stock_name": "贵州茅台",
        "confirmed": 3,
        "total": 3,
        "window": 15,
    }
    rendered = render_template(mock_template, context)
    print(f"rendered title={rendered['title']}")
    print(f"rendered summary={rendered['summary']}")
    assert "贵州茅台" in rendered["title"]
    assert "3/3" in rendered["summary"]
    assert "15" in rendered["summary"]

    # 测试缺失占位符保留原样
    context_partial = {"stock_name": "测试"}
    rendered_partial = render_template(mock_template, context_partial)
    print(f"partial title={rendered_partial['title']}")
    assert "{confirmed}" in rendered_partial["summary"]  # 缺失占位符保留
    print("OK")
