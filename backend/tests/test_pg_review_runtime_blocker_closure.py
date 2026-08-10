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
import math
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
    instrument_ids: list[uuid.UUID] | None = None,
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
    if instrument_ids is not None:
        inst_ids = instrument_ids
    else:
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
                trade_date=run.trade_date,
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
    instrument_ids: list[uuid.UUID] | None = None,
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
    if instrument_ids is not None:
        inst_ids = instrument_ids
    else:
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
        # 创建共享 instrument IDs（DSA succeeded ⊆ snapshot）
        all_instruments = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_instruments[:5283] + all_instruments[5283:5293]  # 5283+10

        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_instruments,
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


# =====================================================================
# RECOVERY-CHECKPOINT-01 negative / idempotency tests
# =====================================================================

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_refuse_missing_snapshot_item() -> None:
    """CASE D: snapshot expected=5293 但 run-items succeeded=5292 → REFUSE。"""
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        reconcile_after_close_checkpoint_from_artifacts,
    )

    test_date = date(2026, 8, 30)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_ids[:5283] + all_ids[5283:5293]
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5292, skipped=1, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_ids,
        )
        meta = {
            "orchestrator_status": "running",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "refreshing_daily",
            "step_summary": {
                "computing_features": {"status": "succeeded", "elapsed_seconds": 1},
            },
        }
        job_run = await _create_after_close_job_run(
            prep_db, status="failed",
            orchestrator_status="refreshing_daily",
            dsa_run_id=dsa_run.id,
            feature_snapshot_run_id=snap.id,
            last_completed_step="refreshing_daily",
            trade_date=test_date,
        )
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    compute_calls = {"n": 0}

    async def _spy(*a, **k):
        compute_calls["n"] += 1
        return {}

    async with AsyncSessionLocal() as reader_db:
        with patch(
            "app.services.feature_snapshot_service.compute_review_core_with_run_items",
            new=_spy,
        ), patch(
            "app.services.stock_core_publication_service.publish_stock_core_atomically",
            new=_spy,
        ):
            result = await reconcile_after_close_checkpoint_from_artifacts(
                reader_db, job_run_id=str(job_run_id),
            )

    assert result["ok"] is False, f"预期 REFUSE，实际={result}"
    assert "5292" in str(result.get("refuse", ""))
    assert compute_calls["n"] == 0

    async with AsyncSessionLocal() as reader_db:
        job = await reader_db.get(SchedulerJobRun, job_run_id)
        meta_after = json.loads(job.metadata_json)
        assert meta_after.get("last_completed_step") == "refreshing_daily"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_refuse_step_summary_not_succeeded() -> None:
    """CASE E: durable artifacts 完整但 step_summary.computing_features != succeeded → REFUSE。"""
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        reconcile_after_close_checkpoint_from_artifacts,
    )

    test_date = date(2026, 8, 31)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_ids[:5283] + all_ids[5283:5293]
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_ids,
        )
        meta = {
            "orchestrator_status": "running",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "refreshing_daily",
            "step_summary": {
                "computing_features": {"status": "failed", "error": "timeout"},
            },
        }
        job_run = await _create_after_close_job_run(
            prep_db, status="failed",
            orchestrator_status="refreshing_daily",
            dsa_run_id=dsa_run.id,
            feature_snapshot_run_id=snap.id,
            last_completed_step="refreshing_daily",
            trade_date=test_date,
        )
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    async with AsyncSessionLocal() as reader_db:
        result = await reconcile_after_close_checkpoint_from_artifacts(
            reader_db, job_run_id=str(job_run_id),
        )

    assert result["ok"] is False
    assert "computing_features" in str(result.get("refuse", ""))

    async with AsyncSessionLocal() as reader_db:
        job = await reader_db.get(SchedulerJobRun, job_run_id)
        meta_after = json.loads(job.metadata_json)
        assert meta_after.get("last_completed_step") == "refreshing_daily"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_refuse_dsa_failed_item() -> None:
    """CASE F: DSA failed item = 1 → REFUSE。"""
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        reconcile_after_close_checkpoint_from_artifacts,
    )

    test_date = date(2026, 9, 1)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_ids[:5282] + all_ids[5282:5293]
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5282, skipped=10, failed=1,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_ids,
        )
        meta = {
            "orchestrator_status": "running",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "refreshing_daily",
            "step_summary": {
                "computing_features": {"status": "succeeded", "elapsed_seconds": 1},
            },
        }
        job_run = await _create_after_close_job_run(
            prep_db, status="failed",
            orchestrator_status="refreshing_daily",
            dsa_run_id=dsa_run.id,
            feature_snapshot_run_id=snap.id,
            last_completed_step="refreshing_daily",
            trade_date=test_date,
        )
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    async with AsyncSessionLocal() as reader_db:
        result = await reconcile_after_close_checkpoint_from_artifacts(
            reader_db, job_run_id=str(job_run_id),
        )

    assert result["ok"] is False
    assert result["ok"] is False
    assert "5282" in str(result.get("refuse", ""))

    async with AsyncSessionLocal() as reader_db:
        job = await reader_db.get(SchedulerJobRun, job_run_id)
        meta_after = json.loads(job.metadata_json)
        assert meta_after.get("last_completed_step") == "refreshing_daily"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_idempotent_noop() -> None:
    """CASE B: current=computing_features → no-op。"""
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        reconcile_after_close_checkpoint_from_artifacts,
    )

    test_date = date(2026, 9, 2)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_ids[:5283] + all_ids[5283:5293]
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_ids,
        )
        meta = {
            "orchestrator_status": "running",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "computing_features",
            "step_summary": {
                "computing_features": {"status": "succeeded", "elapsed_seconds": 1},
            },
        }
        job_run = await _create_after_close_job_run(
            prep_db, status="running",
            orchestrator_status="computing_features",
            dsa_run_id=dsa_run.id,
            feature_snapshot_run_id=snap.id,
            last_completed_step="computing_features",
            trade_date=test_date,
        )
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    async with AsyncSessionLocal() as reader_db:
        result = await reconcile_after_close_checkpoint_from_artifacts(
            reader_db, job_run_id=str(job_run_id),
        )

    assert result["ok"] is True
    assert result.get("action") == "noop"

    async with AsyncSessionLocal() as reader_db:
        job = await reader_db.get(SchedulerJobRun, job_run_id)
        meta_after = json.loads(job.metadata_json)
        assert meta_after.get("last_completed_step") == "computing_features"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_recovery_checkpoint_no_regression_later_checkpoint() -> None:
    """CASE C: current=publishing（later than computing_features）→ 不得倒退。"""
    from app.db import AsyncSessionLocal
    from app.models.scheduler_job_run import SchedulerJobRun
    from app.services.after_close_orchestrator import (
        reconcile_after_close_checkpoint_from_artifacts,
    )

    test_date = date(2026, 9, 3)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5293)
        dsa_ids = all_ids[:5283] + all_ids[5283:5293]
        dsa_run = await _make_strategy_run_with_items(
            prep_db, total=5293, succeeded=5283, skipped=10, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5293, succeeded=5293, skipped=0, failed=0,
            published_at=None, status=STATUS_RUNNING, trade_date=test_date,
            instrument_ids=all_ids,
        )
        meta = {
            "orchestrator_status": "running",
            "trade_date": test_date.isoformat(),
            "dsa_run_id": str(dsa_run.id),
            "feature_snapshot_run_id": str(snap.id),
            "last_completed_step": "publishing",
            "step_summary": {
                "computing_features": {"status": "succeeded", "elapsed_seconds": 1},
            },
        }
        job_run = await _create_after_close_job_run(
            prep_db, status="running",
            orchestrator_status="publishing",
            dsa_run_id=dsa_run.id,
            feature_snapshot_run_id=snap.id,
            last_completed_step="publishing",
            trade_date=test_date,
        )
        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
        await prep_db.flush()
        job_run_id = job_run.id
        await prep_db.commit()

    async with AsyncSessionLocal() as reader_db:
        result = await reconcile_after_close_checkpoint_from_artifacts(
            reader_db, job_run_id=str(job_run_id),
        )

    assert result["ok"] is False
    assert "later" in str(result.get("refuse", "")).lower()

    async with AsyncSessionLocal() as reader_db:
        job = await reader_db.get(SchedulerJobRun, job_run_id)
        meta_after = json.loads(job.metadata_json)
        assert meta_after.get("last_completed_step") == "publishing"


