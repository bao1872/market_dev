"""Point-in-time Board and universe membership resolution."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.board_taxonomy import (
    BoardDefinitionVersion,
    BoardMembershipHistory,
    UniverseDefinition,
    UniverseMembership,
)


class PITMembershipUnavailableError(RuntimeError):
    """Raised when a scope has no truthful membership version for the requested date."""


@dataclass(frozen=True)
class PITMembership:
    instrument_ids: tuple[uuid.UUID, ...]
    taxonomy_version: str
    compatibility_key: str
    membership_version: str
    population_status: str = "ready"


async def resolve_board_membership_at(
    session: AsyncSession,
    board_id: uuid.UUID,
    trade_date: date,
) -> PITMembership:
    """Resolve a Board using only the definition/memberships valid on trade_date."""
    definition_stmt = (
        select(BoardDefinitionVersion)
        .where(
            BoardDefinitionVersion.board_id == board_id,
            BoardDefinitionVersion.effective_from <= trade_date,
            or_(
                BoardDefinitionVersion.effective_to.is_(None),
                BoardDefinitionVersion.effective_to > trade_date,
            ),
        )
        .order_by(
            BoardDefinitionVersion.effective_from.desc(),
            BoardDefinitionVersion.created_at.desc(),
        )
        .limit(1)
    )
    definition = (await session.execute(definition_stmt)).scalar_one_or_none()
    if definition is None:
        raise PITMembershipUnavailableError(
            f"bootstrap_unavailable: board={board_id} trade_date={trade_date}"
        )
    member_stmt = select(BoardMembershipHistory.instrument_id).where(
        BoardMembershipHistory.board_definition_version_id == definition.id,
        BoardMembershipHistory.effective_from <= trade_date,
        or_(
            BoardMembershipHistory.effective_to.is_(None),
            BoardMembershipHistory.effective_to > trade_date,
        ),
    )
    member_ids = tuple(row[0] for row in await session.execute(member_stmt))
    return PITMembership(
        instrument_ids=member_ids,
        taxonomy_version=definition.taxonomy_version,
        compatibility_key=definition.taxonomy_compatibility_key,
        membership_version=definition.membership_version,
        population_status="ready" if member_ids else "blocked_external_population",
    )


async def list_universe_definitions_at(
    session: AsyncSession,
    trade_date: date,
    *,
    universe_type: str | None = None,
) -> list[UniverseDefinition]:
    stmt = select(UniverseDefinition).where(
        UniverseDefinition.effective_from <= trade_date,
        or_(
            UniverseDefinition.effective_to.is_(None),
            UniverseDefinition.effective_to > trade_date,
        ),
    )
    if universe_type is not None:
        stmt = stmt.where(UniverseDefinition.universe_type == universe_type)
    stmt = stmt.order_by(UniverseDefinition.universe_type, UniverseDefinition.name)
    return list((await session.execute(stmt)).scalars())


async def resolve_universe_membership_at(
    session: AsyncSession,
    universe_key: str,
    trade_date: date,
) -> tuple[UniverseDefinition, PITMembership]:
    stmt = (
        select(UniverseDefinition)
        .where(
            UniverseDefinition.universe_key == universe_key,
            UniverseDefinition.effective_from <= trade_date,
            or_(
                UniverseDefinition.effective_to.is_(None),
                UniverseDefinition.effective_to > trade_date,
            ),
        )
        .order_by(
            UniverseDefinition.effective_from.desc(),
            UniverseDefinition.created_at.desc(),
        )
        .limit(1)
    )
    definition = (await session.execute(stmt)).scalar_one_or_none()
    if definition is None:
        raise PITMembershipUnavailableError(
            f"bootstrap_unavailable: universe={universe_key} trade_date={trade_date}"
        )
    member_stmt = select(UniverseMembership.instrument_id).where(
        UniverseMembership.universe_definition_id == definition.id,
        UniverseMembership.effective_from <= trade_date,
        or_(
            UniverseMembership.effective_to.is_(None),
            UniverseMembership.effective_to > trade_date,
        ),
    )
    member_ids = tuple(row[0] for row in await session.execute(member_stmt))
    status = definition.population_status
    if not member_ids and status == "ready":
        status = "blocked_external_population"
    return definition, PITMembership(
        instrument_ids=member_ids,
        taxonomy_version=definition.version,
        compatibility_key=definition.compatibility_key,
        membership_version=definition.membership_version,
        population_status=status,
    )


def batch_version(values: list[str], *, prefix: str) -> str:
    """Build a deterministic batch identity without pretending mixed versions are one value."""
    normalized = sorted(set(values))
    digest = hashlib.sha256("\n".join(normalized).encode()).hexdigest()[:16]
    return f"{prefix}:{digest}"
