"""Pure event and DTO contract for administrator beta-application alerts."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.models.beta_application import BetaApplication
from app.schemas.notification import NotificationMessageDTO

BETA_APPLICATION_ADMIN_EVENT = "beta_application.admin_notification.created"

_REASON_CODE_LABELS: dict[str, str] = {
    "busy": "工作忙，没时间盯盘",
    "too_many": "股票太多，看不过来",
    "forget": "容易忘记盯盘",
    "quant": "量化研究需要",
    "other": "其他",
}
_ADMIN_ENTRY_PATH = "/admin/beta-applications"


def _format_submitted_at(submitted_at: datetime | None) -> str:
    if submitted_at is None:
        return "未知"
    try:
        cst = ZoneInfo("Asia/Shanghai")
        local_dt = (
            submitted_at.astimezone(cst)
            if submitted_at.tzinfo
            else submitted_at.replace(tzinfo=cst)
        )
        return local_dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return submitted_at.strftime("%Y-%m-%d %H:%M:%S")


def build_beta_application_dto(application: BetaApplication) -> NotificationMessageDTO:
    """Build the frozen administrator notification DTO without I/O."""
    submitted_at_str = _format_submitted_at(application.submitted_at)
    facts: list[dict[str, Any]] = [
        {"key": "application_id", "label": "申请编号", "value": str(application.id)},
        {"key": "submitted_at", "label": "提交时间", "value": submitted_at_str},
        {"key": "wechat", "label": "微信号", "value": application.wechat or "未填写"},
        {"key": "phone", "label": "手机号", "value": application.phone or "未填写"},
        {
            "key": "watch_stock_count",
            "label": "盯盘股票数量",
            "value": application.watch_stock_count,
        },
    ]
    reason_label = _REASON_CODE_LABELS.get(
        application.reason_code,
        application.reason_code,
    )
    facts.append({"key": "reason_code", "label": "使用理由", "value": reason_label})
    if application.reason_other:
        facts.append(
            {"key": "reason_other", "label": "其他补充", "value": application.reason_other}
        )

    return NotificationMessageDTO(
        message_type="SYSTEM_ALERT",
        template_key="beta_application_admin",
        template_version="1.0.0",
        title="新的内测申请",
        summary="收到一份新的内测申请，请及时处理。",
        facts=facts,
        actions=[{"label": "查看后台详情", "url": _ADMIN_ENTRY_PATH}],
        resource_refs={"application_id": str(application.id)},
        data_time=submitted_at_str,
        disclaimer="本通知由系统自动发送，请勿直接回复。",
    )


__all__ = ["BETA_APPLICATION_ADMIN_EVENT", "build_beta_application_dto"]