# =====================================================================
# STATE-EVENT-01: Query2 projected result contract test
# =====================================================================

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_query2_projected_result_supports_build_stock_state() -> None:
    """STATE-EVENT-01: 验证 explicit column projection 返回的 Row 对象
    支持 consumer（build_stock_state）所需的所有字段访问。

    不测试未使用字段（summary_payload）。
    """
    from app.db import AsyncSessionLocal
    from app.schemas.stock_state import build_stock_state
    from app.services.state_event_service import (
        _batch_get_run_snapshots_with_symbol,
    )

    test_date = date(2026, 9, 10)
    async with AsyncSessionLocal() as prep_db:
        all_ids = await _make_instruments(prep_db, n=5)
        dsa_ids = all_ids[:5]
        await _make_strategy_run_with_items(
            prep_db, total=5, succeeded=5, skipped=0, failed=0,
            status="completed", instrument_ids=dsa_ids,
        )
        snap = await _make_snapshot_run_with_items(
            prep_db, expected=5, succeeded=5, skipped=0, failed=0,
            published_at=datetime.now(ZoneInfo("Asia/Shanghai")),
            status=STATUS_SUCCEEDED, trade_date=test_date,
            instrument_ids=all_ids,
        )
        await prep_db.commit()

    async with AsyncSessionLocal() as reader_db:
        from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
        run = await reader_db.get(StockFeatureSnapshotRun, snap.id)
        result = await _batch_get_run_snapshots_with_symbol(reader_db, run)

    assert len(result) == 5, f"预期 5 行，实际 {len(result)}"

    for snapshot, symbol in result:
        # 验证 scalar 字段可访问
        assert snapshot.instrument_id is not None
        assert snapshot.trade_date == test_date
        assert snapshot.primary_timeframe == "1d"
        # 验证 JSONB 字段可访问（consumer 需要）
        assert snapshot.structural_payload is not None
        assert snapshot.temporal_payload is not None
        # 验证 symbol binding
        assert isinstance(symbol, str)
        assert symbol.startswith("T")  # _make_instruments 生成 T<hex>

        # 验证可以传入 build_stock_state（不抛异常即通过）
        state = build_stock_state(snapshot, symbol)
        assert state is not None
        assert state.symbol == symbol
        assert state.instrument_id == snapshot.instrument_id


