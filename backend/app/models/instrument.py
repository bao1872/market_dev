"""Instrument ORM 模型 - 股票主数据。

对应迁移 002_instruments 中的 instruments 表：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- symbol: 股票代码（唯一，如 '000001'）
- name: 股票名称
- market: 市场（SH/SZ/BJ）
- status: 状态（active/delisted/suspended）
- listing_date: 上市日期（可空，pytdx 不直接提供）
- created_at/updated_at: 时间戳

设计说明：
- symbol 唯一约束（A 股代码跨市场不重叠：SH 6xxxxx / SZ 0xxxxx,3xxxxx / BJ 8xxxxx,4xxxxx）
- market + status 复合索引，支持按市场筛选活跃股票
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Date, DateTime, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Instrument(Base):
    """股票主数据。"""

    __tablename__ = "instruments"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_symbol", "symbol"),
        Index("ix_instruments_market_status", "market", "status"),
    )

    def __repr__(self) -> str:
        return f"<Instrument(symbol={self.symbol!r}, name={self.name!r}, market={self.market!r})>"


if __name__ == "__main__":
    # 自测入口：验证模型定义（不写库表）
    print(f"__tablename__={Instrument.__tablename__}")
    print(f"columns={list(Instrument.__table__.columns.keys())}")
    print(f"indexes={[idx.name for idx in Instrument.__table__.indexes]}")
    print("OK")
