"""002 instruments, trading_calendar

Revision ID: 002_instruments
Revises: 001_users
Create Date: 2026-06-18

股票主数据与交易日历（DDL 缺失，按 V1.1 07_DATA_MODEL.md 补充）：
- instruments: 股票主数据（symbol 唯一，market 标识市场）
- trading_calendar: 交易日历（trade_date 唯一，is_trading_day 标识是否交易日）

设计说明：使用 UUID 主键，与 core_schema.sql 中 instrument_id UUID 引用保持一致。
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "002_instruments"
down_revision: str | None = "001_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_index("ix_instruments_symbol", "instruments", ["symbol"])
    op.create_index("ix_instruments_market_status", "instruments", ["market", "status"])

    op.create_table(
        "trading_calendar",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("is_trading_day", sa.Boolean(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False, server_default="'A'"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("trade_date", "market", name="uq_trading_calendar_date_market"),
    )
    op.create_index("ix_trading_calendar_date", "trading_calendar", ["trade_date"])


def downgrade() -> None:
    op.drop_index("ix_trading_calendar_date", table_name="trading_calendar")
    op.drop_table("trading_calendar")
    op.drop_index("ix_instruments_market_status", table_name="instruments")
    op.drop_index("ix_instruments_symbol", table_name="instruments")
    op.drop_table("instruments")
