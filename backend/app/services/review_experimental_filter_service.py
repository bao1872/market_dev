"""Experimental Filter (Round 2B) — thin service layer.

Responsibilities only (prompt §16):
  - call ``compute_scope_evidence`` (L2-A Objective Evidence);
  - evaluate the Phase-1 Experimental Filter via the pure
    ``experimental_filter`` domain module;
  - return the list of ``CandidateResult`` dicts.

It NEVER writes to the DB, never saves Candidate / Signal / Discovery, never calls
legacy ``filter_engine`` / ``review_signal_service``, and never reads legacy
``P/Q/U/C/V`` payloads.  Round 2B CandidateResult is a runtime-only exploration
result (prompt §2 / §25): no persistence / repository / API / frontend.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.review import experimental_filter
from app.domain.review.experimental_filter import ExperimentConfig
from app.services.scope_evidence_service import compute_scope_evidence


async def evaluate_scope_experiment(
    session: AsyncSession,
    trade_date: date,
    scope_type: str,
    scope_key: str,
    config: ExperimentConfig | None = None,
) -> list[dict[str, Any]]:
    """Compute L2-A Evidence then evaluate all Phase-1 archetypes.

    Returns a list of ``CandidateResult`` dicts (one per archetype).  Read-only;
    nothing is persisted.
    """
    if config is None:
        config = ExperimentConfig()
    evidence = await compute_scope_evidence(session, trade_date, scope_type, scope_key)
    return experimental_filter.evaluate_scope(evidence, config)
