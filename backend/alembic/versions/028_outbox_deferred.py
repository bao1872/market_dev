"""028 outbox_deferred

Revision ID: 028_outbox_deferred
Revises: 027_job_runs_and_heartbeats
Create Date: 2026-06-24

为 outbox 表支持静默时段 deferred 状态：
- 新增 next_attempt_at 字段（timestamp nullable），用于 deferred 状态记录下次可投递时间
- 扩展 status CHECK 约束，允许 'deferred' 状态
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "028_outbox_deferred"
down_revision: str | None = "027_job_runs_heartbeats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 新增 next_attempt_at 字段
    op.add_column(
        "outbox",
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="下次可投递时间（deferred 状态使用）",
        ),
    )
    # 扩展 status CHECK 约束，加入 deferred
    op.drop_constraint("outbox_status_check", "outbox", type_="check")
    op.create_check_constraint(
        "outbox_status_check",
        "outbox",
        sa.text("status IN ('pending','processed','failed','deferred')"),
    )


def downgrade() -> None:
    # 恢复 status CHECK 约束
    op.drop_constraint("outbox_status_check", "outbox", type_="check")
    op.create_check_constraint(
        "outbox_status_check",
        "outbox",
        sa.text("status IN ('pending','processed','failed')"),
    )
    # 删除 next_attempt_at 字段
    op.drop_column("outbox", "next_attempt_at")
