"""Thin L2 Observation Group service (v2.3, §7.7 / plan §8).

Ownership is strictly: read an already-persisted L1 ``ReviewScopeObservationFact``
-> take its ``observation_payload`` (the canonical L1 SSOT) -> call the pure
``build_l2_observation_groups`` projection -> return the L2 backend object.

This service MUST NOT:
- recompute L1 facts,
- query First Pyramid / bars / VolumeContext,
- perform Analysis / Interpretation / Attribution,
- mutate the persisted payload.

It reuses the existing persistence read-back owner
(``get_scope_observation_fact``) rather than duplicating SQL selection.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.observation_groups import build_l2_observation_groups
from app.services.review_observation_persistence_service import (
    get_scope_observation_fact,
)


async def get_scope_observation_groups(
    db: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
) -> dict[str, Any] | None:
    """Read a persisted L1 fact and project its L2 Observation Groups.

    Returns ``None`` when no persisted fact exists for the grain (same contract as
    the underlying read-back).  The projected L2 object is derived purely from
    the canonical ``observation_payload``; no recomputation occurs here.
    """
    fact = await get_scope_observation_fact(db, trade_date, scope_type, scope_key)
    if fact is None:
        return None
    payload = fact.observation_payload
    if not isinstance(payload, dict):
        return None
    return build_l2_observation_groups(payload)


__all__ = ["get_scope_observation_groups"]
