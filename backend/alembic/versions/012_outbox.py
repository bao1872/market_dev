"""012 outbox

Revision ID: 012_outbox
Revises: 011_job_runs
Create Date: 2026-06-18

Outbox 模式事件表（DDL 无，按 V1.1 09_TASK_AND_EVENT_PIPELINE.md 补充）：
- outbox: 事务性发件箱（status 状态机，relay worker 轮询投递）

字段说明：
- aggregate_type: 聚合根类型（如 strategy_run, selection_plan_run 等）
- aggregate_id: 聚合根 ID
- event_type: 事件类型
- payload: 事件负载 JSONB
- headers: 事件头 JSONB（如 trace_id, tenant_id）
- status: pending/processed/failed
- retry_count: 重试次数
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "012_outbox"
down_revision: str | None = "011_job_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "outbox",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("headers", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("status", sa.Text(), nullable=False, server_default="'pending'"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','processed','failed')",
            name="outbox_status_check",
        ),
    )
    # relay worker 轮询索引：按状态 + 创建时间排序
    op.create_index("ix_outbox_status_created", "outbox", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_outbox_status_created", table_name="outbox")
    op.drop_table("outbox")
