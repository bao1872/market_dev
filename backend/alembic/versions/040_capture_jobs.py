"""040 capture_jobs

Revision ID: 040_capture_jobs
Revises: 039_instruments_pinyin_initials
Create Date: 2026-06-26

advice.md: 新增持久化 CaptureJob 表，支持截图失败重试 + 达上限 dead。
字段: event_id/instrument_id/user_id/message_group_id/status/attempt_count/
      image_url/error_code/error_message/created_at/finished_at
"""
from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "040_capture_jobs"
down_revision: str | None = "039_instruments_pinyin_initials"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "capture_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False, comment="触发的监控事件 ID"),
        sa.Column("instrument_id", postgresql.UUID(as_uuid=True), nullable=False, comment="标的 ID"),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False, comment="用户 ID（生成 capture token 用）"),
        sa.Column("message_group_id", sa.Text(), nullable=False, comment="消息组 ID（关联 card/image Outbox）"),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending", comment="pending/running/succeeded/failed/dead"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0", comment="尝试次数"),
        sa.Column("image_url", sa.Text(), nullable=True, comment="截图成功后的图片 URL"),
        sa.Column("error_code", sa.Text(), nullable=True, comment="失败错误码"),
        sa.Column("error_message", sa.Text(), nullable=True, comment="失败错误信息"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True, comment="成功/失败/dead 时间"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_capture_jobs_status_created", "capture_jobs", ["status", "created_at"])
    op.create_index("ix_capture_jobs_message_group_id", "capture_jobs", ["message_group_id"])


def downgrade() -> None:
    op.drop_index("ix_capture_jobs_message_group_id", table_name="capture_jobs")
    op.drop_index("ix_capture_jobs_status_created", table_name="capture_jobs")
    op.drop_table("capture_jobs")
