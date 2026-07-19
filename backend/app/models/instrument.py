"""Instrument ORM 模型 - 股票主数据。

对应迁移 002_instruments / 039_instruments_pinyin_initials 中的 instruments 表：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- symbol: 股票代码（唯一，如 '000001'）
- name: 股票名称
- pinyin_initials: 名称拼音首字母（小写，如 '东睦股份' -> 'dmgf'，主数据同步时生成）
- market: 市场（SH/SZ/BJ）
- status: 状态（active/delisted/suspended）
- listing_date: 上市日期（可空，pytdx 不直接提供）
- created_at/updated_at: 时间戳

设计说明：
- symbol 唯一约束（A 股代码跨市场不重叠：SH 6xxxxx / SZ 0xxxxx,3xxxxx / BJ 8xxxxx,4xxxxx）
- market + status 复合索引，支持按市场筛选活跃股票
- pinyin_initials 索引，支持拼音首字母前缀搜索（如 'dmgf' -> 东睦股份）
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Date, DateTime, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_indexes
from app.models.base import Base


class Instrument(Base):
    """股票主数据。"""

    __tablename__ = "instruments"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 拼音首字母（小写），主数据同步时由 pinyin_util 生成；可空（兼容历史数据回补前）
    pinyin_initials: Mapped[str | None] = mapped_column(String(20), nullable=True)
    market: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # CHANGE-20260713-010: 总股本/流通股本（pytdx get_finance_info zongguben/liutongguben）
    # 每日同步链更新，用户请求时只从 DB 读取，不调用 pytdx
    total_share: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=0), nullable=True
    )
    float_share: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=0), nullable=True
    )
    share_as_of: Mapped[date | None] = mapped_column(Date, nullable=True)
    # CHANGE-20260718-005: 复权因子对账版本跟踪（全市场一致性修复）
    # 版本 < 当前常量版本时标记 needs_reaudit（即使因子值看起来正确）
    # 弥补 xdxr fingerprint 无法发现"fingerprint 未变但历史序列已错误"的缺口
    factor_algorithm_version: Mapped[str | None] = mapped_column(
        String(8), nullable=True
    )
    factor_reconciliation_version: Mapped[int | None] = mapped_column(
        nullable=True
    )
    factor_reconciled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_instruments_symbol", "symbol"),
        Index("ix_instruments_market_status", "market", "status"),
        Index("ix_instruments_pinyin_initials", "pinyin_initials"),
    )

    def __repr__(self) -> str:
        return f"<Instrument(symbol={self.symbol!r}, name={self.name!r}, market={self.market!r})>"


if __name__ == "__main__":
    # 自测入口：验证模型定义（不写库表）
    print(f"__tablename__={Instrument.__tablename__}")
    print(f"columns={list(Instrument.__table__.columns.keys())}")
    print(f"indexes={[idx.name for idx in table_indexes(Instrument)]}")
    print("OK")
