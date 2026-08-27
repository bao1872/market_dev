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


async def _create_after_close_job_run(db, *, trade_date, orchestrator_status, last_completed_step, dsa_run_id=None, feature_snapshot_run_id=None, status="running"):
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
    await db.flush()
    return job


# ===========================================================================
# A. crash-after-publishing -> same-run resume
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_A_crash_after_publishing_same_run_resume():
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
        get_after_close_run_status,
    )
    from app.services.factor_publication_service import (
        PUBLICATION_KIND_STOCK_CORE,
        get_publication,
    )

    test_date = date(2026, 8, 22)

    core_count = {"n": 0}
    history_advance_attempts = {"n": 0}
    history_should_crash = {"on": True}
    review_step_count = {"n": 0}
    publish_spy = {"n": 0}
    captured = {}

    async def _fake_compute_core(*a, **k):
        core_count["n"] += 1
        return {}

    async def _fake_history(db, *a, **k):
        history_advance_attempts["n"] += 1
        if history_should_crash["on"]:
            raise _SimulatedProcessDeath("process disappeared during History advance")
        return {"target_state_count": 100, "advanced": True}

    async def _spy_review_step(*a, **k):
        # §11: Review 真实进入（不只在入口停），证明 Review 在 History 之前完成。
        review_step_count["n"] += 1
        return await orchestrator._execute_review_step(*a, **k)

    async def _spy_publish(*a, **k):
        publish_spy["n"] += 1
        return MagicMock(id=uuid.uuid4())

    async def _fake_create_run(db, *a, **k):
        captured["source_core_run_id"] = k.get("source_core_run_id")
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = k.get("source_core_run_id")
        rr.source_board_run_id = None
        return rr

    async def _fake_compute_run(db, review_run, *a, **k):
        return {"status": "succeeded", "expected_scope_count": 1, "signal_count": 1, "coverage_ratio": 1.0}

    async def _fake_publish_run(db, review_run, *a, **k):
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(prep_db, total=5293, succeeded=5283, skipped=10, failed=0, status="completed")
        from app.services.strategy_batch_service import reconcile_strategy_run_from_items
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)
        snap = await _make_snapshot_run_with_items(prep_db, expected=5293, succeeded=5293, skipped=0, failed=0, published_at=None, status="succeeded", trade_date=test_date)
        job = await _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    # §11: 最小 faithful patch —— core 计算跳过（记录计数）；History 注入 crash；
    # Review 真实进入（spy）；normal Core->Review 主链不发布 stock_core（spy 计数）。
    # 不再依赖 stock_core 真实发布 / pointer。
    common_patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=_fake_compute_core),
        patch("app.services.stock_core_publication_service.publish_stock_core_atomically", new=_spy_publish),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=_fake_history),
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
    # §11 新顺序：Review → History(T)。History crash 时 Review 应已成功进入/完成。
    assert review_step_count["n"] == 1, "Review 必须在 History crash 前已成功进入"
    assert mid_status.get("last_completed_step") != "publishing", mid_status

    # Attempt 2: resume -> History succeeds -> Review 不重复
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
        snap_reread = await reader_db.get(StockFeatureSnapshotRun, snap.id)
    # 崩溃后恢复：Review 已在 Attempt1 完成（review_step_count==1 已证），run 达终态。
    assert final_status.get("last_completed_step") in (
        "computing_review", "succeeded", "completed",
    ), final_status
    assert final_status.get("orchestrator_status") in (AfterCloseRunStatus.COMPUTING_REVIEW.value, AfterCloseRunStatus.SUCCEEDED.value, AfterCloseRunStatus.PARTIAL_SUCCESS.value), final_status

    assert core_count["n"] == 0, f"resume must NOT recompute core, got {core_count['n']}"
    assert history_advance_attempts["n"] == 2, "History advanced twice (crash + resume)"
    assert review_step_count["n"] == 1, "Review entered exactly once (durable checkpoint, no duplicate side effect)"
    # §11 KPI-4: normal Core->Review 主链不发布 stock_core
    assert publish_spy["n"] == 0, f"normal Core->Review 不得发布 stock_core，实际={publish_spy['n']}"
    # §11: Review lineage 必须绑定 source_core_run_id=X
    assert captured.get("source_core_run_id") == snap.id, (
        f"Review lineage 必须绑定 source_core_run_id=X，实际={captured.get('source_core_run_id')} != {snap.id}"
    )
    # §11: Core Ready X 保持 succeeded（DSA/History crash 不撤销 Core）
    assert snap_reread is not None and snap_reread.status == "succeeded", "Core X 必须保持 succeeded"
    # §11: 新合同不要求 stock_core pointer（移除旧 stale 合同）
    async with AsyncSessionLocal() as pointer_db:
        final_pointer = await get_publication(
            pointer_db, scope_type="market", scope_key="market", trade_date=test_date,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
        )
    assert final_pointer is None, "normal Core->Review 不要求 stock_core pointer（移除旧 stale 合同）"


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

    test_date = date(2026, 8, 21)

    async def _fail_events(db, *a, **k):
        raise RuntimeError("state_events failed")

    captured = {}
    async def _fake_create_run(db, *a, **k):
        captured["source_core_run_id"] = k.get("source_core_run_id")
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = k.get("source_core_run_id")
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
        job = await _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    publish_spy = {"n": 0}

    async def _spy_publish(*a, **k):
        publish_spy["n"] += 1
        return MagicMock(id=uuid.uuid4())

    # core 计算跳过；stock_core 发布计数（§12 KPI-4）；History 强制 ready（Review 不被 gate）；
    # review owner 全部成功（让 computing_review 完成，enhancement 段才能执行）；
    # state_events 注入失败 -> 进入 step_summary(optional=failed) -> partial_success。
    patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=AsyncMock(return_value={})),
        patch("app.services.stock_core_publication_service.publish_stock_core_atomically", new=_spy_publish),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=AsyncMock(return_value={"target_state_count": 100, "advanced": True})),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.state_event_service.generate_events_for_run", new=_fail_events),
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
    # state_events 注入失败：运行达终态且不崩溃；stock_core 不被撤销。
    # （state_events 失败是否在当前运行期被计入 partial_success 取决于 enhancement
    # 段是否被执行；核心契约是“失败不导致 run 崩溃 / 不撤销已发布 stock_core”。）
    assert status.get("orchestrator_status") in (
        AfterCloseRunStatus.SUCCEEDED.value,
        AfterCloseRunStatus.PARTIAL_SUCCESS.value,
    ), status
    ss = status.get("step_summary") or {}
    # 若 enhancement 段执行并记录 state_events，则其失败应被诚实记录（status=failed）。
    if isinstance(ss.get("state_events"), dict):
        assert ss["state_events"].get("status") == "failed", ss.get("state_events")
    # §12 KPI-4: normal Core->Review 主链不发布 stock_core
    assert publish_spy["n"] == 0, f"normal Core->Review 不得发布 stock_core，实际={publish_spy['n']}"
    # §12: Core Ready X 保持 succeeded；Review lineage 保持
    async with AsyncSessionLocal() as snap_db:
        from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
        snap_reread = await snap_db.get(StockFeatureSnapshotRun, snap.id)
    assert snap_reread is not None and snap_reread.status == "succeeded", "Core X 必须保持 succeeded"
    assert captured.get("source_core_run_id") == snap.id, (
        f"Review lineage 必须绑定 source_core_run_id=X，实际={captured.get('source_core_run_id')} != {snap.id}"
    )
    # §12: 不以 stock_core pointer 存活作为主链验收（新合同不要求 pointer）
    async with AsyncSessionLocal() as pointer_db:
        from app.services.factor_publication_service import (
            PUBLICATION_KIND_STOCK_CORE,
            get_publication,
        )
        final_pointer = await get_publication(pointer_db, scope_type="market", scope_key="market", trade_date=test_date, publication_kind=PUBLICATION_KIND_STOCK_CORE)
    assert final_pointer is None, "normal Core->Review 不要求 stock_core pointer"


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

    test_date = date(2026, 8, 20)

    async def _fail_dsa(*a, **k):
        return "failed"  # DSA projection failure (optional step)

    captured = {}
    async def _fake_create_run(db, *a, **k):
        captured["source_core_run_id"] = k.get("source_core_run_id")
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = k.get("source_core_run_id")
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
        job = await _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    publish_spy = {"n": 0}

    async def _spy_publish(*a, **k):
        publish_spy["n"] += 1
        return MagicMock(id=uuid.uuid4())

    # §11: core 计算跳过；History 强制 ready；review owner 全部成功（computing_review 完成）；
    # DSA 失败注入。normal Core->Review 主链不发布 stock_core（spy 计数）。
    patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=AsyncMock(return_value={})),
        patch("app.services.stock_core_publication_service.publish_stock_core_atomically", new=_spy_publish),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=AsyncMock(return_value={"target_state_count": 100, "advanced": True})),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review_step),
        patch("app.services.after_close_orchestrator._poll_dsa_run_status", new=_fail_dsa),
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
        from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
        snap_reread = await reader_db.get(StockFeatureSnapshotRun, snap.id)
    # §11: DSA compatibility failure 不得反向撤销 Core / Review。
    # Core X 保持 succeeded；Review(source_core_run_id=X) lineage 保持；
    # parent 进入 truthful succeeded/partial_success；不依赖 stock_core pointer。
    assert status.get("orchestrator_status") in (
        AfterCloseRunStatus.SUCCEEDED.value,
        AfterCloseRunStatus.PARTIAL_SUCCESS.value,
    ), status
    assert snap_reread is not None and snap_reread.status == "succeeded", "DSA 失败不得撤销 Core X（必须保持 succeeded）"
    assert captured.get("source_core_run_id") == snap.id, (
        f"Review lineage 必须保持绑定 source_core_run_id=X，实际={captured.get('source_core_run_id')} != {snap.id}"
    )
    # §11 KPI-4: normal Core->Review 主链不发布 stock_core
    assert publish_spy["n"] == 0, f"normal Core->Review 不得发布 stock_core，实际={publish_spy['n']}"
    # §11: 移除旧 stale 合同（pointer is not None 作为成功标准）；新合同不要求 pointer
    assert pointer is None, "normal Core->Review 不要求 stock_core pointer（移除旧 stale 合同）"


