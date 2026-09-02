"""KPI-2 fencing + terminal-owner contract (A, B1/B2/B3).

These prove CONTROL FLOW and conditional logic only.  A FakeSession cannot
emulate two real PostgreSQL transactions, so nothing here claims that real
concurrency has been proven — that belongs to registered targeted PG.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import AuctionScanRun
from app.services.auction_scan_run_lifecycle import (
    AuctionScanConflictError,
    acquire_v32_scan_run,
)
from app.services.auction_scan_run_terminal import (
    AuctionScanLeaseLostError,
    assert_run_ownership,
    complete_scan_run,
    finalize_scan_run,
    mark_scan_run_failed,
)
from app.services.auction_scope_persistence_service import persist_v32_scope_results

_T = date(2026, 8, 14)


class _Nested:
    """Fake savepoint so the acquire path stays faithful to production."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeResult:
    def __init__(self, value: Any, rowcount: int = 1) -> None:
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> Any:
        return self._value

    def first(self) -> Any:
        """Multi-column ownership select -> (worker_id, lease_epoch, status)."""
        if self._value is None:
            return None
        return (self._value.worker_id, self._value.lease_epoch, self._value.status)


class FakeSession:
    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.executed: list[Any] = []
        self.flush_count = 0
        #: rows reported for UPDATE; 0 simulates losing the CAS race
        self.update_rowcount = 1

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.executed.append(stmt)
        value = self.existing
        if value is None:
            for obj in self.added:
                if isinstance(obj, AuctionScanRun):
                    value = obj
                    break
        return FakeResult(value, rowcount=self.update_rowcount)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def begin_nested(self) -> _Nested:
        return _Nested(self)

    async def commit(self) -> None:
        raise AssertionError("the orchestrator owns the commit, not the owner")


def _run(status: str, *, attempt: int = 1, heartbeat: datetime | None = None,
         worker: str = "worker-A"):
    run = AuctionScanRun(
        trade_date=_T,
        auction_type="scope_v32",
        algorithm_version=V32_ALGORITHM_VERSION,
        status=status,
        attempt_count=attempt,
        worker_id=worker,
    )
    run.id = uuid4()
    run.heartbeat_at = heartbeat if heartbeat is not None else datetime.now(UTC)
    return run


# ===========================================================================
# B1: first-create race loser
# ===========================================================================
class _RaceLosingSession(FakeSession):
    """First create attempt hits the unique key; the re-read finds a winner."""

    def __init__(self, winner: Any) -> None:
        super().__init__(existing=None)
        self._winner = winner
        self._flushes = 0

    async def flush(self) -> None:
        self._flushes += 1
        if self._flushes == 1:
            raise IntegrityError("stmt", {}, Exception("uq_auction_scan_run_date_type_ver"))
        await super().flush()

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        self.executed.append(stmt)
        value = self._winner if self._flushes >= 1 else None
        return FakeResult(value)


async def test_race_loser_is_idempotent_when_winner_succeeded() -> None:
    session = _RaceLosingSession(_run("succeeded", attempt=1))
    result = await acquire_v32_scan_run(session, trade_date=_T, worker_id="loser")
    assert result is None, (
        "the loser must fall back to the lifecycle decision, not raise a bare "
        "IntegrityError"
    )


async def test_race_loser_conflicts_when_winner_still_running() -> None:
    session = _RaceLosingSession(_run("running", heartbeat=datetime.now(UTC)))
    with pytest.raises(AuctionScanConflictError):
        await acquire_v32_scan_run(session, trade_date=_T, worker_id="loser")


async def test_race_loser_recovers_when_winner_failed() -> None:
    winner = _run("failed", attempt=2)
    session = _RaceLosingSession(winner)
    result = await acquire_v32_scan_run(session, trade_date=_T, worker_id="loser")
    assert result is winner
    assert winner.attempt_count == 3
    assert winner.status == "running"


# ===========================================================================
# B2: atomic stale takeover (compare-and-swap)
# ===========================================================================
async def test_stale_takeover_loser_is_rejected() -> None:
    stale = datetime.now(UTC) - timedelta(seconds=7200)
    session = FakeSession(_run("running", attempt=4, heartbeat=stale))
    session.update_rowcount = 0  # another worker won the CAS

    with pytest.raises(AuctionScanConflictError):
        await acquire_v32_scan_run(session, trade_date=_T, worker_id="worker-B")


async def test_stale_takeover_update_is_guarded_by_lease_tokens() -> None:
    stale = datetime.now(UTC) - timedelta(seconds=7200)
    session = FakeSession(_run("running", attempt=4, heartbeat=stale))

    await acquire_v32_scan_run(session, trade_date=_T, worker_id="worker-B")

    updates = [
        s for s in session.executed
        if getattr(s, "is_update", False) and hasattr(s, "whereclause")
    ]
    assert updates, "takeover must go through a conditional UPDATE"
    where_sql = str(updates[0].whereclause)
    assert "lease_epoch" in where_sql, "UPDATE must match the observed lease_epoch"
    assert "worker_id" in where_sql, "UPDATE must match the observed worker_id"


