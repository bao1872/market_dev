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
from app.domain.auction.scope_payload import SCHEMA_VERSION, parse_scope_payload
from app.models.auction import (
    AuctionAnalysisPublication,
    AuctionScanRun,
    AuctionScopeResult,
)

__all__ = [
    "V32_ALGORITHM_VERSION",
    "build_scan_run_kwargs",
    "build_scope_result_kwargs",
    "build_publication_kwargs",
    "persist_v32_scope_results",
]

#: V3.2 algorithm identity.  A distinct value keeps the V3.2 publication row
#: separate from legacy publications on the same trade_date.
V32_ALGORITHM_VERSION = "auction-v3.2"

_DEFAULT_PRICE_ADJUSTMENT_VERSION = "none"


def build_scan_run_kwargs(
    *,
    trade_date: date,
    coverage: ScanCoverage,
    algorithm_version: str = V32_ALGORITHM_VERSION,
    auction_type: str = "scope_v32",
) -> dict[str, Any]:
    """Assemble a V3.2 ``AuctionScanRun`` payload (pure, no session).

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
        "algorithm_version": algorithm_version,
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
    return {
        "scan_run_id": scan_run_id,
        "trade_date": trade_date,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "scope_name": scope_name,
        "payload": payload,
        "reason_codes": list(reason_codes or []),
    }


def build_publication_kwargs(
    *,
    trade_date: date,
    scan_run_id: uuid.UUID,
    capture_run_id: uuid.UUID,
    coverage_ratio: float,
    test_namespace: str,
    truth_status: str,
    capture_source: str,
    algorithm_version: str = V32_ALGORITHM_VERSION,
    gate_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the V3.2 publication row — the ONLY visibility boundary.

    ``truth_status`` / ``test_namespace`` / ``capture_source`` are REQUIRED and
    must be INHERITED from the real capture run and truth gate.  They
    deliberately have no default: defaulting ``truth_status`` to ``"verified"``
    would forge a multi-source consensus claim that a single-family
    (pytdx/tongdaxin) source cannot make — PRD §0.0-A freezes that pytdx/mootdx
    are ONE supply chain and must never count as two independent provider
    families (``AuctionTruthPolicy.min_independent_sources = 2``).

    ``capture_source`` records the lane (``verified_consensus`` for today's live
    consensus run, ``historical_backfill`` for a historical day) so provenance
    stays auditable inside ``gate_evidence``.
    """
    if not truth_status:
        raise ValueError("truth_status must be inherited from the real capture run")
    if not test_namespace:
        raise ValueError("test_namespace must be inherited from the real capture run")

    evidence = dict(gate_evidence or {})
    evidence.setdefault("capture_source", capture_source)
    evidence.setdefault("algorithm_version", algorithm_version)

    return {
        "trade_date": trade_date,
        "scan_run_id": scan_run_id,
        # REAL acquisition identity supplied by the caller; never fabricated.
        "capture_run_id": capture_run_id,
        "algorithm_version": algorithm_version,
        "test_namespace": test_namespace,
        "coverage_ratio": coverage_ratio,
        "truth_status": truth_status,
        "gate_evidence": evidence,
        "published_at": datetime.now(UTC),
    }


async def persist_v32_scope_results(
    session: AsyncSession,
    *,
    trade_date: date,
    scope_results: list[dict[str, Any]],
    capture_run_id: uuid.UUID,
    test_namespace: str,
    coverage: ScanCoverage,
    algorithm_version: str = V32_ALGORITHM_VERSION,
    truth_status: str,
    capture_source: str,
    gate_evidence: dict[str, Any] | None = None,
) -> uuid.UUID:
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

    session.add(
        AuctionAnalysisPublication(
            **build_publication_kwargs(
                trade_date=trade_date,
                scan_run_id=run.id,
                capture_run_id=capture_run_id,
                coverage_ratio=coverage.coverage_ratio,
                test_namespace=test_namespace,
                algorithm_version=algorithm_version,
                truth_status=truth_status,
                capture_source=capture_source,
                gate_evidence=gate_evidence,
            )
        )
    )

    await session.commit()
    return run.id


def payload_schema_version() -> str:
    """Expose the payload schema version recorded inside every V3.2 payload."""
    return SCHEMA_VERSION