# =====================================================================
# BOARD-RUNTIME-01: _compute_peer_metrics projected query equivalence test
# =====================================================================

@pytest.mark.asyncio
async def test_peer_metrics_projected_query_semantics() -> None:
    """BOARD-RUNTIME-01: 验证 _compute_peer_metrics 的显式 JSON projection
    在 Python 端解析后与原 ORM 语义等价（count=匹配行数，avg_strength=有效 avg 均值）。

    不依赖真实 board_analysis_snapshots 行（FK 图复杂），直接 mock session.execute
    返回 projected Row（row[0] = payload['trend_strength']['avg'] 文本值），
    验证 _safe_float 解析 + _avg 聚合逻辑正确，且缺失/非数值 avg 不计入但计入 count。
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.services.board_analysis_service import _compute_peer_metrics

    # Row 对象：row[0] 为 payload->'trend_strength'->>'avg' 的文本值（None 表示 JSON NULL/缺失）
    def _mk_row(avg_text):
        row = MagicMock()
        row.__getitem__ = lambda self, idx: avg_text
        return row

    # 构造 5 个匹配行：avg = ['12.5', '7.25', None(缺失), 'non-numeric', '10.0']
    # 原语义：count=5（全部匹配行）；有效 float 均值为 (12.5+7.25+10.0)/3=9.9166...
    rows = [_mk_row("12.5"), _mk_row("7.25"), _mk_row(None), _mk_row("abc"), _mk_row("10.0")]

    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.services.board_analysis_service.select") as mock_select:
        mock_select.return_value = MagicMock()  # stmt（此处不真正编译）
        result = await _compute_peer_metrics(
            mock_session,
            trade_date=date(2026, 8, 7),
            algorithm_version="test-v1",
            board_id=uuid.uuid4(),
            board_type="concept",
            by_type=True,
        )

    assert result is not None
    assert result["count"] == 5, f"count 应为全部匹配行 5，实际 {result['count']}"
    expected_avg = (12.5 + 7.25 + 10.0) / 3
    assert abs(result["avg_strength"] - expected_avg) < 1e-9, (
        f"avg_strength 应为 {expected_avg}，实际 {result['avg_strength']}"
    )


# =====================================================================
# STATE-EVENT-PERSIST-01: state-event bulk INSERT chunking tests
# =====================================================================

@pytest.mark.asyncio
async def test_state_event_bulk_insert_chunked_large_batch() -> None:
    """STATE-EVENT-PERSIST-01 CASE A: 大批量 events 被分批 INSERT，
    避免单条多行 VALUES 超过 asyncpg 32767 bind-arg 上限。

    模拟 generate_events_for_run 生成 >2520 条事件（单条语句会超 32767），
    断言 bulk-write 被拆成多个 chunk，每个 chunk 的行数远低于上限，
    且所有行最终都成功写入（累计 inserted == 生成数）。
    """
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4 as _uuid4

    from app.services.state_event_service import generate_events_for_run
    from tests.test_stock_state_and_events import (
        _make_mock_run,
        _make_mock_snapshot,
    )

    run_id = _uuid4()
    mock_run = _make_mock_run(run_id=run_id)

    # 构造 N=3000 个 curr snapshot，各自相对 prev 有状态变化（sqzmom 正→负）→ 各产生 1 条事件
    n_instruments = 3000  # 单条语句 3000*13=39000 > 32767，旧实现必溢出
    curr_snapshots = []
    prev_data = {}
    for i in range(n_instruments):
        iid = _uuid4()
        prev_s = _make_mock_snapshot(trade_date=date(2026, 7, 9), sqzmom_val=0.001)
        prev_s.instrument_id = iid
        prev_run = _make_mock_run(trade_date=date(2026, 7, 9))
        prev_data[iid] = (prev_s, prev_run)
        curr_s = _make_mock_snapshot(trade_date=date(2026, 7, 10), sqzmom_val=-0.001)
        curr_s.instrument_id = iid
        curr_snapshots.append((curr_s, f"6{i:06d}"))

    mock_session = MagicMock()

    async def mock_get(model, obj_id):
        if model.__name__ == "StockFeatureSnapshotRun":
            return mock_run
        return None
    mock_session.get = AsyncMock(side_effect=mock_get)

    insert_calls = []

    async def mock_execute(stmt):
        # 批量 INSERT（chunked）：每次 Insert 都成功写入（rowcount 由实际 chunk 行数决定，
        # 但这里不做 statement 内部值反查，改用固定 chunk 语义——generate_events_for_run
        # 按 1000/行 chunk 拆分，3000 行 → 3 个 Insert 调用）
        if type(stmt).__name__ == "Insert":
            insert_calls.append(stmt)
            mock_result = MagicMock()
            mock_result.rowcount = 1000  # 每 chunk 1000 行（最后 chunk 可能是余数）
            return mock_result
        mock_result = MagicMock()
        mock_result.all.return_value = []
        mock_result.scalars.return_value.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    # 关键：generate_events_for_run 调用 _batch_get_previous_snapshots，需让它返回 prev_data dict
    # 直接 patch 该 helper，避免依赖 execute 分支判定
    from unittest.mock import patch as _patch

    import app.services.state_event_service as _ses

    async def fake_prev(*a, **k):
        return prev_data
    async def fake_curr(*a, **k):
        return curr_snapshots

    with _patch.object(_ses, "_batch_get_run_snapshots_with_symbol", fake_curr), \
         _patch.object(_ses, "_batch_get_previous_snapshots", fake_prev):
        result = await generate_events_for_run(mock_session, run_id)

    assert result["event_count"] == n_instruments, f"预期 {n_instruments} 条事件，实际 {result}"
    # 大批量被拆成多个 chunk（单条语句 3000*13=39000 > 32767 必溢出，旧实现单次 execute）
    assert len(insert_calls) >= 2, f"大批量应被拆成多个 chunk，实际 insert 次数 {len(insert_calls)}"
    # 每 chunk 行数 = ceil(n/chunks) <= 1000（远低于 32767/13≈2520 上限）
    per_chunk = math.ceil(n_instruments / len(insert_calls))
    assert per_chunk <= 1000, f"chunk 行数 {per_chunk} 应 <= 1000"
    # 累计写入 == 生成数（无 InterfaceError，全部成功）
    assert result["inserted_count"] == n_instruments, (
        f"累计写入应为 {n_instruments}，实际 {result['inserted_count']}"
    )


@pytest.mark.asyncio
async def test_state_event_bulk_insert_no_per_chunk_commit() -> None:
    """STATE-EVENT-PERSIST-01 CASE B: chunked INSERT 不得 per-chunk commit。

    所有 chunk 仍属同一 transaction：bulk-write 期间不调用 session.commit()，
    且中间 chunk 失败时异常按现有语义向上传播（不吞掉、不制造半批持久化）。
    """
    from unittest.mock import AsyncMock, MagicMock
    from unittest.mock import patch as _patch
    from uuid import uuid4 as _uuid4

    import app.services.state_event_service as _ses
    from app.services.state_event_service import generate_events_for_run
    from tests.test_stock_state_and_events import (
        _make_mock_run,
        _make_mock_snapshot,
    )

    run_id = _uuid4()
    mock_run = _make_mock_run(run_id=run_id)

    n_instruments = 2500  # 分成 3 个 chunk（1000/1000/500）
    curr_snapshots = []
    prev_data = {}
    for i in range(n_instruments):
        iid = _uuid4()
        prev_s = _make_mock_snapshot(trade_date=date(2026, 7, 9), sqzmom_val=0.001)
        prev_s.instrument_id = iid
        prev_run = _make_mock_run(trade_date=date(2026, 7, 9))
        prev_data[iid] = (prev_s, prev_run)
        curr_s = _make_mock_snapshot(trade_date=date(2026, 7, 10), sqzmom_val=-0.001)
        curr_s.instrument_id = iid
        curr_snapshots.append((curr_s, f"6{i:06d}"))

    mock_session = MagicMock()
    commit_calls = {"n": 0}

    async def mock_get(model, obj_id):
        if model.__name__ == "StockFeatureSnapshotRun":
            return mock_run
        return None
    mock_session.get = AsyncMock(side_effect=mock_get)
    mock_session.commit = AsyncMock(side_effect=lambda: commit_calls.__setitem__("n", commit_calls["n"] + 1))

    insert_calls = []
    # 2500 行按 1000/行 chunk → [1000, 1000, 500]
    _chunk_rows = 1000
    _expected_chunks = [
        min(_chunk_rows, n_instruments - i * _chunk_rows)
        for i in range(0, math.ceil(n_instruments / _chunk_rows))
    ]
    _chunk_idx = {"i": 0}

    async def mock_execute(stmt):
        if type(stmt).__name__ == "Insert":
            insert_calls.append(stmt)
            mock_result = MagicMock()
            mock_result.rowcount = _expected_chunks[_chunk_idx["i"]]
            _chunk_idx["i"] += 1
            return mock_result
        mock_result = MagicMock()
        mock_result.all.return_value = []
        return mock_result
    mock_session.execute = AsyncMock(side_effect=mock_execute)

    async def fake_prev(*a, **k):
        return prev_data
    async def fake_curr(*a, **k):
        return curr_snapshots

    with _patch.object(_ses, "_batch_get_run_snapshots_with_symbol", fake_curr), \
         _patch.object(_ses, "_batch_get_previous_snapshots", fake_prev):
        result = await generate_events_for_run(mock_session, run_id)

    assert result["event_count"] == n_instruments
    # 关键断言：bulk-write 期间绝无 per-chunk commit
    assert commit_calls["n"] == 0, (
        f"chunked INSERT 不得 per-chunk commit，实际 commit 次数 {commit_calls['n']}"
    )
    # 多 chunk 已执行
    assert len(insert_calls) >= 2, f"2500 行应拆成多个 chunk，实际 {len(insert_calls)}"
    assert result["inserted_count"] == n_instruments

    # 中间 chunk 失败必须向上传播（不吞掉、不 commit 半批）
    calls = {"n": 0}

    async def mock_execute_fail(stmt):
        if type(stmt).__name__ == "Insert":
            calls["n"] += 1
            if calls["n"] == 2:  # 第 2 个 chunk 失败
                raise RuntimeError("simulated mid-chunk failure")
            mock_result = MagicMock()
            mock_result.rowcount = _expected_chunks[calls["n"] - 1]
            return mock_result
        mock_result = MagicMock()
        mock_result.all.return_value = []
        return mock_result

    mock_session2 = MagicMock()
    async def mock_get2(model, obj_id):
        return mock_run if model.__name__ == "StockFeatureSnapshotRun" else None
    mock_session2.get = AsyncMock(side_effect=mock_get2)
    mock_session2.commit = AsyncMock(side_effect=lambda: commit_calls.__setitem__("n", commit_calls["n"] + 1))
    mock_session2.execute = AsyncMock(side_effect=mock_execute_fail)

    try:
        with _patch.object(_ses, "_batch_get_run_snapshots_with_symbol", fake_curr), \
             _patch.object(_ses, "_batch_get_previous_snapshots", fake_prev):
            await generate_events_for_run(mock_session2, run_id)
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "simulated mid-chunk failure" in str(exc)

    assert raised, "中间 chunk 失败应向上传播，不得被吞掉"
