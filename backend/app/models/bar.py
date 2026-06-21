"""Bar ORM 模型 - 多周期行情。

对应迁移：
- 003_bars: bars_daily（日线）、bars_minute（分钟线）
- 018_multi_period_bars: bars_weekly（周线）、bars_monthly（月线）、bars_15min（15分钟）、bars_60min（1小时）

字段精度（与迁移一致）：
- 价格 OHLC: NUMERIC(20,4)
- 成交量/额: NUMERIC(20,2)
- 复权因子: NUMERIC(20,8)，默认 1.0
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, ForeignKeyConstraint, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BarDaily(Base):
    """日线行情 ORM 模型。

    PK: (instrument_id, trade_date)
    FK: instrument_id -> instruments.id)
    """

    __tablename__ = "bars_daily"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_date"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


class BarMinute(Base):
    """分钟线行情 ORM 模型。

    PK: (instrument_id, trade_time)
    FK: instrument_id -> instruments.id)
    """

    __tablename__ = "bars_minute"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_time"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_time: Mapped[datetime] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


class BarWeekly(Base):
    """周线行情 ORM 模型。

    PK: (instrument_id, trade_date)
    FK: instrument_id -> instruments.id)
    trade_date 表示该周最后一个交易日。
    """

    __tablename__ = "bars_weekly"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_date"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


class BarMonthly(Base):
    """月线行情 ORM 模型。

    PK: (instrument_id, trade_date)
    FK: instrument_id -> instruments.id)
    trade_date 表示该月第一个交易日（前对齐，label='left', closed='right'）。
    """

    __tablename__ = "bars_monthly"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_date"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


class Bar15Min(Base):
    """15分钟线行情 ORM 模型。

    PK: (instrument_id, trade_time)
    FK: instrument_id -> instruments.id)
    """

    __tablename__ = "bars_15min"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_time"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_time: Mapped[datetime] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


class Bar60Min(Base):
    """60分钟线行情 ORM 模型。

    PK: (instrument_id, trade_time)
    FK: instrument_id -> instruments.id)
    """

    __tablename__ = "bars_60min"
    __table_args__ = (
        PrimaryKeyConstraint("instrument_id", "trade_time"),
        ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
    )

    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    trade_time: Mapped[datetime] = mapped_column(nullable=False)
    open: Mapped[Decimal | None] = mapped_column(nullable=True)
    high: Mapped[Decimal | None] = mapped_column(nullable=True)
    low: Mapped[Decimal | None] = mapped_column(nullable=True)
    close: Mapped[Decimal | None] = mapped_column(nullable=True)
    volume: Mapped[Decimal | None] = mapped_column(nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(nullable=True)
    adj_factor: Mapped[Decimal | None] = mapped_column(
        nullable=True, default=Decimal("1.0")
    )


# ForeignKey 与 ForeignKeyConstraint 均在 __table_args__ 中使用
_ = (ForeignKey, ForeignKeyConstraint)

if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (BarDaily, BarMinute, BarWeekly, BarMonthly, Bar15Min, Bar60Min):
        cols = [c.name for c in cls.__table__.columns]
        pk = [c.name for c in cls.__table__.primary_key.columns]
        print(f"{cls.__tablename__}: PK={pk}, columns={cols}")
        assert "instrument_id" in cols
        assert "adj_factor" in cols
    print("OK")
