# PURE_UNIT_TEST=1
"""[Corrective-3 / W7B1] Chip consensus poll owner 单元测试（不连接数据库）。

直接覆盖 app.services.chip_consensus_worker_poll.poll_chip_consensus_once。
9 个用例对齐 W7B1 冻结 owner boundary：

1. no claim → rollback + False
2. claim args → job name / worker / lease exact
3. claim success → commit 发生
4. heartbeat → token 来自 claim，30s，start
5. missing metadata → 精确 failure code + True
6. lease lost → 不写普通 failure terminal，True
7. unexpected error → CHIP_JOB_EXECUTION_FAILED
8. cleanup → claim-success 后 heartbeat.stop
9. façade → session/worker/logger 注入正确

structural ordering（publication < Scheduler finalize < heartbeat stop）由
现有 test_chip_worker_orchestration.py 的源码结构守卫承担，此处不复制。

运行：
    cd backend
    PURE_UNIT_TEST=1 .venv/bin/python -m pytest tests/test_chip_consensus_worker_poll.py -v
"""
from __future__ import annotations

import types
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.after_close_chip_consensus_service import (
    _CHIP_LEASE_SECONDS,
    CHIP_CONSENSUS_JOB_NAME,
)
from app.services.fenced_job_run_service import JobLeaseLostError


class _FakeAsyncSession:
    """模拟 AsyncSession 上下文管理器（纯单元测试用，最小可用）。"""

    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> _FakeAsyncSession:
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _build_fake_session_factory(sessions: list[_FakeAsyncSession]):
    """构造 fake session_factory：每次调用取一个 session（超出返回空 session）。"""

    class _Factory:
        def __init__(self) -> None:
            self._sessions = sessions
            self._idx = 0

        def __call__(self) -> _FakeAsyncSession:
            if self._idx >= len(self._sessions):
                return _FakeAsyncSession()
            s = self._sessions[self._idx]
            self._idx += 1
            return s

    return _Factory()


class _FakeClaim:
    def __init__(self, job_run_id, lease_epoch, metadata, previous_status="queued"):
        self.token = types.SimpleNamespace(job_run_id=job_run_id, lease_epoch=lease_epoch)
        self.metadata = metadata
        self.previous_status = previous_status


class _FakeHeartbeat:
    """记录 FencedJobHeartbeat 的构造与生命周期调用。

    raise_on_ensure_owned=True 时 ensure_owned() 抛真实 JobLeaseLostError，
    用于 lease-lost 路径（不写普通 failure terminal）。
    """

    instances: list[_FakeHeartbeat] = []
    raise_on_ensure_owned = False

    def __init__(self, token, interval_seconds) -> None:
        self.token = token
        self.interval_seconds = interval_seconds
        self.started = False
        self.stopped = False
        _FakeHeartbeat.instances.append(self)

    async def start(self) -> None:
        self.started = True

    def ensure_owned(self) -> None:
        if _FakeHeartbeat.raise_on_ensure_owned:
            raise JobLeaseLostError("lease epoch 已被抢占")

    async def stop(self) -> None:
        self.stopped = True


def _happy_summary(status: str = "succeeded"):
    return {
        "status": status,
        "succeeded_count": 1,
        "failed_count": 0,
        "skipped_count": 0,
        "total_count": 1,
        "failed_instruments": [],
        "skipped_instruments": [],
        "anchor_rebuild_required": False,
    }


@pytest.fixture(autouse=True)
def _reset_heartbeats():
    """每个用例独立清理 FakeHeartbeat 实例与 lease-lost 开关。"""
    _FakeHeartbeat.instances.clear()
    _FakeHeartbeat.raise_on_ensure_owned = False
    yield
    _FakeHeartbeat.instances.clear()
    _FakeHeartbeat.raise_on_ensure_owned = False


