"""068 stock feature snapshot add event_freshness_payload column

Revision ID: 068_stock_feature_snapshot_event_freshness_payload
Revises: 067_scheduler_job_runs_lease_epoch_attempt_no
Create Date: 2026-07-24

变更内容（CHANGE-20260724-002 Phase 4）：
- stock_feature_snapshots 新增 event_freshness_payload JSONB NOT NULL DEFAULT '{}' 列
  - 事件新鲜度层：最近一次客观事件距现在的 bar/trading_day 数
  - 与连续层（structural_payload / temporal_payload）分离，避免双真源
  - 包含 daily_structure.smc（18 项 SMC freshness）和 monitor_interaction（Node/BB 穿越事件）
  - schema_version 4→5：旧 v4 快照被 schema gate 自动排除（查询过滤 schema_version==5）

设计说明：
- NOT NULL DEFAULT '{}'：历史 v4 行获得空 payload，但因 schema_version=4 被 gate 排除，不会被读取
- 不加 GIN 索引，优先节省磁盘（与 structural/temporal/summary payload 一致）
- 前向 migration：只新增列，不修改/删除现有列

配合：
- app.models.stock_feature_snapshot.StockFeatureSnapshot（ORM 字段）
- app.services.feature_snapshot_service._SCHEMA_VERSION = 5
- app.services.event_freshness_service.build_empty_event_freshness_payload

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "068_stock_feature_snapshot_event_freshness_payload"
down_revision: str | None = "067_scheduler_job_runs_lease_epoch_attempt_no"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """添加 stock_feature_snapshots.event_freshness_payload 列。"""
    op.add_column(
        "stock_feature_snapshots",
        sa.Column(
            "event_freshness_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
            comment="事件新鲜度 payload JSONB（SMC 18 项 + monitor 交互事件 freshness）",
        ),
    )


def downgrade() -> None:
    """移除 stock_feature_snapshots.event_freshness_payload 列。"""
    op.drop_column("stock_feature_snapshots", "event_freshness_payload")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "068_stock_feature_snapshot_event_freshness_payload"
    assert down_revision == "067_scheduler_job_runs_lease_epoch_attempt_no"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
