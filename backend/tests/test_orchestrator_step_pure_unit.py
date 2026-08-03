"""盘后编排 step 执行器的纯单元测试。

不依赖数据库或外部服务，全部使用 AsyncMock。本文件与 test_after_close_worker.py
（依赖 Postgres）分离，避免 conftest 的源扫描把整个文件误分类为 postgres
而把无 DB 依赖的测试 skip 掉。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

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
    assert summary["status"] == "skipped_unavailable"
    assert summary["optional"] is True
    assert summary["error_code"] in {"STEP_TIMEOUT", "STEP_UNAVAILABLE"}
