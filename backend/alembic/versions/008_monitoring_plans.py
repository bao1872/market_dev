"""008 monitoring_plans 及关联表

Revision ID: 008_monitoring_plans
Revises: 007_selection_plans
Create Date: 2026-06-18

监控组合方案完整模型：
- monitoring_plans: 监控方案主表
- monitoring_plan_revisions: 方案版本（mode/窗口/冷却配置）
- monitoring_plan_members: 方案成员（role: TRIGGER/CONFIRM/VETO/OBSERVE）
- monitoring_plan_states: 监控状态（lock_version 乐观锁）
- monitoring_state_evidence: 状态证据链
- composite_monitor_events: 组合监控事件（composite_event_key 唯一）
- composite_event_evidence: 组合事件证据链

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "008_monitoring_plans"
down_revision: str | None = "007_selection_plans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 监控方案主表
    op.create_table(
        "monitoring_plans",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="'draft'"),
        sa.Column("current_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # 方案版本
    op.create_table(
        "monitoring_plan_revisions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("monitoring_plan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("confirmation_window_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ordered", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False, server_default="600"),
        sa.Column("process_event_policy", sa.Text(), nullable=False, server_default="'IN_APP_ONLY'"),
        sa.Column("notification_config", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["monitoring_plan_id"], ["monitoring_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("monitoring_plan_id", "revision"),
        sa.CheckConstraint("mode IN ('INDEPENDENT','ANY','ALL')", name="monitoring_plan_revisions_mode_check"),
    )

    # 方案成员
    op.create_table(
        "monitoring_plan_members",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("revision_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_definition_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version_policy", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("params", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("conditions", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.ForeignKeyConstraint(["revision_id"], ["monitoring_plan_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_definition_id"], ["strategy_definitions.id"]),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "position"),
        sa.CheckConstraint("version_policy IN ('PINNED','STABLE_TRACK')", name="monitoring_plan_members_version_policy_check"),
        sa.CheckConstraint("role IN ('TRIGGER','CONFIRM','VETO','OBSERVE')", name="monitoring_plan_members_role_check"),
    )

    # 监控状态（lock_version 乐观锁）
    op.create_table(
        "monitoring_plan_states",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("monitoring_plan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_member_ids", sa.dialects.postgresql.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("vetoed_by_member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("state_payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["monitoring_plan_id"], ["monitoring_plans.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["monitoring_plan_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "revision_id", "instrument_id"),
    )
    op.create_index(
        "ix_monitor_state_expiry",
        "monitoring_plan_states",
        ["status", "window_deadline_at", "cooldown_until"],
    )

    # 状态证据链
    op.create_table(
        "monitoring_state_evidence",
        sa.Column("state_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("summary", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["state_id"], ["monitoring_plan_states.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["monitoring_plan_members.id"]),
        sa.ForeignKeyConstraint(["strategy_event_id"], ["strategy_events.id"]),
        sa.PrimaryKeyConstraint("state_id", "member_id", "strategy_event_id"),
    )

    # 组合监控事件
    op.create_table(
        "composite_monitor_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("monitoring_plan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("composite_event_key", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["monitoring_plan_id"], ["monitoring_plans.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["monitoring_plan_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("composite_event_key"),
    )
    op.create_index(
        "ix_composite_event_user_time",
        "composite_monitor_events",
        ["user_id", sa.text("event_time DESC")],
    )

    # 组合事件证据链（DDL 表名：composite_event_evidence）
    op.create_table(
        "composite_event_evidence",
        sa.Column("composite_event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_event_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("summary", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["composite_event_id"], ["composite_monitor_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["monitoring_plan_members.id"]),
        sa.ForeignKeyConstraint(["strategy_event_id"], ["strategy_events.id"]),
        sa.PrimaryKeyConstraint("composite_event_id", "member_id", "strategy_event_id"),
    )


def downgrade() -> None:
    op.drop_table("composite_event_evidence")
    op.drop_index("ix_composite_event_user_time", table_name="composite_monitor_events")
    op.drop_table("composite_monitor_events")
    op.drop_table("monitoring_state_evidence")
    op.drop_index("ix_monitor_state_expiry", table_name="monitoring_plan_states")
    op.drop_table("monitoring_plan_states")
    op.drop_table("monitoring_plan_members")
    op.drop_table("monitoring_plan_revisions")
    op.drop_table("monitoring_plans")
