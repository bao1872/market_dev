"""盘后编排服务测试 - 验证 create/execute/get_status/retry 的事件写入。

覆盖：
- create_after_close_run 创建任务并写入 queued 事件
- get_after_close_run_status 返回编排状态 + 事件时间线
- retry_after_close_run 重置 failed 任务并写入事件
- execute_after_close_run 成功路径写入各步骤事件
- execute_after_close_run 失败路径写入 failed 事件

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：
- create/retry 内部调用 db.commit()，测试中用 patch.object 替换为 flush（不破坏 nested 事务）
- execute 使用独立 AsyncSessionLocal，测试中 mock 为返回 db_session 的假 context manager
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_AFTER_CLOSE,
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
    execute_after_close_run,
    get_after_close_run_status,
    repair_stale_after_close_snapshot_runs,
    retry_after_close_run,
)
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.job_run_event_service import append_event
from app.services.strategy_batch_service import StrategyBatchService


@pytest.fixture(autouse=True)
def _mock_review_phase_boundary():
    """盘后编排器 review 阶段边界 mock（模块级 autouse）。

    本文件测试目标是编排器主流程（事件/状态/repair/publish），
    review 计算与发布合同由 tests/test_review_*.py 专门覆盖。
    [CHANGE-20260801-REVIEW-CLOSURE] computing_review 阶段要求
    stock_core + board_analysis 正式 pointer；本文件 fixtures 不构造
    publication 数据，故 mock create_run 返回已 published 的 run，
    走 idempotent_reuse_published_run 路径（跳过计算与发布）。
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
    ):
        yield



