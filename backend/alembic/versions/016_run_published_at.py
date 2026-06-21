"""016 run_published_at: strategy_runs 新增 published_at 列

Revision ID: 016_run_published_at
Revises: 015_strategy_batch
Create Date: 2026-06-20

变更内容：
- strategy_runs 新增 published_at 列（DateTime(timezone=True), nullable=True）
- published_at 非空时表示该 run 已发布，普通用户可查询
- run 状态机：queued → running → completed/partial_failed → published/failed

依赖：005_strategy_runs（strategy_runs 表）、015_strategy_batch（扩展字段）
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016_run_published_at"
down_revision: str | None = "015_strategy_batch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # strategy_runs 新增 published_at 列
    op.add_column(
        "strategy_runs",
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="发布时间（非空表示已发布，用户可查询）",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "published_at")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "016_run_published_at"
    assert down_revision == "015_strategy_batch"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
