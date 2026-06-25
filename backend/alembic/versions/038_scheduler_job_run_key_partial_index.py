"""038 replace global unique constraint on run_key with partial unique index

Revision ID: 038_scheduler_job_run_key_partial_index
Revises: 037_job_run_events
Create Date: 2026-06-25

变更内容：
- 删除 036 创建的全局唯一约束 uq_scheduler_job_runs_run_key（强制 run_key 全局唯一）
- 新增部分唯一索引 uq_scheduler_job_runs_active_run_key
  仅约束 run_key IS NOT NULL AND status IN ('queued', 'running') 的活跃记录

设计说明（为什么改为部分唯一索引）：
- 036 的全局唯一约束导致 interrupted/failed 状态的记录仍占用 run_key，
  无法为同一 run_key 创建新的 attempt（管理员手动重试 / 调度器自动续跑）
- 新的部分唯一索引只阻止「活跃任务重复」（queued/running 并发抢锁），
  对已结束（succeeded/failed/interrupted）的记录放行，允许新建 attempt
- 配合 acquire_job_run_lock 的 SELECT ... FOR UPDATE 仅查活跃记录 + INSERT，
  保证并发安全：同 run_key 同时只能有一个 queued/running 任务在执行
- 配合 recover_stale_scheduler_job_runs（Phase 3）：抢锁前先把僵尸 running
  改为 interrupted，腾出部分唯一索引的槽位，新任务可立即接管

幂等保证未削弱：
- 同一 run_key 在任一时刻最多只有一个 queued/running 记录（数据库级强约束）
- 重试场景下旧记录已变为 interrupted，新记录可创建新的 running attempt
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "038_scheduler_job_run_key_partial_index"
down_revision: str | None = "037_job_run_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [SchedulerJobRun] - 删除 036 的全局唯一约束：interrupted/failed 也占用 run_key 阻止重试
    op.drop_constraint(
        "uq_scheduler_job_runs_run_key",
        "scheduler_job_runs",
        type_="unique",
    )

    # [SchedulerJobRun] - 部分唯一索引：仅约束活跃记录（queued/running）
    # run_key IS NULL 的记录（无幂等键的向后兼容路径）不被约束
    # status NOT IN ('queued','running') 的记录（succeeded/failed/interrupted）不被约束，允许重试
    op.create_index(
        "uq_scheduler_job_runs_active_run_key",
        "scheduler_job_runs",
        ["run_key"],
        unique=True,
        postgresql_where=sa.text(
            "run_key IS NOT NULL AND status IN ('queued', 'running')"
        ),
    )


def downgrade() -> None:
    # 反向：先删除部分唯一索引，再重建全局唯一约束
    op.drop_index(
        "uq_scheduler_job_runs_active_run_key",
        table_name="scheduler_job_runs",
    )
    op.create_unique_constraint(
        "uq_scheduler_job_runs_run_key",
        "scheduler_job_runs",
        ["run_key"],
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "038_scheduler_job_run_key_partial_index"
    assert down_revision == "037_job_run_events"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
