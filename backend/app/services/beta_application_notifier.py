"""内测申请管理员飞书通知器 - 构建卡片 + Outbox 写入（spec 第四节）。

职责：
- build_beta_application_card(application): 构建飞书互动卡片（含申请编号/提交时间/
  微信号/手机号/盯盘数/理由/其他补充/后台入口）
- send_admin_notification(db, application): 通过 Outbox 写入 beta_application_admin 事件，
  设置 feishu_delivery_status='pending'。失败仅 logger.error，不影响用户提交

设计要点：
- 复用 feishu_card_builder.dto_to_feishu_card 渲染逻辑，保持卡片风格一致
- message_type=SYSTEM_ALERT（红色头部，符合"管理员需注意的新申请"语义）
- payload 只含申请数据，不含 webhook 配置（安全考虑，webhook 由 relay 运行时从
  system_channel 读取）
- 使用 savepoint 隔离 Outbox 写入，失败时回滚 savepoint 但不影响已提交的申请

用法:
    from app.services.beta_application_notifier import send_admin_notification

    await send_admin_notification(db, application)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beta_application import BetaApplication
from app.schemas.notification import NotificationMessageDTO
from app.services.feishu_card_builder import dto_to_feishu_card
from app.services.outbox_relay import write_outbox

logger = logging.getLogger("beta_application_notifier")

# Outbox 事件类型（与 beta_application_service 共享，单一权威定义）
BETA_APPLICATION_ADMIN_EVENT = "beta_application_admin"

# reason_code → 中文标签映射（飞书通知展示用）
# 仅在此模块定义，因为 constants/beta_application.py 只维护代码枚举，
# 中文标签属于展示层，且飞书通知是特定展示场景
_REASON_CODE_LABELS: dict[str, str] = {
    "busy": "工作忙，没时间盯盘",
    "too_many": "股票太多，看不过来",
    "forget": "容易忘记盯盘",
    "quant": "量化研究需要",
    "other": "其他",
}

# 管理员后台入口路径（飞书卡片 action button url）
_ADMIN_ENTRY_PATH = "/admin/beta-applications"


def _format_submitted_at(submitted_at: datetime | None) -> str:
    """格式化提交时间为可读字符串。

    Args:
        submitted_at: 提交时间（带时区）

    Returns:
        格式化后的字符串（如 "2026-06-28 14:30:00"），None 返回 "未知"
    """
    if submitted_at is None:
        return "未知"
    # 转为本地时区显示
    try:
        from zoneinfo import ZoneInfo

        cst = ZoneInfo("Asia/Shanghai")
        local_dt = submitted_at.astimezone(cst) if submitted_at.tzinfo else submitted_at.replace(tzinfo=cst)
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return submitted_at.strftime("%Y-%m-%d %H:%M:%S")


def build_beta_application_dto(application: BetaApplication) -> NotificationMessageDTO:
    """构建内测申请通知的 NotificationMessageDTO。

    复用现有 DTO + dto_to_feishu_card 渲染逻辑，保持卡片风格一致。
    message_type=SYSTEM_ALERT（红色头部，管理员需注意的新申请）。

    公开函数：outbox_relay 投递时调用，传入 FeishuWebhookAdapter.send。

    Args:
        application: BetaApplication ORM 对象

    Returns:
        NotificationMessageDTO（可传入 dto_to_feishu_card 或 FeishuWebhookAdapter.send）
    """
    submitted_at_str = _format_submitted_at(application.submitted_at)

    # 构建关键事实列表（spec 要求：申请编号/提交时间/微信号/手机号/盯盘数/理由/其他补充）
    facts: list[dict[str, Any]] = [
        {"key": "application_id", "label": "申请编号", "value": str(application.id)},
        {"key": "submitted_at", "label": "提交时间", "value": submitted_at_str},
        {"key": "wechat", "label": "微信号", "value": application.wechat or "未填写"},
        {"key": "phone", "label": "手机号", "value": application.phone or "未填写"},
        {"key": "watch_stock_count", "label": "盯盘股票数量", "value": application.watch_stock_count},
    ]

    # 使用理由（中文标签）
    reason_label = _REASON_CODE_LABELS.get(application.reason_code, application.reason_code)
    facts.append({"key": "reason_code", "label": "使用理由", "value": reason_label})

    # 其他补充说明（仅 reason_code='other' 或有 reason_other 时显示）
    if application.reason_other:
        facts.append({"key": "reason_other", "label": "其他补充", "value": application.reason_other})

    # 后台入口 action button
    actions: list[dict[str, Any]] = [
        {"label": "查看后台详情", "url": _ADMIN_ENTRY_PATH},
    ]

    return NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="beta_application_admin",
        template_version="1.0.0",
        title="新的内测申请",
        summary="收到一份新的内测申请，请及时处理。",
        facts=facts,
        actions=actions,
        resource_refs={"application_id": str(application.id)},
        data_time=submitted_at_str,
        disclaimer="本通知由系统自动发送，请勿直接回复。",
    )


def build_beta_application_card(application: BetaApplication) -> dict[str, Any]:
    """构建飞书互动卡片（spec 第四节要求的所有字段）。

    卡片内容：申请编号/提交时间/微信号/手机号/盯盘股票数量/使用理由/其他补充/后台入口。
    管理员飞书是系统级内部通知，需完整联系方式以便管理员联系用户。

    Args:
        application: BetaApplication ORM 对象

    Returns:
        飞书 interactive card JSON（可直接作为 msg_type=interactive 的 card 字段）
    """
    dto = build_beta_application_dto(application)
    return dto_to_feishu_card(dto)


async def send_admin_notification(
    db: AsyncSession,
    application: BetaApplication,
) -> None:
    """通过 Outbox 写入 beta_application_admin 事件（best-effort）。

    spec 要求：先 DB 后 Outbox，Outbox 失败不影响用户提交。
    使用独立事务（begin_nested savepoint）隔离 Outbox 写入，失败时回滚
    savepoint 但不影响已提交的申请。

    流程：
    1. 设置 application.feishu_delivery_status='pending'
    2. write_outbox（savepoint 内，payload 含申请数据，不含 webhook 配置）
    3. commit（提交 pending 状态 + outbox 记录）
    4. 失败：设置 feishu_delivery_status='failed' + feishu_last_error，commit

    Args:
        db: 异步数据库会话
        application: 已提交的申请对象（已 commit，仍 attached 到 session）
    """
    payload: dict[str, Any] = {
        "application_id": str(application.id),
        "wechat": application.wechat,
        "phone": application.phone,
        "watch_stock_count": application.watch_stock_count,
        "reason_code": application.reason_code,
        "reason_other": application.reason_other,
        "submitted_at": application.submitted_at.isoformat() if application.submitted_at else None,
        "source": application.source,
    }
    try:
        # 设置 pending 状态（在外层事务，savepoint 失败时回滚到此处之前）
        application.feishu_delivery_status = "pending"
        application.feishu_last_error = None

        # write_outbox 在 savepoint 内，失败时仅回滚 savepoint
        async with db.begin_nested():
            await write_outbox(
                db=db,
                event_type=BETA_APPLICATION_ADMIN_EVENT,
                payload=payload,
                aggregate_type="beta_application",
                aggregate_id=application.id,
            )
        await db.commit()
    except Exception as e:
        # Outbox 写入失败：记录日志，不影响已提交的申请
        # 补充上下文后继续（不 re-raise，因为申请已成功提交）
        logger.error(
            "[BetaApplicationNotifier] Outbox 写入失败 app_id=%s: %s",
            application.id, e,
        )
        # best-effort：尝试更新状态为 failed
        try:
            application.feishu_delivery_status = "failed"
            application.feishu_last_error = f"outbox write failed: {e}"
            await db.commit()
        except Exception as commit_err:
            logger.error(
                "[BetaApplicationNotifier] 状态更新失败 app_id=%s: %s",
                application.id, commit_err,
            )
            try:
                await db.rollback()
            except Exception as rollback_err:
                logger.error(
                    "[BetaApplicationNotifier] rollback 异常 app_id=%s: %s",
                    application.id, rollback_err,
                )


if __name__ == "__main__":
    # 自测入口：验证卡片构建（不连接 DB）
    import uuid as uuid_module

    app = BetaApplication(
        id=uuid_module.uuid4(),
        wechat="test_wechat_id",
        phone="13800138000",
        watch_stock_count=15,
        reason_code="busy",
        reason_other=None,
        source="landing_page",
        submitted_at=datetime.now(),
    )

    card = build_beta_application_card(app)
    print(f"header.title={card['header']['title']['content']}")
    print(f"header.template={card['header']['template']}")
    print(f"elements_count={len(card['elements'])}")

    # 验证卡片结构
    assert card["header"]["title"]["content"] == "新的内测申请"
    assert card["header"]["template"] == "red"  # SYSTEM_ALERT → red

    # 验证 facts 包含所有必需字段
    import json
    card_text = json.dumps(card, ensure_ascii=False)
    required_fields = ["申请编号", "提交时间", "微信号", "手机号", "盯盘", "理由"]
    for field in required_fields:
        assert field in card_text, f"卡片缺少字段: {field}"
    print("required_fields: OK")

    # 验证 reason_code='other' 时显示补充说明
    app_other = BetaApplication(
        id=uuid_module.uuid4(),
        wechat="other_user",
        phone=None,
        watch_stock_count=10,
        reason_code="other",
        reason_other="我的特殊需求",
        source="landing_page",
        submitted_at=datetime.now(),
    )
    card_other = build_beta_application_card(app_other)
    card_other_text = json.dumps(card_other, ensure_ascii=False)
    assert "其他补充" in card_other_text
    assert "我的特殊需求" in card_other_text
    print("reason_other: OK")

    # 验证后台入口
    assert "查看后台详情" in card_text
    assert "/admin/beta-applications" in card_text
    print("admin_entry: OK")

    # 验证函数签名
    import inspect
    assert inspect.iscoroutinefunction(send_admin_notification)
    sig = inspect.signature(send_admin_notification)
    params = list(sig.parameters.keys())
    assert params == ["db", "application"], f"参数不匹配: {params}"
    print(f"send_admin_notification params={params}")

    print("OK")
