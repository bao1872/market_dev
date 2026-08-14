"""History Series Read Contract (v2.3 Analysis B/C prerequisite).

Thin read-only abstraction over the existing ``review_scope_observation_facts``
table.  This module is the **History Layer**: it fetches persisted daily
Canonical Observation Fact snapshots, filters by date range, orders them by
``trade_date``, and exposes availability metadata.

It deliberately does NOT perform any analysis:

    - no percentile / position computation
    - no velocity / acceleration / persistence derivation
    - no trend / structure-change classification
    - no signal / opportunity / risk generation

All of those belong to Analysis B (Historical Dynamics, §7.9) and Analysis C
(Internal Structure Dynamics, §7.10).  This service only answers:

    "what ordered historical observation series exists for this scope, and
     how complete / available is it?"

Ownership is therefore identical in spirit to the persistence service: serialize
read-back + availability metadata only.  No new table, no migration, no schema
change (audit conclusion: the missing prerequisite was a read contract, not
historical storage).

Activation guard (consistent with ``review_observation_persistence_service``):
only ``industry_l1 / industry_l2 / industry_l3 / concept`` are ever persisted,
so a query for a non-activated scope type fails closed instead of silently
returning an empty series that would look like "valid but empty history".
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_review import ReviewScopeObservationFact
from app.services.review_observation_persistence_service import (
    ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES,
    list_scope_observation_facts,
)


class ScopeHistoryNotActivatedError(Exception):
    """Raised when a non-activated scope type is queried for history.

    Mirrors ``ScopePersistenceNotActivatedError``: market / major_index / style
    are never persisted historically, so a history query for them is a contract
    error, not an empty-but-valid series.
    """


class ScopeHistoryDateRangeError(Exception):
    """Raised when from_date > to_date."""


def _date_or_none(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


async def get_observation_series(
    db: AsyncSession,
    *,
    scope_type: str,
    scope_key: str,
    from_date: date,
    to_date: date,
) -> dict[str, Any]:
    """Read the ordered historical observation series for one scope.

    Input owner: persisted ``ReviewScopeObservationFact`` rows (read-only).
    Output owner: ordered series + availability metadata (no analytics).

    Args:
        db: async DB session.
        scope_type: one activated scope family
            (``industry_l1 / industry_l2 / industry_l3 / concept``).
        scope_key: the specific scope identifier.
        from_date: inclusive lower bound (business trade_date).
        to_date: inclusive upper bound (business trade_date).

    Returns:
        A dict with two top-level keys:

        ``series``: ordered list of per-date snapshots, each
            ``{"trade_date": str, "readiness": str, "payload": dict}``.
            Ordered ascending by ``trade_date`` (guaranteed by the underlying
            ``list_scope_observation_facts`` order_by).  ``payload`` is the
            canonical L1 observation payload stored as-is — this service does
            NOT extract, transform, or recompute any field inside it.

        ``availability``: metadata describing series completeness:
            ``scope_type``, ``scope_key``, ``requested_from_date``,
            ``requested_to_date``, ``series_from_date``, ``series_to_date``,
            ``total_snapshots`` (rows returned), ``ready_snapshots``
            (count where ``readiness == "ready"``), ``partial_or_unavailable``
            (total minus ready), and ``status``:
              - ``empty``: no snapshots at all in the requested range;
              - ``partial``: at least one snapshot exists but some are not
                ``ready`` (PIT unavailable / no_members on that date);
              - ``ready``: every returned snapshot is ``ready``.

        No gap/calendar reconciliation is performed here — missing trade_dates
        are simply absent from ``series``.  Gap handling and minimum-sample
        policy are Analysis-layer (B/C) responsibilities, not this contract.

    Raises:
        ScopeHistoryNotActivatedError: scope_type is not in the activated set.
        ScopeHistoryDateRangeError: from_date > to_date.
    """
    if scope_type not in ACTIVATED_OBSERVATION_PERSISTENCE_SCOPE_TYPES:
        raise ScopeHistoryNotActivatedError(
            f"scope_type={scope_type!r} not activated for observation history; "
            "only industry_l1/l2/l3 and concept are persisted historically"
        )
    if from_date > to_date:
        raise ScopeHistoryDateRangeError(
            f"from_date={from_date.isoformat()} must be <= "
            f"to_date={to_date.isoformat()}"
        )

    rows: list[ReviewScopeObservationFact] = await list_scope_observation_facts(
        db,
        scope_type=scope_type,
        scope_key=scope_key,
        from_date=from_date,
        to_date=to_date,
    )

    series: list[dict[str, Any]] = [
        {
            "trade_date": row.trade_date.isoformat(),
            "readiness": row.readiness,
            "payload": row.observation_payload,
        }
        for row in rows
    ]

    total = len(series)
    ready = sum(1 for s in series if s["readiness"] == "ready")
    if total == 0:
        status = "empty"
    elif ready == total:
        status = "ready"
    else:
        status = "partial"

    availability: dict[str, Any] = {
        "scope_type": scope_type,
        "scope_key": scope_key,
        "requested_from_date": _date_or_none(from_date),
        "requested_to_date": _date_or_none(to_date),
        "series_from_date": series[0]["trade_date"] if series else None,
        "series_to_date": series[-1]["trade_date"] if series else None,
        "total_snapshots": total,
        "ready_snapshots": ready,
        "partial_or_unavailable": total - ready,
        "status": status,
    }

    return {"series": series, "availability": availability}
