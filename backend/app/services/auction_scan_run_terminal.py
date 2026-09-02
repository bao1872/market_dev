"""Terminal-state and lease-fencing owner for AuctionScanRun (KPI-2 closure).

Dependency direction is one-way:

    auction_scan_run_lifecycle  (identity / acquire / recover / lease rule)
                ^
                | imports
    auction_scan_run_terminal   (this module)

Contract:

    consumer computes its own business metrics
        -> this owner validates lease ownership against the AUTHORITATIVE row
           (never the caller's possibly-stale Python object)
        -> this owner sets terminal status / finished_at / error state
        -> this owner PROJECTS the consumer's metrics, never recomputes them

Legacy keeps its real ``partial`` state; nothing here adapts legacy to V3.2.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auction import AuctionScanRun
from app.services.auction_scan_run_lifecycle import LEASE_EXPIRED_SECONDS

__all__ = [
    "AuctionScanLeaseLostError",
    "TERMINAL_STATUSES",
    "assert_run_ownership",
    "complete_scan_run",
    "finalize_scan_run",
    "lease_expired_seconds",
    "mark_scan_run_failed",
]

#: Real terminal states.  ``partial`` is a legacy state whose meaning must be
#: preserved; V3.2 simply never uses it.
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed"})


class AuctionScanLeaseLostError(ValueError):
    """The caller no longer owns the run (its lease was taken over).

    Distinct from an acquire-time conflict: this means "you used to hold it and
    do not any more", so the caller must abort rather than blindly retry.
    """


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


async def _authoritative_ownership(
    db: AsyncSession, run: AuctionScanRun
) -> tuple[str | None, int | None, str | None]:
    """Read the OWNERSHIP COLUMNS straight from the database.

    Selecting scalar columns rather than the entity avoids the SQLAlchemy
    identity map: a full-entity select can hand back the very same stale Python
    object the caller already holds, which would make the check vacuous.

    Returns ``(worker_id, lease_epoch, status)``.
    """
    stmt = select(
        AuctionScanRun.worker_id,
        AuctionScanRun.lease_epoch,
        AuctionScanRun.status,
    ).where(AuctionScanRun.id == run.id)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise AuctionScanLeaseLostError(f"scan run {run.id} no longer exists")
    return row[0], row[1], row[2]


async def assert_run_ownership(
    db: AsyncSession,
    run: AuctionScanRun,
    *,
    expected_worker_id: str | None = None,
    expected_lease_epoch: int | None = None,
) -> AuctionScanRun:
    """Verify the caller still owns the run, against the AUTHORITATIVE row.

    Checking ``run.status == "running"`` in memory is not enough: a stale
    takeover may have replaced ``worker_id`` / ``lease_epoch`` after this
    object was loaded.  This reads the row back and compares fencing tokens, so
    a deposed worker is rejected at every write boundary.
    """
    worker_id, lease_epoch, status = await _authoritative_ownership(db, run)
    if status != "running":
        # the run has already reached a terminal state (succeeded / failed /
        # partial) on the authoritative row; a stale in-memory object must not
        # be allowed to write to a closed run.
        raise AuctionScanLeaseLostError(
            f"scan run {run.id} is no longer running (authoritative status="
            f"{status!r}); refusing to write"
        )
    if expected_worker_id is not None and worker_id != expected_worker_id:
        raise AuctionScanLeaseLostError(
            f"scan run {run.id} is owned by worker {worker_id!r}, "
            f"not {expected_worker_id!r}"
        )
    if expected_lease_epoch is not None and lease_epoch != expected_lease_epoch:
        raise AuctionScanLeaseLostError(
            f"scan run {run.id} lease moved to epoch {lease_epoch!r}, "
            f"caller holds {expected_lease_epoch!r}"
        )
    return run


async def finalize_scan_run(
    db: AsyncSession,
    run: AuctionScanRun,
    *,
    status: str,
    metrics: Mapping[str, Any] | None = None,
    error_message: str | None = None,
    expected_worker_id: str | None = None,
    expected_lease_epoch: int | None = None,
    now: datetime | None = None,
) -> AuctionScanRun:
    """Set a run's terminal state — the ONLY path allowed to do so.

    Args:
        status: one of :data:`TERMINAL_STATUSES`.
        metrics: consumer-computed statistics, projected verbatim.  This owner
            never recomputes coverage / counts / ratios.
        error_message: failure detail (meaningful for ``failed``).
        expected_worker_id / expected_lease_epoch: fencing tokens; when given,
            ownership is validated against the authoritative row so a deposed
            worker cannot finalize.
    """
    if status not in TERMINAL_STATUSES:
        raise ValueError(
            f"unsupported terminal status {status!r}; expected one of "
            f"{sorted(TERMINAL_STATUSES)}"
        )

    if expected_worker_id is not None or expected_lease_epoch is not None:
        # validate against the authoritative DB scalars before writing
        await assert_run_ownership(
            db,
            run,
            expected_worker_id=expected_worker_id,
            expected_lease_epoch=expected_lease_epoch,
        )

    if metrics:
        for column in (
            "eligible_count",
            "ready_count",
            "coverage_ratio",
            "missing_count",
            "missing_reasons",
        ):
            if column in metrics:
                setattr(run, column, metrics[column])

    run.status = status
    run.finished_at = _now(now)
    run.error_message = (error_message or None) if status == "failed" else None

    await db.flush()
    return run


async def complete_scan_run(
    db: AsyncSession,
    run: AuctionScanRun,
    *,
    coverage: Any = None,
    expected_worker_id: str | None = None,
    expected_lease_epoch: int | None = None,
    now: datetime | None = None,
) -> AuctionScanRun:
    """Mark a run succeeded (V3.2 path); coverage is PROJECTED, never recomputed."""
    metrics = dict(coverage.as_scan_run_fields()) if coverage is not None else None
    return await finalize_scan_run(
        db,
        run,
        status="succeeded",
        metrics=metrics,
        expected_worker_id=expected_worker_id,
        expected_lease_epoch=expected_lease_epoch,
        now=now,
    )


async def mark_scan_run_failed(
    db: AsyncSession,
    run: AuctionScanRun,
    *,
    error_message: str,
    expected_worker_id: str | None = None,
    expected_lease_epoch: int | None = None,
    now: datetime | None = None,
) -> AuctionScanRun:
    """Mark a run failed on the SAME identity (never a second run row)."""
    return await finalize_scan_run(
        db,
        run,
        status="failed",
        error_message=error_message,
        expected_worker_id=expected_worker_id,
        expected_lease_epoch=expected_lease_epoch,
        now=now,
    )


def lease_expired_seconds() -> int:
    """Expose the lease threshold so callers do not hard-code a second copy."""
    return LEASE_EXPIRED_SECONDS


def run_identity(run: AuctionScanRun) -> tuple[UUID | None, str, str, str]:
    """The unique run identity, for diagnostics and error messages."""
    return run.id, str(run.trade_date), run.auction_type, run.algorithm_version
