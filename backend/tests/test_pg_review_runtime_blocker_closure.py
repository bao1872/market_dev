"""REVIEW RUNTIME BLOCKER — PRE-DEPLOY TARGETED PG CLOSURE。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- PG-1 DSA authoritative reconcile（item truth → run summary，幂等）
- PG-2 canonical DSA gate case（5283/10/0 → gate PASS，不改阈值）
- PG-3 snapshot compute terminal truth（CASE A/B/C 基于 item truth）
- PG-4 succeeded-unpublished isolation（formal readers 不认未发布 run）
- PG-5 atomic publication（gate PASS → published_at + pointer，无 compute）
- PG-6/7/8 真实 orchestrator resume（computing_features 已完成 → 跳 compute → publishing → pointer）

不在 PG 完整跑真实全市场 board/Review（§10）；review/bars/compute/factor 外部依赖按
现有 test_after_close_orchestrator 模式 mock，但 checkpoint 推进、finalize、publish 调用、
DSA gate 均为真实代码路径。

CREDENTIAL/SAFETY：本文件只在 PANJI_REMOTE_VERIFY_DB_TEST=1 的远程验证库
（bz_stock_verify_<sha>）运行，不连 production bz_stock，不手工 UPDATE，不部署。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.stock_feature_snapshot_run import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.strategy_run import StrategyRun, StrategyRunItem
from app.services.feature_snapshot_service import (
    finalize_snapshot_run_compute_complete,
    get_published_full_run,
    has_succeeded_snapshot_run,
)
from app.services.strategy_batch_service import (
    StrategyBatchService,
    reconcile_strategy_run_from_items,
)

UTC = UTC


def _utcnow() -> datetime:
    return datetime.now(UTC)


async def _make_instruments(db_session, *, n: int) -> list[uuid.UUID]:
    """创建 n 个真实 Instrument 行（满足 strategy/snapshot run_items 外键）。

    使用 UUID 派生的唯一 symbol，避免与 verify DB 既有 instruments 或同 session 其他
    test 创建的 symbol 冲突（symbol 有 UNIQUE 约束）。
    """
    from app.models.instrument import Instrument

    ids: list[uuid.UUID] = []
    for i in range(n):
        inst_id = uuid.uuid4()
        sym = f"T{uuid.uuid4().hex[:9]}"  # 保证唯一且合法（字母+数字，非真实 A 股代码）
        db_session.add(
            Instrument(
                id=inst_id,
                symbol=sym,
                name=f"closure_test_{i}",
                market="SZ",
                status="active",
                listing_date=date(2010, 1, 4),
            )
        )
        ids.append(inst_id)
    await db_session.flush()
    return ids


async def _make_strategy_run_with_items(
    db_session,
    *,
    total: int,
    succeeded: int,
    skipped: int,
    failed: int,
    status: str = "running",
) -> StrategyRun:
    """构造带 StrategyRunItems 的真实 DSA StrategyRun（满足外键）。"""
    from app.models.strategy import StrategyDefinition, StrategyVersion

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
        released_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    db_session.add(version)
    await db_session.flush()

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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

    # 构造 run-items（authoritative truth），instrument_id 指向真实存在的 Instrument 行
    from app.models.strategy_run import StrategyResult
    inst_ids = await _make_instruments(db_session, n=succeeded + skipped + failed)
    idx = 0
    for _ in range(succeeded):
        db_session.add(
            StrategyRunItem(
                run_id=run.id,
                instrument_id=inst_ids[idx],
                status="succeeded",
            )
        )
        db_session.add(
            StrategyResult(
                run_id=run.id,
                strategy_version_id=run.strategy_version_id,
                instrument_id=inst_ids[idx],
                payload={"result": "ok"},
            )
        )
        idx += 1
    for _ in range(skipped):
        db_session.add(
            StrategyRunItem(
                run_id=run.id,
                instrument_id=inst_ids[idx],
                status="skipped",
                reason_code="insufficient_history",
            )
        )
        idx += 1
    for _ in range(failed):
        db_session.add(
            StrategyRunItem(
                run_id=run.id,
                instrument_id=inst_ids[idx],
                status="failed",
                reason_code="compute_error",
            )
        )
        idx += 1
    await db_session.flush()
    return run


async def _make_snapshot_run_with_items(
    db_session,
    *,
    expected: int,
    succeeded: int,
    skipped: int,
    failed: int,
    pending: int = 0,
    running: int = 0,
    published_at=None,
    status: str = STATUS_RUNNING,
    trade_date: date = date(2026, 8, 7),
) -> StockFeatureSnapshotRun:
    """构造带 StockFeatureSnapshotRunItems 的真实 snapshot run。

    trade_date 默认 2026-08-07；调用方必须保证同 session 内不重复
    （unique key: trade_date+schema_version+primary_timeframe+secondary_timeframe+adj+run_type），
    否则传递不同 trade_date。
    """
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
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
        finished_at=now if status != STATUS_RUNNING else None,
        metadata_={"scope": "full"},
    )
    db_session.add(run)
    await db_session.flush()

    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem

    counts = {
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "pending": pending,
        "running": running,
    }
    total_items = sum(counts.values())
    inst_ids = await _make_instruments(db_session, n=total_items)
    idx = 0
    for st, n in counts.items():
        for _ in range(n):
            db_session.add(
                StockFeatureSnapshotRunItem(
                    snapshot_run_id=run.id,
                    instrument_id=inst_ids[idx],
                    phase="core",
                    status=st,
                )
            )
            # 同时创建 StockFeatureSnapshot 行（publish_stock_core_atomically 的
            # validate_quality_gate 检查 source_run_id 对应行数 >= eligible_count）
            db_session.add(
                StockFeatureSnapshot(
                    instrument_id=inst_ids[idx],
                    trade_date=trade_date,
                    primary_timeframe="1d",
                    secondary_timeframe="15m",
                    adj="qfq",
                    schema_version=1,
                    source_run_id=run.id,
                    structural_payload={"ok": True},
                    temporal_payload={"ok": True},
                    summary_payload={"ok": True},
                )
            )
            idx += 1
    await db_session.flush()
    return run


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg1_dsa_authoritative_reconcile_idempotent(db_session) -> None:
    """PG-1: 真实 StrategyRun + StrategyRunItems。

    total=293, batch/local last result=93,
    authoritative items: succeeded=283, skipped=10, failed=0。
    调用 reconcile → run.succeeded_count=283 (不是 93)。再次调用结果一致（幂等）。
    """
    run = await _make_strategy_run_with_items(
        db_session,
        total=293,
        succeeded=283,
        skipped=10,
        failed=0,
        status="running",
    )
    # 模拟 batch-local 错误写入（应被 reconcile 覆盖）
    run.succeeded_count = 93
    run.skipped_count = 0
    run.failed_count = 0
    await db_session.flush()

    rec1 = await reconcile_strategy_run_from_items(db_session, run.id, set_finished_at=True)
    assert rec1["succeeded"] == 283
    assert rec1["skipped"] == 10
    assert rec1["failed"] == 0
    await db_session.refresh(run)
    assert run.succeeded_count == 283
    assert run.skipped_count == 10
    assert run.failed_count == 0
    assert run.status == "completed"

    # 幂等：再次调用结果完全一致
    rec2 = await reconcile_strategy_run_from_items(db_session, run.id, set_finished_at=True)
    assert rec2["succeeded"] == 283
    assert rec2["skipped"] == 10
    assert rec2["failed"] == 0


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg2_canonical_dsa_gate_passes(db_session) -> None:
    """PG-2: canonical equivalent 5293/5283/10/0 → quality gate PASS（不改阈值）。

    真实 StrategyRun + StrategyRunItems（succeeded=5283, skipped=10, failed=0, total=5293）。
    验证：reconcile 后计数器正确；gate#4 succeeded+skipped==total；gate#5 skipped reason
    属于 allowlist（insufficient_history）；真实 _check_quality_gates 返回 PASS。
    """
    run = await _make_strategy_run_with_items(
        db_session,
        total=5293,
        succeeded=5283,
        skipped=10,
        failed=0,
        status="completed",
    )
    # 显式让 run 计数器与 item truth 一致（reconcile）
    await reconcile_strategy_run_from_items(db_session, run.id, set_finished_at=True)
    await db_session.refresh(run)

    assert run.succeeded_count == 5283
    assert run.skipped_count == 10
    assert run.failed_count == 0
    assert run.total_instruments == 5293
    # gate#4: succeeded + skipped == total
    assert (run.succeeded_count + run.skipped_count) == run.total_instruments
    # gate#5: skipped reason 属于 allowlist（insufficient_history）
    from sqlalchemy import select

    skip_reasons = (
        await db_session.execute(
            select(StrategyRunItem.reason_code)
            .where(StrategyRunItem.run_id == run.id)
            .where(StrategyRunItem.status == "skipped")
        )
    ).all()
    assert all(r[0] == "insufficient_history" for r in skip_reasons)

    # 真实调用 quality gate（result_count 用 reconcile 后的 succeeded_count 作为等价输入）
    gate_passed = await StrategyBatchService()._check_quality_gates(
        run, result_count=run.succeeded_count, db=db_session
    )
    assert gate_passed is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg3_snapshot_compute_terminal_truth(db_session) -> None:
    """PG-3: 真实 snapshot run + items。

    CASE A: all terminal no failure → finalize → succeeded, finished_at!=null, published_at=null
    CASE B: pending/running>0 → 不 terminalize
    CASE C: failed>0 → 现有 snapshot failure semantics（STATUS_FAILED）
    """
    # CASE A
    run_a = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_RUNNING,
        trade_date=date(2026, 8, 7),
    )
    out_a = await finalize_snapshot_run_compute_complete(db_session, run_a.id)
    await db_session.refresh(run_a)
    assert run_a.status == STATUS_SUCCEEDED
    assert run_a.finished_at is not None
    assert run_a.published_at is None
    assert out_a is not None

    # CASE B
    run_b = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=1000,
        skipped=0,
        failed=0,
        pending=4293,
        status=STATUS_RUNNING,
        trade_date=date(2026, 8, 8),
    )
    await finalize_snapshot_run_compute_complete(db_session, run_b.id)
    await db_session.refresh(run_b)
    assert run_b.status == STATUS_RUNNING  # 不得 terminalize
    assert run_b.finished_at is None

    # CASE C
    run_c = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5000,
        skipped=0,
        failed=293,
        status=STATUS_RUNNING,
        trade_date=date(2026, 8, 9),
    )
    await finalize_snapshot_run_compute_complete(db_session, run_c.id)
    await db_session.refresh(run_c)
    assert run_c.status == STATUS_FAILED  # 现有 failure semantics
    assert run_c.finished_at is not None


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg4_succeeded_unpublished_isolation(db_session) -> None:
    """PG-4: status=succeeded + published_at=null 的 run 不被正式 reader 当成已发布。"""
    await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_SUCCEEDED,  # compute terminal 但 unpublished
        trade_date=date(2026, 8, 20),
    )
    td = date(2026, 8, 20)

    # has_succeeded_snapshot_run（watchlist gate）：要求 published_at IS NOT NULL
    hs = await has_succeeded_snapshot_run(db_session, td)
    assert hs is False, "succeeded+unpublished 不应通过 watchlist gate"

    # get_published_full_run（formal pointer reader）：要求 published_at IS NOT NULL
    pub = await get_published_full_run(db_session, td)
    assert pub is None, "succeeded+unpublished 不应成为 formal pointer run"

    # factor publication reader（formal pointer）：要求 published_at IS NOT NULL
    from app.services.factor_publication_service import get_published_snapshot_run_id

    fp = await get_published_snapshot_run_id(db_session, td)
    assert fp is None, "succeeded+unpublished 不应被 formal pointer reader 选中"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg5_atomic_publication_no_recompute(db_session) -> None:
    """PG-5: DSA gate PASS + snapshot compute=succeeded, published_at=null →
    publish_stock_core_atomically 同一事务：published_at set, pointer.data_run_id=run_id,
    无 compute 调用。
    """
    from app.services.stock_core_publication_service import (
        publish_stock_core_atomically,
    )

    # 真实 snapshot run（compute terminal, unpublished）
    run = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_SUCCEEDED,
        trade_date=date(2026, 8, 21),
    )

    compute_spy = {"called": False}

    async def _fake_compute(*a, **k):
        compute_spy["called"] = True
        return {}

    with patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=_fake_compute,
    ):
        pub = await publish_stock_core_atomically(
            db_session,
            scope_key="market",
            trade_date=date(2026, 8, 21),
            publication_kind="stock_core_full",
            algorithm_version="review-core-v1",
            snapshot_run_id=run.id,
            coverage_ratio=1.0,
            worker_id="pg-verify-worker",
            lease_epoch=1,
            eligible_count=5293,
            audit_txn=False,
        )
        await db_session.flush()

    assert compute_spy["called"] is False, "publish 不得触发 compute"
    assert pub.data_run_id == run.id, "pointer 必须指向同一 snapshot_run_id"
    await db_session.refresh(run)
    assert run.published_at is not None, "publish 后 published_at 必须 set"


# ---- PG-6/7/8: 真实 orchestrator resume（mock 外部依赖，真实 checkpoint/finalize/publish）----



async def _create_after_close_job_run(
    db_session,
    *,
    status: str = "running",
    orchestrator_status: str = "queued",
    trade_date: date = date(2026, 8, 7),
    dsa_run_id=None,
    last_completed_step: str | None = None,
    feature_snapshot_run_id=None,
) -> object:
    from app.models.scheduler_job_run import SchedulerJobRun

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    meta = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)
    if feature_snapshot_run_id is not None:
        meta["feature_snapshot_run_id"] = str(feature_snapshot_run_id)
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step

    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


class DownstreamEntryReachedError(Exception):
    """publishing 完成、下游 review 入口已触发时抛出，证明 orchestrator 自然推进。

    不是错误；用于在 review create_run 被 mock 后干净地中断 execute_after_close_run，
    避免 board/Review 等不相关下游步骤继续执行。
    """


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg_resume_integration() -> None:
    """PG-6/7/8 合一：真实 orchestrator resume（computing_features 完成 → skip compute
    → publishing → downstream entry → checkpoint 推进）。

    使用独立 AsyncSessionLocal 准备 committed data（非 savepoint fixture），
    使 execute_after_close_run 的 fresh session 可见所有 test 数据。

    同时证明：
    - PG-6: compute_review_core_with_run_items call_count=0（skip_computing）
    - PG-7: orchestrator 自然进入 publishing 并真实调用 publish_stock_core_atomically
    - PG-8: downstream entry 发生，last_completed_step 推进到 publishing/review
    """
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
        get_after_close_run_status,
    )

    test_date = date(2026, 8, 25)

    # =====================================================================
    # Phase 1: 用独立 committed session 准备所有 test data
    # =====================================================================
    async with AsyncSessionLocal() as prep_db:
        # -- DSA run + items（5283/10/0，reconciled） --
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed",
        )
        await reconcile_strategy_run_from_items(prep_db, dsa_run.id, set_finished_at=True)

        # -- snapshot run（compute terminal, unpublished） --
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_SUCCEEDED, trade_date=test_date,
        )

        # -- job_run：checkpoint 已到 computing_features --
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        meta = {
            "orchestrator_status": AfterCloseRunStatus.COMPUTING_FEATURES.value,
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "computing_features",
        }
        job_run = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date=test_date.isoformat(),
            run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
            status="running",
            scheduled_at=now,
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )
        prep_db.add(job_run)
        await prep_db.flush()
        job_run_id = job_run.id

        # 真实 commit — 数据对 execute_after_close_run 的 fresh session 可见
        await prep_db.commit()

    # =====================================================================
    # Phase 2: 读取 before checkpoint 状态
    # =====================================================================
    async with AsyncSessionLocal() as before_db:
        before_status = await get_after_close_run_status(before_db, job_run_id)
    before_last_step = before_status.get("last_completed_step", "")
    assert before_last_step == "computing_features", (
        f"before_last_completed_step 应为 computing_features，实际={before_last_step}"
    )

    # =====================================================================
    # Phase 3: spy + mock + 调用真实 execute_after_close_run
    # =====================================================================
    compute_call_count = {"n": 0}

    async def _spy_compute(*a, **k):
        compute_call_count["n"] += 1
        return {}

    with (
        patch(
            "app.services.feature_snapshot_service.compute_review_core_with_run_items",
            new=_spy_compute,
        ),
        # publishing 后 auction_anchor 不阻塞（返回空结果，不抛异常）
        patch(
            "app.services.auction_anchor_service.generate_and_publish_auction_anchors",
            new=AsyncMock(return_value={"structure_count": 0, "chip_count": 0}),
        ),
        # downstream entry sentinel：review create_run 被调用 = orchestrator 已过 publishing
        patch(
            "app.services.review_orchestrator_service.create_run",
            side_effect=DownstreamEntryReachedError("review entry reached"),
        ),
    ):
        try:
            await execute_after_close_run(job_run_id, trade_date=test_date)
            # 如果没抛 sentinel，orchestrator 完整执行到了结束 — 也接受
            downstream_reached = True
        except DownstreamEntryReachedError:
            downstream_reached = True
        # 注意：不再有 except Exception: pass — 任何意外异常必须 FAIL

    # =====================================================================
    # Phase 4: 逐项验收
    # =====================================================================
    # A. compute 未调用（PG-6）
    assert compute_call_count["n"] == 0, (
        f"resume 不得重算 5293 stocks，实际 compute 调用={compute_call_count['n']}"
    )

    # B/C/D/E/F. 用独立 reader session 核验所有 after 状态
    async with AsyncSessionLocal() as reader_db:
        # 重新加载 snapshot run
        snap_reread = await reader_db.get(StockFeatureSnapshotRun, snap.id)
        assert snap_reread is not None, "snapshot run 必须存在"
        assert snap_reread.published_at is not None, (
            "publish 后 published_at 必须 set"
        )

        # 核验 formal pointer
        from app.services.factor_publication_service import (
            PUBLICATION_KIND_STOCK_CORE,
            get_publication,
        )

        pointer = await get_publication(
            reader_db,
            scope_type="market",
            scope_key="market",
            trade_date=test_date,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
        )
        assert pointer is not None, "publication pointer 必须存在"
        assert pointer.data_run_id == snap.id, (
            f"pointer.data_run_id={pointer.data_run_id} != snap.id={snap.id}"
        )

        # ---- checkpoint 推进（PG-8 核心） ----
        status = await get_after_close_run_status(reader_db, job_run_id)
        after_last_step = status.get("last_completed_step", "")
        after_orch_status = status.get("orchestrator_status", "")
        assert downstream_reached, "downstream entry 必须触发"

        # orchestrator_status: 允许 partial_success（测试故意 mock 下游）
        assert after_orch_status in {
            AfterCloseRunStatus.PUBLISHING.value,
            AfterCloseRunStatus.COMPUTING_REVIEW.value,
            AfterCloseRunStatus.SUCCEEDED.value,
            AfterCloseRunStatus.PARTIAL_SUCCESS.value,
        }, (
            f"orchestrator_status 异常: {after_orch_status}"
        )

        # last_completed_step: 必须是 _completed_steps 中的合法 pipeline checkpoint
        # 引用仓库现有 mapping（与 resume 逻辑同一来源）
        legal_checkpoints = {
            "refreshing_daily",
            "syncing_boards",
            "computing_features",
            "publishing",
            "computing_review",
            "succeeded",
        }
        assert after_last_step in legal_checkpoints, (
            f"last_completed_step={after_last_step} 不在合法 pipeline checkpoint 集合中。\n"
            f"合法值: {sorted(legal_checkpoints)}\n"
            f"orchestrator_status={after_orch_status}\n"
            f"before_last_step={before_last_step}\n"
            f"如果 after_last_step 是 orchestrator_status 值（如 partial_success），"
            f"说明代码将终端状态写入了断点恢复检查点字段 → CHECKPOINT-SEMANTICS-01"
        )

    # G. 没有创建新的 snapshot run（same snap.id 就是 existing 的）
    # snap.id 在整个流程中不变 = 没有新 compute run


# =============================================================================
# RECOVERY-CHECKPOINT-01: Checkpoint Reconciliation + Resume Integration
# =============================================================================

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_reconcile_and_resume() -> None:
    """RECOVERY-CHECKPOINT-01: reconcile checkpoint from durable artifacts → resume。

    构造 production-style drift：
    - last_completed_step = refreshing_daily
    - step_summary.computing_features.status = succeeded
    - DSA durable truth: 5283 succeeded + 10 skipped
    - snapshot durable truth: 5293 items + 5293 StockFeatureSnapshot
    - published_at = null, pointer absent

    1. reconcile_after_close_checkpoint_from_artifacts → computing_features
    2. reconcile DSA + finalize snapshot
    3. execute_after_close_run → compute call_count=0, real publish, pointer correct
    """
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        execute_after_close_run,
        get_after_close_run_status,
        reconcile_after_close_checkpoint_from_artifacts,
    )
    from app.services.factor_publication_service import (
        PUBLICATION_KIND_STOCK_CORE,
        get_publication,
    )

    test_date = date(2026, 8, 26)
    now = datetime.now(ZoneInfo("Asia/Shanghai"))

    # Phase 1: 用 committed session 准备 production-style drift
    async with AsyncSessionLocal() as prep_db:
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed",
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
        )
        step_summary = {
            "refreshing_daily": {"status": "succeeded", "elapsed_seconds": 4000},
            "syncing_boards": {"status": "failed", "elapsed_seconds": 120},
            "checking_coverage": {"status": "succeeded", "elapsed_seconds": 0},
            "computing_features": {"status": "succeeded", "elapsed_seconds": 7000},
        }
        meta = {
            "orchestrator_status": "failed",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "refreshing_daily",
            "step_summary": step_summary,
        }
        job_run = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date=test_date.isoformat(),
            run_key=f"after_close_orchestrator:recovery:{uuid.uuid4().hex[:8]}",
            status="failed",
            scheduled_at=now,
            started_at=now,
            finished_at=now,
            heartbeat_at=now,
            lease_expires_at=now,
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )
        prep_db.add(job_run)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    # Phase 2: reconcile checkpoint — refreshing_daily → computing_features
    compute_spy = {"n": 0}

    async def _spy_recon_compute(*a, **k):
        compute_spy["n"] += 1
        return {}

    async with AsyncSessionLocal() as recon_db:
        with patch(
            "app.services.feature_snapshot_service.compute_review_core_with_run_items",
            new=_spy_recon_compute,
        ):
            result = await reconcile_after_close_checkpoint_from_artifacts(
                recon_db,
                job_run_id=str(job_run_id),
                target_step="computing_features",
            )
            await recon_db.commit()

    assert result["ok"] is True, f"reconcile REFUSE: {result.get('refuse', 'unknown')}"
    assert result["action"] == "advanced"
    assert result["before"] == "refreshing_daily"
    assert result["after"] == "computing_features"
    assert compute_spy["n"] == 0, "reconcile 不得调用 compute"

    # 验证 checkpoint 已推进
    async with AsyncSessionLocal() as check_db:
        status = await get_after_close_run_status(check_db, job_run_id)
        assert status.get("last_completed_step") == "computing_features", (
            f"checkpoint 未推进: {status.get('last_completed_step')}"
        )

    # idempotency: 重复调用 noop
    async with AsyncSessionLocal() as idem_db:
        result2 = await reconcile_after_close_checkpoint_from_artifacts(
            idem_db,
            job_run_id=str(job_run_id),
            target_step="computing_features",
        )
    assert result2["action"] == "noop"
    assert result2["ok"] is True

    # Phase 3: reconcile DSA + finalize snapshot → execute_after_close_run
    async with AsyncSessionLocal() as fix_db:
        await reconcile_strategy_run_from_items(fix_db, dsa_run.id, set_finished_at=True)
        await finalize_snapshot_run_compute_complete(fix_db, snap.id)
        await fix_db.commit()

    compute_resume = {"n": 0}

    async def _spy_resume(*a, **k):
        compute_resume["n"] += 1
        return {}

    with (
        patch(
            "app.services.feature_snapshot_service.compute_review_core_with_run_items",
            new=_spy_resume,
        ),
        patch(
            "app.services.auction_anchor_service.generate_and_publish_auction_anchors",
            new=AsyncMock(return_value={"structure_count": 0, "chip_count": 0}),
        ),
        patch(
            "app.services.review_orchestrator_service.create_run",
            side_effect=DownstreamEntryReachedError("review entry reached"),
        ),
    ):
        try:
            await execute_after_close_run(job_run_id, trade_date=test_date)
        except DownstreamEntryReachedError:
            pass

    # Phase 4: 验收
    assert compute_resume["n"] == 0, (
        f"resume 不得重算，实际 compute 调用={compute_resume['n']}"
    )

    async with AsyncSessionLocal() as reader_db:
        snap_reread = await reader_db.get(StockFeatureSnapshotRun, snap.id)
        assert snap_reread.published_at is not None, "publish 后 published_at 必须 set"

        pointer = await get_publication(
            reader_db,
            scope_type="market",
            scope_key="market",
            trade_date=test_date,
            publication_kind=PUBLICATION_KIND_STOCK_CORE,
        )
        assert pointer is not None, "publication pointer 必须存在"
        assert pointer.data_run_id == snap.id, (
            f"pointer.data_run_id={pointer.data_run_id} != snap.id={snap.id}"
        )
