"""MarketBoard + MarketBoardMembership 模型（PRD §7.5 qstock 板块同步）。

只存最新态，不增加历史日期维度。
- MarketBoard: 板块目录（行业/概念）
- MarketBoardMembership: 板块成分股关系
"""

# ruff: noqa: N815, N811 - camelCase 属性为前端 JSON API 契约，DB 列名通过 mapped_column 映射为 snake_case

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import TIMESTAMP, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class MarketBoard(Base):
    """板块目录 - 行业/概念板块（只存最新态）。"""

    __tablename__ = "market_boards"
    __table_args__ = (
        UniqueConstraint("external_code", "type", name="uq_market_boards_code_type"),
    )

    id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, server_default="gen_random_uuid()"
    )
    externalCode: Mapped[str] = mapped_column(
        "external_code", String(32), nullable=False, comment="外部代码（qstock 原始代码）"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="板块名称")
    type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="板块类型：industry | concept"
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default="now()"
    )

    memberships: Mapped[list[MarketBoardMembership]] = relationship(
        back_populates="board", cascade="all, delete-orphan"
    )


class MarketBoardMembership(Base):
    """板块成分股关系 - 只存最新态。"""

    __tablename__ = "market_board_memberships"

    boardId: Mapped[UUID] = mapped_column(
        "board_id",
        PgUUID(as_uuid=True),
        ForeignKey("market_boards.id", ondelete="CASCADE"),
        primary_key=True,
    )
    instrumentId: Mapped[UUID] = mapped_column(
        "instrument_id",
        PgUUID(as_uuid=True),
        ForeignKey("instruments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    updatedAt: Mapped[datetime] = mapped_column(
        "updated_at", TIMESTAMP(timezone=True), nullable=False, server_default="now()"
    )

    board: Mapped[MarketBoard] = relationship(back_populates="memberships")
