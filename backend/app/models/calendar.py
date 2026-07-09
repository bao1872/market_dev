"""TradingCalendar ORM 模型 - 交易日历。

对应迁移 002_instruments / 046_calendar_source_status / 047_calendar_semantics_fix 中的
trading_calendar 表：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- trade_date: 交易日期（唯一，配合 market 复合唯一约束）
- is_trading_day: 是否为交易日
- market: 市场标识（A 表示 A 股整体；HS 表示沪深）
- source: 数据来源（MOOTDX_HOLIDAY/MOOTDX_HISTORICAL/MANUAL_OVERRIDE），用于自愈决策
- status: 确认状态（OPEN/CLOSED/UNKNOWN）
- verified_at: 最近一次被权威数据确认的时间戳
- note: 人工备注（256 字符）
- validation_error: 校验失败时的错误说明（512 字符）
- created_at: 创建时间

设计说明：
- (trade_date, market) 复合唯一约束，支持多市场日历
- trade_date 单列索引，支持日期范围查询
- source/status 配合 is_trading_day_async 自愈机制：
  - OPEN: 权威交易日，直接返回 True
  - CLOSED: 权威非交易日（周末/节假日），直接返回 False
  - UNKNOWN: 未确认，降级查询 Mootdx 在线（避免把 DB false 当权威）
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import Boolean, Date, DateTime, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base
from app.services.mootdx_calendar_provider import (
    CALENDAR_STATUS_UNKNOWN,
    MOOTDX_HOLIDAY_SOURCE,
)


class TradingCalendar(Base):
    """交易日历。"""

    __tablename__ = "trading_calendar"

    id: Mapped[UUID] = mapped_column(primary_key=True, server_default=func.gen_random_uuid())
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    market: Mapped[str] = mapped_column(String(8), nullable=False, default="A")
    # [Calendar] - 描述: source 数据来源（MOOTDX_HOLIDAY/MOOTDX_HISTORICAL/MANUAL_OVERRIDE）
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=MOOTDX_HOLIDAY_SOURCE, default=MOOTDX_HOLIDAY_SOURCE
    )
    # [Calendar] - 描述: status 确认状态（OPEN/CLOSED/UNKNOWN）
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=CALENDAR_STATUS_UNKNOWN, default=CALENDAR_STATUS_UNKNOWN
    )
    # [Calendar] - 描述: verified_at 最近一次被权威数据确认的时间戳
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # [Calendar] - 描述: note 人工备注（可空，最大 256 字符）
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # [Calendar] - 描述: validation_error 校验失败说明（可空，最大 512 字符）
    validation_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
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
            f"is_trading_day={self.is_trading_day!r}, market={self.market!r}, "
            f"source={self.source!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证模型定义（不写库表）
    print(f"__tablename__={TradingCalendar.__tablename__}")
    print(f"columns={list(TradingCalendar.__table__.columns.keys())}")
    print(f"indexes={[idx.name for idx in table_indexes(TradingCalendar)]}")
    print(f"constraints={[c.name for c in table_constraints(TradingCalendar) if c.name]}")
    # 验证新增字段
    cols = list(TradingCalendar.__table__.columns.keys())
    assert "source" in cols, "缺少 source 字段"
    assert "status" in cols, "缺少 status 字段"
    assert "verified_at" in cols, "缺少 verified_at 字段"
    assert "note" in cols, "缺少 note 字段"
    assert "validation_error" in cols, "缺少 validation_error 字段"
    print("OK: 新增字段验证通过")
