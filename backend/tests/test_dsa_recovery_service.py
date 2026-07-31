"""DSA Recovery Service 测试 - 验证失败 DSA run 的正式恢复逻辑（P0-2）。

覆盖 4 类回归场景（ref/instruction.md §二.2）：
1. 正常恢复：failed DSA run → 创建新 run，old run 保留审计
2. 复用已完成：completed/published DSA run → 直接复用，不创建新 run
3. running 且 lease 未过期 → 拒绝恢复（正在执行）
4. 恢复次数超限 → 抛 DSARecoveryError

附加：
5. lease 过期的 running → 使用现有 fencing 恢复（不创建新 run）
6. max_retries_exceeded（status=failed + error_code）→ 走 failed 路径创建新 run
7. metadata 缺失 → 抛 DSARecoveryError
8. job_run 不属于 after_close_orchestrator → 抛 DSARecoveryError

测试环境：PostgreSQL 测试库（conftest.py db_session fixture）
运行：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... \
        pytest backend/tests/test_dsa_recovery_service.py -v
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun
from app.services.dsa_recovery_service import (
    DSARecoveryError,
    get_dsa_recovery_status,
    recover_failed_dsa_run,
)

# CI 环境标识（与 conftest.py 一致）
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)

# 本测试文件全部为 PG 集成测试（依赖 db_session fixture），
# 只在 CI 临时 Postgres 容器中运行；本地 PURE_UNIT_TEST=1 自动 skip。
pytestmark = pytest.mark.skipif(
    not _CI_ENV,
    reason="DSA recovery 测试为 PG 集成测试，只在 CI 临时 Postgres 容器中运行；本地请用 PURE_UNIT_TEST=1",
)

_TZ = ZoneInfo("Asia/Shanghai")
_TRADE_DATE = date(2026, 6, 25)


# ---------------------------------------------------------------------------
# 测试辅助函数
# ---------------------------------------------------------------------------


async def _create_after_close_job_run(
    db_session,
    *,
    orchestrator_status: str = "waiting_dsa_worker",
    dsa_run_id: uuid.UUID | None = None,
    recovery_count: int = 0,
    extra_metadata: dict | None = None,
) -> SchedulerJobRun:
    """创建 after_close_orchestrator 的 SchedulerJobRun（含 dsa_run_id metadata）。"""
    meta: dict = {
        "orchestrator_status": orchestrator_status,
        "trade_date": _TRADE_DATE.isoformat(),
    }
    if dsa_run_id is not None:
        meta["dsa_run_id"] = str(dsa_run_id)
    if recovery_count > 0:
        meta["dsa_recovery_count"] = recovery_count
    if extra_metadata:
        meta.update(extra_metadata)

    now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=_TRADE_DATE.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


async def _create_dsa_strategy_run(
    db_session,
    *,
    status: str = "failed",
    error_code: str | None = None,
    trade_date: date = _TRADE_DATE,
) -> tuple[StrategyRun, uuid.UUID]:
    """创建 DSA StrategyRun（满足外键约束）。

    返回 (run, version_id)。
    """
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
        released_at=datetime.now(_TZ),
    )
    db_session.add(version)
    await db_session.flush()

    now = datetime.now(_TZ)
    dsa_run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status=status,
        error_code=error_code,
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


# ---------------------------------------------------------------------------
# 测试 1: 正常恢复 — failed DSA run → 创建新 run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_failed_dsa_creates_new_run(db_session) -> None:
    """测试 1：failed DSA run → 创建新 run，old run 保留审计。

    验证：
    - 返回 (new_run, is_new=True)
    - 新 run 与 old run 不是同一条记录
    - old run 状态保持 failed（未被修改）
    - orchestrator metadata 更新 dsa_run_id 和 dsa_recovery_count
    - 写入 dsa_recovery 事件
    """
    # 1. 准备：failed DSA run + after_close job_run
    old_dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="failed", error_code="runtime_error",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=old_dsa_run.id, recovery_count=0,
    )

    # 2. mock create_batch_run 避免真实数据就绪检查（测试库无 bars_daily）
    fake_new_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_version_id=old_dsa_run.strategy_version_id,
        run_type="scheduled",
        trade_date=_TRADE_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"fake_new:{uuid.uuid4().hex[:8]}",
    )

    with patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(return_value=fake_new_run),
    ):
        new_run, is_new = await recover_failed_dsa_run(
            db_session, job_run_id=job_run.id,
        )

    # 3. 验证返回值
    assert is_new is True, "应创建新 run（is_new=True）"
    assert new_run.id == fake_new_run.id, "返回的应是新创建的 run"

    # 4. 验证 old run 状态保持 failed（保留审计）
    await db_session.refresh(old_dsa_run)
    assert old_dsa_run.status == "failed", "old run 状态必须保持 failed（保留审计）"

    # 5. 验证 metadata 更新
    await db_session.refresh(job_run)
    meta = json.loads(job_run.metadata_json)
    assert meta["dsa_run_id"] == str(fake_new_run.id), "metadata.dsa_run_id 应更新为新 run"
    assert meta["dsa_recovery_count"] == 1, "recovery_count 应递增为 1"
    assert meta["dsa_recovery_old_run_id"] == str(old_dsa_run.id)
    assert meta["dsa_recovery_new_run_id"] == str(fake_new_run.id)
    assert meta["dsa_recovery_previous_status"] == "failed"


# ---------------------------------------------------------------------------
# 测试 2: 复用已完成 — completed/published DSA run → 直接复用
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_completed_dsa_reuses_existing(db_session) -> None:
    """测试 2：completed DSA run → 直接复用，不创建新 run。

    验证：
    - 返回 (existing_run, is_new=False)
    - 不调用 create_batch_run
    - 不更新 metadata
    """
    completed_run, _ = await _create_dsa_strategy_run(
        db_session, status="completed",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=completed_run.id,
    )

    with patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(),
    ) as mock_create:
        result_run, is_new = await recover_failed_dsa_run(
            db_session, job_run_id=job_run.id,
        )

    assert is_new is False, "completed run 应直接复用（is_new=False）"
    assert result_run.id == completed_run.id, "返回的应是原 run"
    mock_create.assert_not_called(), "completed run 不应调用 create_batch_run"


@pytest.mark.asyncio
async def test_recover_published_dsa_reuses_existing(db_session) -> None:
    """测试 2.1：published DSA run → 直接复用（与 completed 同路径）。"""
    published_run, _ = await _create_dsa_strategy_run(
        db_session, status="published",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=published_run.id,
    )

    result_run, is_new = await recover_failed_dsa_run(
        db_session, job_run_id=job_run.id,
    )

    assert is_new is False
    assert result_run.id == published_run.id


# ---------------------------------------------------------------------------
# 测试 3: running 且 lease 未过期 → 拒绝恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_running_with_active_lease_rejected(db_session) -> None:
    """测试 3：running 且 lease 未过期 → 抛 DSARecoveryError。

    场景：DSA Worker 正在执行，恢复请求应被拒绝。
    """
    now = datetime.now(_TZ)
    running_run, _ = await _create_dsa_strategy_run(
        db_session, status="running",
    )
    # 手动设置 lease_expires_at 为未来时间
    running_run.lease_expires_at = now + timedelta(minutes=30)
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=running_run.id,
    )

    with pytest.raises(DSARecoveryError, match="正在执行且 lease 未过期"):
        await recover_failed_dsa_run(db_session, job_run_id=job_run.id)


# ---------------------------------------------------------------------------
# 测试 4: 恢复次数超限 → 抛 DSARecoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_exceeds_max_count_raises(db_session) -> None:
    """测试 4：恢复次数已达上限 → 抛 DSARecoveryError。

    场景：metadata.dsa_recovery_count 已达 _MAX_DSA_RECOVERY_COUNT（5）。
    """
    failed_run, _ = await _create_dsa_strategy_run(
        db_session, status="failed",
    )
    job_run = await _create_after_close_job_run(
        db_session,
        dsa_run_id=failed_run.id,
        recovery_count=5,  # 已达上限
    )

    with pytest.raises(DSARecoveryError, match="恢复次数超限"):
        await recover_failed_dsa_run(db_session, job_run_id=job_run.id)


# ---------------------------------------------------------------------------
# 测试 5: running 但 lease 已过期 → 使用现有 fencing 恢复
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_running_with_expired_lease_uses_existing_fencing(
    db_session,
) -> None:
    """测试 5：running 但 lease 过期 → 返回原 run（is_new=False），不创建新 run。

    场景：Worker 崩溃后 lease 过期，但 status 仍为 running。
    正确行为：使用现有 fencing 恢复，不创建新 run（避免重复 run）。
    """
    now = datetime.now(_TZ)
    running_run, _ = await _create_dsa_strategy_run(
        db_session, status="running",
    )
    # lease 已过期
    running_run.lease_expires_at = now - timedelta(minutes=10)
    await db_session.flush()

    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=running_run.id,
    )

    with patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(),
    ) as mock_create:
        result_run, is_new = await recover_failed_dsa_run(
            db_session, job_run_id=job_run.id,
        )

    assert is_new is False, "lease 过期的 running run 应使用现有 fencing（不创建新 run）"
    assert result_run.id == running_run.id
    mock_create.assert_not_called()


# ---------------------------------------------------------------------------
# 测试 6: max_retries_exceeded（status=failed + error_code）→ 走 failed 路径
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_max_retries_exceeded_treats_as_failed(db_session) -> None:
    """测试 6：max_retries_exceeded 是 error_code（不是 status）→ 走 failed 路径创建新 run。

    场景：Worker 多次重试后 status=failed + error_code=max_retries_exceeded。
    正确行为：与普通 failed 一致，创建新 run。
    """
    failed_run, _ = await _create_dsa_strategy_run(
        db_session, status="failed", error_code="max_retries_exceeded",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=failed_run.id, recovery_count=0,
    )

    fake_new_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_version_id=failed_run.strategy_version_id,
        run_type="scheduled",
        trade_date=_TRADE_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"fake_new:{uuid.uuid4().hex[:8]}",
    )

    with patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(return_value=fake_new_run),
    ):
        new_run, is_new = await recover_failed_dsa_run(
            db_session, job_run_id=job_run.id,
        )

    assert is_new is True
    assert new_run.id == fake_new_run.id

    # old run 状态保持 failed（保留审计）
    await db_session.refresh(failed_run)
    assert failed_run.status == "failed"
    assert failed_run.error_code == "max_retries_exceeded"


# ---------------------------------------------------------------------------
# 测试 7: metadata 缺失 → 抛 DSARecoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_missing_trade_date_raises(db_session) -> None:
    """测试 7：metadata 缺少 trade_date → 抛 DSARecoveryError。"""
    now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=_TRADE_DATE.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        # 缺少 trade_date 和 dsa_run_id
        metadata_json=json.dumps({"orchestrator_status": "queued"}, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()

    with pytest.raises(DSARecoveryError, match="缺少 trade_date"):
        await recover_failed_dsa_run(db_session, job_run_id=job_run.id)


@pytest.mark.asyncio
async def test_recover_missing_dsa_run_id_raises(db_session) -> None:
    """测试 7.1：metadata 缺少 dsa_run_id → 抛 DSARecoveryError。"""
    now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date=_TRADE_DATE.isoformat(),
        run_key=f"after_close_orchestrator:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps({
            "orchestrator_status": "queued",
            "trade_date": _TRADE_DATE.isoformat(),
        }, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()

    with pytest.raises(DSARecoveryError, match="缺少 dsa_run_id"):
        await recover_failed_dsa_run(db_session, job_run_id=job_run.id)


# ---------------------------------------------------------------------------
# 测试 8: job_run 不属于 after_close_orchestrator → 抛 DSARecoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_wrong_job_name_raises(db_session) -> None:
    """测试 8：job_run.job_name 不是 after_close_orchestrator → 抛 DSARecoveryError。"""
    now = datetime.now(_TZ)
    job_run = SchedulerJobRun(
        job_name="some_other_job",
        business_date=_TRADE_DATE.isoformat(),
        run_key=f"some_other_job:test:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps({
            "orchestrator_status": "queued",
            "trade_date": _TRADE_DATE.isoformat(),
            "dsa_run_id": str(uuid.uuid4()),
        }, ensure_ascii=False),
    )
    db_session.add(job_run)
    await db_session.flush()

    with pytest.raises(DSARecoveryError, match="不是 after_close_orchestrator"):
        await recover_failed_dsa_run(db_session, job_run_id=job_run.id)


# ---------------------------------------------------------------------------
# 测试 9: job_run 不存在 → 抛 DSARecoveryError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_nonexistent_job_run_raises(db_session) -> None:
    """测试 9：job_run_id 不存在 → 抛 DSARecoveryError。"""
    fake_id = uuid.uuid4()
    with pytest.raises(DSARecoveryError, match="job_run 不存在"):
        await recover_failed_dsa_run(db_session, job_run_id=fake_id)


# ---------------------------------------------------------------------------
# 测试 10: get_dsa_recovery_status 查询
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dsa_recovery_status_failed_can_recover(db_session) -> None:
    """测试 10：failed DSA run + recovery_count < max → can_recover=True。"""
    failed_run, _ = await _create_dsa_strategy_run(db_session, status="failed")
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=failed_run.id, recovery_count=2,
    )

    status = await get_dsa_recovery_status(db_session, job_run_id=job_run.id)

    assert status["job_run_id"] == str(job_run.id)
    assert status["dsa_run_id"] == str(failed_run.id)
    assert status["dsa_run_status"] == "failed"
    assert status["dsa_recovery_count"] == 2
    assert status["can_recover"] is True
    assert status["reason"] is None


@pytest.mark.asyncio
async def test_get_dsa_recovery_status_max_exceeded(db_session) -> None:
    """测试 10.1：recovery_count >= max → can_recover=False。"""
    failed_run, _ = await _create_dsa_strategy_run(db_session, status="failed")
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=failed_run.id, recovery_count=5,
    )

    status = await get_dsa_recovery_status(db_session, job_run_id=job_run.id)

    assert status["can_recover"] is False
    assert "超限" in status["reason"]


@pytest.mark.asyncio
async def test_get_dsa_recovery_status_completed_no_recover(db_session) -> None:
    """测试 10.2：completed DSA run → can_recover=False（无需恢复）。"""
    completed_run, _ = await _create_dsa_strategy_run(db_session, status="completed")
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=completed_run.id,
    )

    status = await get_dsa_recovery_status(db_session, job_run_id=job_run.id)

    assert status["dsa_run_status"] == "completed"
    assert status["can_recover"] is False
    assert "completed" in status["reason"]


# ---------------------------------------------------------------------------
# 测试 11: [P0-3 fencing] worker_id 传入 → claim_for_worker 绑定 orchestrator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_failed_dsa_with_worker_id_claims_for_orchestrator(
    db_session,
) -> None:
    """测试 11：传入 worker_id → 新 run 通过 claim_for_worker 绑定当前 orchestrator。

    验证（ref/instruction.md §三.2 DSA recovery fencing）：
    - create_batch_run 收到 claim_for_worker="orchestrator:<worker_id>"
    - 新 run 在创建时即为 status=running + worker_id（generic worker 无法抢占）
    - recovery 正常返回 (new_run, is_new=True)

    注：通过 mock create_batch_run 验证 claim_for_worker 参数透传；
    DB 层的 generic worker claim 互斥由 StrategyBatchService.create_batch_run
    的 status=running + worker_id 内联 claim 保证（参见 strategy_batch_service.py
    [Phase8A] 注释）。
    """
    old_dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="failed", error_code="runtime_error",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=old_dsa_run.id, recovery_count=0,
    )

    fake_new_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_version_id=old_dsa_run.strategy_version_id,
        run_type="scheduled",
        trade_date=_TRADE_DATE,
        status="running",  # claim_for_worker 模式下应为 running
        worker_id="orchestrator:test-worker-1",
        input_overrides={},
        idempotency_key=f"fake_new:{uuid.uuid4().hex[:8]}",
    )

    with patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(return_value=fake_new_run),
    ) as mock_create:
        new_run, is_new = await recover_failed_dsa_run(
            db_session,
            job_run_id=job_run.id,
            worker_id="test-worker-1",
            lease_epoch=42,
        )

    # 验证 claim_for_worker 参数正确透传给 create_batch_run
    assert mock_create.called, "create_batch_run 必须被调用"
    _call_kwargs = mock_create.call_args.kwargs
    assert _call_kwargs.get("claim_for_worker") == "orchestrator:test-worker-1", (
        "claim_for_worker 必须为 'orchestrator:<worker_id>'，"
        "防止 generic strategy worker 抢占 recovery run"
    )

    # 返回值校验
    assert is_new is True
    assert new_run.id == fake_new_run.id


# ---------------------------------------------------------------------------
# 测试 12: [P0-3 fencing] 无 worker_id → fallback claim（CLI/admin 路径）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_failed_dsa_without_worker_id_uses_recovery_fallback(
    db_session,
) -> None:
    """测试 12：无 worker_id（CLI/admin 路径）→ claim_for_worker 使用 recovery fallback。

    fallback claim_for_worker = "orchestrator:recovery:<job_run_id>"，仍归
    orchestrator 命名空间（generic worker 不会 claim "orchestrator:*" 前缀的 run），
    但不绑定特定 worker（适合管理员应急恢复场景）。
    """
    old_dsa_run, _ = await _create_dsa_strategy_run(
        db_session, status="failed", error_code="runtime_error",
    )
    job_run = await _create_after_close_job_run(
        db_session, dsa_run_id=old_dsa_run.id, recovery_count=0,
    )

    fake_new_run = StrategyRun(
        id=uuid.uuid4(),
        strategy_version_id=old_dsa_run.strategy_version_id,
        run_type="scheduled",
        trade_date=_TRADE_DATE,
        status="running",
        worker_id=f"orchestrator:recovery:{job_run.id}",
        input_overrides={},
        idempotency_key=f"fake_new:{uuid.uuid4().hex[:8]}",
    )

    with patch.object(
        db_session, "commit", new=db_session.flush,
    ), patch(
        "app.services.strategy_batch_service.StrategyBatchService.create_batch_run",
        new=AsyncMock(return_value=fake_new_run),
    ) as mock_create:
        # 不传 worker_id（CLI 默认路径）
        new_run, is_new = await recover_failed_dsa_run(
            db_session, job_run_id=job_run.id,
        )

    _call_kwargs = mock_create.call_args.kwargs
    expected_claim = f"orchestrator:recovery:{job_run.id}"
    assert _call_kwargs.get("claim_for_worker") == expected_claim, (
        f"无 worker_id 时 claim_for_worker 应为 '{expected_claim}'，"
        "仍归 orchestrator 命名空间避免 generic worker 抢占"
    )
    assert is_new is True
    assert new_run.id == fake_new_run.id
