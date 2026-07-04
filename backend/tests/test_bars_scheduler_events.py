"""bars_scheduler 事件写入测试 - 验证 DAILY_DONE / DSA_CREATED / 覆盖率填充。

覆盖：
- _append_daily_done_event 写入 DAILY_DONE 事件（含覆盖率 payload）
- _check_daily_coverage_and_trigger_dsa 覆盖率不足时填充 BatchResult 但不写 DSA_CREATED
- 模拟 worker START / ERROR 事件写入
- DSA_CREATED 事件写入（模拟 _check_daily_coverage_and_trigger_dsa 达标场景）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：测试中不调用 db_session.commit()，避免破坏 fixture 的 nested 事务；
      append_event 只 flush 不 commit，数据在 session 内可见，list_events 可查询。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.models.instrument import Instrument
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.bars_scheduler_service import BarsSchedulerService, BatchResult
from app.services.job_run_event_service import append_event, list_events


async def _create_job_run(db_session) -> SchedulerJobRun:
    """创建测试用 SchedulerJobRun（满足外键约束）。"""
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date="2026-06-25",
        run_key=f"bars_scheduler:test:{uuid.uuid4().hex[:8]}",
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
async def test_daily_done_event_payload(db_session) -> None:
    """测试 1：DAILY_DONE 事件写入，payload 含覆盖率（模拟 _append_daily_done_event 逻辑）。"""
    job_run = await _create_job_run(db_session)

    # 模拟 _append_daily_done_event 内部调用 append_event（不 commit，仅 flush）
    covered, total, coverage = 9, 10, 0.9
    await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="DAILY_DONE",
        level="info",
        message=f"日线覆盖 {covered}/{total} = {coverage:.1%}",
        payload={"covered": covered, "total": total, "coverage": coverage},
    )

    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 1
    event = events[0]
    assert event.step == "DAILY_DONE"
    assert event.level == "info"
    assert "9/10" in event.message
    assert "90.0%" in event.message
    assert event.payload["covered"] == 9
    assert event.payload["total"] == 10
    assert event.payload["coverage"] == 0.9


@pytest.mark.asyncio
async def test_dsa_created_event(db_session) -> None:
    """测试 2：DSA_CREATED 事件写入（模拟 _check_daily_coverage_and_trigger_dsa 达标场景）。

    直接调用 append_event 写入 DSA_CREATED，验证事件结构与 payload。
    """
    job_run = await _create_job_run(db_session)
    fake_run_id = uuid.uuid4()

    # 模拟 _check_daily_coverage_and_trigger_dsa 内部 DSA 触发后写入事件
    await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="DSA_CREATED",
        level="info",
        message=f"DSA 选股已触发: run_id={fake_run_id}",
        payload={"run_id": str(fake_run_id)},
    )

    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 1
    event = events[0]
    assert event.step == "DSA_CREATED"
    assert event.level == "info"
    assert str(fake_run_id) in event.message
    assert event.payload["run_id"] == str(fake_run_id)


@pytest.mark.asyncio
async def test_coverage_below_threshold_fills_result(db_session) -> None:
    """测试 3：覆盖率 < 90% 时 _check_daily_coverage_and_trigger_dsa 填充 BatchResult 并写 COVERAGE_INSUFFICIENT。

    使用独特未来日期（无 BarDaily 数据），覆盖率 0% < 90%。
    该路径会写 COVERAGE_INSUFFICIENT warn 事件并调用 db.commit()；
    为避免破坏 db_session fixture 的 nested 事务隔离，将 commit 替换为 flush。
    """
    job_run = await _create_job_run(db_session)
    # 使用独特未来日期避免与其他测试数据冲突
    trade_date = date(2099, 11, 30)

    # 创建 active instruments，确保 BarsCoverageService 分母 > 0
    for i in range(5):
        db_session.add(
            Instrument(
                id=uuid.uuid4(),
                symbol=f"{600000 + i:06d}",
                name=f"测试标的{i}",
                market="SH",
                status="active",
            )
        )
    await db_session.flush()

    service = BarsSchedulerService()
    result = BatchResult(total=10)

    # [Test] - 覆盖率不足路径会 db.commit()，用 flush 替换以保持 nested 事务隔离
    with patch.object(db_session, "commit", new=db_session.flush):
        dsa_run_id = await service._check_daily_coverage_and_trigger_dsa(
            trade_date=trade_date,
            db_session=db_session,
            job_run_id=job_run.id,
            result=result,
        )

    assert dsa_run_id is None
    # 覆盖率字段仍被填充（0% 因为该日期无 BarDaily 数据）
    assert result.daily_covered == 0
    assert result.daily_total is not None
    assert result.daily_total > 0  # 测试库应有 active 标的
    assert result.daily_coverage is not None
    assert result.daily_coverage < 0.9

    events = await list_events(db_session, job_run.id, limit=10)
    steps = [e.step for e in events]
    # 不应有 DSA_CREATED 事件（覆盖率不足）
    assert "DSA_CREATED" not in steps
    # 应有 COVERAGE_INSUFFICIENT warn 事件（新增的诊断事件）
    assert "COVERAGE_INSUFFICIENT" in steps
    cov_event = next(e for e in events if e.step == "COVERAGE_INSUFFICIENT")
    assert cov_event.level == "warn"
    assert cov_event.payload["coverage"] == result.daily_coverage


@pytest.mark.asyncio
async def test_worker_start_event(db_session) -> None:
    """测试 4：模拟 worker 写入 START 事件。"""
    job_run = await _create_job_run(db_session)

    # 模拟 worker.py scheduled_bars_refresh 的 START 事件写入
    await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="START",
        level="info",
        message="开始更新日线",
    )

    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 1
    assert events[0].step == "START"
    assert events[0].level == "info"
    assert events[0].message == "开始更新日线"


@pytest.mark.asyncio
async def test_worker_error_event(db_session) -> None:
    """测试 5：模拟 worker 异常时写入 ERROR 事件（含 traceback payload）。"""
    job_run = await _create_job_run(db_session)

    exc = RuntimeError("pytdx 连接超时")
    import traceback as tb_mod
    await append_event(
        db=db_session,
        job_run_id=job_run.id,
        step="ERROR",
        level="error",
        message=str(exc)[:500],
        payload={
            "traceback": tb_mod.format_exc()[:4000],
            "error_type": type(exc).__name__,
        },
    )

    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 1
    assert events[0].step == "ERROR"
    assert events[0].level == "error"
    assert events[0].message == "pytdx 连接超时"
    assert events[0].payload["error_type"] == "RuntimeError"
    assert "traceback" in events[0].payload


@pytest.mark.asyncio
async def test_event_timeline_ordering(db_session) -> None:
    """测试 6：多条事件按 created_at 倒序返回（模拟完整时间线）。

    依次写入 START → DAILY_DONE → DSA_CREATED，验证 list_events 倒序返回。
    """
    job_run = await _create_job_run(db_session)

    e1 = await append_event(db_session, job_run.id, "START", message="开始")
    await db_session.flush()
    e1.created_at = datetime(2026, 6, 25, 16, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e2 = await append_event(db_session, job_run.id, "DAILY_DONE", message="日线完成")
    await db_session.flush()
    e2.created_at = datetime(2026, 6, 25, 17, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    e3 = await append_event(db_session, job_run.id, "DSA_CREATED", message="DSA 创建")
    await db_session.flush()
    e3.created_at = datetime(2026, 6, 25, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    await db_session.flush()

    events = await list_events(db_session, job_run.id, limit=10)
    assert len(events) == 3
    # 倒序：最新（18:00）在前
    assert events[0].step == "DSA_CREATED"
    assert events[1].step == "DAILY_DONE"
    assert events[2].step == "START"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
