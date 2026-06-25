"""035 add error_message and failure_stage to strategy_runs

Revision ID: 035_strategy_run_error_fields
Revises: 034_message_delivery_group_id
Create Date: 2026-06-25

变更内容：
- strategy_runs 新增 error_message 列（Text，nullable）
  存储 DSA 运行失败的详细原因（run 级别，与 strategy_run_items.error_message 的 per-stock 级别区分）
- strategy_runs 新增 failure_stage 列（String(64)，nullable）
  标识失败发生的阶段，枚举值见 app.models.strategy_run.ALL_FAILURE_STAGES：
  DATA_READINESS / LOAD_VERSION / LOAD_RUNTIME / LOAD_INSTRUMENTS /
  CALCULATE_INSTRUMENTS / WRITE_RESULTS / QUALITY_GATE / PUBLISH / WORKER_INTERRUPTED
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "035_strategy_run_error_fields"
down_revision: str | None = "034_message_delivery_group_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [StrategyRun] - 运行级错误信息: 存储 DSA 运行失败的详细原因
    op.add_column(
        "strategy_runs",
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="运行级错误详情（per-stock 错误见 strategy_run_items.error_message）",
        ),
    )
    # [StrategyRun] - 失败阶段: DATA_READINESS/LOAD_VERSION/.../WORKER_INTERRUPTED
    op.add_column(
        "strategy_runs",
        sa.Column(
            "failure_stage",
            sa.String(64),
            nullable=True,
            comment="失败阶段枚举（见 ALL_FAILURE_STAGES）",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy_runs", "failure_stage")
    op.drop_column("strategy_runs", "error_message")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "035_strategy_run_error_fields"
    assert down_revision == "034_message_delivery_group_id"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
