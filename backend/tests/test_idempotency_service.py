"""幂等服务测试 - 验证 acquire_job_run_lock 的幂等语义。

覆盖：
- 首次调用返回 SchedulerJobRun，run_key 正确
- 同一 run_key 第二次调用返回 None（SKIPPED_DUPLICATE）
- 不同 run_key 互不影响
- metadata 正确序列化到 metadata_json
- 边界：failed/interrupted 状态的记录也返回 None（本版本不自动重试）

测试环境：SQLite 内存数据库（跳过 pg_advisory_xact_lock，仅依赖唯一约束）
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.idempotency_service import acquire_job_run_lock


@pytest_asyncio.fixture(loop_scope="session")
async def test_db():
    """每个测试独立的内存 SQLite 异步 DB 会话（包含 run_key 唯一约束）。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SchedulerJobRun.__table__.create)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_acquire_lock_first_call_succeeds(test_db) -> None:
    """第一次调用应返回 SchedulerJobRun，run_key 与传入一致。"""
    run_key = "bars_scheduler:2026-06-25"
    job_run = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key,
        job_name="bars_scheduler",
        business_date="2026-06-25",
        worker_instance_id="test-worker-1",
    )
    await test_db.commit()

    assert job_run is not None
    assert job_run.run_key == run_key
    assert job_run.job_name == "bars_scheduler"
    assert job_run.business_date == "2026-06-25"
    assert job_run.status == "running"
    assert job_run.worker_instance_id == "test-worker-1"
    assert job_run.scheduled_at is not None
    assert job_run.started_at is not None
    assert job_run.heartbeat_at is not None
    assert job_run.lease_expires_at is not None


@pytest.mark.asyncio
async def test_acquire_lock_second_call_returns_none(test_db) -> None:
    """同一 run_key 第二次调用应返回 None（幂等跳过）。"""
    run_key = "strategy_scheduler:2026-06-25"

    # 第一次：成功获取
    first = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key,
        job_name="strategy_scheduler",
        business_date="2026-06-25",
    )
    await test_db.commit()
    assert first is not None

    # 第二次：应返回 None（SKIPPED_DUPLICATE）
    second = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key,
        job_name="strategy_scheduler",
        business_date="2026-06-25",
    )
    assert second is None


@pytest.mark.asyncio
async def test_acquire_lock_different_run_keys_both_succeed(test_db) -> None:
    """不同 run_key 互不影响，均能成功获取锁。"""
    run_key_1 = "bars_scheduler:2026-06-24"
    run_key_2 = "bars_scheduler:2026-06-25"

    job_run_1 = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key_1,
        job_name="bars_scheduler",
        business_date="2026-06-24",
    )
    await test_db.commit()

    job_run_2 = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key_2,
        job_name="bars_scheduler",
        business_date="2026-06-25",
    )
    await test_db.commit()

    assert job_run_1 is not None
    assert job_run_2 is not None
    assert job_run_1.id != job_run_2.id
    assert job_run_1.run_key == run_key_1
    assert job_run_2.run_key == run_key_2


@pytest.mark.asyncio
async def test_acquire_lock_with_metadata(test_db) -> None:
    """metadata 应正确序列化为 JSON 存入 metadata_json。"""
    run_key = "monitor_scheduler:2026-06-25:morning"
    metadata = {"session_label": "morning", "extra": "value"}

    job_run = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key,
        job_name="monitor_scheduler",
        business_date="2026-06-25",
        metadata=metadata,
    )
    await test_db.commit()

    assert job_run is not None
    assert job_run.metadata_json is not None
    parsed = json.loads(job_run.metadata_json)
    assert parsed["session_label"] == "morning"
    assert parsed["extra"] == "value"


@pytest.mark.asyncio
async def test_acquire_lock_skips_failed_status(test_db) -> None:
    """边界：已存在的 failed 状态记录也应返回 None（本版本不自动重试）。"""
    run_key = "calendar_scheduler:2026-06-25"

    # 先手动插入一条 failed 记录
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)
    existing_run = SchedulerJobRun(
        id=uuid.uuid4(),
        job_name="calendar_scheduler",
        business_date="2026-06-25",
        run_key=run_key,
        status="failed",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
    )
    test_db.add(existing_run)
    await test_db.commit()

    # 再次调用应返回 None
    result = await acquire_job_run_lock(
        db=test_db,
        run_key=run_key,
        job_name="calendar_scheduler",
        business_date="2026-06-25",
    )
    assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
