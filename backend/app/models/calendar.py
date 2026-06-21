"""TradingCalendar ORM 模型 - 交易日历。

对应迁移 002_instruments 中的 trading_calendar 表：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- trade_date: 交易日期（唯一，配合 market 复合唯一约束）
- is_trading_day: 是否为交易日
- market: 市场标识（A 表示 A 股整体；HS 表示沪深）
- created_at: 创建时间

设计说明：
- (trade_date, market) 复合唯一约束，支持多市场日历
- trade_date 单列索引，支持日期范围查询
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TradingCalendar(Base):
    """交易日历。"""

    __tablename__ = "trading_calendar"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="A")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("trade_date", "market", name="uq_trading_calendar_date_market"),
        Index("ix_trading_calendar_date", "trade_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<TradingCalendar(trade_date={self.trade_date!r}, "
            f"is_trading_day={self.is_trading_day!r}, market={self.market!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证模型定义（不写库表）
    print(f"__tablename__={TradingCalendar.__tablename__}")
    print(f"columns={list(TradingCalendar.__table__.columns.keys())}")
    print(f"indexes={[idx.name for idx in TradingCalendar.__table__.indexes]}")
    print(f"constraints={[c.name for c in TradingCalendar.__table__.constraints if c.name]}")
    print("OK")
