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


def _line_index_of(source: str, token: str) -> int:
    for i, ln in enumerate(source.splitlines()):
        if token in ln:
            return i
    raise AssertionError(f"token not found in source: {token!r}")


def _indent_of(source: str, token: str) -> int:
    ln = source.splitlines()[_line_index_of(source, token)]
    return len(ln) - len(ln.lstrip())


def test_calendar_seed_import_after_duplicate_check() -> None:
    """PANJI-GOV-W1-R1 回归：calendar_seed import 必须晚于 duplicate 短路。

    duplicate 路径（job_run is None -> return）不得在加载 calendar_seed 依赖树
    之后才命中；且 calendar_seed import 必须位于 try 内，使其 import 异常仍由
    原有 except 路径处理。
    """
    from app.services.calendar_scheduler_worker_runtime import (
        run_calendar_scheduler_worker_runtime,
    )

    source = inspect.getsource(run_calendar_scheduler_worker_runtime)
    seed_token = "from app.services.calendar_seed import seed_calendar_from_mootdx"
    dup_token = "if job_run is None:"
    try_token = "try:"

    seed_idx = _line_index_of(source, seed_token)
    dup_idx = _line_index_of(source, dup_token)

    # 1) import 必须发生在 duplicate 检查之后（源码顺序）。
    assert seed_idx > dup_idx, (
        "calendar_seed import 必须位于 duplicate 检查之后，"
        "否则 duplicate short-circuit 会先于该 import 命中。"
    )
    # 2) import 必须嵌套在 try/with 内（缩进深于 try:），import 异常仍由 except 处理。
    assert _indent_of(source, seed_token) > _indent_of(source, try_token), (
        "calendar_seed import 必须位于 try 内，否则其 import 异常不在原有 except 路径中。"
    )
