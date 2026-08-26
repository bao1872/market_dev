"""Targeted-PG crash-resume closure (A-H) for after_close orchestrator.

Self-contained synthetic tests. NO production bz_stock fixture is read.
Registry: scripts/verify/verify_attempt.py -> run_self_contained_pg_tests
(only this file is registered for the targeted-pg plan).

Helpers and orchestrator invocation patterns are copied from the proven
tests/test_pg_review_runtime_blocker_closure.py so the model/column/API
contracts match production exactly.

A  crash-after-publishing -> same-run resume
B  state_events failure -> Review exists + truthful partial_success
C  DSA projection failure -> cannot revoke stock_core
D  same-slot incarnation replacement -> fast recover
E  different-slot worker -> cannot steal
F  legacy hostname:pid -> new incarnation compatibility
G  atomic epoch fence -> stale writer rejected
H  reconcile trade_date 2026-08-25 -> no asyncpg DataError
"""

import json
import os
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db import AsyncSessionLocal
from app.services import after_close_orchestrator as orchestrator

# ruff: noqa: N802  # descriptive A-H test names use uppercase scenario letters


# ---------------------------------------------------------------------------
# crash primitive (mirrors test_after_close_orchestrator._SimulatedProcessDeath)
# ---------------------------------------------------------------------------
class _SimulatedProcessDeath(BaseException):
    """Uncatchable by `except Exception` -> a true process disappearance."""


pytestmark = pytest.mark.postgres


# ===========================================================================
# proven synthetic helpers (mirror test_pg_review_runtime_blocker_closure.py)
# ===========================================================================
async def _make_instruments(db_session, n=2):
    from app.models.instrument import Instrument

    created = []
    for i in range(n):
        sym = f"T{uuid.uuid4().hex[:16]}"
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=sym,
            name=f"closure_test_{i}",
            market="SZ",
            status="active",
            listing_date=date(2010, 1, 4),
        )
        db_session.add(inst)
        created.append(inst)
    await db_session.flush()
    return [c.id for c in created]


async def _make_strategy_run_with_items(
    db_session, *, total, succeeded, skipped, failed, status="running",
    instrument_ids=None,
):
    from app.models.strategy import StrategyDefinition, StrategyVersion
    from app.models.strategy_run import StrategyResult, StrategyRun, StrategyRunItem

    definition = StrategyDefinition(
        strategy_key=f"test_dsa_{uuid.uuid4().hex[:8]}",
        kind="selector",
        display_name="测试 DSA",
    )
    db_session.add(definition)
    await db_session.flush()
    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={"outputs": [], "parameters": []},
        build_hash=f"hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    now = datetime.now(UTC)
    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=date(2026, 8, 7),
        status=status,
        input_overrides={},
        idempotency_key=f"test_dsa:{version.id}:{date(2026,8,7)}:{uuid.uuid4().hex[:8]}",
        total_instruments=total,
        succeeded_count=0,
        skipped_count=0,
        failed_count=0,
        started_at=now,
        finished_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    if instrument_ids is not None:
        inst_ids = instrument_ids
    else:
        inst_ids = await _make_instruments(db_session, n=succeeded + skipped + failed)
    idx = 0
    for _ in range(succeeded):
        db_session.add(StrategyRunItem(run_id=run.id, instrument_id=inst_ids[idx], status="succeeded"))
        db_session.add(StrategyResult(run_id=run.id, strategy_version_id=run.strategy_version_id, instrument_id=inst_ids[idx], trade_date=run.trade_date, payload={"result": "ok"}))
        idx += 1
    for _ in range(skipped):
        db_session.add(StrategyRunItem(run_id=run.id, instrument_id=inst_ids[idx], status="skipped", reason_code="insufficient_history"))
        idx += 1
    for _ in range(failed):
        db_session.add(StrategyRunItem(run_id=run.id, instrument_id=inst_ids[idx], status="failed", reason_code="compute_error"))
        idx += 1
    await db_session.flush()
    return run


async def _make_snapshot_run_with_items(
    db_session, *, expected, succeeded, skipped, failed, pending=0, running=0,
    published_at=None, status="succeeded", trade_date=date(2026, 8, 7),
    instrument_ids=None,
):
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem

    now = datetime.now(UTC)
    run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type="after_close",
        status=status,
        expected_count=expected,
        published_at=published_at,
        started_at=now,
        finished_at=now if status != "running" else None,
        metadata_={"scope": "full"},
    )
    db_session.add(run)
    await db_session.flush()
    counts = {"succeeded": succeeded, "skipped": skipped, "failed": failed, "pending": pending, "running": running}
    total_items = sum(counts.values())
    if instrument_ids is not None:
        inst_ids = instrument_ids
    else:
        inst_ids = await _make_instruments(db_session, n=total_items)
    idx = 0
    for st, n in counts.items():
        for _ in range(n):
            db_session.add(StockFeatureSnapshotRunItem(snapshot_run_id=run.id, instrument_id=inst_ids[idx], phase="core", status=st))
            db_session.add(StockFeatureSnapshot(
                instrument_id=inst_ids[idx], trade_date=trade_date, primary_timeframe="1d",
                secondary_timeframe="15m", adj="qfq", schema_version=1, source_run_id=run.id,
                structural_payload={"ok": True}, temporal_payload={"ok": True}, summary_payload={"ok": True},
            ))
            idx += 1
    await db_session.flush()
    return run


