"""Worker 幂等测试 - 验证 _create_job_run 幂等版本与调用点行为。

覆盖：
- bars_scheduler 同一 business_date 第二次调用返回 None（SKIPPED_DUPLICATE）
- monitor_scheduler 同一 session_label 第二次调用返回 None，调用方能按 run_key 查询复用
- 不同 business_date 互不影响
- 边界：不传 run_key 时保持原行为（向后兼容）

测试环境：SQLite 内存数据库（跳过 pg_advisory_xact_lock，仅依赖唯一约束）
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.scheduler_job_run import SchedulerJobRun
from app.worker import _create_job_run, _find_or_create_monitor_session_job_run


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
async def test_bars_scheduler_skipped_duplicate(test_db) -> None:
    """同一 business_date 第二次调用 _create_job_run(run_key=...) 应返回 None。"""
    run_key = "bars_scheduler:2026-06-25"

    # 第一次：成功获取
    job_run_1 = await _create_job_run(
        test_db, "bars_scheduler", "2026-06-25", run_key=run_key,
    )
    assert job_run_1 is not None
    assert job_run_1.run_key == run_key

    # 第二次：应返回 None（SKIPPED_DUPLICATE）
    job_run_2 = await _create_job_run(
        test_db, "bars_scheduler", "2026-06-25", run_key=run_key,
    )
    assert job_run_2 is None


@pytest.mark.asyncio
async def test_monitor_scheduler_session_reuse(test_db) -> None:
    """同一 session_label 第二次调用返回 None，调用方能按 run_key 查询复用。"""
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    # 第一次：创建 morning session
    job_run_1 = await _find_or_create_monitor_session_job_run(
        test_db, now, str(trade_date), "morning",
    )
    await test_db.commit()
    assert job_run_1 is not None
    assert job_run_1.run_key == "monitor_scheduler:2026-06-24:morning"

    # 第二次：返回 None（session 已存在）
    job_run_2 = await _find_or_create_monitor_session_job_run(
        test_db, now, str(trade_date), "morning",
    )
    assert job_run_2 is None

    # 调用方按 run_key 查询复用（模拟 run_monitor_scheduler_worker 的复用逻辑）
    run_key = f"monitor_scheduler:{trade_date}:morning"
    stmt = select(SchedulerJobRun).where(SchedulerJobRun.run_key == run_key).limit(1)
    result = await test_db.execute(stmt)
    reused = result.scalar_one_or_none()
    assert reused is not None
    assert reused.id == job_run_1.id
    # 验证 metadata 中的 session_label
    meta = json.loads(reused.metadata_json or "{}")
    assert meta.get("session_label") == "morning"


@pytest.mark.asyncio
async def test_different_business_dates_both_succeed(test_db) -> None:
    """不同 business_date 互不影响，均能成功创建 job_run。"""
    # 第一天
    job_run_1 = await _create_job_run(
        test_db, "bars_scheduler", "2026-06-24",
        run_key="bars_scheduler:2026-06-24",
    )
    await test_db.commit()
    assert job_run_1 is not None

    # 第二天（不同 run_key）
    job_run_2 = await _create_job_run(
        test_db, "bars_scheduler", "2026-06-25",
        run_key="bars_scheduler:2026-06-25",
    )
    await test_db.commit()
    assert job_run_2 is not None

    assert job_run_1.id != job_run_2.id
    assert job_run_1.run_key == "bars_scheduler:2026-06-24"
    assert job_run_2.run_key == "bars_scheduler:2026-06-25"


@pytest.mark.asyncio
async def test_backward_compatible_no_run_key(test_db) -> None:
    """不传 run_key 时保持原行为：直接 INSERT，永远返回 SchedulerJobRun（非 None）。"""
    # 不传 run_key，应走向后兼容路径
    job_run_1 = await _create_job_run(test_db, "test_job", "2026-06-25")
    assert job_run_1 is not None
    assert job_run_1.run_key is None  # 未设置 run_key

    # 同一 business_date 再次调用，不传 run_key 也能成功（无幂等保护）
    job_run_2 = await _create_job_run(test_db, "test_job", "2026-06-25")
    assert job_run_2 is not None
    assert job_run_2.id != job_run_1.id


@pytest.mark.asyncio
async def test_monitor_scheduler_different_sessions_both_succeed(test_db) -> None:
    """边界：上午和下午 session 互不影响，均能成功创建。"""
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    morning = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    afternoon = datetime(2026, 6, 24, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    job_run_morning = await _find_or_create_monitor_session_job_run(
        test_db, morning, str(trade_date), "morning",
    )
    await test_db.commit()
    assert job_run_morning is not None

    job_run_afternoon = await _find_or_create_monitor_session_job_run(
        test_db, afternoon, str(trade_date), "afternoon",
    )
    await test_db.commit()
    assert job_run_afternoon is not None

    assert job_run_morning.id != job_run_afternoon.id
    assert job_run_morning.run_key == "monitor_scheduler:2026-06-24:morning"
    assert job_run_afternoon.run_key == "monitor_scheduler:2026-06-24:afternoon"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
