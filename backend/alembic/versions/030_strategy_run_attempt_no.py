"""030 add attempt_no to strategy_runs

Revision ID: 030_strategy_run_attempt_no
Revises: 029_job_run_worker_and_cycle
Create Date: 2026-06-24

变更内容：
- strategy_runs 新增 attempt_no 列，默认 1
- 业务含义：同一 (strategy_version_id, trade_date, run_type) 下的重试序号
- 与 attempt_count 区分：attempt_count 为租约恢复计数，attempt_no 为业务重试序号
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "030_strategy_run_attempt_no"
down_revision: str | None = "029_job_run_worker_and_cycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "strategy_runs",
        sa.Column(
            "attempt_no",
            sa.Integer(),
            nullable=False,
            server_default="1",
            comment="业务重试序号（同一 version/date/run_type 内的第几次尝试）",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "attempt_no")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "030_strategy_run_attempt_no"
    assert down_revision == "029_job_run_worker_and_cycle"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
