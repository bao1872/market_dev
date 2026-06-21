"""003 bars_daily, bars_minute

Revision ID: 003_bars
Revises: 002_instruments
Create Date: 2026-06-18

行情仓储（DDL 缺失，按 V1.1 07_DATA_MODEL.md 补充）：
- bars_daily: 日线行情（复合主键 instrument_id + trade_date）
- bars_minute: 分钟线行情（复合主键 instrument_id + trade_time）

设计说明：
- instrument_id 为 UUID，引用 instruments(id)，与 core_schema.sql 一致
- adj_factor 前复权因子，默认 1.0
- NUMERIC 精度：价格 20,4；成交量/额 20,2；复权因子 20,8
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "003_bars"
down_revision: str | None = "002_instruments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bars_daily",
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("open", sa.Numeric(20, 4), nullable=True),
        sa.Column("high", sa.Numeric(20, 4), nullable=True),
        sa.Column("low", sa.Numeric(20, 4), nullable=True),
        sa.Column("close", sa.Numeric(20, 4), nullable=True),
        sa.Column("volume", sa.Numeric(20, 2), nullable=True),
        sa.Column("amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("adj_factor", sa.Numeric(20, 8), nullable=True, server_default="1.0"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("instrument_id", "trade_date"),
    )
    op.create_index("ix_bars_daily_date", "bars_daily", ["trade_date"])

    op.create_table(
        "bars_minute",
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(20, 4), nullable=True),
        sa.Column("high", sa.Numeric(20, 4), nullable=True),
        sa.Column("low", sa.Numeric(20, 4), nullable=True),
        sa.Column("close", sa.Numeric(20, 4), nullable=True),
        sa.Column("volume", sa.Numeric(20, 2), nullable=True),
        sa.Column("amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("adj_factor", sa.Numeric(20, 8), nullable=True, server_default="1.0"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("instrument_id", "trade_time"),
    )
    op.create_index("ix_bars_minute_time", "bars_minute", ["trade_time"])


def downgrade() -> None:
    op.drop_index("ix_bars_minute_time", table_name="bars_minute")
    op.drop_table("bars_minute")
    op.drop_index("ix_bars_daily_date", table_name="bars_daily")
    op.drop_table("bars_daily")
