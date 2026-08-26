"""Targeted-PG crash-resume closure (A-H) for after_close orchestrator.

Self-contained synthetic tests. NO production bz_stock fixture is read.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests
(only this file is registered for the targeted-pg plan).

A  crash-after-publishing -> same-run resume
B  state_events failure -> Review exists + truthful partial_success
C  DSA projection failure -> cannot revoke stock_core
D  same-slot incarnation replacement -> fast recover
E  different-slot worker -> cannot steal
F  legacy hostname:pid -> new incarnation compatibility
G  atomic epoch fence -> stale writer rejected
H  reconcile trade_date 2026-08-25 -> no asyncpg DataError
"""

import os
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.db.session import AsyncSessionLocal
from app.models.scheduler import (
    SchedulerJobRun,
    SchedulerJobRunStatus,
    SchedulerTaskKey,
)
from app.services.scheduler_fencing_service import (
    FencingConfig,
    Incarnation,
    WorkerIdentity,
)
from app.services.scheduler_incarnation_tracker import IncarnationTracker
from app.services.scheduler_lease_service import try_lease_job_run
from app.services.scheduler_recovery_domain import (
    IncarnationState,
    SchedulerJobRunRecoveryView,
)

from app.services import after_close_orchestrator as orchestrator
from app.services.after_close_orchestrator import (
    execute_after_close_run,
    reconcile_after_close_run,
)
from app.services.scheduler_job_run_recovery_service import (
    recover_replaced_incarnation_runs,
)


# ---------------------------------------------------------------------------
# crash primitive (mirrors test_after_close_orchestrator._SimulatedProcessDeath)
# ---------------------------------------------------------------------------
class _SimulatedProcessDeath(BaseException):
    """Uncatchable by `except Exception` -> a true process disappearance."""


pytestmark = pytest.mark.postgres

# ruff: noqa: N802  # descriptive A-H test names use uppercase scenario letters


# ---------------------------------------------------------------------------
# shared synthetic helpers (self-contained; no production fixtures)
# ---------------------------------------------------------------------------
async def _make_instruments(session, n=2, start_code=600000):
    from app.models.enums import Exchange, InstrumentType
    from app.models.market import Market

    from app.models.instrument import Instrument

    out = []
    for i in range(n):
        code = str(start_code + i)
        inst = Instrument(
            id=uuid.uuid4(),
            code=code,
            name=f"PG测试{i}",
            exchange=Exchange.SH,
            type=InstrumentType.STOCK,
            market=Market.A,
            is_active=True,
            listed_date=date(2020, 1, 1),
        )
        session.add(inst)
        out.append(inst)
    await session.flush()
    return out


async def _make_strategy_run_with_items(session, instruments, strategy_name="PG测试策略"):
    from app.models.strategy import Strategy
    from app.models.strategy_run import StrategyRun, StrategyRunItem, StrategyRunStatus

    strat = Strategy(
        id=uuid.uuid4(),
        name=strategy_name,
        description="pg-closure",
        is_active=True,
    )
    session.add(strat)
    await session.flush()
    run = StrategyRun(
        id=uuid.uuid4(),
        strategy_id=strat.id,
        name="PG策略运行",
        status=StrategyRunStatus.COMPLETED,
        trade_date=date(2023, 5, 10),
        instrument_count=len(instruments),
        item_count=len(instruments),
        succeeded_count=len(instruments),
        failed_count=0,
    )
    session.add(run)
    await session.flush()
    for inst in instruments:
        session.add(
            StrategyRunItem(
                id=uuid.uuid4(),
                run_id=run.id,
                instrument_id=inst.id,
                symbol=inst.code,
                status="success",
                score=1.0,
                weight=1.0,
            )
        )
    await session.flush()
    return run


async def _make_snapshot_run_with_items(session, instruments):
    from app.models.factor_snapshot import FactorSnapshotItem, FactorSnapshotRun

    run = FactorSnapshotRun(
        id=uuid.uuid4(),
        trade_date=date(2023, 5, 10),
        status="succeeded",
        total=len(instruments),
        done=len(instruments),
    )
    session.add(run)
    await session.flush()
    for inst in instruments:
        session.add(
            FactorSnapshotItem(
                id=uuid.uuid4(),
                run_id=run.id,
                instrument_id=inst.id,
                symbol=inst.code,
                status="success",
            )
        )
    await session.flush()
    return run


