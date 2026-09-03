"""C1 Cross-sectional Analysis service (v2.3 §7.8.1).

Ownership is strictly: read already-persisted L1 ``ReviewScopeObservationFact``
rows -> assemble the comparable peer cohort from the canonical
``observation_payload`` (L1 SSOT) -> delegate to the pure
``compute_cross_sectional`` projection -> return cross-sectional evidence.

This service MUST NOT:
- access bars / ticks / First Pyramid / indicators,
- recompute any L1/L2 fact,
- perform Analysis / Interpretation / Attribution beyond the projection contract,
- mutate the persisted payloads.

Peer cohort construction (PRD §6.4.1 / §7.8.1 A):

    A scope's comparable peers are all other persisted facts that share the
    same ``scope_type`` (i.e. same family topology) for the same ``trade_date``.
    The current scope is included in the cohort so ``peer_count`` and
    ``valid_peer_count`` are measured consistently with the L1 percentile rule.

The service reuses the existing persistence read owners
(``get_scope_observation_fact`` / ``list_scope_observation_facts``) rather than
duplicating SQL selection.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review.analysis.cross_sectional import compute_cross_sectional
from app.services.review_observation_persistence_service import (
    get_scope_observation_fact_by_run,
    list_scope_observation_facts,
)
from app.services.review_publication_service import get_published_review_run_id


async def get_cross_sectional(
    db: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
) -> dict[str, Any] | None:
    """Read persisted L1 facts and compute C1 cross-sectional evidence.

    [R3 Cross-sectional P0] Published-run lineage: the current scope fact AND the
    comparable peer cohort are resolved through the *published* ReviewRun for
    ``trade_date`` (``review_run_id`` gate).  This replaces the previous
    ``get_scope_observation_fact`` + same-day global ``list_scope_observation_facts``
    path, which could mix a later same-day run into the published cohort
    (run-lineage contamination).  No recomputation occurs here.

    Returns ``None`` when no published run / fact exists for the current grain.
    """
    run_id = await get_published_review_run_id(db, trade_date)
    if run_id is None:
        return None

    current_fact = await get_scope_observation_fact_by_run(
        db, run_id, trade_date, scope_type, scope_key
    )
    if current_fact is None:
        return None
    current_payload = current_fact.observation_payload
    if not isinstance(current_payload, dict):
        return None

    # Comparable peer cohort: same published run + same family + same trade_date
    # (includes the current scope itself, per C1 peer universe contract).
    cohort_facts = await list_scope_observation_facts(
        db,
        review_run_id=run_id,
        scope_type=scope_type,
        from_date=trade_date,
        to_date=trade_date,
    )

    peer_payloads: dict[str, dict[str, Any]] = {}
    for fact in cohort_facts:
        payload = fact.observation_payload
        if isinstance(payload, dict):
            peer_payloads[fact.scope_key] = payload

    return compute_cross_sectional(
        current_payload=current_payload,
        peer_payloads=peer_payloads,
        current_scope_key=scope_key,
    )


__all__ = ["get_cross_sectional"]