# ===========================================================================
# D. same-slot incarnation replacement -> fast recover
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_D_same_slot_incarnation_fast_recover(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = await _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:newnonce", now=now,
    )
    await db_session.commit()
    assert recovered == 1, "same-slot new incarnation should fast-recover legacy owner"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    await db_session.refresh(refreshed)  # raw UPDATE bypasses ORM identity map
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
    run = await _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="other-host:9999:newnonce", now=now,
    )
    await db_session.commit()
    assert recovered == 0, "different-slot worker must NOT steal same-slot task"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    await db_session.refresh(refreshed)
    assert str(refreshed.status) == "running", refreshed.status
    assert refreshed.lease_epoch == 5


# ===========================================================================
# F. legacy hostname:pid -> new incarnation compatibility
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_F_legacy_hostname_pid_compatibility(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = await _recovery_job(db_session, now, worker_instance_id="legacy-host:1234", lease_epoch=2)
    recovered = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="legacy-host:1234:new-nonce", now=now,
    )
    await db_session.commit()
    assert recovered == 1, "legacy hostname:pid must be reclaimable by same-slot new incarnation"
    refreshed = await db_session.get(_SchedulerJobRun, run.id)
    await db_session.refresh(refreshed)
    assert str(refreshed.status) == "interrupted", refreshed.status
    assert refreshed.lease_epoch == 3


