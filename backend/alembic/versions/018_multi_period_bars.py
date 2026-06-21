"""018 bars_weekly, bars_monthly, bars_15min, bars_60min

Revision ID: 018_multi_period_bars
Revises: 017_filter_indexes
Create Date: 2026-06-20

多周期行情表（周线/月线/15分钟/1小时）：
- bars_weekly: 周线行情，PK(instrument_id, trade_date)，trade_date 为该周最后一个交易日
- bars_monthly: 月线行情，PK(instrument_id, trade_date)，trade_date 为该月最后一个交易日
- bars_15min: 15分钟线行情，PK(instrument_id, trade_time)
- bars_60min: 60分钟线行情，PK(instrument_id, trade_time)

设计说明：
- 字段结构与 bars_daily/bars_minute 完全一致
- 价格 OHLC: NUMERIC(20,4)；成交量/额: NUMERIC(20,2)；复权因子: NUMERIC(20,8)
- 用于个股详情页多周期 K 线展示
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "018_multi_period_bars"
down_revision: str | None = "017_filter_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 周线行情表
    op.create_table(
        "bars_weekly",
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
    op.create_index("ix_bars_weekly_date", "bars_weekly", ["trade_date"])

    # 月线行情表
    op.create_table(
        "bars_monthly",
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
    op.create_index("ix_bars_monthly_date", "bars_monthly", ["trade_date"])

    # 15分钟线行情表
    op.create_table(
        "bars_15min",
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
    op.create_index("ix_bars_15min_time", "bars_15min", ["trade_time"])

    # 60分钟线行情表
    op.create_table(
        "bars_60min",
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
    op.create_index("ix_bars_60min_time", "bars_60min", ["trade_time"])


def downgrade() -> None:
    op.drop_index("ix_bars_60min_time", table_name="bars_60min")
    op.drop_table("bars_60min")
    op.drop_index("ix_bars_15min_time", table_name="bars_15min")
    op.drop_table("bars_15min")
    op.drop_index("ix_bars_monthly_date", table_name="bars_monthly")
    op.drop_table("bars_monthly")
    op.drop_index("ix_bars_weekly_date", table_name="bars_weekly")
    op.drop_table("bars_weekly")
