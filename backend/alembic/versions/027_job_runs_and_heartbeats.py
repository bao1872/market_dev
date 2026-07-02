"""027 create scheduler_job_runs and worker_heartbeats tables

Revision ID: 027_job_runs_heartbeats
Revises: 026_strat_run_lease
Create Date: 2026-06-24
"""

from alembic import op
import sqlalchemy as sa

revision = "027_job_runs_heartbeats"
down_revision = "026_strat_run_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 定时任务执行记录表
    op.create_table(
        "scheduler_job_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_name", sa.String(128), nullable=False, comment="任务名称，如 bars_daily/strategy_scheduler/monitor_cycle"),
        sa.Column("business_date", sa.String(10), nullable=True, comment="业务日期 YYYY-MM-DD"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True, comment="计划执行时间"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True, comment="实际开始时间"),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="完成时间"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running", comment="running/succeeded/failed"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True, comment="心跳时间"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True, comment="租约过期时间"),
        sa.Column("total_count", sa.Integer(), nullable=True, comment="总数"),
        sa.Column("succeeded_count", sa.Integer(), nullable=True, comment="成功数"),
        sa.Column("failed_count", sa.Integer(), nullable=True, comment="失败数"),
        sa.Column("progress", sa.Float(), nullable=True, comment="进度 0.0-1.0"),
        sa.Column("error_code", sa.String(128), nullable=True, comment="错误码"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="错误信息"),
        sa.Column("metadata_json", sa.Text(), nullable=True, comment="额外元数据 JSON"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )

    # Worker 心跳表（复合主键）
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_name", sa.String(128), nullable=False, comment="Worker 名称，如 bars_scheduler/strategy_batch"),
        sa.Column("instance_id", sa.String(64), nullable=False, comment="实例标识，hostname:pid"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, comment="启动时间"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False, comment="最近心跳时间"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running", comment="running/idle/stopped"),
        sa.Column("current_job_id", sa.String(36), nullable=True, comment="当前执行的任务 ID"),
        sa.Column("build_sha", sa.String(40), nullable=True, comment="构建版本 SHA"),
        sa.Column("metadata_json", sa.Text(), nullable=True, comment="额外元数据 JSON"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("worker_name", "instance_id"),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_table("scheduler_job_runs")
