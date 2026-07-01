"""Worker 幂等测试 - 验证 _create_job_run 幂等版本与调用点行为。

覆盖：
- bars_scheduler 同一 business_date 第二次调用返回 None（SKIPPED_DUPLICATE）
- monitor_scheduler 同一 session_label 第二次调用返回 None，调用方能按 run_key 查询复用
- 不同 business_date 互不影响
- 边界：不传 run_key 时保持原行为（向后兼容）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：
- _create_job_run 内部调用 acquire_job_run_lock（Phase 2 起调用 recover_stale_scheduler_job_runs，
  使用 jsonb_set），必须用 PostgreSQL 测试库，不能用 SQLite
- _create_job_run 内部 db.commit()，用 patch.object 替换为 flush 避免破坏 fixture nested 事务
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.scheduler_job_run import SchedulerJobRun
from app.worker import _create_job_run, _find_or_create_monitor_session_job_run


@pytest.mark.asyncio
async def test_bars_scheduler_skipped_duplicate(db_session) -> None:
    """同一 business_date 第二次调用 _create_job_run(run_key=...) 应返回 None。"""
    run_key = f"bars_scheduler:test:{uuid.uuid4().hex[:8]}"

    # 第一次：成功获取（patch commit 为 flush 避免破坏 fixture nested 事务）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _create_job_run(
            db_session, "bars_scheduler", "2026-06-25", run_key=run_key,
        )
    assert job_run_1 is not None
    assert job_run_1.run_key == run_key

    # 第二次：应返回 None（SKIPPED_DUPLICATE）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _create_job_run(
            db_session, "bars_scheduler", "2026-06-25", run_key=run_key,
        )
    assert job_run_2 is None


@pytest.mark.asyncio
async def test_monitor_scheduler_session_reuse(db_session) -> None:
    """同一 session_label 第二次调用返回 None，调用方能按 run_key 查询复用。"""
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    # 用 uuid 后缀避免与其他测试的 monitor_scheduler:2026-06-24:morning 冲突
    session_label = f"morning_{uuid.uuid4().hex[:6]}"
    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    # 第一次：创建 session
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _find_or_create_monitor_session_job_run(
            db_session, now, str(trade_date), session_label,
        )
    assert job_run_1 is not None
    assert job_run_1.run_key == f"monitor_scheduler:2026-06-24:{session_label}"

    # 第二次：返回 None（session 已存在）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _find_or_create_monitor_session_job_run(
            db_session, now, str(trade_date), session_label,
        )
    assert job_run_2 is None

    # 调用方按 run_key 查询复用（模拟 run_monitor_scheduler_worker 的复用逻辑）
    run_key = f"monitor_scheduler:{trade_date}:{session_label}"
    stmt = select(SchedulerJobRun).where(SchedulerJobRun.run_key == run_key).limit(1)
    result = await db_session.execute(stmt)
    reused = result.scalar_one_or_none()
    assert reused is not None
    assert reused.id == job_run_1.id
    # 验证 metadata 中的 session_label
    meta = json.loads(reused.metadata_json or "{}")
    assert meta.get("session_label") == session_label


@pytest.mark.asyncio
async def test_different_business_dates_both_succeed(db_session) -> None:
    """不同 business_date 互不影响，均能成功创建 job_run。"""
    run_key_1 = f"bars_scheduler:test:{uuid.uuid4().hex[:8]}"
    run_key_2 = f"bars_scheduler:test:{uuid.uuid4().hex[:8]}"

    # 第一天
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _create_job_run(
            db_session, "bars_scheduler", "2026-06-24",
            run_key=run_key_1,
        )
    assert job_run_1 is not None

    # 第二天（不同 run_key）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _create_job_run(
            db_session, "bars_scheduler", "2026-06-25",
            run_key=run_key_2,
        )
    assert job_run_2 is not None

    assert job_run_1.id != job_run_2.id
    assert job_run_1.run_key == run_key_1
    assert job_run_2.run_key == run_key_2


@pytest.mark.asyncio
async def test_backward_compatible_no_run_key(db_session) -> None:
    """不传 run_key 时保持原行为：直接 INSERT，永远返回 SchedulerJobRun（非 None）。"""
    # 不传 run_key，应走向后兼容路径（不调用 acquire_job_run_lock）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _create_job_run(db_session, "test_job", "2026-06-25")
    assert job_run_1 is not None
    assert job_run_1.run_key is None  # 未设置 run_key

    # 同一 business_date 再次调用，不传 run_key 也能成功（无幂等保护）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _create_job_run(db_session, "test_job", "2026-06-25")
    assert job_run_2 is not None
    assert job_run_2.id != job_run_1.id


@pytest.mark.asyncio
async def test_monitor_scheduler_different_sessions_both_succeed(db_session) -> None:
    """边界：上午和下午 session 互不影响，均能成功创建。"""
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    # 用 uuid 后缀避免与其他测试冲突
    morning_label = f"morning_{uuid.uuid4().hex[:6]}"
    afternoon_label = f"afternoon_{uuid.uuid4().hex[:6]}"
    morning = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    afternoon = datetime(2026, 6, 24, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_morning = await _find_or_create_monitor_session_job_run(
            db_session, morning, str(trade_date), morning_label,
        )
    assert job_run_morning is not None

    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_afternoon = await _find_or_create_monitor_session_job_run(
            db_session, afternoon, str(trade_date), afternoon_label,
        )
    assert job_run_afternoon is not None

    assert job_run_morning.id != job_run_afternoon.id
    assert job_run_morning.run_key == f"monitor_scheduler:2026-06-24:{morning_label}"
    assert job_run_afternoon.run_key == f"monitor_scheduler:2026-06-24:{afternoon_label}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
