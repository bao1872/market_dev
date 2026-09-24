"""Worker 幂等测试 - 验证 _create_job_run 幂等版本与调用点行为。

覆盖：
- bars_scheduler 同一 business_date 第二次调用返回 None（SKIPPED_DUPLICATE）
- monitor_scheduler 同一 session_label 第二次调用返回 None，exclusive-owner 调用方必须跳过，不得查询复用 active row
- 不同 business_date 互不影响
- 边界：不传 run_key 时保持原行为（向后兼容）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session fixture，事务性回滚）
注意：
- _create_job_run 内部调用 acquire_job_run_lock（Phase 2 起调用 recover_stale_scheduler_job_runs，
  使用 jsonb_set），必须用 PostgreSQL 测试库，不能用 SQLite
- _create_job_run 内部 db.commit()，用 patch.object 替换为 flush 避免破坏 fixture nested 事务
"""

from __future__ import annotations

import uuid
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.monitor_scheduler_worker_runtime import (
    _find_or_create_monitor_session_job_run,
)
from app.worker import _create_job_run


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
async def test_monitor_scheduler_session_exclusive_ownership(db_session) -> None:
    """C2C: 同一 session_label 第二次调用返回 None（已被其他进程占有）→ 调用方 SKIP。

    旧合同「第二次 create 返回 None → 调用方按 run_key 查询复用」是错误语义，已被废除：
    exclusive-owner 模型下运行中的 row 只由其 owner 写入，第二个进程不得 SELECT 复用 / 接管。
    """
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    # 用 uuid 后缀避免与其他测试的 monitor_scheduler:2026-06-24:morning 冲突
    session_label = f"morning_{uuid.uuid4().hex[:6]}"
    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    # 第一次：创建 session
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _find_or_create_monitor_session_job_run(
            db_session, now, str(trade_date), session_label,
            create_job_run=_create_job_run,
        )
    assert job_run_1 is not None
    assert job_run_1.run_key == f"monitor_scheduler:2026-06-24:{session_label}"

    # 第二次：session 已被其他进程占有 → create 返回 None（exclusive owner）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _find_or_create_monitor_session_job_run(
            db_session, now, str(trade_date), session_label,
            create_job_run=_create_job_run,
        )
    assert job_run_2 is None

    # 验证数据库里只有一条该 run_key 的 running 记录（owner 未被第二个进程接管）
    run_key = f"monitor_scheduler:{trade_date}:{session_label}"
    stmt = select(SchedulerJobRun).where(SchedulerJobRun.run_key == run_key).limit(1)
    result = await db_session.execute(stmt)
    running = result.scalar_one_or_none()
    assert running is not None
    assert running.id == job_run_1.id
    assert running.status == "running"
    assert running.worker_instance_id == job_run_1.worker_instance_id


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
async def test_board_sync_scheduler_skipped_duplicate(db_session) -> None:
    """board_sync_scheduler 同一 business_date 第二次调用应返回 None（SKIPPED_DUPLICATE）。"""
    run_key = f"board_sync:test:{uuid.uuid4().hex[:8]}"

    # 第一次：成功获取
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_1 = await _create_job_run(
            db_session, "board_sync_scheduler", "2026-07-11", run_key=run_key,
        )
    assert job_run_1 is not None
    assert job_run_1.run_key == run_key

    # 第二次：应返回 None（SKIPPED_DUPLICATE）
    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_2 = await _create_job_run(
            db_session, "board_sync_scheduler", "2026-07-11", run_key=run_key,
        )
    assert job_run_2 is None


def test_board_sync_not_in_after_close_orchestrator() -> None:
    """[BOARD-LOCAL-OWNERSHIP-01] board_sync 已迁出 after_close_orchestrator。

    板块/概念同步改由本地手动 `scripts/ops/panji-board-sync` → 生产 importer
    （通过 SSH stdin 传输规范化快照）执行；盘后 DAG 不再包含任何板块同步步骤。
    """
    import inspect

    from app.services.after_close_orchestrator import execute_after_close_run

    source = inspect.getsource(execute_after_close_run)
    assert "syncing_boards" not in source, \
        "after_close_orchestrator 不得再包含 syncing_boards 步骤"
    assert "board_sync" not in source, \
        "after_close_orchestrator 不得再包含 board_sync 逻辑"
    assert "skip_board_sync" not in source, \
        "after_close_orchestrator 不得再包含 skip_board_sync 控制"


def test_board_sync_not_separate_worker_type() -> None:
    """验证 board_sync_scheduler 不再是独立 WORKER_TYPE（合并进 bars_scheduler）。"""
    import inspect

    from app.worker import main

    source = inspect.getsource(main)
    assert "run_board_sync_scheduler_worker" not in source, \
        "main() 不应再调用已移除的 run_board_sync_scheduler_worker"
    assert '"board_sync_scheduler"' not in source, \
        "main() 不应再包含 board_sync_scheduler 分支"

    # 确认独立函数已删除
    import app.worker as worker_module
    assert not hasattr(worker_module, "run_board_sync_scheduler_worker"), \
        "run_board_sync_scheduler_worker 函数应已删除"


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
            create_job_run=_create_job_run,
        )
    assert job_run_morning is not None

    with patch.object(db_session, "commit", new=db_session.flush):
        job_run_afternoon = await _find_or_create_monitor_session_job_run(
            db_session, afternoon, str(trade_date), afternoon_label,
            create_job_run=_create_job_run,
        )
    assert job_run_afternoon is not None

    assert job_run_morning.id != job_run_afternoon.id
    assert job_run_morning.run_key == f"monitor_scheduler:2026-06-24:{morning_label}"
    assert job_run_afternoon.run_key == f"monitor_scheduler:2026-06-24:{afternoon_label}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