@contextmanager
def _patch_chip_poll(*, claim, execute_boom=None, publication_outcome=None):
    """patch 所有 chip poll owner 依赖（lazy import 位置）。"""
    active = [uuid.uuid4()]
    pending = [uuid.uuid4()]
    chip_run = MagicMock()
    chip_run.id = uuid.uuid4()
    outcome = publication_outcome if publication_outcome is not None else MagicMock(
        to_metadata=MagicMock(return_value={}),
    )

    with patch(
        "app.services.fenced_job_run_service.claim_next_job_run",
        new=AsyncMock(return_value=claim),
    ), patch(
        "app.services.fenced_job_run_service.FencedJobHeartbeat",
        new=_FakeHeartbeat,
    ), patch(
        "app.services.fenced_job_run_service.finalize_job_run",
        new=AsyncMock(return_value=True),
    ) as finalize, patch(
        "app.services.fenced_job_run_service.merge_job_run_metadata",
        new=AsyncMock(),
    ), patch(
        "app.services.feature_snapshot_service.get_active_a_share_instruments",
        new=AsyncMock(return_value=active),
    ), patch(
        "app.services.after_close_chip_consensus_service.get_pending_chip_instruments",
        new=AsyncMock(return_value=pending),
    ), patch(
        "app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus",
        new=(execute_boom if execute_boom is not None else AsyncMock(return_value=_happy_summary())),
    ), patch(
        "app.services.chip_consensus_run_lifecycle.resolve_or_create_chip_run",
        new=AsyncMock(return_value=chip_run),
    ), patch(
        "app.services.chip_consensus_run_lifecycle.finalize_chip_run",
        new=AsyncMock(),
    ), patch(
        "app.services.chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction",
        new=AsyncMock(return_value=outcome),
    ):
        yield {"finalize": finalize, "heartbeat_cls": _FakeHeartbeat}


# =============================================================================
# 1. no claim → rollback + False
# =============================================================================


@pytest.mark.asyncio
async def test_no_claim_rolls_back_and_returns_false() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_factory([claim_session])
    logger = MagicMock()

    with _patch_chip_poll(claim=None):
        result = await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is False
    assert claim_session.rolled_back is True
    assert claim_session.committed is False
    # heartbeat 在 claim 之后才创建，no claim 时不应创建
    assert _FakeHeartbeat.instances == []


# =============================================================================
# 2. claim args → job name / worker / lease exact
# =============================================================================


@pytest.mark.asyncio
async def test_claim_args_exact() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    job_run_id = uuid.uuid4()
    claim = _FakeClaim(job_run_id, 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})
    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    captured = {}

    async def _track_claim(db, job_name, worker_instance_id, lease_seconds):
        captured["job_name"] = job_name
        captured["worker_instance_id"] = worker_instance_id
        captured["lease_seconds"] = lease_seconds
        return claim

    with patch(
        "app.services.fenced_job_run_service.claim_next_job_run", new=_track_claim
    ), patch("app.services.fenced_job_run_service.FencedJobHeartbeat", new=_FakeHeartbeat), \
         patch("app.services.fenced_job_run_service.finalize_job_run", new=AsyncMock(return_value=True)), \
         patch("app.services.fenced_job_run_service.merge_job_run_metadata", new=AsyncMock()), \
         patch("app.services.feature_snapshot_service.get_active_a_share_instruments", new=AsyncMock(return_value=[uuid.uuid4()])), \
         patch("app.services.after_close_chip_consensus_service.get_pending_chip_instruments", new=AsyncMock(return_value=[uuid.uuid4()])), \
         patch("app.services.after_close_chip_consensus_service.execute_after_close_chip_consensus", new=AsyncMock(return_value=_happy_summary())), \
         patch("app.services.chip_consensus_run_lifecycle.resolve_or_create_chip_run", new=AsyncMock(return_value=MagicMock(id=uuid.uuid4())), ), \
         patch("app.services.chip_consensus_run_lifecycle.finalize_chip_run", new=AsyncMock()), \
         patch("app.services.chip_consensus_run_lifecycle.publish_chip_and_upgrade_auction", new=AsyncMock(return_value=MagicMock(to_metadata=MagicMock(return_value={})))):
        result = await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert captured["job_name"] == CHIP_CONSENSUS_JOB_NAME
    assert captured["worker_instance_id"] == "w1"
    assert captured["lease_seconds"] == _CHIP_LEASE_SECONDS


# =============================================================================
# 3. claim success → commit 发生
# =============================================================================


@pytest.mark.asyncio
async def test_claim_success_commits() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim = _FakeClaim(uuid.uuid4(), 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})
    claim_session = _FakeAsyncSession()
    session_factory = _build_fake_session_factory([claim_session])
    logger = MagicMock()

    with _patch_chip_poll(claim=claim):
        result = await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert claim_session.committed is True


