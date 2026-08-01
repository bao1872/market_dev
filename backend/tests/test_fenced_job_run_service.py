"""Pure-unit contracts for fenced scheduler job heartbeats."""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.fenced_job_run_service import (
    FencedJobHeartbeat,
    FencedJobToken,
    JobLeaseLostError,
)


def _token() -> FencedJobToken:
    return FencedJobToken(
        job_run_id=uuid.uuid4(),
        worker_instance_id="worker:test",
        lease_epoch=3,
        lease_seconds=90,
    )


@pytest.mark.asyncio
async def test_heartbeat_keeps_refreshing_past_watchdog_window() -> None:
    calls = 0

    async def refresh(_token: FencedJobToken) -> bool:
        nonlocal calls
        calls += 1
        return True

    heartbeat = FencedJobHeartbeat(
        _token(), interval_seconds=0.005, refresh=refresh,
    )
    await heartbeat.start()
    await asyncio.sleep(0.021)
    heartbeat.ensure_owned()
    await heartbeat.stop()

    assert calls >= 3
    assert heartbeat.task is None


@pytest.mark.asyncio
async def test_failed_heartbeat_marks_lease_lost_and_task_stops() -> None:
    async def refresh(_token: FencedJobToken) -> bool:
        return False

    heartbeat = FencedJobHeartbeat(
        _token(), interval_seconds=0.001, refresh=refresh,
    )
    await heartbeat.start()
    await asyncio.sleep(0.01)

    with pytest.raises(JobLeaseLostError):
        heartbeat.ensure_owned()
    assert heartbeat.task is not None
    assert heartbeat.task.done()
    await heartbeat.stop()
    assert heartbeat.task is None


@pytest.mark.asyncio
async def test_heartbeat_is_cancelled_on_exception_path() -> None:
    blocker = asyncio.Event()

    async def refresh(_token: FencedJobToken) -> bool:
        await blocker.wait()
        return True

    heartbeat = FencedJobHeartbeat(
        _token(), interval_seconds=0.001, refresh=refresh,
    )
    await heartbeat.start()
    await asyncio.sleep(0.005)
    await heartbeat.stop()

    assert heartbeat.task is None
    assert not heartbeat.lost
