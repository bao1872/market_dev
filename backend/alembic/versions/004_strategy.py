"""004 strategy_definitions, strategy_versions

Revision ID: 004_strategy
Revises: 003_bars
Create Date: 2026-06-18

策略目录与版本：
- strategy_definitions: 策略定义（strategy_key 唯一，kind 区分 selector/monitor）
- strategy_versions: 策略版本（manifest JSONB，build_hash 构建哈希）

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004_strategy"
down_revision: str | None = "003_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_definitions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("strategy_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_key"),
        sa.CheckConstraint("kind IN ('selector','monitor')", name="strategy_definitions_kind_check"),
    )

    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("strategy_definition_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("manifest", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("build_hash", sa.Text(), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["strategy_definition_id"], ["strategy_definitions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_definition_id", "version"),
    )


def downgrade() -> None:
    op.drop_table("strategy_versions")
    op.drop_table("strategy_definitions")
