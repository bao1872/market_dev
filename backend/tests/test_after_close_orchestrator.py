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
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
    execute_after_close_run,
    get_after_close_run_status,
    retry_after_close_run,
)
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.job_run_event_service import append_event
from app.services.strategy_batch_service import StrategyBatchService


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

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
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
        new=AsyncMock(return_value=fake_published_run),
    ), patch(
        "app.services.after_close_orchestrator.get_active_a_share_instruments",
        new=AsyncMock(return_value=[uuid.uuid4()]),
    ), patch(
        "app.services.after_close_orchestrator.compute_for_trade_date",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证事件序列：应包含 refreshing_daily → waiting_dsa_worker → quality_gate
    #   → feature_snapshot → publishing → succeeded
    from app.services.job_run_event_service import list_events
    events = await list_events(db_session, job_run.id, limit=20)
    steps = [e.step for e in events]

    assert AfterCloseRunStatus.REFRESHING_DAILY.value in steps, f"缺少 refreshing_daily 事件: {steps}"
    assert AfterCloseRunStatus.WAITING_DSA_WORKER.value in steps, f"缺少 waiting_dsa_worker 事件: {steps}"
    assert AfterCloseRunStatus.QUALITY_GATE.value in steps, f"缺少 quality_gate 事件: {steps}"
    assert AfterCloseRunStatus.FEATURE_SNAPSHOT.value in steps, f"缺少 feature_snapshot 事件: {steps}"
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
        "app.services.after_close_orchestrator.compute_for_trade_date",
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
        "app.services.after_close_orchestrator.compute_for_trade_date",
        new=AsyncMock(return_value={"snapshot_count": 1, "failed_count": 0}),
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
        "app.services.after_close_orchestrator.compute_for_trade_date",
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

    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        new=fake_session_local,
    ), patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch.object(
        db_session, "get",
        new=AsyncMock(side_effect=lambda model, id: {
            (SchedulerJobRun, job_run.id): job_run,
            (StrategyRun, dsa_run.id): dsa_run,
        }.get((model, id))),
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
    ):
        await execute_after_close_run(
            job_run_id=job_run.id,
            trade_date=date(2026, 6, 25),
            dsa_poll_interval=0,
            dsa_poll_timeout=1,
        )

    # 验证心跳任务被启动 1 次
    assert len(heartbeat_calls) == 1, (
        f"应启动 1 次后台心跳任务，实际 {len(heartbeat_calls)} 次"
    )
    # 验证 refresh_all_instruments 被调用（事件已 set）
    assert refresh_started.is_set(), "refresh_all_instruments 应被调用"


@pytest.mark.asyncio
async def test_update_orchestrator_status_preserves_mode_field(db_session) -> None:
    """[TDD-RED] - 验证 _update_orchestrator_status 保留 mode 字段。

    场景：dsa_only 任务已有 metadata.mode='dsa_only'，调用 _update_orchestrator_status
    切换状态后，mode 字段应保留（与 last_completed_step 同等对待）。

    根因：原实现 _update_orchestrator_status 只保留 last_completed_step，
    每次 state 切换都会丢失 mode 字段，导致 worker 无法识别 dsa_only 模式，
    重新走了 refreshing_daily 步骤。

    given: job_run.metadata_json 含 mode='dsa_only' + last_completed_step='daily_ready'
    when: 调用 _update_orchestrator_status(status=QUEUED)
    then: 新 metadata_json 仍含 mode='dsa_only' + last_completed_step='daily_ready'
    """
    from app.services.after_close_orchestrator import (
        AfterCloseRunStatus,
        _update_orchestrator_status,
    )

    # 准备：创建已有 mode=dsa_only 的 job_run
    job_run = await _create_after_close_job_run(
        db_session,
        status="interrupted",
        orchestrator_status="interrupted",
    )
    # 手动设置含 mode 字段的 metadata
    job_run.metadata_json = json.dumps({
        "orchestrator_status": "interrupted",
        "trade_date": "2026-06-25",
        "mode": "dsa_only",
        "last_completed_step": "daily_ready",
    }, ensure_ascii=False)
    await db_session.flush()

    # 执行：调用 _update_orchestrator_status 切换到 QUEUED（模拟 resume）
    await _update_orchestrator_status(
        db=db_session,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message="[resume] 从断点恢复",
    )
    await db_session.flush()

    # 验证：mode 字段应保留
    meta = json.loads(job_run.metadata_json)
    assert meta["orchestrator_status"] == "queued", (
        f"orchestrator_status 应为 queued，实际 {meta.get('orchestrator_status')}"
    )
    assert meta.get("mode") == "dsa_only", (
        f"mode 字段应保留为 'dsa_only'，实际 metadata={meta}（根因：_update_orchestrator_status 丢失 mode 字段）"
    )
    assert meta.get("last_completed_step") == "daily_ready", (
        f"last_completed_step 应保留，实际 {meta.get('last_completed_step')}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