async def _create_after_close_job_run(
    db_session,
    *,
    status: str = "running",
    orchestrator_status: str = "queued",
    trade_date: date = date(2026, 6, 25),
    dsa_run_id: uuid.UUID | None = None,
) -> SchedulerJobRun:
    """直接创建测试用 after_close SchedulerJobRun（不经过 create_after_close_run）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    meta = {
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date.isoformat(),
    }
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)

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


async def _create_dsa_strategy_run(
    db_session,
    *,
    status: str = "completed",
    trade_date: date = date(2026, 6, 25),
) -> tuple[StrategyRun, uuid.UUID]:
    """创建测试用 DSA StrategyRun（满足 orchestrator 查询）。

    需要先创建 StrategyDefinition + StrategyVersion 满足外键约束。
    """
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
    dsa_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status=status,
        input_overrides={},
        idempotency_key=f"test_dsa:{version.id}:{trade_date}:{uuid.uuid4().hex[:8]}",
        total_instruments=100,
        succeeded_count=95,
        failed_count=0,
        started_at=now,
        finished_at=now,
    )
    db_session.add(dsa_run)
    await db_session.flush()
    return dsa_run, version.id


@pytest.mark.asyncio
async def test_create_after_close_run_writes_queued_event(db_session) -> None:
    """测试 1：create_after_close_run 创建任务并写入 queued 事件。

    mock acquire_job_run_lock 避免真实 DB 锁；
    mock db.commit 替换为 flush 避免破坏 fixture nested 事务。
    """
    trade_date = date(2026, 6, 25)
    fake_job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:{trade_date.isoformat()}",
        status="running",
        scheduled_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        heartbeat_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        lease_expires_at=datetime.now(ZoneInfo("Asia/Shanghai")),
    )
    db_session.add(fake_job_run)
    await db_session.flush()

    async def _fake_acquire(db, **kwargs):
        # Phase 2: acquire_job_run_lock 返回 (job_run, is_new) tuple
        return (fake_job_run, True)

    with patch(
        "app.services.after_close_orchestrator.acquire_job_run_lock",
        new=_fake_acquire,
    ), patch.object(db_session, "commit", new=db_session.flush):
        result, is_new = await create_after_close_run(db=db_session, trade_date=trade_date)

    assert is_new is True
    assert result.id == fake_job_run.id
    assert result.status == "running"

    # 验证 metadata_json 含 orchestrator_status=queued
    assert result.metadata_json is not None
    meta = json.loads(result.metadata_json)
    assert meta["orchestrator_status"] == AfterCloseRunStatus.QUEUED.value
    assert meta["trade_date"] == "2026-06-25"

    # 验证事件写入
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, result.id, limit=10)
    assert len(events) >= 1
    queued_events = [e for e in events if e.step == AfterCloseRunStatus.QUEUED.value]
    assert len(queued_events) >= 1
    assert queued_events[0].level == "info"
    assert "盘后编排" in queued_events[0].message


@pytest.mark.asyncio
async def test_create_after_close_run_returns_existing_on_duplicate(db_session) -> None:
    """测试 1.1：create_after_close_run 在 acquire_job_run_lock 返回 (existing, False) 时直接返回已有任务。

    Phase 2: acquire_job_run_lock 已返回 existing（不再需要 create_after_close_run 内部 SELECT）。
    模拟同日已有运行中任务的幂等场景：acquire 返回 (existing, False) → 函数直接返回 (existing, False)。
    """
    trade_date = date(2026, 6, 25)
    existing_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_orchestrator:{trade_date.isoformat()}",
        status="running",
        scheduled_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        heartbeat_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        lease_expires_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        metadata_json=json.dumps({"orchestrator_status": "refreshing_daily"}),
    )
    db_session.add(existing_run)
    await db_session.flush()

    async def _fake_acquire_returns_existing(db, **kwargs):
        # Phase 2: acquire_job_run_lock 返回 (existing, False)，已有活跃任务
        return (existing_run, False)

    with patch(
        "app.services.after_close_orchestrator.acquire_job_run_lock",
        new=_fake_acquire_returns_existing,
    ):
        result, is_new = await create_after_close_run(db=db_session, trade_date=trade_date)

    assert is_new is False
    assert result.id == existing_run.id
    assert result.status == "running"


@pytest.mark.asyncio
async def test_get_after_close_run_status_returns_events(db_session) -> None:
    """测试 2：get_after_close_run_status 返回编排状态 + 事件时间线。

    直接创建 job_run + 写入事件，验证查询结果。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(
        db_session,
        orchestrator_status=AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        dsa_run_id=dsa_run.id,
    )

    # 写入多条事件（设置递增 created_at 确保倒序可预测）
    e1 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.QUEUED.value,
        message="盘后编排已创建",
    )
    await db_session.flush()
    e1.created_at = datetime(2026, 6, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e2 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.REFRESHING_DAILY.value,
        message="开始刷新日线",
    )
    await db_session.flush()
    e2.created_at = datetime(2026, 6, 25, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e3 = await append_event(
        db=db_session, job_run_id=job_run.id,
        step=AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        message=f"等待 DSA: dsa_run_id={dsa_run.id}",
        payload={"dsa_run_id": str(dsa_run.id)},
    )
    await db_session.flush()
    e3.created_at = datetime(2026, 6, 25, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    result = await get_after_close_run_status(db=db_session, job_run_id=job_run.id)

    assert result["job_run_id"] == str(job_run.id)
    assert result["status"] == "running"
    assert result["orchestrator_status"] == AfterCloseRunStatus.WAITING_DSA_WORKER.value
    assert result["trade_date"] == "2026-06-25"
    assert result["dsa_run_id"] == str(dsa_run.id)
    assert result["dsa_run_status"] == "completed"
    assert len(result["events"]) == 3
    # 倒序：最新事件在前（18:00 -> 17:00 -> 16:00）
    assert result["events"][0]["step"] == AfterCloseRunStatus.WAITING_DSA_WORKER.value
    assert result["events"][1]["step"] == AfterCloseRunStatus.REFRESHING_DAILY.value
    assert result["events"][2]["step"] == AfterCloseRunStatus.QUEUED.value


@pytest.mark.asyncio
async def test_retry_after_close_run_writes_event(db_session) -> None:
    """测试 3：retry_after_close_run 重置 failed 任务并写入 queued 事件。

    mock db.commit 替换为 flush 避免破坏 fixture nested 事务。
    """
    job_run = await _create_after_close_job_run(
        db_session,
        status="failed",
        orchestrator_status=AfterCloseRunStatus.FAILED.value,
    )
    job_run.error_message = "模拟失败"
    await db_session.flush()

    with patch.object(db_session, "commit", new=db_session.flush):
        result = await retry_after_close_run(db=db_session, job_run_id=job_run.id)

    # [Phase5] retry 重置为 queued（由独立 Worker 领取），不再是 running
    assert result.status == "queued"
    assert result.error_message is None
    assert result.finished_at is None

    assert result.metadata_json is not None
    meta = json.loads(result.metadata_json)
    assert meta["orchestrator_status"] == AfterCloseRunStatus.QUEUED.value

    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=10)
    queued_events = [e for e in events if e.step == AfterCloseRunStatus.QUEUED.value]
    assert len(queued_events) >= 1
    assert "重试" in queued_events[-1].message


@pytest.mark.asyncio
async def test_execute_writes_status_events(db_session) -> None:
    """测试 4：execute_after_close_run 成功路径写入各步骤事件。

    mock AsyncSessionLocal 返回 db_session（不真正关闭）；
    mock BarsSchedulerService.refresh_all_instruments 返回含 dsa_run_id 的 BatchResult；
    mock _poll_dsa_run_status 返回 completed；
    mock StrategyBatchService._check_quality_gates 返回 True；
    mock StrategyBatchService.publish_run 返回 mock run。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    # 构造 mock AsyncSessionLocal（async with 返回 db_session，不关闭）
    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    # mock refresh_all_instruments 返回含 dsa_run_id 的 BatchResult
    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    # mock publish_run 返回的对象
    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # [FixA] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证事件序列：应包含 refreshing_daily → computing_features → publishing → succeeded
    # [Phase8A] 旧四状态（creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）
    # 收敛为 computing_features
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    steps = [e.step for e in events]

    assert AfterCloseRunStatus.REFRESHING_DAILY.value in steps, f"缺少 refreshing_daily 事件: {steps}"
    assert AfterCloseRunStatus.COMPUTING_FEATURES.value in steps, f"缺少 computing_features 事件: {steps}"
    assert AfterCloseRunStatus.PUBLISHING.value in steps, f"缺少 publishing 事件: {steps}"
    assert AfterCloseRunStatus.SUCCEEDED.value in steps, f"缺少 succeeded 事件: {steps}"

    # [AfterClose] - 不断言事件顺序：同一事务内 created_at 可能相同，
    # list_events 的倒序仅保证 created_at 不同时正确排序。
    # 验证 job_run 状态更新为 succeeded（最终状态）
    assert job_run.status == "succeeded"
    assert job_run.finished_at is not None


@pytest.mark.asyncio
async def test_execute_failure_writes_failed_event(db_session) -> None:
    """测试 5：execute_after_close_run 失败路径写入 failed 事件。

    mock refresh_all_instruments 抛异常，验证 failed 事件写入 + job_run.status=failed。
    """
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    exc = RuntimeError("pytdx 连接超时（模拟）")

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: job_run if id == job_run.id else None),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(side_effect=exc),
    ):
        with pytest.raises(RuntimeError, match="pytdx 连接超时"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=date(2026, 6, 25),
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # 验证 failed 事件写入
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    failed_events = [e for e in events if e.step == AfterCloseRunStatus.FAILED.value]
    assert len(failed_events) >= 1, f"缺少 failed 事件: {[e.step for e in events]}"
    assert failed_events[0].level == "error"
    assert "pytdx 连接超时" in failed_events[0].message
    assert failed_events[0].payload is not None
    assert failed_events[0].payload["error_type"] == "RuntimeError"
    assert "traceback" in failed_events[0].payload

    # 验证 job_run 状态更新为 failed
    assert job_run.status == "failed"
    assert job_run.error_message is not None
    assert "pytdx" in job_run.error_message
    assert job_run.finished_at is not None


@pytest.mark.asyncio
async def test_execute_feature_snapshot_failure_skips_publishing(db_session) -> None:
    """测试 5.1：feature_snapshot 失败比例超阈值时不应进入 publishing。

    [Blocker2] 场景：compute_for_trade_date 抛 RuntimeError（失败比例超阈值），
    要求：
    1. publish_run 不被调用（不发布失败日期结果）
    2. orchestrator 状态更新为 failed
    3. failed 事件写入，消息中包含 feature_snapshot 失败上下文
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # [Blocker2] 模拟 feature_snapshot 失败比例超阈值
    snapshot_exc = RuntimeError(
        "feature_snapshot 失败比例 40.0% 超过阈值 30% (failed=2, total=5)"
    )

    publish_call_count = 0

    async def _fake_publish_run(*args, **kwargs):
        nonlocal publish_call_count
        publish_call_count += 1
        return fake_published_run

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        # [Blocker2] 测试 fixture 使用 savepoint 隔离，显式 rollback 会回滚 fixture 数据；
        # mock 为 no-op，由 fixture 退出时统一回滚（生产中由 async with 自动 rollback）。
        db_session, "rollback", new=AsyncMock(return_value=None),
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: {
            (SchedulerJobRun, job_run.id): job_run,
            (StrategyRun, dsa_run.id): dsa_run,
        }.get((model, id))),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=_fake_publish_run,
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(side_effect=snapshot_exc),
    ):
        with pytest.raises(RuntimeError, match="失败比例"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=date(2026, 6, 25),
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # [Blocker2] 验证 1：publish_run 未被调用
    assert publish_call_count == 0, (
        f"feature_snapshot 失败时不应进入 publishing，但 publish_run 被调用了 {publish_call_count} 次"
    )

    # [Blocker2] 验证 2：job_run 状态为 failed
    assert job_run.status == "failed"
    assert job_run.error_message is not None
    assert "失败比例" in job_run.error_message

    # [Blocker2] 验证 3：failed 事件写入
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    failed_events = [e for e in events if e.step == AfterCloseRunStatus.FAILED.value]
    assert len(failed_events) >= 1
    assert "失败比例" in failed_events[0].message

    # [Blocker2] 验证 4：不应有 publishing / succeeded 事件
    steps = [e.step for e in events]
    assert AfterCloseRunStatus.PUBLISHING.value not in steps, (
        f"feature_snapshot 失败不应有 publishing 事件: {steps}"
    )
    assert AfterCloseRunStatus.SUCCEEDED.value not in steps, (
        f"feature_snapshot 失败不应有 succeeded 事件: {steps}"
    )


@pytest.mark.asyncio
async def test_execute_feature_snapshot_success_creates_succeeded_run(db_session) -> None:
    """[Phase7 测试 6] after_close feature_snapshot 成功写 run.status='succeeded'。

    场景：compute_for_trade_date 成功返回 snapshot_count=1, failed_count=0。
    要求：
    1. 创建 StockFeatureSnapshotRun 记录
    2. run.status='succeeded'
    3. run.published_at 非空
    4. run.snapshot_count=1, run.failed_count=0
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # [Phase7] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        # StockFeatureSnapshotRun 走真实 DB 查询
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=target_trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 StockFeatureSnapshotRun 记录已创建且 succeeded
    from sqlalchemy import select
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == target_trade_date,
        StockFeatureSnapshotRun.run_type == "after_close",
    )
    result = await db_session.execute(stmt)
    runs = result.scalars().all()
    assert len(runs) >= 1, f"应创建至少 1 个 snapshot run，实际 {len(runs)}"
    run = runs[0]
    assert run.status == "succeeded", f"run.status 应为 succeeded，实际 {run.status}"
    assert run.published_at is not None, "succeeded run 应写 published_at"
    assert run.snapshot_count == 1
    assert run.failed_count == 0


@pytest.mark.asyncio
async def test_compute_for_trade_date_not_passed_dsa_run_id_kwarg(db_session) -> None:
    """[BUGFIX] 验证 compute_for_trade_date 不接收 dsa_run_id / strategy_version_id kwargs。

    根因：orchestrator 曾传入 dsa_run_id + strategy_version_id 给
    compute_for_trade_date()，但该函数签名只接受 progress_callback + source_run_id，
    导致 TypeError: got an unexpected keyword argument 'dsa_run_id'。

    本测试设置 strategy_version_id 非 None + DSA 未完成（dsa_already_completed=False），
    即曾触发 bug 的条件，验证修复后不再传无效 kwargs。
    StrategyResult 写入由 strategy_batch_service.write_results 完成，不依赖此 kwarg。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="running")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    # 用 MagicMock 包裹 AsyncMock 以捕获 call args
    compute_spy = AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0})

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "execute_run",
        new=AsyncMock(),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=compute_spy,
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=target_trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 compute_for_trade_date 被调用
    assert compute_spy.called, "compute_for_trade_date 应被调用"

    # 验证不传 dsa_run_id / strategy_version_id（函数签名不接受的 kwargs）
    call_kwargs = compute_spy.call_args.kwargs
    assert "dsa_run_id" not in call_kwargs, (
        f"compute_for_trade_date 不应接收 dsa_run_id kwarg，实际调用 kwargs: {call_kwargs}"
    )
    assert "strategy_version_id" not in call_kwargs, (
        f"compute_for_trade_date 不应接收 strategy_version_id kwarg，实际调用 kwargs: {call_kwargs}"
    )
    # 验证仍正确传递 progress_callback + snapshot_run_id
    # [FIX 2026-07-31] orchestrator 调用 compute_review_core_with_run_items
    # 传递的是 snapshot_run_id（不是 source_run_id），与函数签名一致
    assert "progress_callback" in call_kwargs, "应传递 progress_callback"
    assert "snapshot_run_id" in call_kwargs, "应传递 snapshot_run_id"