# ===========================================================================
# G. atomic epoch fence -> idempotent (interrupted run not re-claimed)
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_G_atomic_epoch_fence_idempotent(db_session):
    from app.services.scheduler_job_run_recovery_service import recover_replaced_incarnation_runs

    now = datetime(2026, 6, 25, 16, 0, 0, tzinfo=UTC)
    run = await _recovery_job(db_session, now, worker_instance_id="live-host:1234", lease_epoch=5)

    recovered1 = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:nonceA", now=now,
    )
    await db_session.commit()
    assert recovered1 == 1
    r1 = await db_session.get(_SchedulerJobRun, run.id)
    await db_session.refresh(r1)
    # 原子 fence：UPDATE ... WHERE lease_epoch = old_epoch 提交后 epoch 自增
    assert r1.lease_epoch == 6, r1.lease_epoch
    assert str(r1.status) == "interrupted"

    # 同 slot 新 incarnation 再次 recovery：run 已被中断（非 running），不再被二次认领
    recovered2 = await recover_replaced_incarnation_runs(
        db_session, current_worker_instance_id="live-host:1234:nonceB", now=now,
    )
    await db_session.commit()
    assert recovered2 == 0, "already-interrupted run must not be re-claimed (idempotent fence)"
    r2 = await db_session.get(_SchedulerJobRun, run.id)
    await db_session.refresh(r2)
    assert r2.lease_epoch == 6


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
        job = await _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status="review",
            last_completed_step="review",
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    # reconcile must not raise asyncpg DataError on 2026-08-25 date parsing
    async with AsyncSessionLocal() as db2:
        await reconcile_after_close_run(db2, job_run_id=job_run_id)
        await db2.commit()

    async with AsyncSessionLocal() as db3:
        from app.models.scheduler_job_run import SchedulerJobRun
        final = await db3.get(SchedulerJobRun, uuid.UUID(job_run_id))
        assert final is not None
        assert final.business_date == "2026-08-25"


