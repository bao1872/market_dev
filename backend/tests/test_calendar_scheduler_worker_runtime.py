"""PANJI-GOV-W1: Calendar Scheduler runtime extraction verification (pure unit).

These tests verify only the structural contract of the mechanical extraction
(behavior unchanged).  They do NOT connect to a database.
"""

from __future__ import annotations

import inspect


def test_runtime_function_importable() -> None:
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    assert callable(run_calendar_scheduler_worker_runtime)
    assert inspect.iscoroutinefunction(run_calendar_scheduler_worker_runtime)


def test_facade_delegates_to_runtime() -> None:
    from app.worker import run_calendar_scheduler_worker

    source = inspect.getsource(run_calendar_scheduler_worker)
    # 兼容 façade 不重新实现 lifecycle，只注入依赖并委托给 runtime。
    assert "run_calendar_scheduler_worker_runtime" in source
    assert "AsyncIOScheduler" not in source
    assert "CronTrigger" not in source


def test_runtime_preserves_calendar_contract() -> None:
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    source = inspect.getsource(run_calendar_scheduler_worker_runtime)
    # cron: 每日 02:00 Asia/Shanghai
    assert 'CronTrigger(hour=2, minute=0, timezone=ZoneInfo("Asia/Shanghai"))' in source
    # job identity 不变
    assert 'id="calendar_scheduler"' in source
    assert 'name="calendar_scheduler"' in source
    assert 'run_key=f"calendar_scheduler:{today}"' in source
    # 复用既有 job-run 状态规则（不重新实现第二套）
    assert "create_job_run(" in source
    assert "finish_job_run(" in source
    assert "recover_stale_job_runs(" in source
    # 调用既有业务 owner
    assert "seed_calendar_from_mootdx" in source
    assert "shanghai_business_date" in source


def test_facade_injects_canonical_helpers() -> None:
    from app.worker import run_calendar_scheduler_worker

    source = inspect.getsource(run_calendar_scheduler_worker)
    # 注入既有 canonical 协作者，不产生第二套状态 owner / service locator。
    assert "AsyncSessionLocal" in source
    assert "_heartbeat_loop" in source
    assert "_create_job_run" in source
    assert "_finish_job_run" in source
    assert "recover_stale_scheduler_job_runs" in source
