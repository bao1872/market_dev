"""KPI-2: shared AuctionScanRun lifecycle contract (create / idempotent /
lease conflict / fencing recovery / retry) for both consumers.

The legacy consumer must keep its semantics (parity is covered by
``tests/test_auction_scan_service.py``); this file pins the shared owner's
decision table and the V3.2 identity, and proves V3.2 recovery never touches a
legacy child table.

Idempotency is proven as a RETURN VALUE, never by catching an IntegrityError.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import (
    AuctionEventTracking,
    AuctionInstrumentResult,
    AuctionScopeResult,
)
from app.services.auction_scan_run_lifecycle import (
    V32_AUCTION_TYPE,
    acquire_or_recover_scan_run,
    acquire_v32_scan_run,
    v32_clear_children,
)

_T = date(2026, 8, 14)


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """Records executed statements and side effects."""

    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flush_count = 0

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.executed.append(stmt)
        return FakeResult(self.existing)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()


def _run(status: str, *, attempt: int = 1, heartbeat: datetime | None = None):
    from app.models.auction import AuctionScanRun

    run = AuctionScanRun(
        trade_date=_T,
        auction_type=V32_AUCTION_TYPE,
        algorithm_version=V32_ALGORITHM_VERSION,
        status=status,
        attempt_count=attempt,
    )
    run.id = uuid4()
    run.heartbeat_at = heartbeat if heartbeat is not None else datetime.now(UTC)
    return run


async def _noop_clear(db: Any, run_id: Any) -> None:
    return None


async def _acquire(existing: Any, **kwargs: Any):
    session = FakeSession(existing)
    calls: list[Any] = []

    async def _clear(db: Any, run_id: Any) -> None:
        calls.append(run_id)

    result = await acquire_or_recover_scan_run(
        session,
        trade_date=_T,
        auction_type=V32_AUCTION_TYPE,
        algorithm_version=V32_ALGORITHM_VERSION,
        worker_id="worker-1",
        lease_epoch=None,
        clear_children=_clear,
        **kwargs,
    )
    return session, result, calls


async def test_absent_run_is_created_with_v32_identity() -> None:
    session, run, _ = await _acquire(None)
    assert run is not None
    assert run.auction_type == V32_AUCTION_TYPE
    assert run.algorithm_version == V32_ALGORITHM_VERSION
    assert run.status == "running"
    assert run.attempt_count == 1
    assert run in session.added


async def test_succeeded_run_is_idempotent_and_not_recreated() -> None:
    existing = _run("succeeded", attempt=3)
    session, run, calls = await _acquire(existing)
    assert run is None, "idempotent hit must return None, not a second run"
    assert session.added == []
    assert calls == [], "an idempotent hit must not trigger child cleanup"


async def test_running_with_valid_lease_is_not_stolen() -> None:
    from app.services.auction_scan_service import AuctionScanConflictError

    existing = _run("running", heartbeat=datetime.now(UTC))
    with pytest.raises(AuctionScanConflictError):
        await _acquire(existing)


async def test_stale_running_is_recovered_by_fencing_without_new_attempt() -> None:
    stale = datetime.now(UTC) - timedelta(seconds=7200)  # > 1800s lease
    existing = _run("running", attempt=4, heartbeat=stale)
    session, run, calls = await _acquire(existing)

    assert run is existing
    assert run.status == "running"
    assert run.attempt_count == 4, "fencing is a recovery, not a new attempt"
    assert run.worker_id == "worker-1"
    assert calls == [existing.id]


async def test_failed_run_is_retried_with_incremented_attempt() -> None:
    existing = _run("failed", attempt=2)
    session, run, calls = await _acquire(existing)

    assert run is existing
    assert run.status == "running"
    assert run.attempt_count == 3
    assert run.finished_at is None
    assert calls == [existing.id], "retry must clear the previous half-written children"


async def test_partial_run_is_recovered_like_failed() -> None:
    existing = _run("partial", attempt=5)
    session, run, calls = await _acquire(existing)
    assert run is existing
    assert run.attempt_count == 6


async def test_v32_entry_point_uses_the_v32_identity() -> None:
    session = FakeSession(None)
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")
    assert run is not None
    assert run.auction_type == V32_AUCTION_TYPE
    assert run.algorithm_version == V32_ALGORITHM_VERSION


async def test_v32_clear_children_touches_only_scope_results() -> None:
    """V3.2 recovery must never delete legacy child rows."""
    session = FakeSession(None)
    await v32_clear_children(session, uuid4())

    deleted_models = {
        stmt.table.name
        for stmt in session.executed
        if isinstance(stmt, type(delete(AuctionScopeResult))) or hasattr(stmt, "table")
    }
    assert deleted_models == {AuctionScopeResult.__tablename__}
    assert AuctionInstrumentResult.__tablename__ not in deleted_models
    assert AuctionEventTracking.__tablename__ not in deleted_models


async def test_owner_queries_by_the_full_run_identity() -> None:
    """The run is located by (trade_date, auction_type, algorithm_version)."""
    session = FakeSession(None)
    await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")

    selects = [s for s in session.executed if isinstance(s, type(select(AuctionScopeResult)))]
    assert selects, "the owner must look up an existing run by identity"
    where_sql = str(selects[0].whereclause)
    assert "algorithm_version" in where_sql
    assert "auction_type" in where_sql
    assert "trade_date" in where_sql