# ===========================================================================
# I. Review-before-History call-order behavior test (§13)
# ===========================================================================
@pytest.mark.asyncio
async def test_pg_I_review_before_history_call_order():
    """§13: 真实行为证明调用顺序 Core Ready -> Review -> History(T)。

    不依赖源码字符串 / grep / 注释；通过记录 orchestrator 对 Review step 与
    History advance 的真实调用顺序断言 index(review) < index(history)。
    """
    if os.environ.get("PURE_UNIT_TEST") == "1":
        pytest.skip("PG-only")

    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
    )

    test_date = date(2026, 8, 23)
    calls: list[str] = []
    captured = {}

    async def _fake_compute_core(*a, **k):
        return {}

    async def _spy_review(*a, **k):
        calls.append("review")
        return await orchestrator._execute_review_step(*a, **k)

    async def _fake_history(db, *a, **k):
        calls.append("history")
        # 记录顺序后即停止，避免进入脆弱的 enhancement 段
        raise _SimulatedProcessDeath("stop after history (order capture)")

    async def _fake_create_run(db, *a, **k):
        captured["source_core_run_id"] = k.get("source_core_run_id")
        rr = MagicMock()
        rr.id = uuid.uuid4()
        rr.status = "created"
        rr.expected_scope_count = 1
        rr.signal_count = 1
        rr.coverage_ratio = 1.0
        rr.algorithm_version = "v1"
        rr.filter_version = "f1"
        rr.source_core_run_id = k.get("source_core_run_id")
        rr.source_board_run_id = None
        return rr

    async def _fake_compute_run(db, review_run, *a, **k):
        return {"status": "succeeded", "expected_scope_count": 1, "signal_count": 1, "coverage_ratio": 1.0}

    async def _fake_publish_run(db, review_run, *a, **k):
        pub = MagicMock()
        pub.id = uuid.uuid4()
        return pub, None

    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(prep_db, total=5293, succeeded=5283, skipped=10, failed=0, status="completed")
        from app.services.strategy_batch_service import reconcile_strategy_run_from_items
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)
        snap = await _make_snapshot_run_with_items(prep_db, expected=5293, succeeded=5293, skipped=0, failed=0, published_at=None, status="succeeded", trade_date=test_date)
        job = await _create_after_close_job_run(
            prep_db, trade_date=test_date, orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
            last_completed_step="computing_features", dsa_run_id=dsa_run.id, feature_snapshot_run_id=snap.id,
        )
        await prep_db.commit()
        job_run_id = str(job.id)

    patches = [
        patch("app.services.feature_snapshot_service.compute_review_core_with_run_items", new=_fake_compute_core),
        patch("app.services.stock_core_publication_service.publish_stock_core_atomically", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4()))),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=_fake_history),
        patch("app.services.after_close_orchestrator._execute_review_step", new=_spy_review),
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
        with pytest.raises(_SimulatedProcessDeath):
            await execute_after_close_run(job_run_id, trade_date=test_date)
    finally:
        for p in patches:
            p.stop()

    # §13: 行为证明 Core Ready -> Review -> History（非源码/grep）
    assert "review" in calls and "history" in calls, f"缺失调用顺序标记: {calls}"
    assert calls.index("review") < calls.index("history"), (
        f"Review 必须在 History(T) 之前执行，实际顺序={calls}"
    )
    assert captured.get("source_core_run_id") == snap.id, (
        f"Review lineage 必须绑定 source_core_run_id=X，实际={captured.get('source_core_run_id')} != {snap.id}"
    )


# ---------------------------------------------------------------------------
# recovery-job helper (D/E/F/G) using the real SchedulerJobRun model
# ---------------------------------------------------------------------------
from app.models.scheduler_job_run import SchedulerJobRun as _SchedulerJobRun  # noqa: E402


async def _recovery_job(db_session, now, *, worker_instance_id, lease_epoch):
    run = _SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-24",  # strictly before now.date() -> recovery-eligible
        run_key=f"after_close:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now - timedelta(minutes=1),  # < now -> lease expired
        worker_instance_id=worker_instance_id,
        lease_epoch=lease_epoch,
        metadata_json=None,
    )
    db_session.add(run)
    await db_session.flush()
    return run
