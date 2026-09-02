"""Minimal V3.2 writer / orchestrator — ONE entry point.

Chain:

    load inputs
      -> acquire_v32_scan_run          (one run identity + lease token)
      -> prepare_v32_analysis          (pure computation owner)
      -> persist_v32_scope_results     (same run, same lease token)
      -> complete_scan_run             (same run, same lease token)
      -> publish_auction_analysis      (formal publication owner)

The caller owns the transaction: nothing here commits.  Failure marks the SAME
run failed and never publishes.  A repeated succeeded run returns an idempotent
result instead of recomputing or re-publishing.

This module owns NO formula.  Computation belongs to
:mod:`app.domain.auction.analysis_preparation`; lifecycle belongs to
:mod:`app.services.auction_scan_run_lifecycle` / ``_terminal``.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auction.analysis_preparation import prepare_v32_analysis
from app.domain.auction.member_fact import AuctionMemberFactConfig
from app.services.auction_publication_service import publish_auction_analysis
from app.services.auction_scan_run_lifecycle import (
    AuctionScanConflictError,
    acquire_v32_scan_run,
)
from app.services.auction_scan_run_terminal import (
    complete_scan_run,
    mark_scan_run_failed,
)
from app.services.auction_scope_persistence_service import persist_v32_scope_results
from app.services.auction_v32_input_loader import (
    V32InputUnavailableError,
    load_v32_inputs,
)

__all__ = ["V32RunOutcome", "run_v32_auction_analysis"]

_STATUS_SUCCEEDED = "succeeded"
_STATUS_IDEMPOTENT = "idempotent"
_STATUS_CONFLICT = "conflict"
_STATUS_UNAVAILABLE = "unavailable"
_STATUS_FAILED = "failed"


class V32RunOutcome:
    """Result of one V3.2 lane execution.  Plain data, no DB handles."""

    __slots__ = ("status", "run_id", "capture_run_id", "scope_count", "detail")

    def __init__(
        self,
        status: str,
        *,
        run_id: UUID | None = None,
        capture_run_id: UUID | None = None,
        scope_count: int = 0,
        detail: str | None = None,
    ) -> None:
        self.status = status
        self.run_id = run_id
        self.capture_run_id = capture_run_id
        self.scope_count = scope_count
        self.detail = detail

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "capture_run_id": self.capture_run_id,
            "scope_count": self.scope_count,
            "detail": self.detail,
        }


async def run_v32_auction_analysis(
    db: AsyncSession,
    *,
    trade_date: date,
    capture_run_id: UUID,
    worker_id: str,
    lease_epoch: int | None = None,
    truth_status: str = "verified",
    test_namespace: str = "production",
    config: AuctionMemberFactConfig | None = None,
    window: int = 120,
) -> V32RunOutcome:
    """Run the V3.2 lane once.  Never commits; the caller owns the transaction."""
    # 1. one run identity + lease token
    try:
        run = await acquire_v32_scan_run(
            db, trade_date=trade_date, worker_id=worker_id, lease_epoch=lease_epoch
        )
    except AuctionScanConflictError as exc:
        return V32RunOutcome(_STATUS_CONFLICT, detail=str(exc))

    if run is None:
        # an identical run already succeeded: idempotent, do not recompute
        return V32RunOutcome(
            _STATUS_IDEMPOTENT, capture_run_id=capture_run_id
        )

    lease = run.lease_epoch

    try:
        # 2. bounded bulk inputs (fail-closed if the capture/calendar is absent)
        inputs = await load_v32_inputs(
            db, trade_date=trade_date, capture_run_id=capture_run_id, window=window
        )

        # 3. pure computation owner
        prepared = prepare_v32_analysis(
            trade_date=trade_date,
            trade_dates=inputs.trade_slots,
            observations_by_date=inputs.observations_by_date,
            edges=inputs.edges,
            config=config or _default_config(),
        )

        # 4. persist into the SAME run, with the SAME lease token
        await persist_v32_scope_results(
            db,
            run=run,
            trade_date=trade_date,
            scope_results=[
                {
                    "scope_type": scope.family,
                    "scope_id": None,
                    "scope_name": None,  # derived from the canonical payload
                    "payload": scope.payload,
                }
                for scope in prepared.scopes
            ],
            worker_id=worker_id,
            lease_epoch=lease,
        )

        # 5. terminal state on the SAME run, before publication
        await complete_scan_run(
            db,
            run,
            coverage=prepared.coverage,
            expected_worker_id=worker_id,
            expected_lease_epoch=lease,
        )

        # 6. formal publication owner (only after the run is succeeded)
        await publish_auction_analysis(
            db,
            scan_run_id=run.id,
            capture_run_id=capture_run_id,
            truth_status=truth_status,
            test_namespace=test_namespace,
        )

        return V32RunOutcome(
            _STATUS_SUCCEEDED,
            run_id=run.id,
            capture_run_id=capture_run_id,
            scope_count=len(prepared.scopes),
        )

    except V32InputUnavailableError as exc:
        await mark_scan_run_failed(
            db,
            run,
            error_message=str(exc),
            expected_worker_id=worker_id,
            expected_lease_epoch=lease,
        )
        return V32RunOutcome(_STATUS_UNAVAILABLE, run_id=run.id, detail=str(exc))

    except Exception as exc:  # noqa: BLE001 - lane boundary, still no commit
        await mark_scan_run_failed(
            db,
            run,
            error_message=str(exc),
            expected_worker_id=worker_id,
            expected_lease_epoch=lease,
        )
        return V32RunOutcome(_STATUS_FAILED, run_id=run.id, detail=str(exc))


def _default_config() -> AuctionMemberFactConfig:
    """Explicit thresholds; the writer never invents hidden constants."""
    return AuctionMemberFactConfig(
        positive_gap_percentile_threshold=90.0,
        negative_gap_percentile_threshold=10.0,
        volume_abnormal_percentile_threshold=90.0,
        amount_abnormal_percentile_threshold=90.0,
    )
