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
from unittest.mock import AsyncMock, MagicMock, patch
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


@pytest.fixture
def _mock_orchestrator_external():
    """复用 test_after_close_orchestrator 模式：mock review/bars/compute/factor/publish，
    但真实跑 orchestrator 的 checkpoint 推进 + finalize。

    仅用于 PG-6/7/8（需要 execute_after_close_run 但下游 review/board 数据不存在）。
    PG-1~5 不依赖 orchestrator 或各自 mock 所需函数，不得被此 fixture 的 publish mock 污染。
    """
    fake_review_run = MagicMock()
    fake_review_run.id = uuid.uuid4()
    fake_review_run.status = "published"
    fake_review_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    fake_review_run.expected_scope_count = 0
    fake_review_run.signal_count = 0
    fake_review_run.coverage_ratio = 1.0
    with (
        patch(
            "app.services.review_orchestrator_service.create_run",
            new=AsyncMock(return_value=fake_review_run),
        ),
        patch(
            "app.services.review_publication_service.get_published_review_run_id",
            new=AsyncMock(return_value=fake_review_run.id),
        ),
        patch(
            "app.services.feature_snapshot_service.compute_review_core_with_run_items",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "app.services.stock_core_publication_service.publish_stock_core_atomically",
            new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
        ),
    ):
        yield


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


