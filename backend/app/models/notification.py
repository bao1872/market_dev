"""通知 ORM 模型 - 渠道/模板/消息/投递记录。

对应迁移 009_notification：
- notification_channels: 通知渠道（adapter_type 区分飞书等）
- notification_templates: 通知模板（template_key + version + locale 唯一）
- notification_messages: 通知消息（idempotency_key 唯一）
- message_deliveries: 投递记录（DDL 表名：message_deliveries）

设计要点：
- 渠道配置 target_config 为 JSONB，敏感字段（app_secret/sign_secret）在 API 读取时脱敏。
- 模板版本化：template_key + version + locale 唯一，active 状态不可修改。
- 消息幂等：idempotency_key 唯一，防止重复创建。
- 投递幂等：message_deliveries.idempotency_key 唯一，至少一次投递。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import text as sql_text

from app.models.base import Base


class NotificationChannel(Base):
    """通知渠道 - 用户配置的飞书/webhook/email 等投递渠道。

    target_config JSONB 存储渠道配置，敏感字段（app_secret/sign_secret）在 API 读取时脱敏。
    """

    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    adapter_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="feishu_webhook/feishu_platform_app/email"
    )
    display_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="渠道展示名称"
    )
    target_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="渠道配置 JSONB"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="pending",
        comment="pending/active/invalid/disabled/degraded",
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="最近验证时间"
    )
    last_error_code: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="最近错误码"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationChannel(adapter_type={self.adapter_type!r}, "
            f"status={self.status!r})>"
        )


class NotificationTemplate(Base):
    """通知模板 - 版本化模板，active 状态不可修改。

    template_key + version + locale 唯一，新文案发布新版本。
    """

    __tablename__ = "notification_templates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    template_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="模板键（如 monitor_event, system_alert）"
    )
    version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="模板版本号"
    )
    locale: Mapped[str] = mapped_column(
        Text(), nullable=False, default="zh-CN", comment="语言区域"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="draft",
        comment="draft/active/archived，active 不可修改",
    )
    schema: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="模板字段 schema JSONB"
    )
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="模板正文 JSONB"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationTemplate(template_key={self.template_key!r}, "
            f"version={self.version!r}, locale={self.locale!r})>"
        )


class NotificationMessage(Base):
    """通知消息 - 用户消息，幂等键防止重复创建。

    body 存储符合 notification_message.schema.json 的 DTO。
    read_at 非空表示已读。
    """

    __tablename__ = "notification_messages"
    __table_args__ = (
        Index(
            "ix_notification_messages_user_time",
            "user_id",
            sql_text("created_at DESC"),
            "read_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    message_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="MONITOR_EVENT 等"
    )
    template_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="模板键"
    )
    template_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="模板版本"
    )
    source_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="来源类型（如 strategy_run/selection_plan_run）"
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="来源聚合 ID"
    )
    body: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="消息 DTO JSONB"
    )
    idempotency_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="幂等键（唯一）"
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="已读时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<NotificationMessage(message_type={self.message_type!r}, "
            f"template_key={self.template_key!r})>"
        )


class MessageDelivery(Base):
    """消息投递记录 - 每次投递尝试一条记录，幂等键唯一。

    status: success/failed/pending/retrying
    attempt_count: 已尝试次数
    next_attempt_at: 下次重试时间（指数退避）
    """

    __tablename__ = "message_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    notification_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_messages.id"),
        nullable=False,
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notification_channels.id"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="pending",
        comment="pending/success/failed/retrying",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="已尝试次数"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下次重试时间"
    )
    last_error_code: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="最近错误码"
    )
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="渠道返回 JSONB"
    )
    idempotency_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="投递幂等键（唯一）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<MessageDelivery(status={self.status!r}, "
            f"attempt_count={self.attempt_count})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (NotificationChannel, NotificationTemplate, NotificationMessage, MessageDelivery):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} columns={cols}")
        assert "id" in cols
    print(f"NotificationChannel table={NotificationChannel.__tablename__}")
    print(f"NotificationTemplate table={NotificationTemplate.__tablename__}")
    print(f"NotificationMessage table={NotificationMessage.__tablename__}")
    print(f"MessageDelivery table={MessageDelivery.__tablename__}")
    print("OK")
