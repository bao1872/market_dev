"""Transactional Outbox write boundary shared by business producers."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.outbox import Outbox


async def write_outbox(
    db: AsyncSession,
    event_type: str,
    payload: dict[str, Any],
    aggregate_type: str,
    aggregate_id: UUID | None = None,
    headers: dict[str, Any] | None = None,
) -> Outbox:
    """Write an Outbox record in the caller-owned transaction."""
    if not event_type:
        raise ValueError("event_type 不能为空")
    if not aggregate_type:
        raise ValueError("aggregate_type 不能为空")

    outbox = Outbox(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        payload=payload,
        headers=headers or {},
        status="pending",
        retry_count=0,
    )
    db.add(outbox)
    await db.flush()
    return outbox


__all__ = ["write_outbox"]