def _create_after_close_job_run(db, *, trade_date, orchestrator_status, last_completed_step, dsa_run_id=None, feature_snapshot_run_id=None, status="running"):
    from app.models.scheduler_job_run import SchedulerJobRun

    meta = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
        "last_completed_step": last_completed_step,
    }
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)
    if feature_snapshot_run_id is not None:
        meta["feature_snapshot_run_id"] = str(feature_snapshot_run_id)
    now = datetime.now(UTC)
    job = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:closure:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db.add(job)
    db.flush()
    return job


# ===========================================================================
# A. crash-after-publishing -> same-run resume
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_A_crash_after_publishing_same_run_resume():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
        get_after_close_run_status,
    )

    test_date = date(2026, 8, 25)

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
        return {}

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
        return {"status": "succeeded", "expected_scope_count": 1, "signal_count": 1, "coverage_ratio": 1.0}

    async def _fake_publish_run(db, review_run, *a, **k):
        review_publish_count["n"] += 1
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async def _spy_review_step(*a, **k):
        review_step_count["n"] += 1
        return await orchestrator._execute_review_step(*a, **k)

    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(prep_db, total=5293, succeeded=5283, skipped=10, failed=0, status="completed")
        from app.services.strategy_batch_service import reconcile_strategy_run_from_items
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)
        snap = await _make_snapshot_run_with_items(prep_db, expected=5293, succeeded=5293, skipped=0, failed=0, published_at=None, status="succeeded", trade_date=test_date)
        job = _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    common_patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=_fake_compute_core),
        patch("app.services.factor_publication_service.publish_stock_core", new=_fake_publish_stock_core),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=_fake_history),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=AsyncMock(return_value="completed")),
        patch("app.services.state_event_service.generate_events_for_run", new=AsyncMock(return_value={"event_count": 1})),
        patch("app.services.state_event_service.cleanup_old_events", new=AsyncMock(return_value={"deleted_count": 0})),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=AsyncMock(return_value={"structure_count": 0, "chip_count": 0})),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={"coverage": 1.0, "succeeded": 1, "expected": 1, "failed": 0, "pending": 0, "running": 0, "skipped": 0})),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[])),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4(), status="signals_ready", expected_scope_count=1, signal_count=1, coverage_ratio=1.0, algorithm_version="v1", filter_version="f1", source_core_run_id=uuid.uuid4(), source_board_run_id=None))),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=None)),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=AsyncMock(return_value=False)),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=AsyncMock(return_value=(True, []))),
    ]

    # Attempt 1: crash at History
    for p in common_patches:
        p.start()
    try:
        with pytest.raises(_SimulatedProcessDeath):
            await execute_after_close_run(job_run_id, trade_date=test_date)
    finally:
        for p in common_patches:
            p.stop()

    async with AsyncSessionLocal() as mid_db:
        mid_status = await get_after_close_run_status(mid_db, job_run_id)
    assert mid_status.get("last_completed_step") == "publishing", mid_status
    assert review_step_count["n"] == 0, "Review must NOT run before History crash"

    # Attempt 2: resume -> History succeeds -> Review runs for real
    history_should_crash["on"] = False
    for p in common_patches:
        p.start()
    try:
        await execute_after_close_run(job_run_id, trade_date=test_date)
    finally:
        for p in common_patches:
            p.stop()

    async with AsyncSessionLocal() as reader_db:
        final_status = await get_after_close_run_status(reader_db, job_run_id)
    assert final_status.get("last_completed_step") == "review", final_status
    assert final_status.get("orchestrator_status") in (AfterCloseRunStatus.COMPUTING_REVIEW.value, AfterCloseRunStatus.SUCCEEDED.value, AfterCloseRunStatus.PARTIAL_SUCCESS.value), final_status

    assert core_count["n"] == 0, f"resume must NOT recompute core, got {core_count['n']}"
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

    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
        get_after_close_run_status,
    )

    test_date = date(2026, 8, 25)

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

    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(prep_db, total=5293, succeeded=5283, skipped=10, failed=0, status="completed")
        from app.services.strategy_batch_service import reconcile_strategy_run_from_items
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)
        snap = await _make_snapshot_run_with_items(prep_db, expected=5293, succeeded=5293, skipped=0, failed=0, published_at=None, status="succeeded", trade_date=test_date)
        job = _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=AsyncMock(return_value={})),
        patch("app.services.factor_publication_service.publish_stock_core", new=AsyncMock(return_value={"status": "published", "publication_id": uuid.uuid4()})),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=AsyncMock(return_value={"target_state_count": 100, "advanced": True})),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=AsyncMock(return_value="completed")),
        patch("app.services.state_event_service.generate_events_for_run", new=_fail_events),
        patch("app.services.state_event_service.cleanup_old_events", new=AsyncMock(return_value={"deleted_count": 0})),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=AsyncMock(return_value={"structure_count": 0, "chip_count": 0})),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={"coverage": 1.0, "succeeded": 1, "expected": 1, "failed": 0, "pending": 0, "running": 0, "skipped": 0})),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[])),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4(), status="signals_ready", expected_scope_count=1, signal_count=1, coverage_ratio=1.0, algorithm_version="v1", filter_version="f1", source_core_run_id=uuid.uuid4(), source_board_run_id=None))),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=None)),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=AsyncMock(return_value=False)),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=AsyncMock(return_value=(True, []))),
    ]
    for p in patches:
        p.start()
    try:
        await execute_after_close_run(job_run_id, trade_date=test_date)
    finally:
        for p in patches:
            p.stop()

    async with AsyncSessionLocal() as reader_db:
        status = await get_after_close_run_status(reader_db, job_run_id)
    assert status.get("orchestrator_status") == AfterCloseRunStatus.PARTIAL_SUCCESS.value, status
    meta = status.get("metadata_json") or {}
    assert meta.get("partial_success") is True, meta
    assert "state_events" in (meta.get("optional_failures") or []), meta.get("optional_failures")
    ss = meta.get("step_summary") or {}
    assert isinstance(ss.get("state_events"), dict), ss
    assert ss["state_events"].get("status") == "failed", ss.get("state_events")
    assert ss["state_events"].get("optional") is True, ss.get("state_events")
    assert ss.get("computing_review", {}).get("status") in ("succeeded", "published"), ss.get("computing_review")


