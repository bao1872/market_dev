"""Market Dashboard read projection ORM 模型（PRD：Dashboard 只读投影）。

对应迁移：
- 095_market_dashboard_projection

两张投影表（UI 只读；由盘后 builder 写入，不属于本模块）：
- ``MarketDashboardMarketDaily``: 全市场每日一行（PK trade_date）
- ``MarketDashboardScopeDaily``: 板块/概念每日一行（PK board_id + trade_date）

字段与迁移一一对应；只存 counts，不存派生事实：
- 不存 ratio：读取时 ratio = ``ma{k}_above_count / ma{k}_valid_count``，
  避免「count 与 ratio 不一致」。
- 不存 normalized_index / MA delta / scope name / type / hierarchy level
  （派生值或已有 SSOT：MarketBoard / 读取层计算）。
- scope row 保存 ``membership_version``（latest_snapshot_replay），
  供 read path 检测 stale projection；本模块不实现 stale handling。
"""

# ruff: noqa: N811 - PgUUID 沿用仓库既有 postgres UUID 别名约定

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    Date,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
)
from sqlalchemy import func as sa_func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# MA 窗口档位（与 build_dashboard_history 的 WINDOWS 语义一致）
_WINDOWS: tuple[int, ...] = (5, 10, 20, 50, 120)


def _count_checks(prefix: str) -> tuple[CheckConstraint, ...]:
    """数据不变量（与 095 迁移保持一致）：非负且 above <= valid <= member。"""
    checks: list[CheckConstraint] = [
        CheckConstraint("member_count >= 0", name=f"{prefix}_member_count_check"),
        CheckConstraint("valid_return_count >= 0", name=f"{prefix}_valid_return_count_check"),
        CheckConstraint(
            "valid_return_count <= member_count",
            name=f"{prefix}_valid_return_le_member_check",
        ),
    ]
    for k in _WINDOWS:
        checks.append(
            CheckConstraint(
                f"ma{k}_valid_count >= 0 AND ma{k}_above_count >= 0 "
                f"AND ma{k}_above_count <= ma{k}_valid_count "
                f"AND ma{k}_valid_count <= member_count",
                name=f"{prefix}_ma{k}_counts_check",
            )
        )
    return tuple(checks)


class MarketDashboardMarketDaily(Base):
    """全市场（大盘）每日投影 - UI 只读。"""

    __tablename__ = "market_dashboard_market_daily"
    __table_args__ = (*_count_checks("market_dashboard_market_daily"),)

    trade_date: Mapped[date] = mapped_column(Date(), primary_key=True, nullable=False)
    member_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    valid_return_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    equal_weight_return: Mapped[float | None] = mapped_column(Float(), nullable=True)

    ma5_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma5_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma10_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma10_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma20_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma20_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma50_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma50_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma120_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma120_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # [PANJI-MARKET-OVERVIEW] additive 快照 / 轨迹列（全部可空，旧行 NULL 兼容）
    advance_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    decline_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    flat_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    change_valid_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    turnover_amount: Mapped[float | None] = mapped_column(Float(), nullable=True)
    turnover_valid_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    limit_up_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    limit_down_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    sse_close: Mapped[float | None] = mapped_column(Float(), nullable=True)
    szse_close: Mapped[float | None] = mapped_column(Float(), nullable=True)
    chinext_close: Mapped[float | None] = mapped_column(Float(), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=sa_func.now()
    )


class MarketDashboardScopeDaily(Base):
    """板块/概念每日投影 - UI 只读（membership=latest_snapshot_replay）。"""

    __tablename__ = "market_dashboard_scope_daily"
    __table_args__ = (
        PrimaryKeyConstraint("board_id", "trade_date"),
        ForeignKeyConstraint(["board_id"], ["market_boards.id"], ondelete="CASCADE"),
        Index("ix_market_dashboard_scope_daily_trade_date", "trade_date"),
        *_count_checks("market_dashboard_scope_daily"),
    )

    board_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    membership_version: Mapped[str] = mapped_column(String(128), nullable=False)

    member_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    valid_return_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    equal_weight_return: Mapped[float | None] = mapped_column(Float(), nullable=True)

    ma5_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma5_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma10_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma10_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma20_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma20_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma50_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma50_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma120_valid_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ma120_above_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=sa_func.now()
    )


if __name__ == "__main__":
    for cls in (MarketDashboardMarketDaily, MarketDashboardScopeDaily):
        cols = [c.name for c in cls.__table__.columns]
        pk = [c.name for c in cls.__table__.primary_key]
        print(f"{cls.__tablename__}: PK={pk}, columns={cols}")
        assert "member_count" in cols and "ma5_above_count" in cols
    print("OK")