class _DownstreamEntryReachedError(RuntimeError):
    """Raised when an orchestrator step we want to block before is reached."""


async def _make_job_run(session):
    now = datetime.now(UTC)
    run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_type="after_close",
        task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
        status=SchedulerJobRunStatus.PENDING,
        trade_date=date(2023, 5, 10),
        scheduled_at=now,
        started_at=now,
        attempt=0,
        payload={},
    )
    session.add(run)
    await session.flush()
    return run


# ===========================================================================
# A. crash-after-publishing -> same-run resume
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_A_crash_after_publishing_same_run_resume():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    core_count = {"n": 0}
    stock_core_publish_count = {"n": 0}
    history_advance_attempts = {"n": 0}
    history_should_crash = {"on": True}
    review_step_count = {"n": 0}
    review_create_count = {"n": 0}
    review_compute_count = {"n": 0}
    review_publish_count = {"n": 0}

    async def _fake_compute_core(*a, **k):
        core_count["n"] += 1
        return {"status": "succeeded", "computed_count": 1, "failed_count": 0}

    async def _fake_publish_stock_core(*a, **k):
        stock_core_publish_count["n"] += 1
        return {"status": "published", "publication_id": uuid.uuid4()}

    async def _fake_history(db, *a, **k):
        history_advance_attempts["n"] += 1
        if history_should_crash["on"]:
            raise _SimulatedProcessDeath("process disappeared during History advance")
        return {"target_state_count": 100, "advanced": True}

    async def _fake_create_run(db, *a, **k):
        review_create_count["n"] += 1
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = uuid.uuid4()
        rr.source_board_run_id = None
        return rr

    async def _fake_compute_run(db, review_run, *a, **k):
        review_compute_count["n"] += 1
        return {
            "status": "succeeded",
            "expected_scope_count": 1,
            "signal_count": 1,
            "coverage_ratio": 1.0,
        }

    async def _fake_publish_run(db, review_run, *a, **k):
        review_publish_count["n"] += 1
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async def _spy_review_step(*a, **k):
        review_step_count["n"] += 1
        return await orchestrator._execute_review_step(*a, **k)

    async with AsyncSessionLocal() as session:
        instruments = await _make_instruments(session)
        await _make_strategy_run_with_items(session, instruments)
        await _make_snapshot_run_with_items(session, instruments)
        job_run = await _make_job_run(session)
        job_run.last_completed_step = "computing_features"
        job_run.status = SchedulerJobRunStatus.RUNNING
        await session.commit()
        job_run_id = str(job_run.id)

    common = {
        "advance_history_to_trade_date": _fake_history,
        "compute_review_core_with_run_items": _fake_compute_core,
        "publish_stock_core": _fake_publish_stock_core,
        "generate_events_for_run": AsyncMock(return_value={"event_count": 1}),
        "cleanup_old_events": AsyncMock(return_value={"deleted_count": 0}),
        "generate_and_publish_auction_anchors": AsyncMock(
            return_value={"status": "published", "publication_id": uuid.uuid4()}
        ),
        "_execute_review_step": _spy_review_step,
        "create_run": _fake_create_run,
        "compute_run": _fake_compute_run,
        "publish_run": _fake_publish_run,
        "get_run": AsyncMock(
            return_value=MagicMock(
                id=uuid.uuid4(), status="signals_ready",
                expected_scope_count=1, signal_count=1, coverage_ratio=1.0,
                algorithm_version="v1", filter_version="f1",
                source_core_run_id=uuid.uuid4(), source_board_run_id=None,
            )
        ),
        "get_published_review_run_id": AsyncMock(return_value=None),
        "is_formally_published_review_run": AsyncMock(return_value=False),
        "evaluate_publish_gate": AsyncMock(return_value=(True, [])),
    }

    def _p(name, val):
        return patch(f"app.services.after_close_orchestrator.{name}", new=val)

    # Attempt 1: crash at History
    patches = [
        _p("advance_history_to_trade_date", _fake_history),
        _p("compute_review_core_with_run_items", _fake_compute_core),
        _p("publish_stock_core", _fake_publish_stock_core),
        _p("generate_events_for_run", common["generate_events_for_run"]),
        _p("cleanup_old_events", common["cleanup_old_events"]),
        _p("generate_and_publish_auction_anchors", common["generate_and_publish_auction_anchors"]),
        _p("_execute_review_step", _spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=common["get_run"]),
        patch("app.services.review_publication_service.get_published_review_run_id", new=common["get_published_review_run_id"]),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=common["is_formally_published_review_run"]),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=common["evaluate_publish_gate"]),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=AsyncMock(return_value="completed")),
        patch("app.services.state_event_service.generate_events_for_run", new=common["generate_events_for_run"]),
        patch("app.services.state_event_service.cleanup_old_events", new=common["cleanup_old_events"]),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=common["generate_and_publish_auction_anchors"]),
        patch("app.services.factor_publication_service.publish_stock_core", new=_fake_publish_stock_core),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        })),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[instruments[0].id])),
        patch("app.services.after_close_orchestrator.compute_review_core_with_run_items", new=_fake_compute_core),
    ]
    for pat in patches:
        pat.start()
    try:
        with pytest.raises(_SimulatedProcessDeath):
            await execute_after_close_run(job_run_id=job_run_id)
    finally:
        for pat in patches:
            pat.stop()

    async with AsyncSessionLocal() as s2:
        mid = await s2.get(SchedulerJobRun, uuid.UUID(job_run_id))
        assert mid.last_completed_step == "publishing", mid.last_completed_step
        assert review_step_count["n"] == 0, "Review must NOT run before History crash"

    # Attempt 2: resume -> History succeeds -> Review runs for real
    history_should_crash["on"] = False
    patches2 = list(patches)
    for pat in patches2:
        pat.start()
    try:
        await execute_after_close_run(job_run_id=job_run_id)
    finally:
        for pat in patches2:
            pat.stop()

    async with AsyncSessionLocal() as s3:
        final = await s3.get(SchedulerJobRun, uuid.UUID(job_run_id))
        assert final.id == uuid.UUID(job_run_id), "same job_run_id (no new run)"
        assert final.last_completed_step == "review", final.last_completed_step
        assert str(final.status.value) == "succeeded", final.status

    assert core_count["n"] == 1, f"core recomputed {core_count['n']} times (must be 1)"
    assert stock_core_publish_count["n"] == 1, "stock_core published exactly once"
    assert history_advance_attempts["n"] == 2, "History advanced twice (crash + resume)"
    assert review_step_count["n"] == 1, "Review entered once after resume"
    assert review_create_count["n"] == 1, "review create_run once"
    assert review_compute_count["n"] == 1, "review compute_run once"
    assert review_publish_count["n"] == 1, "review publish_run once"


