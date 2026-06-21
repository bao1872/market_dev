"""015 strategy_batch: strategy_run_items 表 + strategy_runs 扩展 + 索引

Revision ID: 015_strategy_batch
Revises: 014_membership
Create Date: 2026-06-20

变更内容：
1. 新建 strategy_run_items 表（per-stock 执行状态跟踪）
2. strategy_runs 新增 effective_config/effective_config_hash/统计字段
3. strategy_results 新增 (run_id, instrument_id) 唯一约束
4. 新增过滤索引

依赖：005_strategy_runs（strategy_runs/strategy_results 表）、002_instruments（instruments 表）
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "015_strategy_batch"
down_revision: str | None = "014_membership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. 新建 strategy_run_items 表（per-stock 执行状态跟踪）
    op.create_table(
        "strategy_run_items",
        sa.Column(
            "id",
            UUID(),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "run_id",
            UUID(),
            sa.ForeignKey("strategy_runs.id", ondelete="CASCADE"),
            nullable=False,
            comment="所属运行 ID",
        ),
        sa.Column(
            "instrument_id",
            UUID(),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="标的 ID",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default="pending",
            comment="pending/running/succeeded/failed/skipped",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="尝试次数",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="失败原因",
        ),
        sa.Column(
            "result_id",
            UUID(),
            sa.ForeignKey("strategy_results.id"),
            nullable=True,
            comment="关联结果 ID（成功时填充）",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="开始时间",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="完成时间",
        ),
        sa.UniqueConstraint(
            "run_id",
            "instrument_id",
            name="uq_strategy_run_items_run_instrument",
        ),
        comment="策略运行子项 - 单只标的在一次运行中的执行状态",
    )
    op.create_index(
        "ix_run_items_run_status",
        "strategy_run_items",
        ["run_id", "status"],
    )
    op.create_index(
        "ix_run_items_instrument",
        "strategy_run_items",
        ["instrument_id"],
    )

    # 2. strategy_runs 新增列（effective_config + 批量统计）
    op.add_column(
        "strategy_runs",
        sa.Column(
            "effective_config",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="运行时实际使用的配置快照",
        ),
    )
    op.add_column(
        "strategy_runs",
        sa.Column(
            "effective_config_hash",
            sa.Text(),
            nullable=True,
            comment="effective_config 的 SHA256 哈希",
        ),
    )
    op.add_column(
        "strategy_runs",
        sa.Column(
            "total_instruments",
            sa.Integer(),
            nullable=True,
            comment="标的总数",
        ),
    )
    op.add_column(
        "strategy_runs",
        sa.Column(
            "succeeded_count",
            sa.Integer(),
            nullable=True,
            comment="成功数",
        ),
    )
    op.add_column(
        "strategy_runs",
        sa.Column(
            "failed_count",
            sa.Integer(),
            nullable=True,
            comment="失败数",
        ),
    )
    op.add_column(
        "strategy_runs",
        sa.Column(
            "skipped_count",
            sa.Integer(),
            nullable=True,
            comment="跳过数（停牌等）",
        ),
    )

    # 3. strategy_results 新增 (run_id, instrument_id) 唯一约束
    op.create_unique_constraint(
        "uq_strategy_results_run_instrument",
        "strategy_results",
        ["run_id", "instrument_id"],
    )

    # 4. 新增过滤索引
    op.create_index(
        "ix_results_version_date_run",
        "strategy_results",
        ["strategy_version_id", "trade_date", "run_id"],
    )
    op.create_index(
        "ix_metrics_instrument",
        "strategy_result_metrics",
        ["instrument_id", "metric_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_metrics_instrument", table_name="strategy_result_metrics")
    op.drop_index("ix_results_version_date_run", table_name="strategy_results")
    op.drop_constraint(
        "uq_strategy_results_run_instrument", "strategy_results", type_="unique"
    )
    op.drop_column("strategy_runs", "skipped_count")
    op.drop_column("strategy_runs", "failed_count")
    op.drop_column("strategy_runs", "succeeded_count")
    op.drop_column("strategy_runs", "total_instruments")
    op.drop_column("strategy_runs", "effective_config_hash")
    op.drop_column("strategy_runs", "effective_config")
    op.drop_index("ix_run_items_instrument", table_name="strategy_run_items")
    op.drop_index("ix_run_items_run_status", table_name="strategy_run_items")
    op.drop_table("strategy_run_items")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "015_strategy_batch"
    assert down_revision == "014_membership"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
