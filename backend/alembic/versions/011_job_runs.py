"""011 job_runs

Revision ID: 011_job_runs
Revises: 010_config
Create Date: 2026-06-18

Job 运行记录（DDL 缺失，按 V1.1 09_TASK_AND_EVENT_PIPELINE.md 补充）：
- job_runs: Job 运行记录（job_type 区分任务类型，status 状态机）

字段说明：
- job_type: 任务类型（如 strategy_run, selection_plan_run, data_sync 等）
- status: pending/running/succeeded/failed/cancelled
- payload: 输入参数 JSONB
- result: 输出结果 JSONB
- error: 失败原因
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "011_job_runs"
down_revision: str | None = "010_config"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("result", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','cancelled')",
            name="job_runs_status_check",
        ),
    )
    op.create_index("ix_job_runs_type_status", "job_runs", ["job_type", "status"])
    op.create_index("ix_job_runs_created_at", "job_runs", [sa.text("created_at DESC")])


def downgrade() -> None:
    op.drop_index("ix_job_runs_created_at", table_name="job_runs")
    op.drop_index("ix_job_runs_type_status", table_name="job_runs")
    op.drop_table("job_runs")