@pytest.mark.asyncio
async def test_execute_feature_snapshot_failure_creates_failed_run(db_session) -> None:
    """[Phase7 测试 7] after_close feature_snapshot 失败写 run.status='failed' 且不 publishing。

    场景：compute_for_trade_date 抛 RuntimeError（失败比例超阈值）。
    要求：
    1. 创建 StockFeatureSnapshotRun 记录
    2. run.status='failed'
    3. run.published_at 为 None（failed 不发布）
    4. publish_run 不被调用（不 publishing）
    5. orchestrator 状态更新为 failed
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    snapshot_exc = RuntimeError(
        "feature_snapshot 失败比例 40.0% 超过阈值 30% (failed=2, total=5)"
    )

    publish_call_count = 0

    async def _fake_publish_run(*args, **kwargs):
        nonlocal publish_call_count
        publish_call_count += 1
        return MagicMock()

    target_trade_date = date(2026, 6, 25)

    # [Phase7] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    # （finish_snapshot_run 需要真实查询 run 记录以更新 status='failed'）
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        # StockFeatureSnapshotRun 走真实 DB 查询
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "rollback", new=AsyncMock(return_value=None),
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=_fake_publish_run,
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(side_effect=snapshot_exc),
    ):
        with pytest.raises(RuntimeError, match="失败比例"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=target_trade_date,
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # 验证 publish_run 未被调用
    assert publish_call_count == 0, (
        f"feature_snapshot 失败时不应进入 publishing，但 publish_run 被调用了 {publish_call_count} 次"
    )

    # 验证 StockFeatureSnapshotRun 记录已创建且 failed
    from sqlalchemy import select
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == target_trade_date,
        StockFeatureSnapshotRun.run_type == "after_close",
    )
    result = await db_session.execute(stmt)
    runs = result.scalars().all()
    assert len(runs) >= 1, f"应创建至少 1 个 snapshot run，实际 {len(runs)}"
    run = runs[0]
    assert run.status == "failed", f"run.status 应为 failed，实际 {run.status}"
    assert run.published_at is None, "failed run 不应写 published_at"

    # 验证 job_run 状态为 failed
    assert job_run.status == "failed"


@pytest.mark.asyncio
async def test_execute_starts_heartbeat_loop_during_long_refresh(db_session) -> None:
    """测试 6：长阶段（refresh_all_instruments）执行期间应启动后台心跳任务，防止 watchdog 误判 stale。

    场景：c1fec906 任务在 refreshing_daily 阶段调用 refresh_all_instruments（约13分钟），
    期间无 heartbeat_at 更新，watchdog 60s 阈值误判任务 interrupted。

    修复：在 refresh_all_instruments 调用前启动 _job_run_heartbeat_loop 后台任务，
    完成后取消。本测试验证 _job_run_heartbeat_loop 被调用。
    """
    import asyncio as _asyncio

    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 记录心跳任务调用
    heartbeat_calls = []

    async def _fake_heartbeat_loop(*args, **kwargs):
        heartbeat_calls.append({"args": args, "kwargs": kwargs})
        # 模拟心跳任务运行直到被取消
        try:
            await _asyncio.sleep(100)
        except _asyncio.CancelledError:
            pass

    # refresh_all_instruments 执行期间，心跳任务应已启动
    refresh_started = _asyncio.Event()

    async def _fake_refresh(*args, **kwargs):
        refresh_started.set()
        # 让心跳任务有机会被创建
        await _asyncio.sleep(0.05)
        return fake_batch_result

    # [FixA] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=_fake_heartbeat_loop,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=_fake_refresh,
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证心跳任务被启动（refresh 阶段至少 1 次）
    # [CHANGE-20260728-007] 流程含 refresh + feature_snapshot 两个长阶段，
    # 每个阶段各自启动心跳任务，故 heartbeat_calls >= 1。
    # 本测试关注 refresh 阶段是否启动心跳（防止 watchdog 误判 stale），
    # 不限制总次数。
    assert len(heartbeat_calls) >= 1, (
        f"refresh 阶段应至少启动 1 次后台心跳任务，实际 {len(heartbeat_calls)} 次"
    )
    # 验证 refresh_all_instruments 被调用（事件已 set）
    assert refresh_started.is_set(), "refresh_all_instruments 应被调用"


@pytest.mark.asyncio
async def test_feature_snapshot_stage_starts_heartbeat_loop(db_session) -> None:
    """[Heartbeat] feature_snapshot 阶段应启动后台心跳任务，防止租约过期。

    场景：compute_for_trade_date 执行期间耗时较长，需有 _job_run_heartbeat_loop 保活。
    """
    import asyncio as _asyncio

    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    heartbeat_calls = []

    async def _fake_heartbeat_loop(*args, **kwargs):
        heartbeat_calls.append({"args": args, "kwargs": kwargs})
        try:
            await _asyncio.sleep(100)
        except _asyncio.CancelledError:
            pass

    # 模拟 compute_for_trade_date 耗时，期间心跳任务应已启动
    snapshot_started = _asyncio.Event()

    async def _fake_compute(*args, **kwargs):
        snapshot_started.set()
        # 让心跳任务有机会被创建
        await _asyncio.sleep(0.05)
        return {"snapshot_count": 1, "failed_count": 0}

    # [FixA] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=_fake_heartbeat_loop,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=_fake_compute,
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 feature_snapshot 阶段心跳任务被启动 1 次
    assert len(heartbeat_calls) == 1, (
        f"应启动 1 次 feature_snapshot 后台心跳任务，实际 {len(heartbeat_calls)} 次"
    )
    assert heartbeat_calls[0]["args"][0] == job_run.id
    assert snapshot_started.is_set(), "compute_for_trade_date 应被调用"


@pytest.mark.asyncio
async def test_feature_snapshot_progress_callback_updates_heartbeat_and_metadata(
    db_session,
) -> None:
    """[Heartbeat] feature_snapshot 进度回调应更新 heartbeat/lease/metadata 进度。

    场景：compute_for_trade_date 调用 progress_callback，验证 job_run 心跳与 metadata 被更新。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)
    original_heartbeat_at = job_run.heartbeat_at
    original_lease = job_run.lease_expires_at

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    async def _fake_compute(*args, **kwargs):
        progress_callback = kwargs.get("progress_callback")
        if progress_callback is not None:
            await progress_callback(
                processed=1000, total=1000, snapshot_count=999, failed_count=1
            )
        return {"snapshot_count": 999, "failed_count": 1}

    # [FixA] mock db.get：已知 ID 返回 mock 对象，StockFeatureSnapshotRun 走真实 DB 查询
    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=_fake_compute,
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证心跳与 lease 被更新
    assert job_run.heartbeat_at is not None
    assert original_heartbeat_at is not None
    assert job_run.heartbeat_at > original_heartbeat_at
    assert job_run.lease_expires_at is not None
    assert original_lease is not None
    assert job_run.lease_expires_at > original_lease

    # 验证 metadata 含进度
    assert job_run.metadata_json is not None
    meta = json.loads(job_run.metadata_json)
    assert "feature_snapshot_progress" in meta
    progress = meta["feature_snapshot_progress"]
    assert progress["processed"] == 1000
    assert progress["total"] == 1000
    assert progress["snapshot_count"] == 999
    assert progress["failed_count"] == 1
    assert "feature_snapshot_run_id" in meta
    # [Phase8A] last_started_step 现在为 COMPUTING_FEATURES（旧四状态收敛）
    assert meta["last_started_step"] == AfterCloseRunStatus.COMPUTING_FEATURES.value