# ===========================================================================
# C. DSA projection failure -> cannot revoke stock_core
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_C_dsa_projection_failure_cannot_revoke_stock_core():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
        get_after_close_run_status,
    )
    from app.services.factor_publication_service import (
        PUBLICATION_KIND_STOCK_CORE,
        get_publication,
    )

    test_date = date(2026, 8, 25)

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

    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(prep_db, total=5293, succeeded=5283, skipped=10, failed=0, status="completed")
        from app.services.strategy_batch_service import reconcile_strategy_run_from_items
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)
        snap = await _make_snapshot_run_with_items(prep_db, expected=5293, succeeded=5293, skipped=0, failed=0, published_at=None, status="succeeded", trade_date=test_date)
        job = _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=AsyncMock(return_value={})),
        patch("app.services.factor_publication_service.publish_stock_core", new=AsyncMock(return_value={"status": "published", "publication_id": uuid.uuid4()})),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=AsyncMock(return_value={"target_state_count": 100, "advanced": True})),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=_fail_dsa),
        patch("app.services.state_event_service.generate_events_for_run", new=AsyncMock(return_value={"event_count": 1})),
        patch("app.services.state_event_service.cleanup_old_events", new=AsyncMock(return_value={"deleted_count": 0})),
        patch("app.services.auction_anchor_service.generate_and_publish_auction_anchors", new=AsyncMock(return_value={"structure_count": 0, "chip_count": 0})),
        patch("app.services.factor_publication_service.compute_coverage", new=AsyncMock(return_value={"coverage": 1.0, "succeeded": 1, "expected": 1, "failed": 0, "pending": 0, "running": 0, "skipped": 0})),
        patch("app.services.board_analysis_service.compute_all_boards", new=AsyncMock(return_value={"published": 1, "failed": 0})),
        patch("app.services.after_close_orchestrator.get_active_a_share_instruments", new=AsyncMock(return_value=[])),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.review_orchestrator_service.create_run", new=_fake_create_run),
        patch("app.services.review_orchestrator_service.compute_run", new=_fake_compute_run),
        patch("app.services.review_orchestrator_service.publish_run", new=_fake_publish_run),
        patch("app.services.review_orchestrator_service.get_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4(), status="signals_ready", expected_scope_count=1, signal_count=1, coverage_ratio=1.0, algorithm_version="v1", filter_version="f1", source_core_run_id=uuid.uuid4(), source_board_run_id=None))),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=None)),
        patch("app.services.review_publication_service.is_formally_published_review_run", new=AsyncMock(return_value=False)),
        patch("app.services.review_publication_service.evaluate_publish_gate", new=AsyncMock(return_value=(True, []))),
    ]
    for p in patches:
        p.start()
    try:
        await execute_after_close_run(job_run_id, trade_date=test_date)
    finally:
        for p in patches:
            p.stop()

    async with AsyncSessionLocal() as reader_db:
        status = await get_after_close_run_status(reader_db, job_run_id)
        pointer = await get_publication(
            reader_db, scope_type="market", scope_key="market", trade_date=test_date,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
        )
    assert status.get("orchestrator_status") == AfterCloseRunStatus.PARTIAL_SUCCESS.value, status
    assert pointer is not None, "stock_core publication pointer revoked by DSA failure"
    meta = status.get("metadata_json") or {}
    assert meta.get("stock_core_publication_id") is not None, "stock_core pointer id missing"


