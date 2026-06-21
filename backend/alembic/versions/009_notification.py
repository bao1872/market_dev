"""009 notification 相关表

Revision ID: 009_notification
Revises: 008_monitoring_plans
Create Date: 2026-06-18

通知与消息基础设施：
- notification_channels: 通知渠道（adapter_type 区分飞书等）
- notification_templates: 通知模板（template_key + version + locale 唯一）
- notification_messages: 通知消息（idempotency_key 唯一）
- message_deliveries: 投递记录（DDL 表名：message_deliveries）

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "009_notification"
down_revision: str | None = "008_monitoring_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 通知渠道
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("adapter_type", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("target_config", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("secret_ref", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # 通知模板
    op.create_table(
        "notification_templates",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("template_key", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("schema", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("body", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("template_key", "version", "locale"),
    )

    # 通知消息
    op.create_table(
        "notification_messages",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("template_key", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("source_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_notification_messages_user_time",
        "notification_messages",
        ["user_id", sa.text("created_at DESC"), "read_at"],
    )

    # 投递记录（DDL 表名：message_deliveries）
    op.create_table(
        "message_deliveries",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("notification_message_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("provider_response", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["notification_message_id"], ["notification_messages.id"]),
        sa.ForeignKeyConstraint(["channel_id"], ["notification_channels.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("message_deliveries")
    op.drop_index("ix_notification_messages_user_time", table_name="notification_messages")
    op.drop_table("notification_messages")
    op.drop_table("notification_templates")
    op.drop_table("notification_channels")
