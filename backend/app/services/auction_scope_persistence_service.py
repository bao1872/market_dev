"""Auction V3.2 persistence service (REUSE_WITH_V32_SEMANTICS).

Persistence decision (audited, see V3.2 §二/§三):
    PERSISTENCE_DECISION = REUSE_WITH_V32_SEMANTICS

Rationale — everything below was verified against the models, not assumed:

- ``AuctionScanRun`` can honestly represent a V3.2 computation run: its two
  anchor foreign keys (``source_anchor_snapshot_id`` /
  ``source_anchor_publication_id``) are **nullable**, so V3.2 records ``NULL``
  instead of forging an anchor or pretending to run the legacy
  Structure/Chip/Event lifecycle.  It also carries ``algorithm_version``.
- ``AuctionAnalysisPublication.capture_run_id`` is a **non-null** FK, so it must
  come from a real acquisition run:
    * live lane  -> the real ``AuctionQuoteCaptureRun`` of the capture service;
    * historical -> ``get_or_create_historical_capture_run()``, which is
      idempotent and creates one real row per (trade_date, source, namespace).
  No UUID is ever fabricated here: ``capture_run_id`` is a caller-supplied real
  identity.
- ``AuctionAnalysisPublication`` has a unique constraint on
  ``(trade_date, algorithm_version)``, so V3.2 gets its own publication row and
  cannot collide with legacy semantics.

Visibility: results are written inside ONE transaction and only become visible
through the publication row.  Nothing here falls back to "latest succeeded run".
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auction.scope_payload import (
    SCHEMA_VERSION,
    canonical_scope_name,
    parse_scope_payload,
)
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import (
    AuctionScanRun,
    AuctionScopeResult,
)
from app.services.auction_scan_run_lifecycle import V32_AUCTION_TYPE

# KPI-1: the formal publication owner — the ONLY creator of publication rows.

__all__ = [
    "V32_ALGORITHM_VERSION",
    "build_scope_result_kwargs",
    "persist_v32_scope_results",
]

#: V3.2 algorithm identity.  A distinct value keeps the V3.2 publication row
#: separate from legacy publications on the same trade_date.


_DEFAULT_PRICE_ADJUSTMENT_VERSION = "none"


def build_scope_result_kwargs(
    *,
    scan_run_id: uuid.UUID,
    trade_date: date,
    scope_type: str,
    scope_id: uuid.UUID | None,
    scope_name: str | None,
    payload: dict[str, Any],
    reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble one ``AuctionScopeResult`` row for V3.2 (pure, no session).

    Only the V3.2-relevant columns are set.  Legacy columns
    (structure/chip/status_label/confidence_level/...) keep their defaults and
    are deliberately NOT written — V3.2 must not populate legacy semantics.
    """
    # Fail fast on a malformed payload before anything is persisted.
    parse_scope_payload(payload)
    # The canonical display name lives in payload.identity.scope_name only.
    # A caller-supplied scope_name must agree; when absent it is DERIVED from
    # the canonical payload, never invented.  The DB column is a compatibility
    # projection, not a business owner.
    canonical_name = canonical_scope_name(payload)
    if scope_name is not None and scope_name != canonical_name:
        raise ValueError(
            f"scope_name drift: caller supplied {scope_name!r} but the canonical "
            f"payload identity says {canonical_name!r}"
        )
    scope_name = canonical_name
    return {
        "scan_run_id": scan_run_id,
        "trade_date": trade_date,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "payload": payload,
        "reason_codes": list(reason_codes or []),
    }


def validate_v32_run_identity(run: Any, *, trade_date: date) -> None:
    """Fail-closed identity check for the run V3.2 results are written into.

    AuctionScanRun has exactly ONE lifecycle owner
    (:mod:`app.services.auction_scan_run_lifecycle`).  This function never
    creates a run; it only refuses a run that is not the V3.2 run for
    ``trade_date``, which would otherwise collide with
    ``UNIQUE(trade_date, auction_type, algorithm_version)``.
    """
    if run.trade_date != trade_date:
        raise ValueError(
            f"scan run trade_date mismatch: run={run.trade_date} expected={trade_date}"
        )
    if run.auction_type != V32_AUCTION_TYPE:
        raise ValueError(
            f"scan run auction_type mismatch: {run.auction_type!r} "
            f"expected {V32_AUCTION_TYPE!r}"
        )
    if run.algorithm_version != V32_ALGORITHM_VERSION:
        raise ValueError(
            f"scan run algorithm_version mismatch: {run.algorithm_version!r} "
            f"expected {V32_ALGORITHM_VERSION!r}"
        )
    if run.status != "running":
        raise ValueError(
            f"scan run must be running to receive results, got {run.status!r}"
        )


async def persist_v32_scope_results(
    session: AsyncSession,
    *,
    run: AuctionScanRun,
    trade_date: date,
    scope_results: list[dict[str, Any]],
) -> uuid.UUID:
    """Persist V3.2 scope results into an EXISTING run.

    This function does NOT create, complete or publish a run — the single
    lifecycle owner does that.  It only validates the run identity, writes the
    scope result children, and flushes.

    Everything happens in the caller's transaction; ``session.commit()`` is
    never called here.
    """
    validate_v32_run_identity(run, trade_date=trade_date)
    # Validate and normalise EVERY payload before touching the session, so a
    # malformed payload cannot leave half-written children behind.
    prepared_rows = [
        build_scope_result_kwargs(
            scan_run_id=run.id,
            trade_date=trade_date,
            scope_type=row["scope_type"],
            scope_id=row.get("scope_id"),
            scope_name=row.get("scope_name"),
            payload=row["payload"],
            reason_codes=row.get("reason_codes"),
        )
        for row in scope_results
    ]

    for kwargs in prepared_rows:
        session.add(AuctionScopeResult(**kwargs))
    await session.flush()

    # No commit here: the caller/orchestrator owns the single transaction.
    return run.id


def payload_schema_version() -> str:
    """Expose the payload schema version recorded inside every V3.2 payload."""
    return SCHEMA_VERSION
