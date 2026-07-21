"""067 scheduler_job_runs add lease_epoch and attempt_no columns

Revision ID: 067_scheduler_job_runs_lease_epoch_attempt_no
Revises: 066_capture_jobs_indicator_view
Create Date: 2026-07-21

变更内容（PRD V2.0 §4.3 After-close Recovery - JOB-01/JOB-02）：
- scheduler_job_runs 新增 lease_epoch INTEGER NOT NULL DEFAULT 0 列
  - 租约代际：Worker 领取任务时递增，所有写操作校验当前 lease_epoch
  - 防止旧 Worker（lease 已过期）继续写，实现 lease_epoch fencing
  - 默认 0 兼容历史数据（历史记录 lease_epoch=0，不参与 fencing）
- scheduler_job_runs 新增 attempt_no INTEGER NOT NULL DEFAULT 0 列
  - 尝试次数：首次执行 attempt_no=0，每次自动 resume 递增
  - 用于审计和限制最大重试次数（后续可加 attempt_no 上限校验）
  - 默认 0 兼容历史数据

设计说明：
- 不加索引（按 status/job_name 查询已覆盖，不按 lease_epoch/attempt_no 查询）
- NOT NULL DEFAULT 0 保证历史记录有值，fencing 逻辑可统一处理
- lease_epoch fencing 语义：UPDATE ... WHERE id = :id AND lease_epoch = :current_epoch
  - 0 rows affected 表示 lease 已变更（旧 Worker 写入被拒绝）
  - 调用方必须检查 rowcount 并抛出 LeaseEpochMismatchError

配合：
- app.models.scheduler_job_run.SchedulerJobRun（模型字段）
- app.services.scheduler_job_run_recovery_service（auto-resume 递增 attempt_no）
- app.worker._after_close_poll_once（领取时递增 lease_epoch）
- app.services.after_close_orchestrator（写操作校验 lease_epoch）

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "067_scheduler_job_runs_lease_epoch_attempt_no"
down_revision: str | None = "066_capture_jobs_indicator_view"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加 scheduler_job_runs.lease_epoch 和 attempt_no 列，更新部分唯一索引含 resume_queued。"""
    # 1. 添加 lease_epoch 和 attempt_no 列
    op.add_column(
        "scheduler_job_runs",
        sa.Column(
            "lease_epoch",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="租约代际：Worker 领取时递增，写操作校验防 fencing",
        ),
    )
    op.add_column(
        "scheduler_job_runs",
        sa.Column(
            "attempt_no",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="尝试次数：首次 0，自动 resume 递增",
        ),
    )

    # 2. 更新部分唯一索引：resume_queued 也算活跃状态，防止同一 run_key 出现多个活跃任务
    #    旧索引：status IN ('queued', 'running')
    #    新索引：status IN ('queued', 'running', 'resume_queued')
    op.drop_index("uq_scheduler_job_runs_active_run_key", table_name="scheduler_job_runs")
    op.create_index(
        "uq_scheduler_job_runs_active_run_key",
        "scheduler_job_runs",
        ["run_key"],
        unique=True,
        postgresql_where=sa.text(
            "run_key IS NOT NULL AND status IN ('queued', 'running', 'resume_queued')"
        ),
    )


def downgrade() -> None:
    """移除 scheduler_job_runs.lease_epoch 和 attempt_no 列，恢复部分唯一索引。"""
    # 1. 恢复旧的部分唯一索引（不含 resume_queued）
    op.drop_index("uq_scheduler_job_runs_active_run_key", table_name="scheduler_job_runs")
    op.create_index(
        "uq_scheduler_job_runs_active_run_key",
        "scheduler_job_runs",
        ["run_key"],
        unique=True,
        postgresql_where=sa.text(
            "run_key IS NOT NULL AND status IN ('queued', 'running')"
        ),
    )

    # 2. 移除列
    op.drop_column("scheduler_job_runs", "attempt_no")
    op.drop_column("scheduler_job_runs", "lease_epoch")
