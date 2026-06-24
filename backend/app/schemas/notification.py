"""通知消息 Pydantic schemas - 符合 notification_message.schema.json 的 DTO。

提供：
- NotificationMessageDTO: 统一消息 DTO（站内/网页预览/飞书卡片共用）
- DeliveryResult: 投递结果
- NotificationChannelResponse/Request: 渠道响应/请求
- NotificationMessageResponse: 消息响应

DTO 字段对齐 doc/.../schemas/notification_message.schema.json：
- message_type: MONITOR_EVENT/MONITOR_MEMBER_EVENT/SYSTEM_ALERT/CHANNEL_ALERT
- template_key + template_version: 模板标识
- title/summary: 标题与摘要
- facts/timeline/items/actions: 结构化内容
- resource_refs: 资源引用（instrument_id/plan_id/event_id 等）
- data_time: 数据时间
- disclaimer: 免责声明
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# 允许的 message_type 枚举（对齐 schema）
MESSAGE_TYPES = {
    "MONITOR_EVENT",
    "MONITOR_MEMBER_EVENT",
    "SYSTEM_ALERT",
    "CHANNEL_ALERT",
}


def mask_target_config(adapter_type: str, config: dict[str, Any]) -> dict[str, Any]:
    """脱敏 target_config 中的敏感字段。

    - feishu_platform_app: app_secret 脱敏为 ****xxxx（末4位），其余字段保留明文
    - 其他类型: 原样返回
    """
    if not config:
        return {}
    masked = dict(config)
    if adapter_type == "feishu_platform_app" and "app_secret" in masked:
        secret = str(masked["app_secret"])
        if len(secret) > 4:
            masked["app_secret"] = f"****{secret[-4:]}"
        else:
            masked["app_secret"] = "****"
    return masked


class NotificationMessageDTO(BaseModel):
    """统一通知消息 DTO - 符合 notification_message.schema.json。

    站内消息、网页预览、飞书卡片由同一 DTO 渲染。
    Adapter 不新增业务字段，仅按渠道格式化。
    """

    message_type: str = Field(..., description="消息类型枚举")
    template_key: str = Field(..., description="模板键")
    template_version: str = Field(..., description="模板版本")
    locale: str = Field(default="zh-CN", description="语言区域")
    title: str = Field(..., description="标题")
    summary: str = Field(..., description="摘要")
    facts: list[dict[str, Any]] = Field(default_factory=list, description="关键事实列表")
    timeline: list[dict[str, Any]] = Field(default_factory=list, description="时间线")
    items: list[dict[str, Any]] = Field(default_factory=list, description="条目列表")
    actions: list[dict[str, Any]] = Field(default_factory=list, description="操作按钮")
    resource_refs: dict[str, Any] = Field(..., description="资源引用")
    data_time: str = Field(..., description="数据时间 ISO8601")
    disclaimer: str = Field(
        default="仅展示规则触发与历史数据，不构成投资建议。",
        description="免责声明",
    )

    def validate_message_type(self) -> None:
        """校验 message_type 在允许枚举内。"""
        if self.message_type not in MESSAGE_TYPES:
            raise ValueError(
                f"message_type 必须为 {MESSAGE_TYPES} 之一，当前: {self.message_type}"
            )


class DeliveryResult(BaseModel):
    """投递结果 - ChannelAdapter.send 返回。"""

    success: bool = Field(..., description="是否成功")
    error_code: str | None = Field(None, description="错误码")
    error_message: str | None = Field(None, description="错误信息")
    provider_response: dict[str, Any] | None = Field(None, description="渠道返回")


class NotificationChannelResponse(BaseModel):
    """通知渠道响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="渠道 ID")
    user_id: UUID = Field(..., description="用户 ID")
    adapter_type: str = Field(..., description="feishu_webhook/feishu_platform_app/email")
    display_name: str = Field(..., description="渠道名称")
    target_config: dict[str, Any] = Field(default_factory=dict, description="渠道配置（敏感字段脱敏）")
    status: str = Field(..., description="pending/active/invalid/disabled/degraded")
    last_verified_at: datetime | None = Field(None, description="最近验证时间")
    last_error_code: str | None = Field(None, description="最近错误码")
    created_at: datetime = Field(..., description="创建时间")


