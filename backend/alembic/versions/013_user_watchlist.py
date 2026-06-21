"""013 user_watchlist_items

Revision ID: 013_user_watchlist
Revises: 012_outbox
Create Date: 2026-06-18

用户自选股（DDL 中已有，按 core_schema.sql 原样迁移）：
- user_watchlist_items: 用户自选股（user_id + instrument_id 唯一，source 标识来源）

字段说明：
- source: 加入来源（如 selection_plan, manual, monitor 等）
- active: 是否活跃（软删除标记）
- removed_at: 移除时间（软删除）
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013_user_watchlist"
down_revision: str | None = "012_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_watchlist_items",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "instrument_id"),
    )
    op.create_index("ix_user_watchlist_user_active", "user_watchlist_items", ["user_id", "active"])


def downgrade() -> None:
    op.drop_index("ix_user_watchlist_user_active", table_name="user_watchlist_items")
    op.drop_table("user_watchlist_items")