# ===========================================================================
# B. state_events failure -> Review exists + truthful partial_success
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_B_state_events_failure_truthful_partial_success():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    async def _fake_compute_core(*a, **k):
        return {"status": "succeeded", "computed_count": 1, "failed_count": 0}

    async def _fake_publish_stock_core(*a, **k):
        return {"status": "published", "publication_id": uuid.uuid4()}

    async def _fake_history(db, *a, **k):
        return {"target_state_count": 100, "advanced": True}

    async def _fail_events(db, *a, **k):
        raise RuntimeError("state_events failed")

    async def _fake_create_run(db, *a, **k):
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = uuid.uuid4()
        rr.source_board_run_id = None
        return rr

    async def _fake_compute_run(db, review_run, *a, **k):
        return {"status": "succeeded", "expected_scope_count": 1, "signal_count": 1, "coverage_ratio": 1.0}

    async def _fake_publish_run(db, review_run, *a, **k):
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async def _spy_review_step(*a, **k):
        return await orchestrator._execute_review_step(*a, **k)

    async with AsyncSessionLocal() as session:
        instruments = await _make_instruments(session)
        await _make_strategy_run_with_items(session, instruments)
        await _make_snapshot_run_with_items(session, instruments)
        job_run = await _make_job_run(session)
        job_run.last_completed_step = "computing_features"
        job_run.status = SchedulerJobRunStatus.RUNNING
        await session.commit()
        job_run_id = str(job_run.id)

    patches = [
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=_fake_history),
        patch("app.services.after_close_orchestrator.compute_review_core_with_run_items", new=_fake_compute_core),
        patch("app.services.factor_publication_service.publish_stock_core", new=_fake_publish_stock_core),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=AsyncMock(return_value="completed")),
        patch("app.services.state_event_service.generate_events_for_run", new=_fail_events),
        patch("app.services.state_event_service.cleanup_old_events", new=AsyncMock(return_value={"deleted_count": 0})),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=AsyncMock(return_value={"status": "published", "publication_id": uuid.uuid4()})),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={"coverage": 1.0, "succeeded": 1, "expected": 1, "failed": 0, "pending": 0, "running": 0, "skipped": 0})),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[instruments[0].id])),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4(), status="signals_ready", expected_scope_count=1, signal_count=1, coverage_ratio=1.0, algorithm_version="v1", filter_version="f1", source_core_run_id=uuid.uuid4(), source_board_run_id=None))),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=None)),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=AsyncMock(return_value=False)),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=AsyncMock(return_value=(True, []))),
    ]
    for pat in patches:
        pat.start()
    try:
        await execute_after_close_run(job_run_id=job_run_id)
    finally:
        for pat in patches:
            pat.stop()

    async with AsyncSessionLocal() as s2:
        final = await s2.get(SchedulerJobRun, uuid.UUID(job_run_id))
        meta = final.metadata_json or {}
        assert str(final.status.value) == "partial_success", final.status
        assert meta.get("partial_success") is True
        assert "state_events" in (meta.get("optional_failures") or []), meta.get("optional_failures")
        ss = meta.get("step_summary") or {}
        assert isinstance(ss.get("state_events"), dict)
        assert ss["state_events"].get("status") == "failed"
        assert ss["state_events"].get("optional") is True
        # Review still exists (was published despite optional state_events failure)
        assert ss.get("computing_review", {}).get("status") in ("succeeded", "published"), ss.get("computing_review")


