"""C2A-V1: Bars/Calendar 依赖的 scheduler-owned job fencing 真实 PostgreSQL 证据。

本文件不测 Bars/Calendar 业务逻辑；只证明这两个 runtime 现在依赖的
``FencedJobToken`` + ``refresh_job_lease`` + ``finalize_job_run`` 这一组
fenced primitive，对 **scheduler-created running job** 在真实 PostgreSQL 上确实成立：

1. healthy owner：token 来自真实 DB ownership fields → refresh_job_lease 真正延长
   lease/heartbeat → finalize_job_run 成功写 succeeded 终态并释放 owner/lease。
2. ownership 已转移到 newer epoch 后，stale epoch 的 finalize 必须返回 False 且
   不覆盖终态；current epoch（新 owner + 新 epoch）的 finalize 仍成功——正向对照，
   排除「所有 finalize 永远返回 False」的坏实现。

Bars/Calendar runtime 的 wiring（正确构造 token / start heartbeat / fenced finalize）
由 test_bars_scheduler_worker_runtime.py / test_calendar_scheduler_worker_runtime.py
的 pure-unit 负责；此处只验证共享的 DB contract。

测试环境：PostgreSQL 验证库（仅远程 panji-verify 运行）。
本地 PURE_UNIT_TEST=1 下由 conftest 自动 skip（不连库）。
严禁自带 CI skipif（否则 verifier 容器内会误 skip，参见 C1 的 silent-skip 教训）。
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.models.scheduler_job_run import SchedulerJobRun

# 显式 postgres marker：本文件全部为真实 PG 集成测试。
# 与 test_chip_consensus_worker.py 一致——不带 CI skipif，由 conftest 的 postgres
# 自动标注在 PURE_UNIT_TEST=1 下 skip。
pytestmark = pytest.mark.postgres

_TZ = ZoneInfo("Asia/Shanghai")
_BIZ_DATE = date(2026, 9, 18).isoformat()  # "2026-09-18"
_JOB_NAME = "c2a_pg_scheduler"


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------
async def _cleanup_job_run(
    session_factory, job_run_id: uuid.UUID
) -> None:
    """显式删除测试创建的 SchedulerJobRun（不依赖 fixture rollback）。

    refresh_job_lease / finalize_job_run 各自开独立 session 并提交，未提交的
    fixture 事务对其不可见，因此必须显式 commit 删除。
    """
    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM job_run_events WHERE job_run_id = :id"),
            {"id": job_run_id},
        )
        await db.execute(
            text("DELETE FROM scheduler_job_runs WHERE id = :id"),
            {"id": job_run_id},
        )
        await db.commit()


# ---------------------------------------------------------------------------
# Test 1: healthy scheduler-owned lifecycle
# ---------------------------------------------------------------------------
async def test_scheduler_owned_job_refreshes_and_finalizes_on_postgres() -> None:
    """scheduler-created running row：token 来自真实 DB ownership fields，
    refresh_job_lease 真正延长 lease，finalize_job_run 成功写 succeeded 终态并释放。"""
    from app.services.fenced_job_run_service import (
        FencedJobToken,
        finalize_job_run,
        refresh_job_lease,
    )
    from app.worker import _create_job_run
    from tests.conftest import TestAsyncSessionLocal

    run_key = f"c2a-pg:{uuid.uuid4()}"
    # 复用与 Bars/Calendar 相同的 scheduler-create owner 路径（会 commit）。
    async with TestAsyncSessionLocal() as db:
        job_run = await _create_job_run(
            db,
            _JOB_NAME,
            _BIZ_DATE,
            lease_seconds=120,
            run_key=run_key,
        )
    assert job_run is not None
    assert job_run.status == "running"
    assert job_run.worker_instance_id is not None
    job_run_id = job_run.id
    initial_epoch = job_run.lease_epoch

    try:
        # token 直接来源于真实 job_run 的 ownership fields。
        token = FencedJobToken(
            job_run_id=job_run.id,
            worker_instance_id=job_run.worker_instance_id,
            lease_epoch=job_run.lease_epoch,
            lease_seconds=120,
        )

        # 真正刷新 lease（FencedJobHeartbeat 背后使用的 SQL primitive）。
        refresh_time = datetime.now(_TZ)
        refreshed = await refresh_job_lease(
            token, session_factory=TestAsyncSessionLocal, now=refresh_time
        )
        assert refreshed is True

        # 独立 session 回读：lease/heartbeat 真正更新，owner 不变。
        async with TestAsyncSessionLocal() as db:
            row = await db.get(SchedulerJobRun, job_run_id)
            assert row is not None
            assert row.status == "running"
            assert row.worker_instance_id == job_run.worker_instance_id
            assert row.lease_epoch == initial_epoch
            assert row.heartbeat_at == refresh_time
            assert row.lease_expires_at == refresh_time + timedelta(seconds=120)

        # 真正 finalize。
        updated = await finalize_job_run(
            token,
            status="succeeded",
            metadata_updates={"evidence": "c2a-v1"},
            total_count=1,
            succeeded_count=1,
            failed_count=0,
            session_factory=TestAsyncSessionLocal,
        )
        assert updated is True

        # 回读终态：owner/lease 已释放，counts/metadata 正确。
        async with TestAsyncSessionLocal() as db:
            row = await db.get(SchedulerJobRun, job_run_id)
            assert row is not None
            assert row.status == "succeeded"
            assert row.worker_instance_id is None
            assert row.lease_expires_at is None
            assert row.succeeded_count == 1
            assert row.failed_count == 0
            meta = json.loads(row.metadata_json) if row.metadata_json else {}
            assert meta.get("evidence") == "c2a-v1"
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)


# ---------------------------------------------------------------------------
# Test 2: stale epoch terminal 必须被拒绝 + current epoch 正向对照
# ---------------------------------------------------------------------------
async def test_scheduler_owned_job_stale_epoch_cannot_finalize_on_postgres() -> None:
    """ownership 已转移到 newer epoch 后，stale owner 的 finalize 必须返回 False
    且不覆盖终态；current epoch（新 owner + 新 epoch）的 finalize 仍成功。"""
    from app.services.fenced_job_run_service import (
        FencedJobToken,
        finalize_job_run,
    )
    from app.worker import _create_job_run
    from tests.conftest import TestAsyncSessionLocal

    run_key = f"c2a-pg:{uuid.uuid4()}"
    async with TestAsyncSessionLocal() as db:
        job_run = await _create_job_run(
            db,
            _JOB_NAME,
            _BIZ_DATE,
            lease_seconds=120,
            run_key=run_key,
        )
    assert job_run is not None
    job_run_id = job_run.id
    worker = job_run.worker_instance_id
    epoch = job_run.lease_epoch

    try:
        # stale token：仍是旧 owner + 旧 epoch。
        stale_token = FencedJobToken(job_run_id, worker, epoch, 120)

        # 模拟 ownership 转移（C1 已证明普通 stale recovery 会 bump epoch；
        # 此处只测 epoch 转移后 stale owner 失去 finalize 权，职责不重叠）。
        async with TestAsyncSessionLocal() as db:
            current = await db.get(SchedulerJobRun, job_run_id)
            assert current is not None
            current.lease_epoch = epoch + 1
            current.worker_instance_id = "c2a-new-owner"
            current.status = "running"
            await db.commit()

        # stale finalize 必须被拒。
        updated = await finalize_job_run(
            stale_token,
            status="succeeded",
            metadata_updates={"stale": True},
            total_count=99,
            succeeded_count=99,
            failed_count=0,
            session_factory=TestAsyncSessionLocal,
        )
        assert updated is False

        # 回读：终态未被 stale token 覆盖。
        async with TestAsyncSessionLocal() as db:
            row = await db.get(SchedulerJobRun, job_run_id)
            assert row is not None
            assert row.status == "running"
            assert row.lease_epoch == epoch + 1
            assert row.worker_instance_id == "c2a-new-owner"
            assert row.succeeded_count != 99
            meta = json.loads(row.metadata_json) if row.metadata_json else {}
            assert "stale" not in meta

        # 正向对照：current epoch（新 owner + 新 epoch）仍可写。
        current_token = FencedJobToken(job_run_id, "c2a-new-owner", epoch + 1, 120)
        current_updated = await finalize_job_run(
            current_token,
            status="succeeded",
            metadata_updates={"current": True},
            total_count=1,
            succeeded_count=1,
            failed_count=0,
            session_factory=TestAsyncSessionLocal,
        )
        assert current_updated is True

        async with TestAsyncSessionLocal() as db:
            row = await db.get(SchedulerJobRun, job_run_id)
            assert row is not None
            assert row.status == "succeeded"
            meta = json.loads(row.metadata_json) if row.metadata_json else {}
            assert meta.get("current") is True
    finally:
        await _cleanup_job_run(TestAsyncSessionLocal, job_run_id)