@pytest.mark.asyncio
async def test_repair_stale_snapshot_run_marks_failed_when_orchestrator_interrupted(
    db_session,
) -> None:
    """[Repair] orchestrator interrupted + snapshot_run running + 快照不足 → 标记 failed。

    [P0-1] snapshots 必须设置 source_run_id=snapshot_run.id 才会被统计。
    [P0-2] DSA 已 publish 但快照不足 → action='failed'。
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    target_trade_date = trade_date

    # 创建已发布的 DSA run（P0-2: DSA published 前置检查）
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=target_trade_date,
    )
    dsa_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    # 创建中断的 orchestrator job_run（含 dsa_run_id）
    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=target_trade_date,
        dsa_run_id=dsa_run.id,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    # 创建 stuck running snapshot run（started_at 很久以前）
    snapshot_run = StockFeatureSnapshotRun(
        trade_date=target_trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=100,
        snapshot_count=0,
        failed_count=0,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    # [P0-1] 写入少量 snapshots（不足 95%），必须设置 source_run_id
    for i in range(3):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"REPAIR{i:03d}",
            name=f"修复测试{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=target_trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run.id,
            )
        )
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
        success_rate_threshold=0.95,
    )

    assert len(repaired) == 1, f"应修复 1 个 stuck run，实际 {repaired}"
    assert repaired[0]["snapshot_run_id"] == str(snapshot_run.id)
    assert repaired[0]["action"] == "failed"

    # 验证 DB 状态
    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_FAILED
    assert snapshot_run.published_at is None
    assert snapshot_run.metadata_ is not None
    assert snapshot_run.metadata_.get("reason") == "orchestrator_interrupted_or_lease_expired"


@pytest.mark.asyncio
async def test_repair_stale_snapshot_run_succeeds_when_enough_snapshots(
    db_session,
) -> None:
    """[Repair] orchestrator interrupted + snapshot_run running + 快照足够 → 标记 succeeded。

    [P0-1] snapshots 必须设置 source_run_id=snapshot_run.id 才会被统计。
    [P0-2] DSA 必须 published_at 非空才允许标记 snapshot succeeded。
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    expected_count = 100

    # [P0-2] 创建已发布的 DSA run
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    dsa_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        snapshot_count=expected_count,
        failed_count=0,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    # [P0-1] 写入 96 个 snapshots（>= 95%），必须设置 source_run_id
    for i in range(96):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"OK{i:03d}",
            name=f"足够{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run.id,
            )
        )
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
        success_rate_threshold=0.95,
    )

    assert len(repaired) == 1
    assert repaired[0]["action"] == "succeeded"

    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_SUCCEEDED
    assert snapshot_run.published_at is not None


@pytest.mark.asyncio
async def test_repair_skips_running_orchestrator(db_session) -> None:
    """[Repair] orchestrator 仍在 running 时不应修复 snapshot_run。"""
    trade_date = date(2026, 6, 25)

    await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
    )
    await db_session.flush()

    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=100,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
    )

    assert len(repaired) == 0
    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_RUNNING


@pytest.mark.asyncio
async def test_repair_skips_fresh_running_snapshot_run(db_session) -> None:
    """[Repair] 刚启动的 running snapshot_run 不应被修复（未超过 stale 阈值）。"""
    trade_date = date(2026, 6, 25)

    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=100,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(seconds=10),
        metadata_={"scope": "full"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
    )

    assert len(repaired) == 0
    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_RUNNING