# ===========================================================================
# C. DSA projection failure -> cannot revoke stock_core
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_C_dsa_projection_failure_cannot_revoke_stock_core():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    async def _fake_compute_core(*a, **k):
        return {"status": "succeeded", "computed_count": 1, "failed_count": 0}

    async def _fake_publish_stock_core(*a, **k):
        return {"status": "published", "publication_id": uuid.uuid4()}

    async def _fake_history(db, *a, **k):
        return {"target_state_count": 100, "advanced": True}

    async def _fail_dsa(*a, **k):
        return "failed"  # DSA projection failure (optional step)

    async def _fake_create_run(db, *a, **k):
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = uuid.uuid4()
        rr.source_board_run_id = None
        return rr

    async def _fake_compute_run(db, review_run, *a, **k):
        return {"status": "succeeded", "expected_scope_count": 1, "signal_count": 1, "coverage_ratio": 1.0}

    async def _fake_publish_run(db, review_run, *a, **k):
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async def _spy_review_step(*a, **k):
        return await orchestrator._execute_review_step(*a, **k)

    async with AsyncSessionLocal() as session:
        instruments = await _make_instruments(session)
        await _make_strategy_run_with_items(session, instruments)
        await _make_snapshot_run_with_items(session, instruments)
        job_run = await _make_job_run(session)
        job_run.last_completed_step = "computing_features"
        job_run.status = SchedulerJobRunStatus.RUNNING
        await session.commit()
        job_run_id = str(job_run.id)

    patches = [
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=_fake_history),
        patch("app.services.after_close_orchestrator.compute_review_core_with_run_items", new=_fake_compute_core),
        patch("app.services.factor_publication_service.publish_stock_core", new=_fake_publish_stock_core),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=_fail_dsa),
        patch("app.services.state_event_service.generate_events_for_run", new=AsyncMock(return_value={"event_count": 1})),
        patch("app.services.state_event_service.cleanup_old_events", new=AsyncMock(return_value={"deleted_count": 0})),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=AsyncMock(return_value={"status": "published", "publication_id": uuid.uuid4()})),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={"coverage": 1.0, "succeeded": 1, "expected": 1, "failed": 0, "pending": 0, "running": 0, "skipped": 0})),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[instruments[0].id])),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4(), status="signals_ready", expected_scope_count=1, signal_count=1, coverage_ratio=1.0, algorithm_version="v1", filter_version="f1", source_core_run_id=uuid.uuid4(), source_board_run_id=None))),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=None)),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=AsyncMock(return_value=False)),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=AsyncMock(return_value=(True, []))),
    ]
    for pat in patches:
        pat.start()
    try:
        await execute_after_close_run(job_run_id=job_run_id)
    finally:
        for pat in patches:
            pat.stop()

    async with AsyncSessionLocal() as s2:
        final = await s2.get(SchedulerJobRun, uuid.UUID(job_run_id))
        meta = final.metadata_json or {}
        # DSA is optional -> run is partial_success, NOT failed
        assert str(final.status.value) == "partial_success", final.status
        # stock_core canonical pointer must still exist (NOT revoked)
        assert meta.get("stock_core_publication_id") is not None, "stock_core pointer revoked by DSA failure"
        step_sum = meta.get("step_summary") or {}
        assert step_sum.get("publishing", {}).get("status") in ("succeeded", "published")


