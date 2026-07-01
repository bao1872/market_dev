"""025 add retry_count/lease_expires_at/next_retry_at/heartbeat_at to monitor_evaluations

Revision ID: 025_eval_lease
Revises: 024_strat_def_env
Create Date: 2026-06-24
"""

import sqlalchemy as sa

from alembic import op

revision = "025_eval_lease"
down_revision = "024_strat_def_env"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "monitor_evaluations",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "monitor_evaluations",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "monitor_evaluations",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "monitor_evaluations",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("monitor_evaluations", "heartbeat_at")
    op.drop_column("monitor_evaluations", "next_retry_at")
    op.drop_column("monitor_evaluations", "lease_expires_at")
    op.drop_column("monitor_evaluations", "retry_count")