@pytest.mark.asyncio
async def test_repair_clears_stuck_run_before_new_after_close(db_session) -> None:
    """[Repair] stuck running snapshot_run 不应阻塞新的 after_close 执行。

    验证 repair 后，execute_after_close_run 能正常创建新的 snapshot run 并完成。
    """
    trade_date = date(2026, 6, 25)

    # 先制造一个 stuck running snapshot run
    stuck_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=100,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(stuck_run)

    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    # 先 repair
    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
    )
    assert len(repaired) == 1

    # 再执行新的 after_close
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    new_job_run = await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date,
    )

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=new_job_run.id,
            trade_date=trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    assert new_job_run.status == "succeeded"

    # 验证新 snapshot run 被创建
    from sqlalchemy import select
    runs = (
        (await db_session.execute(
            select(StockFeatureSnapshotRun).where(
                StockFeatureSnapshotRun.trade_date == trade_date,
                StockFeatureSnapshotRun.run_type == RUN_TYPE_AFTER_CLOSE,
            )
        )).scalars().all()
    )
    assert len(runs) == 2
    succeeded_runs = [r for r in runs if r.status == STATUS_SUCCEEDED]
    assert len(succeeded_runs) == 1


@pytest.mark.asyncio
async def test_execute_calls_repair_at_start(db_session) -> None:
    """[Repair] execute_after_close_run 启动时会先调用 repair_stale_after_close_snapshot_runs。"""
    trade_date = date(2026, 6, 25)
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.QUEUED.value,
        trade_date=trade_date,
    )

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())
    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    repair_mock = AsyncMock(return_value=[])

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ), patch(
        "app.services.after_close_orchestrator.repair_stale_after_close_snapshot_runs",
        new=repair_mock,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    repair_mock.assert_awaited_once()
    assert job_run.status == "succeeded"


# =============================================================================
# C5: 事件生成在 publishing 成功之后（publishing 失败不生成事件）
# =============================================================================


@pytest.mark.asyncio
async def test_c5_publishing_failure_skips_event_generation(db_session) -> None:
    """C5: publishing 失败时不生成状态事件。

    场景：feature_snapshot 成功，但 publish_run 抛 RuntimeError。
    要求：generate_events_for_run 不被调用（事件只在发布成功后生成）。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    event_gen_call_count = 0

    async def _fake_generate_events(*args, **kwargs):
        nonlocal event_gen_call_count
        event_gen_call_count += 1
        return {"event_count": 0}

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "rollback", new=AsyncMock(return_value=None),
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(side_effect=RuntimeError("publishing 失败：质量门禁不通过")),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=_fake_generate_events,
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ):
        with pytest.raises(RuntimeError, match="publishing 失败"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=date(2026, 6, 25),
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # C5 核心断言：publishing 失败时 generate_events_for_run 不被调用
    assert event_gen_call_count == 0, (
        f"publishing 失败不应生成事件，但 generate_events_for_run 被调用了 {event_gen_call_count} 次"
    )


@pytest.mark.asyncio
async def test_c5_publishing_success_generates_events_once(db_session) -> None:
    """C5: publishing 成功后生成状态事件且仅生成一次。

    场景：feature_snapshot 成功 + publish_run 成功。
    要求：generate_events_for_run 被调用恰好 1 次。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    event_gen_call_count = 0

    async def _fake_generate_events(*args, **kwargs):
        nonlocal event_gen_call_count
        event_gen_call_count += 1
        return {"event_count": 1, "inserted_count": 1}

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=_fake_generate_events,
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # C5 核心断言：publishing 成功后 generate_events_for_run 恰好调用 1 次
    assert event_gen_call_count == 1, (
        f"publishing 成功后应生成事件恰好 1 次，实际调用 {event_gen_call_count} 次"
    )
    assert job_run.status == "succeeded"


@pytest.mark.asyncio
async def test_p0_publish_failure_marks_snapshot_run_failed_no_events(
    db_session,
) -> None:
    """[P0 Atomicity] DSA publish_run 失败时 snapshot run=failed、published_at=null、无事件。

    场景：
    1. feature_snapshot 成功（compute_for_trade_date 返回正常结果）
    2. DSA publish_run 抛异常
    要求：
    1. snapshot run 被标记为 failed
    2. snapshot run.published_at 为 None
    3. generate_events_for_run 不被调用
    4. orchestrator 最终状态为 failed
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    publish_exc = RuntimeError("DSA publish failed: quality gate rejection")
    event_gen_call_count = 0

    async def _fake_publish_run(*args, **kwargs):
        raise publish_exc

    async def _fake_generate_events(*args, **kwargs):
        nonlocal event_gen_call_count
        event_gen_call_count += 1
        return {"event_count": 0}

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "rollback", new=AsyncMock(return_value=None),
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=_fake_publish_run,
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=_fake_generate_events,
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ):
        with pytest.raises(RuntimeError, match="DSA publish failed"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=target_trade_date,
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # 验证 generate_events 未被调用
    assert event_gen_call_count == 0, (
        f"publish_run 失败时不应生成事件，但 generate_events 被调用了 {event_gen_call_count} 次"
    )

    # 验证 snapshot run 被标记为 failed，published_at=None
    from sqlalchemy import select
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == target_trade_date,
        StockFeatureSnapshotRun.run_type == "after_close",
    )
    result = await db_session.execute(stmt)
    runs = result.scalars().all()
    assert len(runs) >= 1, f"应创建至少 1 个 snapshot run，实际 {len(runs)}"
    run = runs[0]
    assert run.status == "failed", f"run.status 应为 failed，实际 {run.status}"
    assert run.published_at is None, "failed run 不应写 published_at"


@pytest.mark.asyncio
async def test_p0_publish_success_finalizes_snapshot_run_succeeded(
    db_session,
) -> None:
    """[P0 Atomicity] DSA publish_run 成功后 snapshot run 才标记 succeeded/published_at。

    场景：
    1. feature_snapshot 成功（compute_for_trade_date 返回正常结果）
    2. DSA publish_run 成功
    要求：
    1. snapshot run 被标记为 succeeded
    2. snapshot run.published_at 非空
    3. generate_events_for_run 被调用 1 次
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(db_session)

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    event_gen_call_count = 0

    async def _fake_generate_events(*args, **kwargs):
        nonlocal event_gen_call_count
        event_gen_call_count += 1
        return {"event_count": 0}

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=_fake_generate_events,
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=target_trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 generate_events 被调用 1 次
    assert event_gen_call_count == 1, (
        f"publishing 成功后应生成事件 1 次，实际调用 {event_gen_call_count} 次"
    )

    # 验证 snapshot run 被标记为 succeeded，published_at 非空
    from sqlalchemy import select
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == target_trade_date,
        StockFeatureSnapshotRun.run_type == "after_close",
    )
    result = await db_session.execute(stmt)
    runs = result.scalars().all()
    assert len(runs) >= 1, f"应创建至少 1 个 snapshot run，实际 {len(runs)}"
    run = runs[0]
    assert run.status == "succeeded", f"run.status 应为 succeeded，实际 {run.status}"
    assert run.published_at is not None, "succeeded run 应写 published_at"


# =============================================================================
# [P0-1/P0-2/P0-3] after_close 恢复 P0 逻辑测试
# =============================================================================