class CreateChannelRequest(BaseModel):
    """创建通知渠道请求。"""

    adapter_type: str = Field(..., description="feishu_webhook/feishu_platform_app/email")
    display_name: str = Field(..., description="渠道名称")
    target_config: dict[str, Any] = Field(..., description="渠道配置（webhook URL 等）")


class UpdateChannelRequest(BaseModel):
    """更新通知渠道请求。"""

    display_name: str | None = None
    target_config: dict[str, Any] | None = None


class NotificationMessageResponse(BaseModel):
    """通知消息响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="消息 ID")
    user_id: UUID = Field(..., description="用户 ID")
    message_type: str = Field(..., description="消息类型")
    template_key: str = Field(..., description="模板键")
    template_version: str = Field(..., description="模板版本")
    source_type: str = Field(..., description="来源类型")
    source_id: UUID | None = Field(None, description="来源 ID")
    body: dict[str, Any] = Field(..., description="消息 DTO JSONB")
    read_at: datetime | None = Field(None, description="已读时间")
    created_at: datetime = Field(..., description="创建时间")


class NotificationMessageListResponse(BaseModel):
    """消息列表响应。"""

    items: list[NotificationMessageResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class NotificationPreviewRequest(BaseModel):
    """消息预览请求 - 构建消息 DTO 并返回多渠道渲染结果。

    网页预览与真实投递共享同一 DTO（spec 要求）。
    """

    message_type: str = Field(..., description="消息类型枚举")
    context: dict[str, Any] = Field(
        ..., description="上下文字典（title/summary/facts/timeline/items/actions/resource_refs/data_time）"
    )
    locale: str = Field(default="zh-CN", description="语言区域")


class NotificationPreviewResponse(BaseModel):
    """消息预览响应 - 渠道无关 DTO + 站内渲染 + 飞书 card JSON。

    网页预览与真实投递共享同一 DTO，确保内容一致。
    """

    dto: NotificationMessageDTO = Field(..., description="渠道无关统一消息 DTO")
    in_app: dict[str, Any] = Field(..., description="站内渲染模型（网页展示用）")
    feishu_card: dict[str, Any] = Field(..., description="飞书 interactive card JSON")


class NotificationChannelListResponse(BaseModel):
    """渠道列表响应。"""

    items: list[NotificationChannelResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class ChannelTestResponse(BaseModel):
    """渠道测试响应。"""

    channel: NotificationChannelResponse = Field(..., description="渠道状态")
    delivery: DeliveryResult = Field(..., description="投递结果")


if __name__ == "__main__":
    # 自测入口：验证 DTO 字段与示例
    print(f"NotificationMessageDTO fields={list(NotificationMessageDTO.model_fields.keys())}")
    print(f"DeliveryResult fields={list(DeliveryResult.model_fields.keys())}")

    # 使用示例验证
    dto = NotificationMessageDTO(
        message_type="MONITOR_EVENT",
        template_key="monitor_event",
        template_version="1.1.0",
        title="监控事件｜贵州茅台",
        summary="3/3 个策略在 15 分钟内完成确认",
        facts=[{"key": "current_price", "label": "当前价格", "value": 1502.3}],
        timeline=[{"time": "2026-06-18T10:18:00+08:00", "label": "Node 碰触 POC"}],
        resource_refs={"instrument_id": "600519.SH", "plan_id": "monitor_plan_001"},
        data_time="2026-06-18T10:28:00+08:00",
    )
    dto.validate_message_type()
    print(f"DTO 构建成功: title={dto.title}")
    print("OK")
