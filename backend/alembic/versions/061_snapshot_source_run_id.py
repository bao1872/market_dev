"""061 snapshot source_run_id - 快照精确归属 run

Revision ID: 061_snapshot_source_run_id
Revises: 060_stock_state_events
Create Date: 2026-07-11

变更内容：
- stock_feature_snapshots 增加 source_run_id 列（FK → stock_feature_snapshot_runs.id）
- 新增组合索引 ix_feature_snapshot_run_instrument(source_run_id, instrument_id)
  支持按 run 批量查询和 run+instrument 精确查询

设计说明：
- 原有 (trade_date, schema_version, primary_timeframe, secondary_timeframe, adj) 组合
  在同日多次 run（full/scoped/retry/force）时会产生归属歧义。
- source_run_id 提供不可歧义的 run 归属，事件生成按 source_run_id 精确查询。
- nullable=True 兼容历史数据（回补后可改为 NOT NULL）。
- 不删除原有唯一约束和索引，source_run_id 作为补充关联。
- 只建组合索引 (source_run_id, instrument_id)，其最左前缀已覆盖纯 source_run_id 查询，
  不再单独建 ix_feature_snapshot_source_run_id 单列索引，减少磁盘占用。

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "061_snapshot_source_run_id"
down_revision: str | None = "060_stock_state_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. 增加 source_run_id 列（nullable 兼容历史数据）
    op.add_column(
        "stock_feature_snapshots",
        sa.Column(
            "source_run_id",
            UUID(as_uuid=True),
            nullable=True,
            comment="归属的 snapshot run ID（精确关联，消除日期+参数猜归属）",
        ),
    )

    # 2. 增加 FK 约束
    op.create_foreign_key(
        "fk_snapshot_source_run_id",
        "stock_feature_snapshots",
        "stock_feature_snapshot_runs",
        ["source_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # 3. 增加组合索引支持批量查询 + 精确查询
    #    最左前缀 (source_run_id) 已覆盖纯 run 查询，不再单独建单列索引
    op.create_index(
        "ix_feature_snapshot_run_instrument",
        "stock_feature_snapshots",
        ["source_run_id", "instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_snapshot_run_instrument", table_name="stock_feature_snapshots")
    op.drop_constraint("fk_snapshot_source_run_id", "stock_feature_snapshots", type_="foreignkey")
    op.drop_column("stock_feature_snapshots", "source_run_id")


if __name__ == "__main__":
    assert revision == "061_snapshot_source_run_id"
    assert down_revision == "060_stock_state_events"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
