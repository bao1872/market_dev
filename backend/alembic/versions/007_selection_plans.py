"""007 selection_plans 及关联表

Revision ID: 007_selection_plans
Revises: 006_monitor_states
Create Date: 2026-06-18

选股组合方案完整模型：
- selection_plans: 选股方案主表
- selection_plan_revisions: 方案版本（revision 递增）
- selection_plan_members: 方案成员（绑定策略版本）
- selection_member_conditions: 成员条件（注意：DDL 表名为 selection_member_conditions）
- selection_plan_runs: 方案运行记录（idempotency_key 唯一）
- selection_plan_results: 方案运行结果（唯一约束 plan_run + instrument）
- selection_result_evidence: 结果证据链（注意：DDL 表名为 selection_result_evidence）

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "007_selection_plans"
down_revision: str | None = "006_monitor_states"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 选股方案主表
    op.create_table(
        "selection_plans",
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
    op.create_index("ix_selection_plans_user", "selection_plans", ["user_id", "status"])

    # 方案版本
    op.create_table(
        "selection_plan_revisions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("selection_plan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("missing_member_policy", sa.Text(), nullable=False, server_default="'FAIL_CLOSED'"),
        sa.Column("universe", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("sort_spec", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("notification_config", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["selection_plan_id"], ["selection_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("selection_plan_id", "revision"),
        sa.CheckConstraint("operator IN ('ALL','ANY')", name="selection_plan_revisions_operator_check"),
        sa.CheckConstraint(
            "missing_member_policy IN ('FAIL_CLOSED','IGNORE_MEMBER')",
            name="selection_plan_revisions_missing_member_policy_check",
        ),
    )

    # 方案成员
    op.create_table(
        "selection_plan_members",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("revision_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_definition_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version_policy", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("params", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["revision_id"], ["selection_plan_revisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["strategy_definition_id"], ["strategy_definitions.id"]),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("revision_id", "position"),
        sa.CheckConstraint("version_policy IN ('PINNED','STABLE_TRACK')", name="selection_plan_members_version_policy_check"),
    )

    # 成员条件（DDL 表名：selection_member_conditions）
    op.create_table(
        "selection_member_conditions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("operator", sa.Text(), nullable=False),
        sa.Column("value1", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value2", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["member_id"], ["selection_plan_members.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("member_id", "position"),
    )

    # 方案运行记录
    op.create_table(
        "selection_plan_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("selection_plan_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_run_set_hash", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["selection_plan_id"], ["selection_plans.id"]),
        sa.ForeignKeyConstraint(["revision_id"], ["selection_plan_revisions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )

    # 方案运行结果
    op.create_table(
        "selection_plan_results",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("matched_member_ids", sa.dialects.postgresql.ARRAY(sa.dialects.postgresql.UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("rank_value", sa.Float(), nullable=True),
        sa.Column("summary", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["plan_run_id"], ["selection_plan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_run_id", "instrument_id"),
    )
    op.create_index(
        "ix_selection_result_match",
        "selection_plan_results",
        ["plan_run_id", "matched", "rank_value"],
    )

    # 结果证据链（DDL 表名：selection_result_evidence）
    op.create_table(
        "selection_result_evidence",
        sa.Column("selection_result_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("member_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_result_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("summary", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.ForeignKeyConstraint(["selection_result_id"], ["selection_plan_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["member_id"], ["selection_plan_members.id"]),
        sa.ForeignKeyConstraint(["strategy_result_id"], ["strategy_results.id"]),
        sa.PrimaryKeyConstraint("selection_result_id", "member_id"),
    )


def downgrade() -> None:
    op.drop_table("selection_result_evidence")
    op.drop_index("ix_selection_result_match", table_name="selection_plan_results")
    op.drop_table("selection_plan_results")
    op.drop_table("selection_plan_runs")
    op.drop_table("selection_member_conditions")
    op.drop_table("selection_plan_members")
    op.drop_table("selection_plan_revisions")
    op.drop_index("ix_selection_plans_user", table_name="selection_plans")
    op.drop_table("selection_plans")
