"""盘后编排 step 执行器的纯单元测试。

不依赖数据库或外部服务，全部使用 AsyncMock。本文件与 test_after_close_worker.py
（依赖 Postgres）分离，避免 conftest 的源扫描把整个文件误分类为 postgres
而把无 DB 依赖的测试 skip 掉。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services import after_close_orchestrator
from app.services.after_close_orchestrator import (
    StepUnavailableError,
    execute_orchestrator_step,
)


@pytest.mark.asyncio
async def test_step_executor_success_stops_heartbeat_and_reports_progress():
    heartbeats = AsyncMock()
    progress = AsyncMock()

    result, summary = await execute_orchestrator_step(
        "example", lambda: asyncio.sleep(0, result={"processed": 2, "total": 3}),
        heartbeat=heartbeats, progress=progress,
    )

    assert result == {"processed": 2, "total": 3}
    assert summary["status"] == "succeeded"
    assert summary["processed"] == 2
    assert summary["finished_at"] is not None
    assert progress.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["timeout", "unavailable"])
async def test_auction_anchor_optional_unavailable_is_non_blocking(mode: str):
    async def operation():
        if mode == "timeout":
            await asyncio.sleep(0.02)
            return {"processed": 1}
        raise StepUnavailableError("no auction data")

    result, summary = await execute_orchestrator_step(
        "auction_anchor", operation, timeout_seconds=0.001, optional=True,
    )

    assert result is None
    # [Step Contract 2026-08-03] 原 "skipped_unavailable" 组合态已废弃：
    # 可选步骤无数据 → unavailable；可选步骤超时 → timed_out（调用方降级为 skipped）。
    if mode == "unavailable":
        assert summary["status"] == "unavailable"
        assert summary["error_code"] == "STEP_UNAVAILABLE"
    else:
        assert summary["status"] == "timed_out"
        assert summary["error_code"] == "STEP_TIMEOUT"
    assert summary["optional"] is True


# ---------------------------------------------------------------------------
# [Phase0] 行为测试：运行中取消 / 心跳 / 运行期 elapsed / 非可选超时
# 这些是 Phase 0 验收门的核心，全部为真实行为断言（不使用 inspect.getsource）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mid_run_cancel_actually_stops_operation(monkeypatch):
    """[Gate] mid_run_cancel_verified：运行中取消必须真正终止业务协程。

    operation 是一个长循环；取消信号在运行途中出现。
    断言：operation 被 cancel（未跑完全程），且 summary.status=cancelled。
    """
    monkeypatch.setattr(
        after_close_orchestrator, "_CANCEL_POLL_INTERVAL_SECONDS", 0.01,
    )

    progressed: list[int] = []
    completed = False

    async def long_operation():
        nonlocal completed
        for i in range(200):
            await asyncio.sleep(0.005)
            progressed.append(i)
        completed = True
        return {"processed": len(progressed)}

    calls = {"n": 0}

    async def cancellation_check() -> bool:
        # 前 2 次未取消，之后返回已取消（模拟管理员运行中点取消）
        calls["n"] += 1
        return calls["n"] > 2

    result, summary = await execute_orchestrator_step(
        "computing_features",
        long_operation,
        timeout_seconds=30,
        cancellation_check=cancellation_check,
        poll_interval=0.01,
    )

    assert summary["status"] == "cancelled"
    assert result is None
    # 关键断言：业务协程被真正终止，没有跑完
    assert completed is False
    assert len(progressed) < 200
    # 关键断言：cancellation_check 在运行期间被周期性调用（不只开始前一次）
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_cancellation_check_polled_periodically_during_run():
    """[Gate] cancellation_check 必须在运行期间被多次调用。"""
    calls = {"n": 0}

    async def cancellation_check() -> bool:
        calls["n"] += 1
        return False

    async def operation():
        await asyncio.sleep(0.12)
        return {"ok": True}

    result, summary = await execute_orchestrator_step(
        "example", operation, timeout_seconds=30,
        cancellation_check=cancellation_check, poll_interval=0.02,
    )

    assert summary["status"] == "succeeded"
    assert result == {"ok": True}
    # 开始前 1 次 + 运行期间多次
    assert calls["n"] >= 4, f"cancellation_check 仅被调用 {calls['n']} 次，未周期轮询"


@pytest.mark.asyncio
async def test_heartbeat_and_running_elapsed_update_during_long_step(monkeypatch):
    """[Gate] heartbeat_updates_during_long_step + running_elapsed_updates。

    断言运行期间：
    - heartbeat 被多次单次 touch（而非传入一个无限循环）；
    - progress 回调在 running 状态下上报递增的 elapsed_seconds。
    """
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.02,
    )

    heartbeat_calls = {"n": 0}

    async def heartbeat() -> None:
        heartbeat_calls["n"] += 1

    running_elapsed: list[float] = []

    async def progress(summary: dict) -> None:
        if summary["status"] == "running":
            running_elapsed.append(summary["elapsed_seconds"])

    async def operation():
        await asyncio.sleep(0.15)
        return {"ok": True}

    _result, summary = await execute_orchestrator_step(
        "computing_features", operation, timeout_seconds=30,
        heartbeat=heartbeat, progress=progress,
    )

    assert summary["status"] == "succeeded"
    # 心跳在长步骤运行期间被多次调用
    assert heartbeat_calls["n"] >= 3, f"心跳仅 {heartbeat_calls['n']} 次"
    # 运行期间上报了多次 running 进度，且 elapsed 单调递增（不再恒为 None）
    assert len(running_elapsed) >= 3, f"running 进度仅 {len(running_elapsed)} 次"
    assert all(e is not None for e in running_elapsed)
    assert running_elapsed == sorted(running_elapsed)
    assert running_elapsed[-1] > running_elapsed[0]


@pytest.mark.asyncio
async def test_running_summary_carries_timeout_seconds_for_watchdog():
    """[Gate] step_timeout_can_trigger：running 期间 summary 必须带 timeout_seconds。

    watchdog 依据 running + elapsed_seconds > timeout 判定 step_timed_out，
    因此运行期上报必须同时具备这两个字段。
    """
    seen: list[dict] = []

    async def progress(summary: dict) -> None:
        seen.append(dict(summary))

    await execute_orchestrator_step(
        "checking_coverage", lambda: asyncio.sleep(0, result={"ok": True}),
        timeout_seconds=300, progress=progress,
    )

    first = seen[0]
    assert first["status"] == "running"
    assert first["timeout_seconds"] == 300
    assert first["elapsed_seconds"] is not None


@pytest.mark.asyncio
async def test_non_optional_timeout_raises_and_marks_timed_out():
    """[Gate] 非可选步骤超时必须抛出，不得被静默降级。"""
    async def slow_operation():
        await asyncio.sleep(5)
        return {"ok": True}

    with pytest.raises(asyncio.TimeoutError):
        await execute_orchestrator_step(
            "publishing", slow_operation, timeout_seconds=0.02, optional=False,
        )


@pytest.mark.asyncio
async def test_timeout_cancels_operation_task():
    """超时后 operation task 必须被 cancel，不得成为脱缰的后台写入。"""
    completed = False

    async def slow_operation():
        nonlocal completed
        await asyncio.sleep(5)
        completed = True

    async def never_cancelled() -> bool:
        return False

    _result, summary = await execute_orchestrator_step(
        "auction_anchor", slow_operation, timeout_seconds=0.03,
        optional=True, cancellation_check=never_cancelled, poll_interval=0.01,
    )

    assert summary["status"] == "timed_out"
    await asyncio.sleep(0.05)
    assert completed is False


# ---------------------------------------------------------------------------
# [Phase 4D.4] Long-running business step liveness contract（PRD 31 PC-43 /
# rules/80 §13.1）：workload-variant long-running step 不得仅因 fixed generic
# absolute elapsed 被判失败；其失败条件只限 execution failure / no-progress /
# business deadline。refreshing_daily 的 absolute timeout 已置为 None。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_running_step_without_absolute_timeout_completes(monkeypatch):
    """[A/B] refreshing_daily 类长任务：超过旧 3600s 但持续 progress，不被判 STEP_TIMEOUT。

    直接验证 execute_orchestrator_step 在 timeout_seconds=None 时不会因总耗时过长
    而失败；operation 耗时远超历史 3600s 常量仍可 succeeded。
    """
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.01,
    )

    async def long_operation():
        # 模拟远超旧 3600s 绝对上限的长任务（此处压缩为 0.3s，但语义等价）
        await asyncio.sleep(0.3)
        return {"processed": 5000, "total": 5293}

    result, summary = await execute_orchestrator_step(
        "refreshing_daily",
        long_operation,
        timeout_seconds=None,  # [Phase 4D.4] 无 absolute 上限
        heartbeat=AsyncMock(),
        progress=AsyncMock(),
        cancellation_check=AsyncMock(return_value=False),
        poll_interval=0.01,
    )

    assert summary["status"] == "succeeded"
    assert result == {"processed": 5000, "total": 5293}
    assert summary["timeout_seconds"] is None


@pytest.mark.asyncio
async def test_step_timeout_seconds_none_reported_in_summary(monkeypatch):
    """[A] refreshing_daily 的 running summary 必须带 timeout_seconds=None，
    watchdog 据此知道该步骤不由 absolute elapsed 判定超时。
    """
    seen: list[dict] = []

    async def progress(summary: dict) -> None:
        seen.append(dict(summary))

    await execute_orchestrator_step(
        "refreshing_daily", lambda: asyncio.sleep(0, result={"ok": True}),
        timeout_seconds=None, progress=progress,
    )

    first = seen[0]
    assert first["status"] == "running"
    assert first["timeout_seconds"] is None


@pytest.mark.asyncio
async def test_long_running_step_explicit_exception_is_failed(monkeypatch):
    """[C] 长任务明确 execution exception → failed（不被 absolute timeout 掩盖，也不被忽略）。"""
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.01,
    )

    async def broken_operation():
        await asyncio.sleep(0.05)
        raise RuntimeError("provider connection refused")

    result, summary = await execute_orchestrator_step(
        "refreshing_daily",
        broken_operation,
        timeout_seconds=None,
        optional=True,  # 避免 failed 后重新 raise，验证 summary 状态即可
        heartbeat=AsyncMock(),
        progress=AsyncMock(),
        cancellation_check=AsyncMock(return_value=False),
        poll_interval=0.01,
    )

    assert summary["status"] == "failed"
    assert summary["error_code"] == "RuntimeError"
    assert result is None


@pytest.mark.asyncio
async def test_short_step_absolute_timeout_contract_unchanged(monkeypatch):
    """[E] 其他 short step 原 absolute timeout 合同不受影响（如 checking_coverage=300）。"""
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.01,
    )

    async def slow_operation():
        await asyncio.sleep(5)
        return {"ok": True}

    with pytest.raises(asyncio.TimeoutError):
        await execute_orchestrator_step(
            "checking_coverage", slow_operation, timeout_seconds=0.02, optional=False,
        )


@pytest.mark.asyncio
async def test_step_timeout_none_disables_absolute_deadline_not_cancellation(monkeypatch):
    """[A/F] timeout=None 不削弱协作取消：cancellation_check 仍生效，且不设 absolute deadline。"""
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.01,
    )
    monkeypatch.setattr(
        after_close_orchestrator, "_CANCEL_POLL_INTERVAL_SECONDS", 0.01,
    )

    progressed: list[int] = []
    completed = False

    async def long_operation():
        nonlocal completed
        for i in range(200):
            await asyncio.sleep(0.003)
            progressed.append(i)
        completed = True
        return {"processed": len(progressed)}

    calls = {"n": 0}

    async def cancellation_check() -> bool:
        calls["n"] += 1
        return calls["n"] > 2  # 运行中段取消

    result, summary = await execute_orchestrator_step(
        "refreshing_daily",
        long_operation,
        timeout_seconds=None,  # 无 absolute 上限，但协作取消仍工作
        cancellation_check=cancellation_check,
        poll_interval=0.01,
    )

    assert summary["status"] == "cancelled"
    assert completed is False
    assert len(progressed) < 200
    assert calls["n"] >= 2


@pytest.mark.asyncio
async def test_tick_loop_does_not_falsify_last_progress_at(monkeypatch):
    """[§22 false-liveness] tick 循环不得把 heartbeat 时间冒充 last_progress_at。

    运行期间 last_progress_at 应保持为 started_at（无业务 progress 注入），
    直到 finally 更新为 finished_at；heartbeat_at 独立推进。
    """
    monkeypatch.setattr(
        after_close_orchestrator, "_HEARTBEAT_INTERVAL_SECONDS", 0.01,
    )

    heartbeats = {"n": 0}
    running_snapshots: list[dict] = []

    async def heartbeat() -> None:
        heartbeats["n"] += 1

    async def progress(summary: dict) -> None:
        if summary["status"] == "running":
            running_snapshots.append(dict(summary))

    started_iso = None

    async def operation():
        nonlocal started_iso
        # 第一步就抓 started_at 的 last_progress_at
        started_iso = running_snapshots[0]["last_progress_at"] if running_snapshots else None
        await asyncio.sleep(0.12)
        return {"ok": True}

    _result, summary = await execute_orchestrator_step(
        "refreshing_daily", operation, timeout_seconds=None,
        heartbeat=heartbeat, progress=progress, poll_interval=0.01,
    )

    assert summary["status"] == "succeeded"
    assert heartbeats["n"] >= 3  # 心跳确实在刷新
    # last_progress_at 在运行期间未被 heartbeat 时间覆盖（保持 started_at）
    for snap in running_snapshots:
        assert snap["last_progress_at"] == snap["started_at"], (
            "tick 把 heartbeat 时间冒充了 last_progress_at（false-liveness）"
        )
    # finally 把 last_progress_at 推进到 finished_at
    assert summary["last_progress_at"] == summary["finished_at"]