@pytest.mark.asyncio
async def test_repair_counts_by_source_run_id_only(db_session) -> None:
    """[P0-1] repair 统计实际行数必须限定 source_run_id == snapshot_run.id。

    场景：同 trade_date 存在两个 snapshot run（A 和 B），A 有 96 条 snapshots，
    B 有 0 条。repair B 时不得统计 A 的 snapshots。
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    expected_count = 100

    # 创建已发布的 DSA run
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    dsa_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    # snapshot run A（有 96 条 snapshots，不属于本测试的 repair 对象）
    snapshot_run_a = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_SUCCEEDED,
        expected_count=expected_count,
        snapshot_count=96,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run_a)
    await db_session.flush()

    # snapshot run B（stuck running，0 条 snapshots）—— 本测试的 repair 对象
    snapshot_run_b = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run_b)
    await db_session.flush()

    # 写入 96 条 snapshots，source_run_id 指向 A（不属于 B）
    for i in range(96):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"SIDA{i:03d}",
            name=f"sourceA{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run_a.id,
            )
        )
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
        success_rate_threshold=0.95,
    )

    # 只 repair B（A 是 succeeded 不在 repair 范围）
    repaired_b = [r for r in repaired if r["snapshot_run_id"] == str(snapshot_run_b.id)]
    assert len(repaired_b) == 1, f"应只 repair B，实际 {repaired}"
    # B 的 actual_count 必须为 0（不统计 A 的 snapshots）
    assert repaired_b[0]["actual_count"] == 0, (
        f"B 的 actual_count 必须为 0（source_run_id 隔离），"
        f"实际={repaired_b[0]['actual_count']}"
    )
    assert repaired_b[0]["action"] == "failed"


@pytest.mark.asyncio
async def test_repair_does_not_succeed_when_dsa_not_published(db_session) -> None:
    """[P0-2] DSA 未 publish 时不得把 running snapshot run 标记 succeeded。

    场景：DSA run status=completed 但 published_at=None，snapshot 行数足够。
    要求：action='failed'，不得写 published_at。
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    expected_count = 100

    # DSA run status=completed 但 published_at=None（未发布）
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    # 不设置 published_at（模拟未发布）
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    # 写入足够的 snapshots（96 >= 95%）
    for i in range(96):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"NPD{i:03d}",
            name=f"未发布{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run.id,
            )
        )
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
        success_rate_threshold=0.95,
    )

    assert len(repaired) == 1
    assert repaired[0]["action"] == "failed", (
        f"DSA 未 publish 时不得标记 succeeded，实际 action={repaired[0]['action']}"
    )
    assert "dsa_not_published" in repaired[0]["reason"]

    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_FAILED
    assert snapshot_run.published_at is None, "DSA 未 publish 时不得写 published_at"


