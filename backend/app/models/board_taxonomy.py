"""Versioned Board taxonomy, point-in-time memberships, and non-Board universes."""
from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BoardDefinitionVersion(Base):
    """A versioned semantic definition for one MarketBoard projection row."""

    __tablename__ = "board_definition_versions"
    __table_args__ = (
        UniqueConstraint(
            "board_id", "effective_from", "definition_hash", "membership_version",
            name="uq_board_definition_versions_identity",
        ),
        Index(
            "ix_board_definition_versions_pit",
            "board_id", "effective_from", "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    board_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_boards.id", ondelete="RESTRICT"), nullable=False,
    )
    taxonomy: Mapped[str] = mapped_column(Text(), nullable=False)
    source: Mapped[str] = mapped_column(Text(), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    taxonomy_compatibility_key: Mapped[str] = mapped_column(Text(), nullable=False)
    board_type: Mapped[str] = mapped_column(Text(), nullable=False)
    hierarchy_level: Mapped[str] = mapped_column(Text(), nullable=False)
    parent_board_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("market_boards.id", ondelete="SET NULL"), nullable=True,
    )
    membership_version: Mapped[str] = mapped_column(Text(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    definition_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class BoardMembershipHistory(Base):
    """Point-in-time membership facts owned by a Board definition version."""

    __tablename__ = "board_membership_history"
    __table_args__ = (
        UniqueConstraint(
            "board_definition_version_id", "instrument_id", "effective_from",
            name="uq_board_membership_history_identity",
        ),
        Index(
            "ix_board_membership_history_pit",
            "board_definition_version_id", "effective_from", "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    board_definition_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("board_definition_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False,
    )
    membership_version: Mapped[str] = mapped_column(Text(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class UniverseDefinition(Base):
    """Versioned major-index/style universe; it is not a MarketBoard type."""

    __tablename__ = "universe_definitions"
    __table_args__ = (
        UniqueConstraint(
            "universe_key", "version", "membership_version", "effective_from",
            name="uq_universe_definitions_identity",
        ),
        Index("ix_universe_definitions_pit", "universe_key", "effective_from", "effective_to"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    universe_key: Mapped[str] = mapped_column(Text(), nullable=False)
    universe_type: Mapped[str] = mapped_column(Text(), nullable=False)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    source: Mapped[str] = mapped_column(Text(), nullable=False)
    version: Mapped[str] = mapped_column(Text(), nullable=False)
    compatibility_key: Mapped[str] = mapped_column(Text(), nullable=False)
    membership_version: Mapped[str] = mapped_column(Text(), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    population_status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )


class UniverseMembership(Base):
    """Point-in-time member of a versioned index/style universe."""

    __tablename__ = "universe_memberships"
    __table_args__ = (
        UniqueConstraint(
            "universe_definition_id", "instrument_id", "effective_from",
            name="uq_universe_memberships_identity",
        ),
        Index(
            "ix_universe_memberships_pit",
            "universe_definition_id", "effective_from", "effective_to",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    universe_definition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("universe_definitions.id", ondelete="CASCADE"), nullable=False,
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False,
    )
    weight: Mapped[float | None] = mapped_column(nullable=True)
    effective_from: Mapped[date] = mapped_column(Date(), nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
