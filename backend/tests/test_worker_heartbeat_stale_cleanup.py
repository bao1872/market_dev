"""Worker 心跳僵尸记录清理测试 - 验证 mark_stale_worker_heartbeats 行为。

覆盖 6 个场景（按本轮任务要求）：
1. fresh running heartbeat 不被标记（heartbeat_at 在阈值内）
2. stale running heartbeat 被标记为 stopped
3. 非 running heartbeat（已 stopped）不被重复处理
4. 多 worker_name / instance_id 可批量处理
5. 函数返回正确处理数量
6. watchdog 调用该函数（集成测试）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
设计要点：
- mark_stale_worker_heartbeats 单元测试直接使用 db_session fixture
- watchdog 集成测试复用 test_recovery_watchdog.py 的 _FakeSessionLocal 模式
- 使用固定 now 注入，避免依赖系统时间
- 使用 timezone-aware UTC（与 _heartbeat_loop 一致）
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.worker_heartbeat import WorkerHeartbeat

# 固定测试时间（UTC），避免依赖系统时钟
_FIXED_NOW = datetime(2026, 7, 4, 6, 0, 0, tzinfo=UTC)
# 阈值 600s = 10 分钟
_THRESHOLD = 600


def _make_heartbeat(
    *,
    worker_name: str,
    instance_id: str | None = None,
    status: str = "running",
    heartbeat_age_seconds: int = 0,
) -> WorkerHeartbeat:
    """构造一个 WorkerHeartbeat 记录，heartbeat_at = FIXED_NOW - age。"""
    if instance_id is None:
        instance_id = f"test-host:{uuid.uuid4().hex[:8]}"
    hb_at = _FIXED_NOW - timedelta(seconds=heartbeat_age_seconds)
    return WorkerHeartbeat(
        worker_name=worker_name,
        instance_id=instance_id,
        started_at=hb_at,
        heartbeat_at=hb_at,
        status=status,
        build_sha="test-sha",
    )


@pytest.mark.asyncio
async def test_fresh_running_heartbeat_not_marked(db_session) -> None:
    """场景 1：fresh running heartbeat（age < 阈值）不被标记。

    构造 age=30s 的 running 记录（远小于 600s 阈值），调用清理函数后应保持 running。
    """
    from app.worker import mark_stale_worker_heartbeats

    hb = _make_heartbeat(
        worker_name="bars_scheduler",
        heartbeat_age_seconds=30,
    )
    db_session.add(hb)
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 0, "fresh running heartbeat 不应被标记"
    # 刷新 ORM 对象状态
    await db_session.refresh(hb)
    assert hb.status == "running", "fresh heartbeat 应保持 running"


@pytest.mark.asyncio
async def test_stale_running_heartbeat_marked_stopped(db_session) -> None:
    """场景 2：stale running heartbeat（age >= 阈值）被标记为 stopped。

    构造 age=1200s（20 分钟）的 running 记录，超过 600s 阈值，应被标记为 stopped。
    """
    from app.worker import mark_stale_worker_heartbeats

    hb = _make_heartbeat(
        worker_name="outbox",
        heartbeat_age_seconds=1200,
    )
    db_session.add(hb)
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 1, "应标记 1 个 stale running heartbeat"
    # populate_existing=True 强制从 DB 刷新（UPDATE 绕过 ORM identity map）
    stmt = (
        select(WorkerHeartbeat)
        .where(
            WorkerHeartbeat.worker_name == "outbox",
            WorkerHeartbeat.instance_id == hb.instance_id,
        )
        .execution_options(populate_existing=True)
    )
    result = await db_session.execute(stmt)
    attached = result.scalar_one()
    assert attached.status == "stopped", "stale heartbeat 应被标记为 stopped"


@pytest.mark.asyncio
async def test_non_running_heartbeat_not_processed(db_session) -> None:
    """场景 3：非 running heartbeat（已 stopped/idle）不被重复处理。

    构造 status='stopped' 的过时记录，调用清理函数后应保持 stopped，返回 0。
    """
    from app.worker import mark_stale_worker_heartbeats

    hb = _make_heartbeat(
        worker_name="delivery",
        status="stopped",
        heartbeat_age_seconds=3600,
    )
    db_session.add(hb)
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 0, "已 stopped 的记录不应被重复处理"
    await db_session.refresh(hb)
    assert hb.status == "stopped", "已 stopped 应保持 stopped"


@pytest.mark.asyncio
async def test_multiple_workers_batch_processing(db_session) -> None:
    """场景 4：多 worker_name / instance_id 可批量处理。

    构造 5 条记录：
    - 2 条 stale running（不同 worker_name）→ 应被标记
    - 1 条 fresh running → 不应被标记
    - 1 条 stale stopped → 不应被处理
    - 1 条 stale running（不同 instance_id，同 worker_name）→ 应被标记
    共 3 条应被标记。
    """
    from app.worker import mark_stale_worker_heartbeats

    records = [
        # stale running - bars_scheduler
        _make_heartbeat(worker_name="bars_scheduler", heartbeat_age_seconds=900),
        # stale running - monitor_scheduler
        _make_heartbeat(worker_name="monitor_scheduler", heartbeat_age_seconds=720),
        # fresh running - outbox
        _make_heartbeat(worker_name="outbox", heartbeat_age_seconds=60),
        # stale stopped - delivery
        _make_heartbeat(
            worker_name="delivery", status="stopped", heartbeat_age_seconds=1800,
        ),
        # stale running - bars_scheduler 另一实例（同 worker_name 不同 instance_id）
        _make_heartbeat(worker_name="bars_scheduler", heartbeat_age_seconds=1100),
    ]
    for r in records:
        db_session.add(r)
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 3, f"应标记 3 个 stale running 记录，实际: {marked}"

    # 验证 fresh running 保持 running
    await db_session.refresh(records[2])
    assert records[2].status == "running", "fresh running 应保持 running"

    # 验证 stale stopped 保持 stopped
    await db_session.refresh(records[3])
    assert records[3].status == "stopped", "stale stopped 应保持 stopped"


@pytest.mark.asyncio
async def test_return_value_matches_marked_count(db_session) -> None:
    """场景 5：函数返回正确处理数量。

    构造 2 条 stale running + 3 条 fresh/非 running，验证返回值 == 2。
    """
    from app.worker import mark_stale_worker_heartbeats

    records = [
        _make_heartbeat(worker_name="w1", heartbeat_age_seconds=700),
        _make_heartbeat(worker_name="w2", heartbeat_age_seconds=800),
        _make_heartbeat(worker_name="w3", heartbeat_age_seconds=100),
        _make_heartbeat(worker_name="w4", status="stopped", heartbeat_age_seconds=900),
        _make_heartbeat(worker_name="w5", status="idle", heartbeat_age_seconds=900),
    ]
    for r in records:
        db_session.add(r)
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 2, f"应返回 2，实际: {marked}"


@pytest.mark.asyncio
async def test_threshold_boundary(db_session) -> None:
    """阈值边界：age == threshold 的记录应被标记（< 不标记，>= 标记）。

    构造 age=600s（恰好等于阈值）和 age=599s（略小于阈值）两条记录。
    UPDATE WHERE heartbeat_at < cutoff：cutoff = now - 600s
    - age=600 → heartbeat_at = now - 600s = cutoff，不满足 < cutoff，不被标记
    - age=601 → heartbeat_at = now - 601s < cutoff，被标记
    """
    from app.worker import mark_stale_worker_heartbeats

    # age=600（恰好等于阈值）→ heartbeat_at == cutoff，不满足 <，不被标记
    hb_exact = _make_heartbeat(worker_name="w_exact", heartbeat_age_seconds=600)
    # age=601（略超阈值）→ heartbeat_at < cutoff，被标记
    hb_over = _make_heartbeat(worker_name="w_over", heartbeat_age_seconds=601)
    db_session.add_all([hb_exact, hb_over])
    await db_session.flush()

    marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert marked == 1, f"边界测试：应只标记 1 条（age=601），实际: {marked}"
    await db_session.refresh(hb_exact)
    assert hb_exact.status == "running", "age=600（等于阈值）不应被标记"


@pytest.mark.asyncio
async def test_watchdog_calls_mark_stale_worker_heartbeats(db_session) -> None:
    """场景 6：watchdog 调用 mark_stale_worker_heartbeats。

    构造 1 条 stale running heartbeat，patch AsyncSessionLocal/_heartbeat_loop/asyncio.sleep，
    验证 watchdog 执行一次后 heartbeat 被标记为 stopped。

    注意：watchdog 调用 mark_stale_worker_heartbeats(db) 不传 now 参数，
    使用 datetime.now(UTC) 作为当前时间。因此 heartbeat_at 必须相对真实当前时间过时，
    不能使用 _FIXED_NOW（可能在未来导致不被标记）。
    """
    from app import worker as worker_mod
    from app.worker import STALE_HEARTBEAT_THRESHOLD_SECONDS, _recovery_watchdog_loop

    # 使用真实当前时间构造 stale heartbeat（age 远超阈值 600s）
    real_now = datetime.now(UTC)
    hb_at = real_now - timedelta(seconds=STALE_HEARTBEAT_THRESHOLD_SECONDS + 600)
    hb = WorkerHeartbeat(
        worker_name="test_watchdog_worker",
        instance_id=f"test-host:{uuid.uuid4().hex[:8]}",
        started_at=hb_at,
        heartbeat_at=hb_at,
        status="running",
        build_sha="test-sha",
    )
    db_session.add(hb)
    await db_session.flush()
    hb_id = (hb.worker_name, hb.instance_id)

    real_sleep = asyncio.sleep
    sleep_count = 0

    async def fake_sleep(seconds):
        nonlocal sleep_count
        sleep_count += 1
        await real_sleep(0)
        raise asyncio.CancelledError()

    # 复用 test_recovery_watchdog.py 的 _FakeSessionLocal 模式
    class _FakeSessionCtx:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    class _FakeSessionLocal:
        def __call__(self):
            return _FakeSessionCtx()

    async def _no_op_heartbeat(worker_name: str, interval: int = 60) -> None:
        return

    with patch.object(worker_mod, "AsyncSessionLocal", _FakeSessionLocal()), \
         patch.object(db_session, "commit", new=db_session.flush), \
         patch.object(worker_mod, "_heartbeat_loop", _no_op_heartbeat), \
         patch.object(worker_mod, "_shutdown", False), \
         patch("asyncio.sleep", new=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await _recovery_watchdog_loop(interval_seconds=1)

    assert sleep_count == 1, "watchdog 应执行一次清理后因 CancelledError 退出"

    # 验证 heartbeat 被标记为 stopped
    stmt = (
        select(WorkerHeartbeat)
        .where(
            WorkerHeartbeat.worker_name == hb_id[0],
            WorkerHeartbeat.instance_id == hb_id[1],
        )
        .execution_options(populate_existing=True)
    )
    result = await db_session.execute(stmt)
    attached = result.scalar_one()
    assert attached.status == "stopped", "watchdog 应将 stale heartbeat 标记为 stopped"


@pytest.mark.asyncio
async def test_idempotent_multiple_calls(db_session) -> None:
    """幂等性：连续两次调用，第二次应返回 0（已 stopped 不再处理）。"""
    from app.worker import mark_stale_worker_heartbeats

    hb = _make_heartbeat(worker_name="w_idempotent", heartbeat_age_seconds=900)
    db_session.add(hb)
    await db_session.flush()

    first_marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )
    second_marked = await mark_stale_worker_heartbeats(
        db_session, now=_FIXED_NOW, threshold_seconds=_THRESHOLD,
    )

    assert first_marked == 1, "第一次调用应标记 1 条"
    assert second_marked == 0, "第二次调用应返回 0（已 stopped 不再处理）"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
