"""stock_core 原子 publication 支撑字段（P0-07，本迁移仅编写、不在共享库执行）

[CHANGE-20260805-CP4A-CP3 / P0-07]
PRD 要求同一事务完成：publication / pointer / run published / supersede / audit / fencing。
现有 `factor_publications` 缺少以下字段，无法表达：
- immutable publication history + superseded relation/time；
- publication audit event；
- publish worker 身份与 lease epoch（fencing）。

本迁移仅**补齐这些字段/表**，供后续原子 publication service 使用。升级/回滚语义：
- upgrade：给 factor_publications 增加 superseded_by / superseded_at / publish_worker_id /
  publish_lease_epoch；新增 stock_core_publication_audit 表。
- downgrade：drop audit 表并还原列。

**注意**：本文件是静态产物，须经静态审查 + 迁移 DDL 单测通过后，才允许在隔离验证库
（bz_stock_verify_<sha>）执行 upgrade→downgrade→upgrade→duplicate-upgrade。**当前不执行**。

Revision ID: 087_stock_core_atomic_publication
Revises: 086_chip_consensus_run_uniqueness
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers
revision = "087_stock_core_atomic_publication"
down_revision = "086_chip_consensus_run_uniqueness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. factor_publications 增加 supersede / fencing 列
    op.add_column(
        "factor_publications",
        sa.Column("superseded_by", sa.Uuid(), nullable=True,
                  comment="被哪个 publication 取代（supersede lineage，NULL=当前有效）"),
    )
    op.add_column(
        "factor_publications",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True,
                  comment="取代发生时间"),
    )
    op.add_column(
        "factor_publications",
        sa.Column("publish_worker_id", sa.Text(), nullable=True,
                  comment="发布 worker 身份（fencing）"),
    )
    op.add_column(
        "factor_publications",
        sa.Column("publish_lease_epoch", sa.BigInteger(), nullable=True,
                  comment="发布 lease epoch（fencing，防并发覆盖）"),
    )

    # 2. 发布审计表（同一事务写审计事件）
    op.create_table(
        "stock_core_publication_audit",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("publication_kind", sa.Text(), nullable=False),
        sa.Column("old_data_run_id", sa.Uuid(), nullable=True),
        sa.Column("new_data_run_id", sa.Uuid(), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("publish_worker_id", sa.Text(), nullable=True),
        sa.Column("publish_lease_epoch", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False,
                  comment="publish / supersede / rollback"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )


def downgrade() -> None:
    op.drop_table("stock_core_publication_audit")
    op.drop_column("factor_publications", "publish_lease_epoch")
    op.drop_column("factor_publications", "publish_worker_id")
    op.drop_column("factor_publications", "superseded_at")
    op.drop_column("factor_publications", "superseded_by")
