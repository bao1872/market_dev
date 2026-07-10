"""job_run_events 时间线服务测试 - 验证 append_event / list_events。

覆盖：
- append_event 写入成功，payload 正确序列化（JSONB）
- list_events 按 created_at 倒序返回
- level=error 事件正常写入
- job_run_id 不存在时：函数不主动抛业务异常，外键约束由 DB 保证（IntegrityError）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
"""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.job_run_event_service import append_event, list_events


async def _create_job_run(db_session) -> SchedulerJobRun:
    """创建测试用 SchedulerJobRun（满足外键约束）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run = SchedulerJobRun(
        job_name="test_job",
        business_date="2026-06-25",
        run_key=f"test_job:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now,
    )
    db_session.add(job_run)
    await db_session.flush()
    return job_run


@pytest.mark.asyncio
async def test_append_event_writes_payload(db_session) -> None:
    """测试 1：append_event 写入成功，payload 正确序列化为 JSONB。"""
    job_run = await _create_job_run(db_session)

    payload = {
        "coverage": 0.95,
        "succeeded": 4800,
        "failed": 100,
        "run_id": str(uuid.uuid4()),
    }
    event = await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="DAILY_DONE",
        level="info",
        message="日线刷新完成",
        payload=payload,
    )

    assert event.id is not None
    assert event.job_run_id == job_run.id
    assert event.step == "DAILY_DONE"
    assert event.level == "info"
    assert event.message == "日线刷新完成"
    assert event.payload == payload
    assert event.payload["coverage"] == 0.95
    assert event.payload["run_id"] == payload["run_id"]
    assert event.created_at is not None


@pytest.mark.asyncio
async def test_list_events_ordered_desc(db_session) -> None:
    """测试 2：list_events 按 created_at 倒序返回（最新在前）。"""
    job_run = await _create_job_run(db_session)

    # 写入 3 条事件（created_at 由 server_default 生成，需 flush 让 DB 生成时间戳）
    e1 = await append_event(db_session, job_run.id, "START", message="开始")
    await db_session.flush()
    # 手动设置不同 created_at 以确保倒序可验证（server_default 可能在同事务内时间相同）
    e1.created_at = datetime(2026, 6, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e2 = await append_event(db_session, job_run.id, "DAILY_DONE", message="日线完成")
    await db_session.flush()
    e2.created_at = datetime(2026, 6, 25, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e3 = await append_event(db_session, job_run.id, "DSA_CREATED", message="DSA 创建")
    await db_session.flush()
    e3.created_at = datetime(2026, 6, 25, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    await db_session.flush()

    events = await list_events(db_session, job_run.id, limit=100)
    assert len(events) == 3
    # 倒序：最新（18:00）在前
    assert events[0].step == "DSA_CREATED"
    assert events[1].step == "DAILY_DONE"
    assert events[2].step == "START"


@pytest.mark.asyncio
async def test_append_event_error_level(db_session) -> None:
    """测试 3：level=error 事件正常写入。"""
    job_run = await _create_job_run(db_session)

    event = await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="ERROR",
        level="error",
        message="日线刷新失败: pytdx 连接超时",
        payload={"error_code": "TIMEOUT", "symbol": "000001"},
    )

    assert event.level == "error"
    assert event.step == "ERROR"
    assert event.payload is not None
    assert event.payload["error_code"] == "TIMEOUT"

    # 验证可按 level 查询
    stmt = (
        select(JobRunEvent)
        .where(
            JobRunEvent.job_run_id == job_run.id,
            JobRunEvent.level == "error",
        )
    )
    result = await db_session.execute(stmt)
    error_events = list(result.scalars().all())
    assert len(error_events) == 1
    assert error_events[0].step == "ERROR"


@pytest.mark.asyncio
async def test_append_event_nonexistent_job_run_id(db_session) -> None:
    """测试 4：append_event 函数不主动校验 job_run_id 存在性（不抛 ValueError 业务异常），
    外键约束由 DB 在 flush 时保证（抛 IntegrityError）。

    设计意图：append_event 只 flush 不 commit，不主动检查 job_run_id 是否存在，
    调用方应确保 job_run_id 有效；DB 外键 ON DELETE CASCADE 保证引用完整性。
    """
    fake_id = uuid.uuid4()
    # 函数本身不抛 ValueError/RuntimeError 等业务异常，直接 flush 触发 DB 外键约束
    with pytest.raises(IntegrityError):
        await append_event(
            db=db_session,
            job_run_id=fake_id,
            step="START",
            message="测试不存在 job_run_id",
        )


@pytest.mark.asyncio
async def test_append_event_default_level_and_message(db_session) -> None:
    """边界：append_event 默认 level=info，message='' 时正常写入。"""
    job_run = await _create_job_run(db_session)

    event = await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="START",
    )
    assert event.level == "info"
    assert event.message == ""
    assert event.payload is None


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