# =============================================================================
# 4. heartbeat → token 来自 claim，30s，start
# =============================================================================


@pytest.mark.asyncio
async def test_heartbeat_created_from_claim_token() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim = _FakeClaim(uuid.uuid4(), 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})
    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    with _patch_chip_poll(claim=claim):
        await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    hb = _FakeHeartbeat.instances[-1]
    assert hb.token is claim.token
    assert hb.interval_seconds == 30.0
    assert hb.started is True


# =============================================================================
# 5. missing metadata → 精确 failure code + True
# =============================================================================


@pytest.mark.asyncio
async def test_missing_metadata_exact_failure_code() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    # metadata 缺 trade_date / core_run_id
    claim = _FakeClaim(uuid.uuid4(), 3, {})
    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    with _patch_chip_poll(claim=claim) as handles:
        result = await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert handles["finalize"].await_count == 1
    _, kwargs = handles["finalize"].call_args
    assert kwargs["error_code"] == "CHIP_JOB_METADATA_MISSING"
    assert kwargs["status"] == "failed"


# =============================================================================
# 6. lease lost → 不写普通 failure terminal，True
# =============================================================================


@pytest.mark.asyncio
async def test_lease_lost_does_not_write_failure_terminal() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim = _FakeClaim(uuid.uuid4(), 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})
    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    _FakeHeartbeat.raise_on_ensure_owned = True
    try:
        with _patch_chip_poll(claim=claim) as handles:
            result = await poll_chip_consensus_once(
                session_factory=session_factory, worker_instance_id="w1", logger=logger,
            )
    finally:
        _FakeHeartbeat.raise_on_ensure_owned = False

    assert result is True
    # 失去租约：禁止任何 SchedulerJobRun 终态写（包括普通 failure terminal）
    assert handles["finalize"].await_count == 0
    # 但 finally 仍 stop heartbeat
    assert _FakeHeartbeat.instances[-1].stopped is True


# =============================================================================
# 7. unexpected error → CHIP_JOB_EXECUTION_FAILED
# =============================================================================


@pytest.mark.asyncio
async def test_unexpected_error_uses_chip_job_execution_failed() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim = _FakeClaim(uuid.uuid4(), 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})

    async def _boom(**kwargs):
        raise RuntimeError("compute crashed")

    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    with _patch_chip_poll(claim=claim, execute_boom=_boom) as handles:
        result = await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert result is True
    assert handles["finalize"].await_count == 1
    _, kwargs = handles["finalize"].call_args
    assert kwargs["error_code"] == "CHIP_JOB_EXECUTION_FAILED"
    assert kwargs["status"] == "failed"


# =============================================================================
# 8. cleanup → claim-success 后 heartbeat.stop
# =============================================================================


@pytest.mark.asyncio
async def test_claim_success_stops_heartbeat() -> None:
    from app.services.chip_consensus_worker_poll import poll_chip_consensus_once

    claim = _FakeClaim(uuid.uuid4(), 3, {"trade_date": "2026-08-05", "core_run_id": str(uuid.uuid4())})
    session_factory = _build_fake_session_factory([_FakeAsyncSession()])
    logger = MagicMock()

    with _patch_chip_poll(claim=claim):
        await poll_chip_consensus_once(
            session_factory=session_factory, worker_instance_id="w1", logger=logger,
        )

    assert _FakeHeartbeat.instances[-1].stopped is True


# =============================================================================
# 9. façade → session / worker / logger 注入正确
# =============================================================================


@pytest.mark.asyncio
async def test_worker_facade_delegates_to_owner() -> None:
    import app.worker as worker_mod

    with patch(
        "app.services.chip_consensus_worker_poll.poll_chip_consensus_once",
        new=AsyncMock(return_value=True),
    ) as mock_owner:
        result = await worker_mod._chip_consensus_poll_once()

    assert result is True
    mock_owner.assert_awaited_once()
    _, kwargs = mock_owner.call_args
    assert kwargs["session_factory"] is worker_mod.AsyncSessionLocal
    assert kwargs["worker_instance_id"] == worker_mod._WORKER_INSTANCE_ID
    assert kwargs["logger"] is worker_mod.logger
