"""042 drop dsa_backfill_jobs and dsa_backfill_instrument_progress

Revision ID: 042_drop_dsa_backfill
Revises: 041_notification_channel_active_unique
Create Date: 2026-06-27

变更内容：
- 删除 dsa_backfill_jobs 表
- 删除 dsa_backfill_instrument_progress 表
- 业务背景：DSA 历史回补功能已下线，相关代码、schema 一并清理
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "042_drop_dsa_backfill"
down_revision: str | None = "041_notification_channel_active_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [DSA Backfill] - 描述: 下线历史回补，删除相关表与索引
    op.drop_index(
        "ix_backfill_progress_instrument",
        table_name="dsa_backfill_instrument_progress",
    )
    op.drop_index(
        "ix_backfill_progress_job_status",
        table_name="dsa_backfill_instrument_progress",
    )
    op.drop_table("dsa_backfill_instrument_progress")

    op.drop_index(
        "ix_dsa_backfill_jobs_dates",
        table_name="dsa_backfill_jobs",
    )
    op.drop_index(
        "ix_dsa_backfill_jobs_status",
        table_name="dsa_backfill_jobs",
    )
    op.drop_table("dsa_backfill_jobs")


def downgrade() -> None:
    # [DSA Backfill] - 描述: 回滚时重建历史回补表（与 032_dsa_backfill.py 一致）
    op.create_table(
        "dsa_backfill_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "strategy_version_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column(
            "target_trade_dates",
            postgresql.ARRAY(sa.Date()),
            nullable=False,
        ),
        sa.Column("total_stocks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("processed_stocks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_stocks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_stocks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "current_instrument_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "error_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"],
            ["strategy_versions.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_dsa_backfill_jobs_status",
        "dsa_backfill_jobs",
        ["status"],
    )
    op.create_index(
        "ix_dsa_backfill_jobs_dates",
        "dsa_backfill_jobs",
        ["start_date", "end_date"],
    )

    op.create_table(
        "dsa_backfill_instrument_progress",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "backfill_job_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("result_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["backfill_job_id"],
            ["dsa_backfill_jobs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instruments.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backfill_job_id",
            "instrument_id",
            name="uq_backfill_progress_job_instrument",
        ),
    )
    op.create_index(
        "ix_backfill_progress_job_status",
        "dsa_backfill_instrument_progress",
        ["backfill_job_id", "status"],
    )
    op.create_index(
        "ix_backfill_progress_instrument",
        "dsa_backfill_instrument_progress",
        ["instrument_id"],
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "042_drop_dsa_backfill"
    assert down_revision == "041_notification_channel_active_unique"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
