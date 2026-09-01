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
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auction.coverage import ScanCoverage
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

# KPI-1: the formal publication owner — the ONLY creator of publication rows.
from app.services.auction_publication_service import publish_auction_analysis

__all__ = [
    "V32_ALGORITHM_VERSION",
    "build_scan_run_kwargs",
    "build_scope_result_kwargs",
    "persist_v32_scope_results",
]

#: V3.2 algorithm identity.  A distinct value keeps the V3.2 publication row
#: separate from legacy publications on the same trade_date.


_DEFAULT_PRICE_ADJUSTMENT_VERSION = "none"


def build_scan_run_kwargs(
    *,
    trade_date: date,
    coverage: ScanCoverage,
    auction_type: str = "scope_v32",
) -> dict[str, Any]:
    """Assemble a V3.2 ``AuctionScanRun`` payload (pure, no session).

    The algorithm version is NOT a parameter: a caller must never be able to
    create a V3.2 run under a different algorithm identity, or write and read
    would drift apart.

    Coverage is NOT computed here: it arrives as a :class:`ScanCoverage` from the
    single coverage owner (``domain/auction/coverage.py``).  This service only
    projects the already-determined values onto the run columns.
    """
    fields = coverage.as_scan_run_fields()
    return {
        "trade_date": trade_date,
        "auction_type": auction_type,
        # No anchor forgery: V3.2 has no anchor snapshot / anchor publication.
        "source_anchor_snapshot_id": None,
        "source_anchor_publication_id": None,
        "algorithm_version": V32_ALGORITHM_VERSION,
        "price_adjustment_version": _DEFAULT_PRICE_ADJUSTMENT_VERSION,
        "status": "succeeded",
        "attempt_count": 1,
        "eligible_count": fields["eligible_count"],
        "ready_count": fields["ready_count"],
        "coverage_ratio": fields["coverage_ratio"],
        "missing_count": fields["missing_count"],
        "missing_reasons": fields["missing_reasons"],
        "started_at": datetime.now(UTC),
        "finished_at": datetime.now(UTC),
    }

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


async def persist_v32_scope_results(
    session: AsyncSession,
    *,
    trade_date: date,
    scope_results: list[dict[str, Any]],
    capture_run_id: uuid.UUID,
    test_namespace: str,
    coverage: ScanCoverage,
    truth_status: str,
) -> uuid.UUID:
    """Persist one V3.2 run.  No commit here — the caller owns the transaction.

    The algorithm version is fixed to the V3.2 owner; it is intentionally not a
    parameter so a caller cannot create a non-V3.2 run under this path.
    """
    algorithm_version = V32_ALGORITHM_VERSION
    """Atomically persist one V3.2 run: scan run -> scope results -> publication.

    Everything happens in a single transaction, so a partially written result
    set is never visible.  Returns the created scan_run_id.

    ``truth_status`` / ``capture_source`` / ``test_namespace`` are inherited
    from the real capture run and truth gate (no defaults, never fabricated).
    """
    run = AuctionScanRun(
        **build_scan_run_kwargs(
            trade_date=trade_date,
            coverage=coverage,
            algorithm_version=algorithm_version,
        )
    )
    session.add(run)
    await session.flush()  # materialise run.id before children reference it

    for row in scope_results:
        session.add(
            AuctionScopeResult(
                **build_scope_result_kwargs(
                    scan_run_id=run.id,
                    trade_date=trade_date,
                    scope_type=row["scope_type"],
                    scope_id=row.get("scope_id"),
                    scope_name=row.get("scope_name"),
                    payload=row["payload"],
                    reason_codes=row.get("reason_codes"),
                )
            )
        )

    # KPI-1: the publication row is created by the EXISTING formal owner.
    # It re-reads ScanRun / CaptureRun / ScopeResult and evaluates the gate;
    # V3.2 must NOT interpret capture_source, coverage thresholds, namespace
    # or truth_status semantics itself.  It only flushes, so the single
    # transaction is closed by the commit below.
    await publish_auction_analysis(
        session,
        scan_run_id=run.id,
        capture_run_id=capture_run_id,
        truth_status=truth_status,
        test_namespace=test_namespace,
    )

    # No commit here: the caller/orchestrator owns the single transaction.
    return run.id


def payload_schema_version() -> str:
    """Expose the payload schema version recorded inside every V3.2 payload."""
    return SCHEMA_VERSION
