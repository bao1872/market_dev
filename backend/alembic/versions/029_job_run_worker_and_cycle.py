"""029 add worker_instance_id and last_cycle_at to scheduler_job_runs

Revision ID: 029_job_run_worker_and_cycle
Revises: 028_outbox_deferred
Create Date: 2026-06-24

为 SchedulerJobRun 补充可观察性字段：
- worker_instance_id: 标识执行该任务的 Worker 实例（hostname:pid）
- last_cycle_at: 长任务最近一次周期执行时间（用于 monitor_scheduler session 聚合）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "029_job_run_worker_and_cycle"
down_revision: str | None = "028_outbox_deferred"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduler_job_runs",
        sa.Column(
            "worker_instance_id",
            sa.String(64),
            nullable=True,
            comment="Worker 实例标识 hostname:pid",
        ),
    )
    op.add_column(
        "scheduler_job_runs",
        sa.Column(
            "last_cycle_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="最近一个周期执行时间",
        ),
    )


def downgrade() -> None:
    op.drop_column("scheduler_job_runs", "last_cycle_at")
    op.drop_column("scheduler_job_runs", "worker_instance_id")
