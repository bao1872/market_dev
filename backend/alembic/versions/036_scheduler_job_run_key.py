"""036 add run_key column to scheduler_job_runs for idempotency

Revision ID: 036_scheduler_job_run_key
Revises: 035_strategy_run_error_fields
Create Date: 2026-06-25

变更内容：
- scheduler_job_runs 新增 run_key 列（String(128)，nullable=True）
  业务幂等键，如 bars_scheduler:2026-06-25 / monitor_scheduler:2026-06-25:morning
- 回填历史数据 run_key（job_name:business_date，monitor_scheduler 额外拼接 session_label）
- 重复 run_key 仅保留最早一条（MIN(id)），其余置 NULL
- 新增唯一约束 uq_scheduler_job_runs_run_key（数据库级强约束保证幂等）
- 新增复合索引 ix_scheduler_job_runs_job_bd (job_name, business_date) 加速按任务+日期查询

幂等设计说明：
- run_key 是业务幂等键，由 job_name + business_date [+ session_label] 拼接
- 唯一约束保证同一 run_key 只能存在一条记录，INSERT ... ON CONFLICT 实现幂等
- 配合应用层 pg_advisory_xact_lock 序列化并发请求（见 idempotency_service）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "036_scheduler_job_run_key"
down_revision: str | None = "035_strategy_run_error_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [SchedulerJobRun] - 业务幂等键: job_name:business_date[:session_label]
    op.add_column(
        "scheduler_job_runs",
        sa.Column(
            "run_key",
            sa.String(128),
            nullable=True,
            comment="业务幂等键，如 bars_scheduler:2026-06-25",
        ),
    )

    # 回填历史数据：非 monitor_scheduler 任务使用 job_name:business_date
    op.execute(
        """
        UPDATE scheduler_job_runs
        SET run_key = job_name || ':' || COALESCE(business_date, '')
        WHERE run_key IS NULL AND business_date IS NOT NULL
        """
    )

    # 回填 monitor_scheduler：额外拼接 session_label（从 metadata_json 解析）
    op.execute(
        """
        UPDATE scheduler_job_runs
        SET run_key = job_name || ':' || business_date || ':' ||
            COALESCE((metadata_json::jsonb ->> 'session_label'), 'unknown')
        WHERE job_name = 'monitor_scheduler'
          AND business_date IS NOT NULL
          AND run_key IS NULL
        """
    )

    # 去重：同一 run_key 仅保留最早一条（MIN(id::text)），其余置 NULL
    # 注意：id 是 UUID 类型，PostgreSQL 无 MIN(uuid) 聚合，需 cast 为 text
    op.execute(
        """
        UPDATE scheduler_job_runs s
        SET run_key = NULL
        WHERE s.id::text NOT IN (
            SELECT MIN(id::text) FROM scheduler_job_runs
            WHERE run_key IS NOT NULL
            GROUP BY run_key
        ) AND s.run_key IS NOT NULL
        """
    )

    # 创建唯一约束（数据库级强约束保证幂等）
    op.create_unique_constraint(
        "uq_scheduler_job_runs_run_key",
        "scheduler_job_runs",
        ["run_key"],
    )

    # 创建复合索引（加速按 job_name + business_date 查询）
    op.create_index(
        "ix_scheduler_job_runs_job_bd",
        "scheduler_job_runs",
        ["job_name", "business_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_scheduler_job_runs_job_bd", table_name="scheduler_job_runs")
    op.drop_constraint("uq_scheduler_job_runs_run_key", "scheduler_job_runs", type_="unique")
    op.drop_column("scheduler_job_runs", "run_key")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "036_scheduler_job_run_key"
    assert down_revision == "035_strategy_run_error_fields"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