@pytest.mark.asyncio
async def test_repair_returns_resume_pending_for_tracked_run(db_session) -> None:
    """[P0-2] metadata 中 feature_snapshot_run_id 匹配的 running snapshot run →

    返回 action='resume_pending'，保持 run 为 running（不标记 succeeded/failed）。
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    expected_count = 100

    # DSA 未 publish（模拟中断在 feature_snapshot 阶段）
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    await db_session.flush()

    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(minutes=30),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    # job_run metadata 中设置 feature_snapshot_run_id 匹配 snapshot_run.id
    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    # 在 metadata 中追加 feature_snapshot_run_id
    meta = json.loads(job_run.metadata_json)
    meta["feature_snapshot_run_id"] = str(snapshot_run.id)
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db_session.flush()

    # 写入少量 snapshots（source_run_id 匹配）
    for i in range(3):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"TRK{i:03d}",
            name=f"tracked{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run.id,
            )
        )
    await db_session.flush()

    repaired = await repair_stale_after_close_snapshot_runs(
        db_session,
        stale_threshold_seconds=60,
        success_rate_threshold=0.95,
    )

    assert len(repaired) == 1
    assert repaired[0]["action"] == "resume_pending", (
        f"tracked run 应返回 resume_pending，实际={repaired[0]['action']}"
    )
    assert repaired[0]["reason"] == "tracked_run_awaiting_resume"

    # 验证 snapshot run 保持 running（未被修改）
    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_RUNNING, "tracked run 应保持 running"
    assert snapshot_run.published_at is None


@pytest.mark.asyncio
async def test_resume_from_feature_snapshot_reads_actual_count(db_session) -> None:
    """[P0-3] 断点从 last_completed_step='feature_snapshot' 恢复发布时，

    snapshot_result 为 None，finish_snapshot_run 必须从数据库读取实际 snapshot 数量。

    场景：
    1. job_run 已完成 feature_snapshot（last_completed_step='feature_snapshot'）
    2. snapshot_run 有 50 条实际 snapshots 在 DB 中
    3. DSA publish_run 成功
    4. 要求：snapshot_run.snapshot_count=50（从 DB 读取），不是 0
    """
    from app.models.instrument import Instrument

    trade_date = date(2026, 6, 25)
    expected_count = 100

    # 创建已发布的 DSA run
    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    await db_session.flush()

    # 创建已存在的 snapshot run（running，有 50 条 snapshots）
    snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=expected_count,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(snapshot_run)
    await db_session.flush()

    # 写入 50 条 snapshots
    for i in range(50):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"FSR{i:03d}",
            name=f"resume{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        await db_session.flush()
        db_session.add(
            StockFeatureSnapshot(
                instrument_id=inst.id,
                trade_date=trade_date,
                primary_timeframe="1d",
                secondary_timeframe="15m",
                adj="qfq",
                schema_version=1,
                structural_payload={},
                temporal_payload={},
                summary_payload={"_source": "feature_snapshot"},
                degraded_reasons=[],
                source_run_id=snapshot_run.id,
            )
        )
    await db_session.flush()

    # 创建 job_run，last_completed_step='feature_snapshot'（跳过 snapshot 阶段）
    job_run = await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.PUBLISHING.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    meta = json.loads(job_run.metadata_json)
    meta["last_completed_step"] = "feature_snapshot"
    meta["feature_snapshot_run_id"] = str(snapshot_run.id)
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db_session.flush()

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())
    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get", new=_fake_get,
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=AsyncMock(return_value={"event_count": 0}),
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证 snapshot_run 被标记 succeeded，snapshot_count 从 DB 读取（50，不是 0）
    await db_session.refresh(snapshot_run)
    assert snapshot_run.status == STATUS_SUCCEEDED
    assert snapshot_run.published_at is not None
    assert snapshot_run.snapshot_count == 50, (
        f"snapshot_count 应从 DB 读取为 50，实际={snapshot_run.snapshot_count}"
    )
    assert snapshot_run.expected_count == expected_count, "expected_count 应保留"


@pytest.mark.asyncio
async def test_resume_skips_completed_steps_no_new_run(db_session) -> None:
    """[P0-4/5] queued 同一 job 恢复且不新建 run + 已完成阶段不重复执行。

    场景：last_completed_step='quality_gate' → 跳过 refreshing_daily、
    waiting_dsa_worker、quality_gate，只执行 feature_snapshot + publishing。
    不创建新的 SnapshotRun（复用 metadata 中的 feature_snapshot_run_id）。
    """
    trade_date = date(2026, 6, 25)

    dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed", trade_date=trade_date,
    )
    dsa_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    await db_session.flush()

    # 已存在的 snapshot run（running，将在 resume 中被复用）
    existing_snapshot_run = StockFeatureSnapshotRun(
        trade_date=trade_date,
        run_type=RUN_TYPE_AFTER_CLOSE,
        status=STATUS_RUNNING,
        expected_count=10,
        started_at=datetime.now(ZoneInfo("Asia/Shanghai")),
        metadata_={"scope": "full", "source": "after_close_orchestrator"},
    )
    db_session.add(existing_snapshot_run)
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session,
        status="running",
        orchestrator_status=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
        trade_date=trade_date,
        dsa_run_id=dsa_run.id,
    )
    meta = json.loads(job_run.metadata_json)
    meta["last_completed_step"] = "quality_gate"
    meta["feature_snapshot_run_id"] = str(existing_snapshot_run.id)
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db_session.flush()

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())
    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    refresh_mock = AsyncMock()
    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get", new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=refresh_mock,
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 5, "failed_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=AsyncMock(return_value={"event_count": 0}),
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # [P0-5] 已完成阶段不重复执行
    assert not refresh_mock.called, (
        "last_completed_step='quality_gate' 时不应调用 refresh_all_instruments"
    )

    # 验证 job_run 成功
    await db_session.refresh(job_run)
    assert job_run.status == "succeeded", (
        f"job_run 应为 succeeded，实际={job_run.status}"
    )


# ---------------------------------------------------------------------------
# [AC-04 / Phase 5A] 盘后编排 readiness 仅依赖日线，15m 缺失不得阻塞
# PRD30 AC-04：盘后编排 readiness 只检查目标交易日日线数据
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac04_daily_ready_15m_missing_allows_proceed() -> None:
    """[AC-04] 日线就绪 + 15m 缺失 → 允许进入下一阶段。

    场景：
    - refresh_all_instruments 返回 dsa_run_id=None（需 checking_coverage）
    - daily_coverage=0.95（>= 0.9，日线就绪）
    - compute_intraday_coverage 被 spy 监控（若被调用说明 15m 仍阻塞）

    预期：
    - 编排通过 checking_coverage 步骤
    - 创建 DSA run 并最终 succeeded
    - compute_intraday_coverage 不应被调用（15m 不再阻塞 after-close run）

    纯 mock 测试：不连接共享数据库或 Redis。
    """
    job_run_id = uuid.uuid4()
    dsa_run_id = uuid.uuid4()

    # 用 MagicMock 模拟 SchedulerJobRun（可变属性）
    job_run = MagicMock()
    job_run.id = job_run_id
    job_run.status = "running"
    job_run.metadata_json = json.dumps({
        "orchestrator_status": "queued",
        "trade_date": "2026-06-25",
    }, ensure_ascii=False)
    job_run.error_message = None
    job_run.finished_at = None

    # 用 MagicMock 模拟 DSA StrategyRun
    dsa_strategy_run = MagicMock()
    dsa_strategy_run.id = dsa_run_id
    dsa_strategy_run.status = "completed"

    # 模拟 AsyncSessionLocal：async with 返回 mock session
    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=lambda model, id, *a, **kw: {
        (SchedulerJobRun, job_run_id): job_run,
        (StrategyRun, dsa_run_id): dsa_strategy_run,
    }.get((model, id)))
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.execute = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.close = AsyncMock()

    class _FakeSessionContext:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    # dsa_run_id=None 触发 checking_coverage 步骤；daily_coverage=0.95 通过日线检查
    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = None  # 关键：触发 checking_coverage
    fake_batch_result.daily_coverage = 0.95

    fake_dsa_run = MagicMock()
    fake_dsa_run.id = dsa_run_id

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    # 关键：用 spy 监控 compute_intraday_coverage 是否被调用
    intraday_spy = AsyncMock()
    intraday_spy.return_value = {"ready": False}  # 模拟 15m 缺失

    fake_snapshot_run = MagicMock()
    fake_snapshot_run.id = uuid.uuid4()

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch.object(
        StrategyBatchService, "create_batch_run",
        new=AsyncMock(return_value=fake_dsa_run),
    ), patch(
        "app.services.after_close_orchestrator._poll_dsa_run_status",
        new=AsyncMock(return_value="completed"),
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.bars_coverage_service.BarsCoverageService.compute_intraday_coverage",
        new=intraday_spy,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ), patch(
        "app.services.auction_anchor_service.generate_and_publish_auction_anchors",
        new=AsyncMock(return_value={"publication_id": uuid.uuid4(), "chip_count": 0, "composite_count": 0}),
    ), patch(
        "app.services.state_event_service.generate_events_for_run",
        new=AsyncMock(return_value={"event_count": 0}),
    ), patch(
        "app.services.state_event_service.cleanup_old_events",
        new=AsyncMock(return_value={"deleted_count": 0}),
    ), patch(
        "app.services.after_close_orchestrator.create_snapshot_run",
        new=AsyncMock(return_value=fake_snapshot_run),
    ), patch(
        "app.services.after_close_orchestrator.finish_snapshot_run",
        new=AsyncMock(),
    ), patch(
        "app.services.after_close_orchestrator.repair_stale_after_close_snapshot_runs",
        new=AsyncMock(return_value=0),
    ), patch(
        "app.services.factor_publication_service.get_publication",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # [AC-04] 关键断言：15m intraday coverage 不应被调用
    assert not intraday_spy.called, (
        "[AC-04] after-close run 不应调用 compute_intraday_coverage，"
        "15m 缺失不得阻塞盘后编排"
    )

    # 编排应成功（日线就绪允许进入下一阶段）
    assert job_run.status == "succeeded", (
        f"[AC-04] 日线就绪 + 15m 缺失时应 succeeded，实际={job_run.status}"
    )


@pytest.mark.asyncio
async def test_ac04_daily_missing_blocks() -> None:
    """[AC-04] 日线未就绪 → 阻塞 after-close run。

    场景：
    - refresh_all_instruments 返回 dsa_run_id=None（需 checking_coverage）
    - daily_coverage=0.5（< 0.9，日线未就绪）

    预期：
    - 编排在 checking_coverage 步骤失败
    - job_run.status == "failed"
    - error_message 含"日线覆盖率"
    - 不创建 DSA run

    纯 mock 测试：不连接共享数据库或 Redis。
    """
    job_run_id = uuid.uuid4()

    job_run = MagicMock()
    job_run.id = job_run_id
    job_run.status = "running"
    job_run.metadata_json = json.dumps({
        "orchestrator_status": "queued",
        "trade_date": "2026-06-25",
    }, ensure_ascii=False)
    job_run.error_message = None
    job_run.finished_at = None

    mock_session = MagicMock()
    mock_session.get = AsyncMock(side_effect=lambda model, id, *a, **kw: job_run if model is SchedulerJobRun and id == job_run_id else None)
    mock_session.flush = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.add = MagicMock()
    mock_session.execute = AsyncMock()
    mock_session.rollback = AsyncMock()
    mock_session.close = AsyncMock()

    class _FakeSessionContext:
        async def __aenter__(self):
            return mock_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    # dsa_run_id=None 触发 checking_coverage；daily_coverage=0.5 不达标
    fake_batch_result = BatchResult(total=100, succeeded=50)
    fake_batch_result.dsa_run_id = None
    fake_batch_result.daily_coverage = 0.5

    create_batch_run_spy = AsyncMock()

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch.object(
        StrategyBatchService, "create_batch_run",
        new=create_batch_run_spy,
    ), patch(
        "app.services.after_close_orchestrator._job_run_heartbeat_loop",
        new=AsyncMock(),
    ):
        await execute_after_close_run(
            job_run_id=job_run_id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # [AC-04] 日线未就绪必须阻塞
    assert job_run.status == "failed", (
        f"[AC-04] 日线未就绪时应 failed，实际={job_run.status}"
    )
    assert job_run.error_message is not None and "日线覆盖率" in job_run.error_message, (
        f"[AC-04] error_message 应含'日线覆盖率'，实际={job_run.error_message}"
    )
    # 不应创建 DSA run
    assert not create_batch_run_spy.called, (
        "[AC-04] 日线未就绪时不应创建 DSA run"
    )


def test_ac04_no_intraday_readiness_in_after_close_source() -> None:
    """[AC-04] 静态核验：after_close_orchestrator 源码不再调用 15m intraday readiness。

    防止回归：确保 after-close 链路不再 import 或调用 compute_intraday_coverage。
    其他模块（如 system_overview）仍可使用 intraday readiness，本测试只约束 after-close。
    使用 AST 检查真实调用，避免注释/文档字符串误报。
    """
    import ast
    import inspect

    from app import worker as worker_mod
    from app.services import after_close_orchestrator as acm

    # AST 检查：after_close_orchestrator 不得调用 compute_intraday_coverage
    source = inspect.getsource(acm)
    tree = ast.parse(source)

    intraday_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            # 检测 func.attr 形式（如 BarsCoverageService.compute_intraday_coverage）
            if isinstance(node.func, ast.Attribute):
                if node.func.attr == "compute_intraday_coverage":
                    intraday_calls.append(node.lineno)
            # 检测直接名称调用
            if isinstance(node.func, ast.Name):
                if node.func.id == "compute_intraday_coverage":
                    intraday_calls.append(node.lineno)

    assert not intraday_calls, (
        f"[AC-04] after_close_orchestrator 不得调用 compute_intraday_coverage，"
        f"发现调用行号: {intraday_calls}，15m readiness 已从 after-close 链路移除"
    )

    # AST 检查：worker 入口必须复用 execute_after_close_run
    worker_source = inspect.getsource(worker_mod)
    worker_tree = ast.parse(worker_source)
    worker_calls_execute = False
    for node in ast.walk(worker_tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id == "execute_after_close_run":
                worker_calls_execute = True
                break
            if isinstance(node.func, ast.Attribute) and node.func.attr == "execute_after_close_run":
                worker_calls_execute = True
                break

    assert worker_calls_execute, (
        "[AC-04] Worker 必须复用 execute_after_close_run 作为唯一 readiness 入口"
    )


@pytest.mark.asyncio
async def test_execute_run_called_after_mfcs_transitions_dsa_to_completed(db_session) -> None:
    """[CHANGE-20260728-007] 验证 running→completed 闭环：MFCS 后调用 execute_run。

    根因：Phase 5 收敛后 orchestrator inline claim DSA run（status=running），
    调用 compute_for_trade_date（只写 snapshot），但从不调用 execute_run
    写入 StrategyResult 或推进 DSA run 状态。publish_run 因 run 仍为 running 而失败。

    修复：MFCS 完成后显式调用 batch_service.execute_run 写入 StrategyResult
    并将 DSA run 推进到 completed/failed。

    本测试验证：
    1. compute_for_trade_date 完成后 execute_run 被调用；
    2. publish_run 在 execute_run 之后被调用（run 已 completed）。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="running")
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=dsa_run.id,
    )

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    fake_published_run = MagicMock()
    fake_published_run.published_at = datetime.now(ZoneInfo("Asia/Shanghai"))

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    execute_run_spy = AsyncMock()
    publish_run_spy = AsyncMock(return_value=fake_published_run)

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch.object(
        StrategyBatchService, "execute_run",
        new=execute_run_spy,
    ), patch.object(
        StrategyBatchService, "_check_quality_gates",
        new=AsyncMock(return_value=True),
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=publish_run_spy,
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ), patch(
        "app.services.factor_publication_service.compute_coverage",
        new=AsyncMock(return_value={
            "coverage": 1.0, "succeeded": 1, "expected": 1,
            "failed": 0, "pending": 0, "running": 0, "skipped": 0,
        }),
    ), patch(
        "app.services.factor_publication_service.publish_stock_core",
        new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())),
    ), patch(
        "app.services.board_analysis_service.compute_all_boards",
        new=AsyncMock(return_value={"published": 1, "failed": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=target_trade_date,
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 1. execute_run 必须被调用（修复 running→completed 闭环）
    assert execute_run_spy.called, (
        "MFCS 完成后必须调用 batch_service.execute_run 写入 StrategyResult "
        "并推进 DSA run 状态从 running 到 completed"
    )
    # 传入的 run_id 应为 dsa_run_id
    call_args = execute_run_spy.call_args
    assert call_args.args[1] == dsa_run.id, (
        f"execute_run 应传入 dsa_run_id={dsa_run.id}，实际: {call_args.args[1]}"
    )

    # 2. publish_run 必须在 execute_run 之后被调用
    assert publish_run_spy.called, (
        "execute_run 完成后应调用 publish_run 发布结果"
    )


@pytest.mark.asyncio
async def test_execute_run_failure_marks_dsa_failed_skips_publish(db_session) -> None:
    """[CHANGE-20260728-007] 验证 execute_run 失败时 DSA run 标记 failed，不调用 publish_run。

    要求：
    1. execute_run 抛异常时，DSA run 状态推进到 failed（不得停留 running）；
    2. publish_run 不得被调用（不得在 publish 前伪造 completed）。
    """
    dsa_run, _ = await _create_dsa_strategy_run(db_session, status="running")
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=dsa_run.id,
    )

    class _FakeSessionContext:
        async def __aenter__(self):
            return db_session
        async def __aexit__(self, *args):
            return False

    fake_session_local = MagicMock(return_value=_FakeSessionContext())

    fake_batch_result = BatchResult(total=100, succeeded=95)
    fake_batch_result.dsa_run_id = dsa_run.id
    fake_batch_result.daily_covered = 95
    fake_batch_result.daily_total = 100
    fake_batch_result.daily_coverage = 0.95

    original_get = db_session.get
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    async def _fake_get(model, id, *args, **kwargs):
        if model is SchedulerJobRun and id == job_run.id:
            return job_run
        if model is StrategyRun and id == dsa_run.id:
            return dsa_run
        if model is StockFeatureSnapshotRun:
            return await original_get(model, id, *args, **kwargs)
        return None

    # execute_run 抛异常
    execute_run_spy = AsyncMock(side_effect=RuntimeError("DSA execute_run 模拟失败"))
    publish_run_spy = AsyncMock()

    target_trade_date = date(2026, 6, 25)

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=_fake_get,
    ), patch.object(
        BarsSchedulerService, "refresh_all_instruments",
        new=AsyncMock(return_value=fake_batch_result),
    ), patch.object(
        StrategyBatchService, "execute_run",
        new=execute_run_spy,
    ), patch.object(
        StrategyBatchService, "publish_run",
        new=publish_run_spy,
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.feature_snapshot_service.compute_review_core_with_run_items",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ):
        with pytest.raises(RuntimeError, match="DSA execute_run 模拟失败"):
            await execute_after_close_run(
                job_run_id=job_run.id,
                trade_date=target_trade_date,
                dsa_poll_interval=0,
                dsa_poll_timeout=1,
            )

    # 1. execute_run 被调用但失败
    assert execute_run_spy.called, "execute_run 应被调用"

    # 2. publish_run 不得被调用（不得在 publish 前伪造 completed）
    assert not publish_run_spy.called, (
        "execute_run 失败后不得调用 publish_run（不得在 publish 前伪造 completed）"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
