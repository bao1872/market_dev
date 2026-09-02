"""Vertical production persistence contract for V3.2 — SINGLE scan-run identity.

The previous version of this file asserted that ``persist_v32_scope_results``
adds an ``AuctionScanRun``.  That contract is now wrong: after the lifecycle
extraction, creating a run there would collide with
``UNIQUE(trade_date, auction_type, algorithm_version)`` and would mean two
owners for one lifecycle.

This file proves the real chain:

    acquire_v32_scan_run()        -> the ONE run id
      -> persist_v32_scope_results(run=that run)  -> no second run
      -> complete_scan_run(...)                   -> status == succeeded
      -> publish_auction_analysis(...)            -> bound to the SAME run id

Only the session and the formal publication owner are faked; the real models,
the lifecycle owner, the payload parser, the identity fail-closed check and the
scope-name single owner all execute for real.

Concurrency (two workers acquiring at once) is NOT proven here — a FakeSession
cannot emulate it.  That belongs to registered targeted PG.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from app.domain.auction.coverage import ScanCoverage
from app.domain.auction.scope_payload import build_scope_payload
from app.domain.auction.version import V32_ALGORITHM_VERSION
from app.models.auction import AuctionScanRun, AuctionScopeResult
from app.services import auction_publication_service as publication
from app.services import auction_scope_persistence_service as persistence
from app.services.auction_scan_run_lifecycle import (
    V32_AUCTION_TYPE,
    acquire_v32_scan_run,
)
from app.services.auction_scan_run_terminal import (
    complete_scan_run,
    mark_scan_run_failed,
)

_T = date(2026, 8, 14)
_SCOPE_KEY = "IND_BANK"
_SCOPE_NAME = "银行"


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def first(self) -> Any:
        """Multi-column ownership select -> (worker_id, lease_epoch, status)."""
        value = self._value
        if value is None:
            return None
        return (value.worker_id, value.lease_epoch, value.status)




class _NestedTransaction:
    """Fake savepoint: keeps the acquire code path faithful to production."""

    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> _NestedTransaction:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeAsyncSession:
    """Minimal async session: records side effects, materialises PKs."""

    def __init__(self, existing: Any = None) -> None:
        self.existing = existing
        self.added: list[Any] = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, stmt: Any, *args: Any, **kwargs: Any) -> FakeResult:
        """Return the authoritative row: an explicitly staged one, else the
        run this session created (so ownership re-reads can find it)."""
        value = self.existing
        if value is None:
            for obj in self.added:
                if isinstance(obj, AuctionScanRun):
                    value = obj
                    break
        return FakeResult(value)

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1
        for obj in self.added:
            if getattr(obj, "id", None) is None:
                obj.id = uuid4()

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction(self)

    async def commit(self) -> None:
        self.commit_count += 1


def _payload() -> dict[str, Any]:
    return build_scope_payload(
        algorithm_version=V32_ALGORITHM_VERSION,
        identity={"scope_key": _SCOPE_KEY, "scope_name": _SCOPE_NAME},
        repricing={"equal_weight_gap": 0.012, "price_valid_count": 20},
        historical_dynamics={"position": 70.0},
        participation={"total_auction_amount": 1_000_000.0},
        cross_sectional={"repricing": {"equal_weight_gap": 80.0}},
        member_attribution={"leaders": [], "jaccard": None},
    )


def _coverage() -> ScanCoverage:
    return ScanCoverage(
        eligible_count=100,
        valid_count=80,
        price_ready_count=80,
        amount_ready_count=80,
        both_ready_count=80,
        missing_count=20,
        coverage_ratio=0.8,
        missing_reasons=("missing_current_auction_quote",),
    )


def _scope_rows(payload: dict[str, Any] | None = None, scope_name: str | None = None):
    return [
        {
            "scope_type": "industry",
            "scope_id": uuid4(),
            "scope_name": scope_name,
            "payload": payload if payload is not None else _payload(),
        }
    ]


@pytest.fixture()
def publish_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _fake_publish(session: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(publication, "publish_auction_analysis", _fake_publish)
    return calls


# ---------------------------------------------------------------------------
# the single-identity chain
# ---------------------------------------------------------------------------
async def test_chain_produces_exactly_one_scan_run() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")

    await persistence.persist_v32_scope_results(
        session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
    )
    await complete_scan_run(session, run, coverage=_coverage())

    runs = [o for o in session.added if isinstance(o, AuctionScanRun)]
    assert len(runs) == 1, "the whole chain must create exactly one AuctionScanRun"
    assert runs[0] is run


async def test_persist_does_not_create_a_second_run() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    added_after_acquire = len(session.added)

    await persistence.persist_v32_scope_results(
        session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
    )

    runs = [o for o in session.added if isinstance(o, AuctionScanRun)]
    assert len(runs) == 1
    assert len(session.added) == added_after_acquire + 1  # one scope result only


async def test_scope_results_are_bound_to_the_same_run() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")

    await persistence.persist_v32_scope_results(
        session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
    )

    results = [o for o in session.added if isinstance(o, AuctionScopeResult)]
    assert results
    assert all(r.scan_run_id == run.id for r in results)


async def test_run_is_succeeded_before_publication_and_bound_to_it(
    publish_calls: list[dict[str, Any]],
) -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    await persistence.persist_v32_scope_results(
        session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
    )

    # publication may only happen once the run is actually succeeded
    assert run.status == "running"
    await complete_scan_run(session, run, coverage=_coverage())
    assert run.status == "succeeded"
    assert run.finished_at is not None

    await publication.publish_auction_analysis(
        session,
        scan_run_id=run.id,
        capture_run_id=uuid4(),
        truth_status="verified",
        test_namespace="production",
    )
    assert len(publish_calls) == 1
    assert publish_calls[0]["scan_run_id"] == run.id


async def test_completion_projects_coverage_without_recomputing() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    coverage = _coverage()

    await complete_scan_run(session, run, coverage=coverage)

    # projected from the coverage owner, not recomputed here
    assert run.eligible_count == coverage.eligible_count
    assert run.ready_count == coverage.valid_count
    assert run.coverage_ratio == coverage.coverage_ratio
    assert run.missing_count == coverage.missing_count


async def test_failure_marks_the_same_run_recoverable() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")

    await mark_scan_run_failed(session, run, error_message="loader exploded")

    runs = [o for o in session.added if isinstance(o, AuctionScanRun)]
    assert len(runs) == 1, "a failure must not produce a second run identity"
    assert run.status == "failed"
    assert run.error_message == "loader exploded"


# ---------------------------------------------------------------------------
# identity fail-closed (retained from the previous contract)
# ---------------------------------------------------------------------------
async def test_persist_rejects_a_run_of_the_wrong_type() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    run.auction_type = "legacy_final"

    with pytest.raises(ValueError, match="auction_type mismatch"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
        )


async def test_persist_rejects_a_run_of_the_wrong_algorithm() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    run.algorithm_version = "auction-v999"

    with pytest.raises(ValueError, match="algorithm_version mismatch"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
        )


async def test_persist_rejects_a_run_of_another_trade_date() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")

    with pytest.raises(ValueError, match="trade_date mismatch"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=date(2026, 8, 15), scope_results=_scope_rows()
        )


async def test_persist_rejects_a_run_that_is_not_running() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    await complete_scan_run(session, run, coverage=_coverage())

    with pytest.raises(ValueError, match="must be running"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
        )


async def test_acquired_run_carries_the_v32_identity() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    assert run.auction_type == V32_AUCTION_TYPE
    assert run.algorithm_version == V32_ALGORITHM_VERSION
    assert run.status == "running"


# ---------------------------------------------------------------------------
# retained guarantees
# ---------------------------------------------------------------------------
async def test_non_v32_payload_fails_before_anything_is_added() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    before = len(session.added)

    tampered = dict(_payload())
    tampered["algorithm_version"] = "auction-v999"
    with pytest.raises(ValueError, match="algorithm_version"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows(tampered)
        )
    assert len(session.added) == before


async def test_scope_name_drift_is_rejected_before_write() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")
    before = len(session.added)

    with pytest.raises(ValueError, match="drift"):
        await persistence.persist_v32_scope_results(
            session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows(scope_name="旧名字")
        )
    assert len(session.added) == before


async def test_persist_does_not_own_the_transaction() -> None:
    session = FakeAsyncSession()
    run = await acquire_v32_scan_run(session, trade_date=_T, worker_id="w1")

    await persistence.persist_v32_scope_results(
        session, run=run, worker_id=run.worker_id, lease_epoch=run.lease_epoch, trade_date=_T, scope_results=_scope_rows()
    )
    assert session.commit_count == 0, "the orchestrator must own the commit"
    assert session.flush_count >= 1
