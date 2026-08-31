"""Chip Consensus 优雅抢占（preemption）测试 - 验证 SIGTERM 安全边界（当前 blocker 修复）。

覆盖本次修复的必测项（用户指令 A-E）：
A. chip 正常执行不受 shutdown_check 影响（shutdown_check=None 不报错、正常返回）
B. chip 处理中 shutdown_check 返回 True：execute 在安全边界抛 ChipPreemptedForShutdown；
   worker 侧将 owned run running -> resume_queued（释放 ownership/lease）后可退出
C. 再次领取：resume_queued 可被 claim_next_job_run 领取（running），从 pending 继续
D. 已成功 snapshots 保留，get_pending_chip_instruments 跳过已成功项（不重算）
E. ownership fencing：requeue 后旧 token 的 fenced 写入（lock_owned_job_run）抛 JobLeaseLostError；
   非 owner token 的 requeue 返回 False

测试环境：PostgreSQL 测试库（与 test_chip_consensus_worker.py 同约定）
运行：
    APP_ENV=test TEST_DATABASE_URL=postgresql://... \
        pytest backend/tests/test_chip_consensus_preemption.py -v
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.services.after_close_chip_consensus_service import (
    ChipPreemptedForShutdown,
    _CHIP_LEASE_SECONDS,
    execute_after_close_chip_consensus,
    get_pending_chip_instruments,
)
from app.services.fenced_job_run_service import (
    FencedJobToken,
    JobLeaseLostError,
    claim_next_job_run,
    lock_owned_job_run,
    requeue_owned_job_to_resume,
)

# CI 环境标识（与 conftest.py / test_chip_consensus_worker.py 一致）
# 额外包含 PANJI_REMOTE_VERIFY_DB_TEST：远程验证容器（panji-verify-python）设置该标志
# 而非 PANJI_CI_DB_TEST，否则本文件在 verify 模式下会被 skip（false-green）。
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_REMOTE_VERIFY_DB_TEST", "").lower() in ("1", "true", "yes")
)

# 本测试文件全部为 PG 集成测试，只在 CI/远程验证 Postgres 中运行；
# 本地 PURE_UNIT_TEST=1 自动 skip（与既有 chip worker 测试约定一致）。
# 同时显式标注 postgres（conftest 偏好作者显式标记，供 -m postgres 精确切分）。
pytestmark = [
    pytest.mark.skipif(
        not _CI_ENV,
        reason="chip preemption 测试为 PG 集成测试，只在 CI/远程验证 Postgres 中运行；本地请用 PURE_UNIT_TEST=1",
    ),
    pytest.mark.postgres,
]

_TZ = __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
_TRADE_DATE = date(2026, 6, 25)
_WORKER_A = "preempt-test-worker-a:1:aaaaaaaaaaaa"
_WORKER_B = "preempt-test-worker-b:1:bbbbbbbbbbbb"


async def _make_job(
    session_factory,
    *,
    status: str,
    worker_instance_id: str | None,
    lease_epoch: int = 1,
    trade_date: date = _TRADE_DATE,
) -> SchedulerJobRun:
    core_run_id = uuid.uuid4()
    now = datetime.now(_TZ)
    meta = {
        "chip_status": status,
        "trade_date": trade_date.isoformat(),
        "core_run_id": str(core_run_id),
        "scope": "all_a_share",
    }
    job_run = SchedulerJobRun(
        job_name="after_close_chip_consensus",
        business_date=trade_date.isoformat(),
        run_key=f"after_close_chip_consensus:preempt:{uuid.uuid4().hex[:8]}",
        status=status,
        scheduled_at=now,
        started_at=now if status == "running" else None,
        heartbeat_at=now,
        lease_expires_at=(
            now + timedelta(seconds=_CHIP_LEASE_SECONDS) if status == "running" else None
        ),
        worker_instance_id=worker_instance_id,
        lease_epoch=lease_epoch,
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with session_factory() as db:
        db.add(job_run)
        await db.commit()
        await db.refresh(job_run)
        # 返回 core_run_id 供 snapshot 关联
        return job_run, core_run_id


async def _seed_snapshot(
    session_factory,
    *,
    instrument_id: uuid.UUID,
    trade_date: date,
    core_run_id: uuid.UUID,
    status: str,
) -> None:
    snap = StockChipConsensusSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        core_run_id=core_run_id,
        status=status,
        chip_hash="seed",
        chip_payload={},
    )
    async with session_factory() as db:
        db.add(snap)
        await db.commit()


# ---------------------------------------------------------------------------
# A. 正常执行不受影响（shutdown_check=None）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_execution_unaffected_by_shutdown_check(TestAsyncSessionLocal):
    """shutdown_check=None 时 execute 应正常完成、不抛抢占异常。"""
    job_run, core_run_id = await _make_job(
        TestAsyncSessionLocal, status="running", worker_instance_id=_WORKER_A,
    )
    token = FencedJobToken(
        job_run_id=job_run.id,
        worker_instance_id=_WORKER_A,
        lease_epoch=job_run.lease_epoch,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )
    inst = uuid.uuid4()
    daily = MagicMock()
    daily.empty = False

    with patch(
        "app.services.after_close_chip_consensus_service.refresh_15m_batch",
        new=AsyncMock(return_value=MagicMock(to_dict=lambda: {})),
    ), patch(
        "app.services.after_close_chip_consensus_service._fetch_chip_bars",
        new=AsyncMock(return_value=(daily, None)),  # 15m 不足 -> skipped 路径，无需 compute
    ):
        result = await execute_after_close_chip_consensus(
            job_run_id=job_run.id,
            trade_date=_TRADE_DATE,
            core_run_id=core_run_id,
            instrument_ids=[inst],
            worker_id=_WORKER_A,
            lease_epoch=job_run.lease_epoch,
            shutdown_check=None,
        )

    assert isinstance(result, dict)
    assert result["total_count"] == 1
    # 不应抛 ChipPreemptedForShutdown
    assert result["status"] in {"skipped", "partial", "succeeded"}


# ---------------------------------------------------------------------------
# B. 处理中 shutdown -> 安全边界抛 ChipPreemptedForShutdown + worker requeue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_raises_at_safe_boundary_and_requeues(TestAsyncSessionLocal):
    """shutdown_check 在第 2 个 instrument 顶部返回 True：
    - 第 1 个 instrument 的快照已持久化（安全边界）
    - execute 抛 ChipPreemptedForShutdown
    - 调用方 requeue_owned_job_to_resume 将 running -> resume_queued（释放 ownership）
    """
    job_run, core_run_id = await _make_job(
        TestAsyncSessionLocal, status="running", worker_instance_id=_WORKER_A,
    )
    token = FencedJobToken(
        job_run_id=job_run.id,
        worker_instance_id=_WORKER_A,
        lease_epoch=job_run.lease_epoch,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )
    inst0, inst1 = uuid.uuid4(), uuid.uuid4()
    daily = MagicMock()
    daily.empty = False
    calls = {"n": 0}

    def _shutdown_check() -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # 第 1 个 instrument 处理完，第 2 个顶部 preempt

    with patch(
        "app.services.after_close_chip_consensus_service.refresh_15m_batch",
        new=AsyncMock(return_value=MagicMock(to_dict=lambda: {})),
    ), patch(
        "app.services.after_close_chip_consensus_service._fetch_chip_bars",
        new=AsyncMock(return_value=(daily, None)),
    ):
        with pytest.raises(ChipPreemptedForShutdown) as exc_info:
            await execute_after_close_chip_consensus(
                job_run_id=job_run.id,
                trade_date=_TRADE_DATE,
                core_run_id=core_run_id,
                instrument_ids=[inst0, inst1],
                worker_id=_WORKER_A,
                lease_epoch=job_run.lease_epoch,
                shutdown_check=_shutdown_check,
            )

    assert exc_info.value.job_run_id == job_run.id

    # 安全边界：inst0 的 skipped 快照已持久化，inst1 未处理
    async with TestAsyncSessionLocal() as db:
        rows = (await db.execute(
            select(StockChipConsensusSnapshot.instrument_id, StockChipConsensusSnapshot.status)
            .where(StockChipConsensusSnapshot.core_run_id == core_run_id)
        )).all()
    done = {r[0]: r[1] for r in rows}
    assert inst0 in done
    assert inst1 not in done

    # worker 侧：run 仍在 running（execute 未终态），owner 仍可 requeue
    requeued = await requeue_owned_job_to_resume(token)
    assert requeued is True

    async with TestAsyncSessionLocal() as db:
        jr = await db.get(SchedulerJobRun, job_run.id)
        assert jr.status == "resume_queued"
        assert jr.worker_instance_id is None
        assert jr.lease_expires_at is None
        assert jr.finished_at is None


# ---------------------------------------------------------------------------
# C + D. resume_queued 再次领取，只处理 pending（已成功快照保留）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_queued_reclaimed_and_skips_succeeded(TestAsyncSessionLocal):
    """resume_queued 任务被新 worker 领取（running），get_pending 只返回未完成项。"""
    job_run, core_run_id = await _make_job(
        TestAsyncSessionLocal, status="resume_queued", worker_instance_id=None,
    )
    inst0, inst1, inst2 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    # inst0 / inst1 已成功 -> 续算应跳过；inst2 待处理
    await _seed_snapshot(TestAsyncSessionLocal, instrument_id=inst0, trade_date=_TRADE_DATE, core_run_id=core_run_id, status="succeeded")
    await _seed_snapshot(TestAsyncSessionLocal, instrument_id=inst1, trade_date=_TRADE_DATE, core_run_id=core_run_id, status="succeeded")

    claim = await claim_next_job_run(
        TestAsyncSessionLocal,
        job_name="after_close_chip_consensus",
        worker_instance_id=_WORKER_B,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )
    assert claim is not None
    assert claim.token.job_run_id == job_run.id
    assert claim.previous_status == "resume_queued"

    async with TestAsyncSessionLocal() as db:
        jr = await db.get(SchedulerJobRun, job_run.id)
        assert jr.status == "running"
        assert jr.worker_instance_id == _WORKER_B

        pending = await get_pending_chip_instruments(
            db,
            trade_date=_TRADE_DATE,
            core_run_id=core_run_id,
            all_instrument_ids=[inst0, inst1, inst2],
        )
    assert pending == [inst2]  # 已成功项被过滤

    # D: 已成功快照保留
    async with TestAsyncSessionLocal() as db:
        kept = (await db.execute(
            select(StockChipConsensusSnapshot.instrument_id)
            .where(
                StockChipConsensusSnapshot.core_run_id == core_run_id,
                StockChipConsensusSnapshot.status == "succeeded",
            )
        )).scalars().all()
    assert set(kept) == {inst0, inst1}


# ---------------------------------------------------------------------------
# E. ownership fencing：requeue 后旧 worker 不能再写
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ownership_fence_after_requeue(TestAsyncSessionLocal):
    """running -> resume_queued 后：
    - 旧 token 的 lock_owned_job_run 抛 JobLeaseLostError（禁止后续写入）
    - 非 owner token 的 requeue_owned_job_to_resume 返回 False（不动行）
    """
    job_run, _ = await _make_job(
        TestAsyncSessionLocal, status="running", worker_instance_id=_WORKER_A,
    )
    owner_token = FencedJobToken(
        job_run_id=job_run.id,
        worker_instance_id=_WORKER_A,
        lease_epoch=job_run.lease_epoch,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )
    requeued = await requeue_owned_job_to_resume(owner_token)
    assert requeued is True

    # 旧 owner token 现在状态已非 running -> fenced 锁失败
    with pytest.raises(JobLeaseLostError):
        async with TestAsyncSessionLocal() as db:
            await lock_owned_job_run(db, owner_token)

    # 非 owner token（不同 worker）requeue 应返回 False，且不改行
    stranger_token = FencedJobToken(
        job_run_id=job_run.id,
        worker_instance_id=_WORKER_B,
        lease_epoch=job_run.lease_epoch,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )
    assert await requeue_owned_job_to_resume(stranger_token) is False

    async with TestAsyncSessionLocal() as db:
        jr = await db.get(SchedulerJobRun, job_run.id)
        assert jr.status == "resume_queued"
        assert jr.worker_instance_id is None
