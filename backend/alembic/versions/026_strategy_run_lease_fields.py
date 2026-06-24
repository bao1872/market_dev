"""026 add lease/heartbeat fields to strategy_runs

Revision ID: 026_strat_run_lease
Revises: 025_eval_lease
Create Date: 2026-06-24
"""

from alembic import op
import sqlalchemy as sa

revision = "026_strat_run_lease"
down_revision = "025_eval_lease"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True, comment="入队时间，创建时赋值"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True, comment="Worker 心跳时间"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True, comment="租约过期时间"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("worker_id", sa.String(64), nullable=True, comment="执行 Worker 标识"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0", comment="尝试次数"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True, comment="下次重试时间"),
    )
    op.add_column(
        "strategy_runs",
        sa.Column("error_code", sa.String(128), nullable=True, comment="错误码"),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "error_code")
    op.drop_column("strategy_runs", "next_retry_at")
    op.drop_column("strategy_runs", "attempt_count")
    op.drop_column("strategy_runs", "worker_id")
    op.drop_column("strategy_runs", "lease_expires_at")
    op.drop_column("strategy_runs", "heartbeat_at")
    op.drop_column("strategy_runs", "queued_at")
