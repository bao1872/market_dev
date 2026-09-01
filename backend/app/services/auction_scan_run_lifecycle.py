"""Shared AuctionScanRun lifecycle owner (acquire / recover / fencing).

Both the legacy Auction scan and the V3.2 scope analysis need the same run
identity and recovery semantics, so this module owns them ONCE.  A second copy
of this state machine would drift.

What this owner owns — and nothing else:

    AuctionScanRun identity  (trade_date, auction_type, algorithm_version)
    status / attempt_count / worker_id / lease_epoch / heartbeat_at
    the acquire-recover decision:
        absent            -> create
        succeeded         -> None (idempotent, caller returns the old summary)
        running, lease OK -> raise conflict (do not steal)
        running, stale    -> fencing takeover (attempt_count unchanged)
        failed / partial  -> recover, attempt_count + 1
        queued            -> claim

What it deliberately does NOT own:
    anchors, structure/chip, AuctionInstrumentResult semantics,
    AuctionEventTracking semantics, or any computation result.

Child cleanup is supplied by the consumer through ``clear_children``:
    legacy -> instrument / scope / event legacy children
    V3.2   -> V3.2 AuctionScopeResult rows only
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import AuctionScanRun

__all__ = [
    "V32_AUCTION_TYPE",
    "acquire_or_recover_scan_run",
    "acquire_v32_scan_run",
    "v32_clear_children",
]

#: V3.2 run identity component.  Combined with
#: ``(trade_date, V32_ALGORITHM_VERSION)`` it is the unique run key.
V32_AUCTION_TYPE = "scope_v32"

ClearChildren = Callable[[AsyncSession, UUID], Awaitable[None]]


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


async def acquire_or_recover_scan_run(
    db: AsyncSession,
    *,
    trade_date: date,
    auction_type: str,
    algorithm_version: str,
    worker_id: str | None,
    lease_epoch: int | None,
    now: datetime | None = None,
    clear_children: ClearChildren,
) -> AuctionScanRun | None:
    """Acquire a run for ``(trade_date, auction_type, algorithm_version)``.

    Returns the run to execute, or ``None`` when an identical run already
    succeeded (caller must then return the existing result instead of
    recomputing — idempotency is a return value, never an IntegrityError catch).
    """
    # Imported lazily: auction_scan_service imports this module, so a
    # module-level import would be circular.
    from app.services.auction_scan_service import (
        AuctionScanConflictError,
        _is_lease_expired,
    )

    current_now = _now(now)
    new_lease_epoch = (
        lease_epoch if lease_epoch is not None else int(current_now.timestamp())
    )

    stmt = (
        select(AuctionScanRun)
        .where(
            AuctionScanRun.trade_date == trade_date,
            AuctionScanRun.auction_type == auction_type,
            AuctionScanRun.algorithm_version == algorithm_version,
        )
        .order_by(AuctionScanRun.created_at.desc())
        .limit(1)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing is None:
        run = AuctionScanRun(
            trade_date=trade_date,
            auction_type=auction_type,
            algorithm_version=algorithm_version,
            price_adjustment_version="pending",
            status="running",
            attempt_count=1,
            worker_id=worker_id,
            lease_epoch=new_lease_epoch,
            started_at=current_now,
            heartbeat_at=current_now,
        )
        db.add(run)
        await db.flush()
        return run

    if existing.status == "succeeded":
        # Idempotent hit: do not create a second run, do not recompute.
        return None

    if existing.status == "running":
        if not _is_lease_expired(existing.heartbeat_at, now=current_now):
            raise AuctionScanConflictError(
                f"trade_date={trade_date} auction_type={auction_type} "
                f"run_id={existing.id} 仍在运行（worker={existing.worker_id}, "
                f"lease_epoch={existing.lease_epoch}, heartbeat={existing.heartbeat_at}），"
                f"拒绝重复执行"
            )
        # stale lease -> fencing takeover; this is a recovery, not a new attempt
        await clear_children(db, existing.id)
        existing.worker_id = worker_id
        existing.lease_epoch = new_lease_epoch
        existing.heartbeat_at = current_now
        existing.started_at = current_now
        existing.error_message = None
        await db.flush()
        return existing

    # failed / partial / queued -> recover as a new attempt
    await clear_children(db, existing.id)
    existing.status = "running"
    existing.attempt_count = existing.attempt_count + 1
    existing.worker_id = worker_id
    existing.lease_epoch = new_lease_epoch
    existing.heartbeat_at = current_now
    existing.started_at = current_now
    existing.finished_at = None
    existing.error_message = None
    await db.flush()
    return existing


async def v32_clear_children(db: AsyncSession, scan_run_id: UUID) -> None:
    """V3.2 child cleanup: only this run's V3.2 scope results.

    Deliberately touches NO legacy child table (instrument / event), so V3.2
    recovery can never destroy legacy data.
    """
    from sqlalchemy import delete

    from app.models.auction import AuctionScopeResult

    await db.execute(
        delete(AuctionScopeResult).where(
            AuctionScopeResult.scan_run_id == scan_run_id
        )
    )
    await db.flush()


async def acquire_v32_scan_run(
    db: AsyncSession,
    *,
    trade_date: date,
    worker_id: str | None,
    lease_epoch: int | None = None,
    now: datetime | None = None,
) -> AuctionScanRun | None:
    """V3.2 entry point: ``(trade_date, scope_v32, auction-v3.2)``."""
    return await acquire_or_recover_scan_run(
        db,
        trade_date=trade_date,
        auction_type=V32_AUCTION_TYPE,
        algorithm_version=V32_ALGORITHM_VERSION,
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        now=now,
        clear_children=v32_clear_children,
    )