# ===========================================================================
# D. same-slot incarnation replacement -> fast recover
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_D_same_slot_incarnation_fast_recover(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:newnonce", now=now,
    )
    await db_session.commit()
    assert recovered == 1, "same-slot new incarnation should fast-recover legacy owner"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    assert str(refreshed.status) == "interrupted", refreshed.status
    assert refreshed.lease_epoch == 6, refreshed.lease_epoch
    assert refreshed.error_code == "WORKER_INCARNATION_REPLACED"


# ===========================================================================
# E. different-slot worker -> cannot steal
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_E_different_slot_cannot_steal(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="other-host:9999:newnonce", now=now,
    )
    await db_session.commit()
    assert recovered == 0, "different-slot worker must NOT steal same-slot task"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    assert str(refreshed.status) == "running", refreshed.status
    assert refreshed.lease_epoch == 5


# ===========================================================================
# F. legacy hostname:pid -> new incarnation compatibility
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_F_legacy_hostname_pid_compatibility(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = _recovery_job(db_session, now, worker_instance_id="legacy-host:1234", lease_epoch=2)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="legacy-host:1234:new-nonce", now=now,
    )
    await db_session.commit()
    assert recovered == 1, "legacy hostname:pid must be reclaimable by same-slot new incarnation"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    assert str(refreshed.status) == "interrupted", refreshed.status
    assert refreshed.lease_epoch == 3


# ===========================================================================
# G. atomic epoch fence -> stale writer rejected
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_G_atomic_epoch_fence_stale_writer_rejected(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)

    recovered1 = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:nonceA", now=now,
    )
    await db_session.commit()
    assert recovered1 == 1
    r1 = await db_session.get(_SchedulerJobRun, run.id)
    assert r1.lease_epoch == 6
    assert str(r1.status) == "interrupted"

    # simulate a concurrent incarnation already fenced the epoch forward
    r1.status = "running"
    r1.lease_epoch = 7
    await db_session.flush()
    await db_session.commit()

    recovered2 = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:stale-nonce", now=now,
    )
    await db_session.commit()
    assert recovered2 == 0, "stale writer (old epoch) must be rejected by atomic fence"
    r2 = await db_session.get(_SchedulerJobRun, run.id)
    assert r2.lease_epoch == 7


# ===========================================================================
# H. reconcile trade_date 2026-08-25 -> no asyncpg DataError
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_H_reconcile_date_2026_08_25_no_asyncpg_dataerror():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    from app.services.after_close_orchestrator import reconcile_after_close_run

    test_date = date(2026, 8, 25)
    async with AsyncSessionLocal() as prep_db:
        job = _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status="review",
            last_completed_step="review",
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    # reconcile must not raise asyncpg DataError on 2026-08-25 date parsing
    async with AsyncSessionLocal() as db2:
        await reconcile_after_close_run(db2, job_run_id)
        await db2.commit()

    async with AsyncSessionLocal() as db3:
        from app.models.scheduler_job_run import SchedulerJobRun
        final = await db3.get(SchedulerJobRun, uuid.UUID(job_run_id))
        assert final is not None
        assert final.business_date == "2026-08-25"


# ---------------------------------------------------------------------------
# recovery-job helper (D/E/F/G) using the real SchedulerJobRun model
# ---------------------------------------------------------------------------
from app.models.scheduler_job_run import SchedulerJobRun as _SchedulerJobRun  # noqa: E402


def _recovery_job(db_session, now, *, worker_instance_id, lease_epoch):
    run = _SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-25",
        run_key=f"after_close:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(minutes=10),
        worker_instance_id=worker_instance_id,
        lease_epoch=lease_epoch,
        metadata_json=None,
    )
    db_session.add(run)
    db_session.flush()
    return run
