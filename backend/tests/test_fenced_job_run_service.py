"""Pure-unit contracts for fenced scheduler job heartbeats."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.services.fenced_job_run_service import (
    FencedJobHeartbeat,
    FencedJobToken,
    JobLeaseLostError,
    finalize_job_run,
    merge_owned_job_run_metadata,
    update_owned_job_run_progress,
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


class _FakeDb:
    """纯单元测试用最小 AsyncSession 替身（带 metadata_json 字段）。"""

    def __init__(self, metadata_json: str | None = None) -> None:
        self.metadata_json = metadata_json
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeDb:
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


@pytest.mark.asyncio
async def test_merge_owned_metadata_merges_and_commits() -> None:
    """owner 有效 → 调 lock_owned_job_run(db, token) 合并 metadata 并 commit。"""
    token = _token()
    fake_db = _FakeDb(metadata_json='{"a": 1}')
    def _factory() -> _FakeDb:
        return fake_db

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=AsyncMock(return_value=fake_db),
    ) as lock:
        await merge_owned_job_run_metadata(
            token, {"chip_run_id": "xxx"}, session_factory=_factory,
        )

    # 必须用 lock_owned_job_run 锁定，而非 db.get(job_run_id)
    lock.assert_awaited_once_with(fake_db, token)
    assert json.loads(fake_db.metadata_json) == {"a": 1, "chip_run_id": "xxx"}
    assert fake_db.committed is True
    assert fake_db.rolled_back is False


@pytest.mark.asyncio
async def test_merge_owned_metadata_lease_lost_rolls_back_and_raises() -> None:
    """lease lost → rollback + 抛 JobLeaseLostError，不 commit、不返回 False。"""
    token = _token()
    fake_db = _FakeDb()
    def _factory() -> _FakeDb:
        return fake_db

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=AsyncMock(side_effect=JobLeaseLostError("lease lost")),
    ):
        with pytest.raises(JobLeaseLostError):
            await merge_owned_job_run_metadata(
                token, {"chip_run_id": "x"}, session_factory=_factory,
            )

    assert fake_db.rolled_back is True
    assert fake_db.committed is False


@pytest.mark.asyncio
async def test_merge_owned_metadata_handles_malformed_json() -> None:
    """历史坏 metadata_json 从 {} 起合并，不得因坏数据让 worker crash。"""
    token = _token()
    fake_db = _FakeDb(metadata_json="not-json{")
    def _factory() -> _FakeDb:
        return fake_db

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=AsyncMock(return_value=fake_db),
    ):
        await merge_owned_job_run_metadata(
            token, {"chip_run_id": "yyy"}, session_factory=_factory,
        )

    assert json.loads(fake_db.metadata_json) == {"chip_run_id": "yyy"}
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_finalize_accepts_partial_failed_status() -> None:
    """partial_failed 是正式一等终态（C2B）：finalize_job_run 接受且不抛。"""
    token = _token()
    fake_db = _FakeDb()
    def _factory() -> _FakeDb:
        return fake_db

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=AsyncMock(return_value=fake_db),
    ):
        ok = await finalize_job_run(
            token,
            status="partial_failed",
            metadata_updates={},
            total_count=2,
            succeeded_count=1,
            failed_count=1,
            session_factory=_factory,
        )

    assert ok is True
    assert fake_db.status == "partial_failed"
    assert fake_db.succeeded_count == 1
    assert fake_db.failed_count == 1
    assert fake_db.worker_instance_id is None
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_finalize_rejects_non_terminal_status() -> None:
    """非终态（如 running）仍被拒绝，保持 fail-closed 终态校验。"""
    token = _token()
    fake_db = _FakeDb()
    def _factory() -> _FakeDb:
        return fake_db

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=AsyncMock(return_value=fake_db),
    ):
        with pytest.raises(ValueError):
            await finalize_job_run(
                token,
                status="running",
                metadata_updates={},
                total_count=0,
                succeeded_count=0,
                failed_count=0,
                session_factory=_factory,
            )


@pytest.mark.asyncio
async def test_update_owned_job_run_progress_writes_fields_and_metadata() -> None:
    """owner 有效 → 写 last_cycle_at / counts / 合并 metadata 并 commit。"""
    holder: dict = {}
    flags: dict = {}

    class _JobRun:
        def __init__(self) -> None:
            self.last_cycle_at = None
            self.succeeded_count = None
            self.failed_count = None
            self.metadata_json = None

    class _Session:
        async def commit(self) -> None:
            flags["commit"] = True

        async def rollback(self) -> None:
            flags["rollback"] = True

    class _CM:
        def __init__(self) -> None:
            self.session = _Session()

        async def __aenter__(self) -> _Session:
            return self.session

        async def __aexit__(self, *exc) -> bool:
            return False

    async def _lock(db, token):  # noqa: ANN001
        jr = holder.get("jr")
        if jr is None:
            jr = _JobRun()
            holder["jr"] = jr
        return jr

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=_lock,
    ):
        token = _token()
        last = datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        await update_owned_job_run_progress(
            token,
            last_cycle_at=last,
            succeeded_count=2,
            failed_count=1,
            metadata_updates={"last_bar_time": "2026-01-01T10:00:00+08:00"},
            session_factory=_CM,
        )

    assert holder["jr"].last_cycle_at == last
    assert holder["jr"].succeeded_count == 2
    assert holder["jr"].failed_count == 1
    assert json.loads(holder["jr"].metadata_json) == {
        "last_bar_time": "2026-01-01T10:00:00+08:00"
    }
    assert flags.get("commit") is True


@pytest.mark.asyncio
async def test_update_owned_job_run_progress_fails_closed_on_stale_lease() -> None:
    """owner 失配（lease 已被转移）→ 抛 JobLeaseLostError 且不写任何字段。"""
    flags: dict = {}

    class _Session:
        async def commit(self) -> None:
            flags["commit"] = True

        async def rollback(self) -> None:
            flags["rollback"] = True

    class _CM:
        def __init__(self) -> None:
            self.session = _Session()

        async def __aenter__(self) -> _Session:
            return self.session

        async def __aexit__(self, *exc) -> bool:
            return False

    async def _lock_stale(db, token):  # noqa: ANN001
        raise JobLeaseLostError("stale epoch")

    with patch(
        "app.services.fenced_job_run_service.lock_owned_job_run",
        new=_lock_stale,
    ):
        token = _token()
        with pytest.raises(JobLeaseLostError):
            await update_owned_job_run_progress(
                token,
                last_cycle_at=datetime(2026, 1, 1, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
                succeeded_count=0,
                failed_count=0,
                metadata_updates={},
                session_factory=_CM,
            )

    # 失配时不得提交任何写
    assert flags.get("commit") is None
