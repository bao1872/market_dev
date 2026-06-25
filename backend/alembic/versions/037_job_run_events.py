"""037 create job_run_events table for task execution timeline

Revision ID: 037_job_run_events
Revises: 036_scheduler_job_run_key
Create Date: 2026-06-25

变更内容：
- 新增 job_run_events 表：每个 SchedulerJobRun 的关键步骤事件时间线
  - job_run_id: FK -> scheduler_job_runs.id，ON DELETE CASCADE（任务删除时事件级联清除）
  - step: 步骤名（START/DAILY_DONE/DSA_CREATED/ERROR/BATCH_START 等）
  - level: info/warn/error
  - message: 人类可读消息
  - payload: JSONB 详细数据（覆盖率、run_id、成功失败数等）
  - created_at: 事件创建时间
- 新增复合索引 ix_job_run_events_job_run_id_created_at (job_run_id, created_at)
  加速按任务查询时间线（任务详情抽屉按时间倒序展示）

用途：
- 任务详情抽屉展示执行时间线，定位任务卡在哪一步
- 盘后编排（after_close_orchestrator）串联全流程事件
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "037_job_run_events"
down_revision: str | None = "036_scheduler_job_run_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [JobRunEvent] - 任务执行事件时间线表
    op.create_table(
        "job_run_events",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "job_run_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("scheduler_job_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="关联 scheduler_job_runs.id",
        ),
        sa.Column(
            "step",
            sa.String(64),
            nullable=False,
            comment="步骤名，如 START/DAILY_DONE/ERROR",
        ),
        sa.Column(
            "level",
            sa.String(16),
            nullable=False,
            server_default="info",
            comment="级别：info/warn/error",
        ),
        sa.Column(
            "message",
            sa.Text(),
            nullable=False,
            comment="人类可读消息",
        ),
        sa.Column(
            "payload",
            JSONB,
            nullable=True,
            comment="详细数据 JSON（覆盖率、run_id 等）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        comment="任务执行事件时间线",
    )

    # [JobRunEvent] - 复合索引加速按任务查询时间线
    op.create_index(
        "ix_job_run_events_job_run_id_created_at",
        "job_run_events",
        ["job_run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_job_run_events_job_run_id_created_at",
        table_name="job_run_events",
    )
    op.drop_table("job_run_events")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "037_job_run_events"
    assert down_revision == "036_scheduler_job_run_key"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
