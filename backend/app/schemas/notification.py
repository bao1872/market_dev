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

注意：MONITOR_MEMBER_EVENT 为【仅历史兼容】枚举值，仅用于读取历史消息，
新代码禁止生成（advice.md 第十一节遗留清理）。新消息统一用 MONITOR_EVENT。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 允许的 message_type 枚举（对齐 schema）
# 注意：MONITOR_MEMBER_EVENT 为【仅历史兼容】枚举值，仅用于读取历史消息，
# 新代码禁止生成；保留枚举值以保证历史消息可读（advice.md 第十一节遗留清理）
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
    # [消息中心] - 结构化字段：前端表格直接展示，避免从 Markdown/富文本解析
    strategy_key: str | None = Field(None, description="策略键（如 watchlist_monitor）")
    strategy_name: str | None = Field(None, description="策略展示名称")
    instrument_count: int | None = Field(None, description="涉及标的数量")
    primary_instrument: dict[str, Any] | None = Field(
        None, description="主要标的（instrument_id/symbol/name）",
    )
    event_summary: str | None = Field(None, description="事件摘要（事件类型/边界等）")
    # [飞书两段式投递] - 纯文本消息内容（delivery_type=text 时使用）
    # 仅 build_monitor_event_text 填充；card 投递忽略此字段
    text_content: str | None = Field(
        None, description="纯文本消息内容（文本投递专用，含触发时间/现价/BB/节点/POC/位置）",
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


class MessageDeliveryResponse(BaseModel):
    """消息投递记录响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="投递记录 ID")
    channel_id: UUID = Field(..., description="渠道 ID")
    notification_message_id: UUID = Field(..., description="关联消息 ID")
    adapter_type: str = Field(..., description="渠道类型（feishu_webhook/feishu_platform_app/email）")
    display_name: str = Field(..., description="渠道展示名称")
    status: str = Field(..., description="pending/success/failed/retrying")
    delivery_type: str = Field(default="text", description="text/image/card")
    attempt_count: int = Field(..., description="已尝试次数")
    next_retry_at: datetime | None = Field(None, description="下次重试时间")
    last_error_code: str | None = Field(None, description="最近错误码")
    message_group_id: str | None = Field(None, description="消息组 ID（关联同一事件的 text+image 两条投递）")
    created_at: datetime = Field(..., description="创建时间")
    # [消息投递管理] - 从关联消息提取的摘要信息，便于 admin 页面展示失败投递对应的股票/事件
    message_summary: str | None = Field(None, description="消息摘要")
    primary_instrument: dict[str, Any] | None = Field(None, description="主要标的")

    @model_validator(mode="before")
    @classmethod
    def _extract_channel_info(cls, data: Any) -> Any:
        """从关联的 NotificationChannel 提取 adapter_type 与 display_name，
        并从 NotificationMessage 提取摘要与主要标的。

        输入为 ORM 对象时转换为 dict，确保 Pydantic 能读取到关联实体上的字段。
        """
        if hasattr(data, "id") and not isinstance(data, dict):
            channel = getattr(data, "channel", None)
            message = getattr(data, "message", None)
            message_body = getattr(message, "body", None) or {}
            primary_instrument = message_body.get("primary_instrument")
            message_summary = message_body.get("summary") or message_body.get("title")
            return {
                "id": getattr(data, "id", None),
                "channel_id": getattr(data, "channel_id", None),
                "notification_message_id": getattr(data, "notification_message_id", None),
                "adapter_type": (
                    getattr(channel, "adapter_type", "")
                    if channel else getattr(data, "adapter_type", "")
                ),
                "display_name": (
                    getattr(channel, "display_name", "")
                    if channel else getattr(data, "display_name", "")
                ),
                "status": getattr(data, "status", ""),
                "delivery_type": getattr(data, "delivery_type", "text"),
                "attempt_count": getattr(data, "attempt_count", 0),
                "next_retry_at": getattr(data, "next_retry_at", None),
                "last_error_code": getattr(data, "last_error_code", None),
                "message_group_id": getattr(data, "message_group_id", None),
                "created_at": getattr(data, "created_at", None),
                "message_summary": message_summary,
                "primary_instrument": primary_instrument,
            }
        return data


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
    deliveries: list[MessageDeliveryResponse] = Field(
        default_factory=list, description="关联投递记录",
    )
    read_at: datetime | None = Field(None, description="已读时间")
    created_at: datetime = Field(..., description="创建时间")
    # [消息中心] - 结构化字段：从 body 提取，新老消息兼容
    strategy_key: str | None = Field(None, description="策略键")
    strategy_name: str | None = Field(None, description="策略展示名称")
    instrument_count: int | None = Field(None, description="涉及标的数量")
    primary_instrument: dict[str, Any] | None = Field(None, description="主要标的")
    event_summary: str | None = Field(None, description="事件摘要")

    @model_validator(mode="after")
    def _extract_structured_fields(self) -> NotificationMessageResponse:
        """从 body 提取结构化字段（兼容旧消息未填充的情况）。"""
        body = self.body or {}
        if self.strategy_key is None:
            self.strategy_key = body.get("strategy_key")
        if self.strategy_name is None:
            self.strategy_name = body.get("strategy_name")
        if self.instrument_count is None:
            self.instrument_count = body.get("instrument_count")
        if self.primary_instrument is None:
            self.primary_instrument = body.get("primary_instrument")
        if self.event_summary is None:
            self.event_summary = body.get("event_summary")
        return self


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


class ChannelLatestEventTestResponse(BaseModel):
    """真实事件图片测试响应。

    仅保留核心字段：事件 ID、股票代码、消息 ID、投递状态。
    不返回 capture_token 等敏感/冗余信息。
    """

    event_id: UUID = Field(..., description="事件 ID")
    symbol: str = Field(..., description="股票代码")
    message_id: UUID = Field(..., description="创建的通知消息 ID")
    delivery_status: str = Field(..., description="投递状态（pending/success/failed/retrying/dead）")


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