# ===========================================================================
# B3: every write boundary is bound to the current lease token
# ===========================================================================
def _deposed_session(run: Any, *, worker: str, lease: int) -> FakeSession:
    """The authoritative row is owned by another worker/lease than `run`."""
    authoritative = _run("running", worker=worker)
    authoritative.id = run.id
    authoritative.lease_epoch = lease
    return FakeSession(existing=authoritative)


async def _acquired() -> tuple[FakeSession, Any]:
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="worker-A")
    return session, run


async def test_deposed_worker_cannot_persist() -> None:
    _session, run = await _acquired()
    lease = run.lease_epoch
    deposed = _deposed_session(run, worker="worker-B", lease=lease + 1)

    with pytest.raises(AuctionScanLeaseLostError):
        await persist_v32_scope_results(
            deposed,
            run=run,
            trade_date=_T,
            scope_results=[],
            worker_id="worker-A",
            lease_epoch=lease,
        )


async def test_deposed_worker_cannot_succeed() -> None:
    _session, run = await _acquired()
    deposed = _deposed_session(run, worker="worker-B", lease=run.lease_epoch + 1)

    with pytest.raises(AuctionScanLeaseLostError):
        await complete_scan_run(
            deposed, run, expected_worker_id="worker-A", expected_lease_epoch=run.lease_epoch
        )


async def test_deposed_worker_cannot_fail() -> None:
    _session, run = await _acquired()
    deposed = _deposed_session(run, worker="worker-B", lease=run.lease_epoch + 1)

    with pytest.raises(AuctionScanLeaseLostError):
        await mark_scan_run_failed(
            deposed,
            run,
            error_message="too late",
            expected_worker_id="worker-A",
            expected_lease_epoch=run.lease_epoch,
        )


async def test_current_owner_is_allowed_to_finalize() -> None:
    _session, run = await _acquired()
    current = FakeSession(existing=run)

    await complete_scan_run(current, run, expected_worker_id="worker-A")
    assert run.status == "succeeded"


# ===========================================================================
# A: terminal states owned by the shared terminal owner
# ===========================================================================
async def test_terminal_owner_supports_all_real_legacy_states() -> None:
    for status in ("succeeded", "partial", "failed"):
        session = FakeSession()
        run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")
        await finalize_scan_run(session, run, status=status)
        assert run.status == status
        assert run.finished_at is not None


async def test_terminal_owner_preserves_legacy_partial_semantics() -> None:
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")

    await finalize_scan_run(
        session,
        run,
        status="partial",
        metrics={"eligible_count": 10, "ready_count": 7, "coverage_ratio": 0.7},
    )
    # partial is a real legacy state and must not be normalised away
    assert run.status == "partial"
    # metrics are PROJECTED verbatim, never recomputed
    assert run.eligible_count == 10
    assert run.ready_count == 7
    assert run.coverage_ratio == 0.7


async def test_terminal_owner_rejects_unknown_status() -> None:
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")
    with pytest.raises(ValueError, match="unsupported terminal status"):
        await finalize_scan_run(session, run, status="almost_done")


async def test_error_message_is_only_kept_for_failures() -> None:
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w")
    await finalize_scan_run(session, run, status="succeeded", error_message="noise")
    assert run.error_message is None


# ===========================================================================
# KPI-2 small fixes: authoritative scalars, legacy tokens, required tokens
# ===========================================================================
async def test_stale_orm_object_is_rejected_by_db_scalars() -> None:
    """The caller's ORM object may be stale; DB scalars must decide."""
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="worker-A")
    stale = run  # the object worker-A still holds

    # the DB says the lease moved on
    authoritative = _run("running", worker="worker-B")
    authoritative.id = stale.id
    authoritative.lease_epoch = stale.lease_epoch + 1
    deposed = FakeSession(existing=authoritative)

    with pytest.raises(AuctionScanLeaseLostError):
        await assert_run_ownership(
            deposed,
            stale,
            expected_worker_id="worker-A",
            expected_lease_epoch=stale.lease_epoch,
        )


def test_legacy_terminal_call_sites_pass_fencing_tokens() -> None:
    """Every legacy terminal transition must present the tokens it acquired.

    This is a call-site contract check; the runtime behaviour of the fencing
    itself is proven by the async tests above.
    """
    import inspect

    from app.services import auction_scan_service as svc

    src = inspect.getsource(svc.run_auction_scan)
    assert "expected_worker_id=run.worker_id" in src
    assert "expected_lease_epoch=run.lease_epoch" in src
    # empty-universe / normal finish / exception — all three
    assert src.count("expected_worker_id=run.worker_id") >= 3


async def test_v32_persistence_requires_fencing_tokens() -> None:
    """A caller that forgets the tokens must fail loudly, not write unfenced."""
    session = FakeSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="worker-A")

    with pytest.raises(TypeError):
        await persist_v32_scope_results(
            session, run=run, trade_date=_T, scope_results=[]  # tokens omitted
        )