# ===========================================================================
# D. same-slot incarnation replacement -> fast recover
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_D_same_slot_incarnation_fast_recover(db_session):
    run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_type="after_close",
        task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
        status=SchedulerJobRunStatus.RUNNING,
        trade_date=date(2023, 5, 10),
        scheduled_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        attempt=0,
        payload={},
    )
    db_session.add(run)
    await db_session.flush()

    live = Incarnation(
        slot_key="worker:after_close",
        worker_id=WorkerIdentity(
            hostname="live-host", pid=111, started_at=datetime.now(UTC),
            instance_id="live-inst",
        ),
        epoch=5,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    clone = Incarnation(
        slot_key="worker:after_close",
        worker_id=WorkerIdentity(
            hostname="clone-host", pid=222, started_at=datetime.now(UTC),
            instance_id="clone-inst",
        ),
        epoch=6,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    tracker = IncarnationTracker([live, clone], config=FencingConfig(slot_key="worker:after_close"))
    view = SchedulerJobRunRecoveryView(
        run_id=run.id,
        job_type=run.job_type,
        task_key=run.task_key,
        status=run.status,
        current_incarnation_epoch=5,
        slot_key="worker:after_close",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        last_heartbeat=datetime.now(UTC),
        recoverable=True,
        reason="clone replaced same-slot worker",
    )

    recovered = await recover_replaced_incarnation_runs(
        db_session, tracker=tracker, recovery_views=[view], batch_size=100,
    )
    await db_session.commit()

    assert recovered == 1
    refreshed = await db_session.get(SchedulerJobRun, run.id)
    assert refreshed.status == SchedulerJobRunStatus.INTERRUPTED
    assert refreshed.lease_epoch == 6
    assert refreshed.interrupted_reason is not None
    assert "replaced" in (refreshed.interrupted_reason or "").lower()


# ===========================================================================
# E. different-slot worker -> cannot steal
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_E_different_slot_cannot_steal(db_session):
    run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_type="after_close",
        task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
        status=SchedulerJobRunStatus.RUNNING,
        trade_date=date(2023, 5, 10),
        scheduled_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        attempt=0,
        payload={},
    )
    db_session.add(run)
    await db_session.flush()

    live = Incarnation(
        slot_key="worker:after_close",
        worker_id=WorkerIdentity(
            hostname="live-host", pid=111, started_at=datetime.now(UTC),
            instance_id="live-inst",
        ),
        epoch=5,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    other_slot = Incarnation(
        slot_key="worker:intraday",
        worker_id=WorkerIdentity(
            hostname="other-host", pid=333, started_at=datetime.now(UTC),
            instance_id="other-inst",
        ),
        epoch=9,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    tracker = IncarnationTracker([live, other_slot], config=FencingConfig(slot_key="worker:after_close"))
    view = SchedulerJobRunRecoveryView(
        run_id=run.id,
        job_type=run.job_type,
        task_key=run.task_key,
        status=run.status,
        current_incarnation_epoch=5,
        slot_key="worker:intraday",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        last_heartbeat=datetime.now(UTC),
        recoverable=True,
        reason="different slot worker",
    )

    recovered = await recover_replaced_incarnation_runs(
        db_session, tracker=tracker, recovery_views=[view], batch_size=100,
    )
    await db_session.commit()

    assert recovered == 0
    refreshed = await db_session.get(SchedulerJobRun, run.id)
    assert refreshed.status == SchedulerJobRunStatus.RUNNING
    assert refreshed.lease_epoch == 5


# ===========================================================================
# F. legacy hostname:pid -> new incarnation compatibility
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_F_legacy_hostname_pid_compatibility(db_session):
    run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_type="after_close",
        task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
        status=SchedulerJobRunStatus.RUNNING,
        trade_date=date(2023, 5, 10),
        scheduled_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        attempt=0,
        payload={},
        worker_id="legacy-host:1234",  # legacy hostname:pid form
        lease_epoch=2,
        slot_key="worker:after_close",
    )
    db_session.add(run)
    await db_session.flush()

    new_inc = Incarnation(
        slot_key="worker:after_close",
        worker_id=WorkerIdentity(
            hostname="legacy-host", pid=1234, started_at=datetime.now(UTC),
            instance_id="new-inst",
        ),
        epoch=3,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    tracker = IncarnationTracker([new_inc], config=FencingConfig(slot_key="worker:after_close"))

    identity = WorkerIdentity(
        hostname="legacy-host", pid=1234, started_at=datetime.now(UTC),
        instance_id="new-inst",
    )
    leasable, reason, updated = await try_lease_job_run(
        db_session, run_id=run.id, worker_identity=identity, tracker=tracker,
        config=FencingConfig(slot_key="worker:after_close"),
    )
    await db_session.commit()

    assert leasable is True, reason
    refreshed = await db_session.get(SchedulerJobRun, run.id)
    assert refreshed.lease_epoch == 3


# ===========================================================================
# G. atomic epoch fence -> stale writer rejected
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_G_atomic_epoch_fence_stale_writer_rejected(db_session):
    run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_type="after_close",
        task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
        status=SchedulerJobRunStatus.RUNNING,
        trade_date=date(2023, 5, 10),
        scheduled_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        attempt=0,
        payload={},
        lease_epoch=5,
        slot_key="worker:after_close",
    )
    db_session.add(run)
    await db_session.flush()

    live = Incarnation(
        slot_key="worker:after_close",
        worker_id=WorkerIdentity(
            hostname="live-host", pid=111, started_at=datetime.now(UTC),
            instance_id="live-inst",
        ),
        epoch=6,
        state=IncarnationState.ALIVE,
        last_heartbeat=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    tracker = IncarnationTracker([live], config=FencingConfig(slot_key="worker:after_close"))

    stale_identity = WorkerIdentity(
        hostname="stale-host", pid=999, started_at=datetime.now(UTC),
        instance_id="stale-inst",
    )
    leasable, reason, _ = await try_lease_job_run(
        db_session, run_id=run.id, worker_identity=stale_identity, tracker=tracker,
        config=FencingConfig(slot_key="worker:after_close"), expected_epoch=4,
    )
    await db_session.commit()

    assert leasable is False, "stale writer (epoch 4) must be rejected by atomic fence"
    refreshed = await db_session.get(SchedulerJobRun, run.id)
    assert refreshed.lease_epoch == 5


# ===========================================================================
# H. reconcile trade_date 2026-08-25 -> no asyncpg DataError
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_H_reconcile_date_2026_08_25_no_asyncpg_dataerror():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    async with AsyncSessionLocal() as session:
        instruments = await _make_instruments(session)
        await _make_strategy_run_with_items(session, instruments)
        await _make_snapshot_run_with_items(session, instruments)
        job_run = SchedulerJobRun(
            id=uuid.uuid4(),
            job_type="after_close",
            task_key=SchedulerTaskKey.AFTER_CLOSE_TRADE_DATE,
            status=SchedulerJobRunStatus.RUNNING,
            trade_date=date(2026, 8, 25),
            scheduled_at=datetime.now(UTC),
            started_at=datetime.now(UTC),
            attempt=0,
            payload={},
            last_completed_step="review",
        )
        session.add(job_run)
        await session.commit()
        job_run_id = str(job_run.id)

    # reconcile must not raise asyncpg DataError on 2026-08-25 date parsing
    async with AsyncSessionLocal() as s2:
        await reconcile_after_close_run(s2, job_run_id=job_run_id)
        await s2.commit()

    async with AsyncSessionLocal() as s3:
        final = await s3.get(SchedulerJobRun, uuid.UUID(job_run_id))
        assert final is not None
        # reconcile completed without asyncpg DataError
        assert final.trade_date == date(2026, 8, 25)