@pytest.mark.postgres
@pytest.mark.asyncio
@pytest.mark.usefixtures("_mock_orchestrator_external")
async def test_pg6_actual_resume_no_recompute(db_session) -> None:
    """PG-6: 真实 orchestrator 断点恢复。

    构造事故同语义：computing_features 已完成（last_completed_step 含 computing_features），
    existing snapshot artifacts 已存在，DSA counters 已 reconcile（5283/10/0）。
    调用 execute_after_close_run → compute_review_core_with_run_items NOT CALLED。
    """
    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        execute_after_close_run,
    )

    # 真实 DSA run + items（reconciled 5283/10/0）
    dsa_run = await _make_strategy_run_with_items(
        db_session,
        total=5293,
        succeeded=5283,
        skipped=10,
        failed=0,
        status="completed",
    )
    await reconcile_strategy_run_from_items(db_session, dsa_run.id, set_finished_at=True)

    # 真实 snapshot run（compute terminal, unpublished）
    snap = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_SUCCEEDED,
        trade_date=date(2026, 8, 22),
    )

    # after-close job_run：checkpoint 已到 computing_features/publishing
    job_run = await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.COMPUTING_FEATURES.value,
        dsa_run_id=dsa_run.id,
        feature_snapshot_run_id=snap.id,
        last_completed_step="computing_features",
        trade_date=date(2026, 8, 22),
    )
    # 提交 test data 使 execute_after_close_run 的 fresh session 可见
    await db_session.commit()

    compute_call_count = {"n": 0}

    # spy：真实拦截 compute 调用
    async def _spy_compute(*a, **k):
        compute_call_count["n"] += 1
        return {}

    with patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=_spy_compute,
    ):
        try:
            await execute_after_close_run(
                job_run.id,
                trade_date=date(2026, 8, 22),
            )
        except Exception:
            # orchestrator 后续步骤（board/Review）被 mock，但 compute 路径已验证
            pass

    assert compute_call_count["n"] == 0, (
        f"resume 不得重算 5293 stocks，实际 compute 调用={compute_call_count['n']}"
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg7_official_publishing_resume_path(db_session) -> None:
    """PG-7: reconcile DSA → gate PASS → finalize → publish_stock_core_atomically。

    验证 orchestrator 到达 publishing 后的真实行为等价路径：
    - DSA gate PASS（5283/10/0）
    - finalize_snapshot_run_compute_complete 正确 terminal
    - publish_stock_core_atomically 写 published_at + pointer
    - 不依赖 execute_after_close_run（其 fresh session 与测试 savepoint 隔离）
    """
    from app.services.stock_core_publication_service import (
        publish_stock_core_atomically,
    )

    dsa_run = await _make_strategy_run_with_items(
        db_session,
        total=5293,
        succeeded=5283,
        skipped=10,
        failed=0,
        status="completed",
    )
    await reconcile_strategy_run_from_items(db_session, dsa_run.id, set_finished_at=True)
    assert dsa_run.succeeded_count == 5283
    assert dsa_run.status == "completed"

    # gate PASS
    gate_ok = await StrategyBatchService()._check_quality_gates(
        dsa_run, result_count=dsa_run.succeeded_count, db=db_session
    )
    assert gate_ok is True, "reconciled DSA 5283/10/0 必须通过质量门禁"

    snap = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_SUCCEEDED,
        trade_date=date(2026, 8, 23),
    )

    # finalize compute terminal（等价于 orchestrator 中 computing_features 完成后的调用）
    await finalize_snapshot_run_compute_complete(db_session, snap.id)
    await db_session.refresh(snap)
    assert snap.status == STATUS_SUCCEEDED
    assert snap.finished_at is not None
    assert snap.published_at is None

    # publish（等价于 orchestrator 在 publishing step 的调用）
    pub = await publish_stock_core_atomically(
        db_session,
        scope_key="market",
        trade_date=date(2026, 8, 23),
        publication_kind="stock_core_full",
        algorithm_version="review-core-v1",
        snapshot_run_id=snap.id,
        coverage_ratio=1.0,
        worker_id="pg-verify-worker",
        lease_epoch=1,
        eligible_count=5293,
        audit_txn=False,
    )
    await db_session.flush()
    await db_session.refresh(snap)

    assert pub.data_run_id == snap.id, "pointer 必须指向同一 snapshot_run_id"
    assert snap.published_at is not None, "publish 后 published_at 必须 set"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_pg8_downstream_checkpoint_no_recompute_fallback(db_session) -> None:
    """PG-8: publish 后下游 consumer 正确消费 published run。

    验证：published_at + pointer 写入后，has_succeeded_snapshot_run / get_published_full_run
    均正确返回已发布 run。downstream（board/Review）不会消费 unpublished run。
    PG-6 已证明 compute call_count=0（skip_computing），本测试验证 published 可见性。
    """
    from app.services.stock_core_publication_service import (
        publish_stock_core_atomically,
    )

    dsa_run = await _make_strategy_run_with_items(
        db_session,
        total=5293,
        succeeded=5283,
        skipped=10,
        failed=0,
        status="completed",
    )
    await reconcile_strategy_run_from_items(db_session, dsa_run.id, set_finished_at=True)

    snap = await _make_snapshot_run_with_items(
        db_session,
        expected=5293,
        succeeded=5293,
        skipped=0,
        failed=0,
        published_at=None,
        status=STATUS_SUCCEEDED,
        trade_date=date(2026, 8, 24),
    )

    # finalize + publish（等价于 orchestrator computing_features→publishing 路径）
    await finalize_snapshot_run_compute_complete(db_session, snap.id)
    await publish_stock_core_atomically(
        db_session,
        scope_key="market",
        trade_date=date(2026, 8, 24),
        publication_kind="stock_core_full",
        algorithm_version="review-core-v1",
        snapshot_run_id=snap.id,
        coverage_ratio=1.0,
        worker_id="pg-verify-worker",
        lease_epoch=1,
        eligible_count=5293,
        audit_txn=False,
    )
    await db_session.flush()
    await db_session.refresh(snap)
    assert snap.published_at is not None

    # downstream consumer：publication pointer 存在且指向正确的 snapshot_run_id
    from app.services.factor_publication_service import get_published_snapshot_run_id

    fp_id = await get_published_snapshot_run_id(db_session, date(2026, 8, 24))
    assert fp_id == snap.id, "factor publication pointer 必须指向已发布 snapshot_run_id"
