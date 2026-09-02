"""KPI-6 orchestrator contracts — only the things that can actually break.

Ten focused tests over the production writer entry point.  Mocks replace the
DB-facing collaborators; the chain wiring itself (which run id, which tokens,
which capture id, publish-or-not) is what is under test.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

import pytest

from app.domain.auction.analysis_preparation import V32PreparationResult, V32PreparedScope
from app.domain.auction.coverage import ScanCoverage
from app.services import auction_v32_analysis_service as writer
from app.services.auction_scan_run_lifecycle import AuctionScanConflictError
from app.services.auction_v32_input_loader import V32InputUnavailableError

_T = date(2026, 8, 14)
_CAPTURE = uuid4()
_RUN_ID = uuid4()


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.flushes = 0

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


class _Run:
    def __init__(self, *, lease: int = 7) -> None:
        self.id = _RUN_ID
        self.lease_epoch = lease
        self.worker_id = "worker-A"
        self.status = "running"


def _coverage() -> ScanCoverage:
    return ScanCoverage(
        eligible_count=10, valid_count=8, price_ready_count=8,
        amount_ready_count=8, both_ready_count=8, missing_count=2,
        coverage_ratio=0.8, missing_reasons=(),
    )


def _prepared(n: int = 2) -> V32PreparationResult:
    scopes = tuple(
        V32PreparedScope(
            family="industry",
            scope_key=f"IND_{i:02d}",
            scope_name=f"行业{i}",
            payload={"identity": {"scope_key": f"IND_{i:02d}", "scope_name": f"行业{i}"}},
        )
        for i in range(n)
    )
    return V32PreparationResult(
        trade_date=_T, coverage=_coverage(), scopes=scopes, diagnostics={}
    )


class _FakeInputs:
    """Minimal loader output: slots + empty observations + no edges."""

    trade_slots = (_T,)
    observations_by_date = {_T: ()}
    edges = ()


@pytest.fixture()
def session() -> _Session:
    return _Session()


def _install(monkeypatch, *, acquire=None, inputs=None, prepare=None):
    """Install recording stubs for the writer's collaborators."""
    calls: dict[str, list[dict[str, Any]]] = {
        "persist": [], "complete": [], "publish": [], "failed": [],
    }

    async def _acquire(db, *, trade_date, worker_id, lease_epoch=None, now=None):
        return acquire if acquire is not None else _Run()

    async def _load(db, *, trade_date, capture_run_id, window=120):
        if inputs is not None:
            return inputs
        return _FakeInputs()

    def _prepare(**kwargs):  # the real preparation owner is synchronous
        return prepare if prepare is not None else _prepared()

    async def _persist(db, **kwargs):
        calls["persist"].append(kwargs)

    async def _complete(db, run, **kwargs):
        calls["complete"].append({"run": run, **kwargs})

    async def _publish(db, **kwargs):
        calls["publish"].append(kwargs)

    async def _failed(db, run, **kwargs):
        calls["failed"].append({"run": run, **kwargs})

    monkeypatch.setattr(writer, "acquire_v32_scan_run", _acquire)
    monkeypatch.setattr(writer, "load_v32_inputs", _load)
    monkeypatch.setattr(writer, "prepare_v32_analysis", _prepare)
    monkeypatch.setattr(writer, "persist_v32_scope_results", _persist)
    monkeypatch.setattr(writer, "complete_scan_run", _complete)
    monkeypatch.setattr(writer, "publish_auction_analysis", _publish)
    monkeypatch.setattr(writer, "mark_scan_run_failed", _failed)
    return calls


async def test_exact_verified_capture_run_id_reaches_publication(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)
    outcome = await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert outcome.status == "succeeded", f"detail={outcome.detail}"
    assert calls["publish"][0]["capture_run_id"] == _CAPTURE


async def test_same_run_id_spans_scope_results_and_publication(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)
    await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert calls["persist"][0]["run"].id == _RUN_ID
    assert calls["publish"][0]["scan_run_id"] == _RUN_ID


async def test_lease_tokens_are_carried_through_every_write_boundary(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)
    await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert calls["persist"][0]["worker_id"] == "worker-A"
    assert calls["persist"][0]["lease_epoch"] == 7
    assert calls["complete"][0]["expected_worker_id"] == "worker-A"
    assert calls["complete"][0]["expected_lease_epoch"] == 7


async def test_writer_never_commits(session: _Session, monkeypatch) -> None:
    _install(monkeypatch)
    await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert session.commits == 0, "the caller owns the transaction"


async def test_succeeded_retry_is_idempotent(session: _Session, monkeypatch) -> None:
    """An already-succeeded run returns idempotently — no recompute, no publish."""

    async def _acquire_none(db, *, trade_date, worker_id, lease_epoch=None, now=None):
        return None

    calls = _install(monkeypatch, acquire=None)
    monkeypatch.setattr(writer, "acquire_v32_scan_run", _acquire_none)

    outcome = await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert outcome.status == "idempotent"
    assert calls["publish"] == []
    assert calls["persist"] == []


async def test_failure_marks_run_failed_and_does_not_publish(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)

    def _boom(**kwargs):
        raise RuntimeError("loader exploded")

    monkeypatch.setattr(writer, "prepare_v32_analysis", _boom)

    outcome = await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert outcome.status == "failed"
    assert calls["failed"], "the SAME run must be marked failed"
    assert calls["failed"][0]["run"].id == _RUN_ID
    assert calls["publish"] == []


async def test_unavailable_inputs_are_reported_separately(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)

    async def _no_consensus(db, *, trade_date, capture_run_id, window=120):
        raise V32InputUnavailableError("no verified consensus capture")

    monkeypatch.setattr(writer, "load_v32_inputs", _no_consensus)

    outcome = await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert outcome.status == "unavailable"
    assert calls["publish"] == []


async def test_running_conflict_is_surfaced_not_swallowed(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)

    async def _conflict(db, *, trade_date, worker_id, lease_epoch=None, now=None):
        raise AuctionScanConflictError("another worker owns this run")

    monkeypatch.setattr(writer, "acquire_v32_scan_run", _conflict)

    outcome = await writer.run_v32_auction_analysis(
        session, trade_date=_T, capture_run_id=_CAPTURE, worker_id="worker-A"
    )
    assert outcome.status == "conflict"
    assert calls["publish"] == []


async def test_truth_status_is_passed_through_to_publication(
    session: _Session, monkeypatch
) -> None:
    calls = _install(monkeypatch)
    await writer.run_v32_auction_analysis(
        session,
        trade_date=_T,
        capture_run_id=_CAPTURE,
        worker_id="worker-A",
        truth_status="verified",
        test_namespace="production",
    )
    assert calls["publish"][0]["truth_status"] == "verified"
    assert calls["publish"][0]["test_namespace"] == "production"


def test_scheduler_reports_v32_status_independently_of_legacy() -> None:
    """The V3.2 lane runs before the legacy scan and keeps its own status."""
    import inspect

    from app.services import auction_scheduler_service as sched

    src = inspect.getsource(sched.run_verified_auction_pipeline)
    v32_at = src.index("await run_v32_auction_analysis(")
    legacy_at = src.index("await run_auction_scan(")
    assert v32_at < legacy_at, (
        "V3.2 must run before the legacy lane so a legacy early-return "
        "cannot skip it"
    )
    # both exits carry the V3.2 fields
    assert src.count('**v32_fields') >= 2
