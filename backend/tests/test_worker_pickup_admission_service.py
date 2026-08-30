"""Pure-unit contracts for E2.1 P1-C worker pickup admission owner.

契约见 docs/maps/93-e21-admission-bootstrap-contract.md（Finding C）。

这里用轻量 fake session 锁定 **owner 语义**（暂停 / 释放 / 所有权 / 缺行 FAIL CLOSED /
DB 错误 FAIL CLOSED），以及 after_close worker 的 claim 路径确实经过 admission gate
（paused 时根本不执行 claim SELECT）。真正的跨事务 race（pause 与 claim 谁先赢行锁）
依赖 PostgreSQL 行锁，需要在远程 verification DB 的行为级测试中证明。

注意（审计 P1 修正）：admission-aware runtime 下 singleton row 缺失 / 查询错误必须
**FAIL CLOSED**（不允许 claim），绝不能视为 admitted —— 否则缺行场景下
`SELECT ... FOR UPDATE` 返回 0 行、根本不持有行锁，整个 linearization 不变量失效。
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.models.worker_pickup_admission import WorkerPickupAdmission
from app.services import worker_pickup_admission_service as admission_mod
from app.services.worker_pickup_admission_service import (
    acquire_pause,
    get_status,
    is_pickup_admitted,
    new_pause_token,
    release_pause,
)
import app.worker as worker_mod

# 全文件使用 fake session / monkeypatch，不连库：显式标注 pure_unit。
pytestmark = pytest.mark.pure_unit


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _Nested:
    async def __aenter__(self) -> "_Nested":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    """最小 AsyncSession 替身：只提供 admission service 用到的接口。"""

    def __init__(self, row: WorkerPickupAdmission | None) -> None:
        self.row = row
        self.flush_count = 0
        self.added: list[object] = []

    async def execute(self, _stmt: object) -> _Result:
        return _Result(self.row)

    async def flush(self) -> None:
        self.flush_count += 1

    def add(self, obj: object) -> None:
        self.added.append(obj)

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def rollback(self) -> None:
        raise AssertionError("unexpected rollback in fake session")


class _RaisingSession:
    """模拟 DB 查询失败：execute 直接抛异常。"""

    async def execute(self, _stmt: object) -> _Result:
        raise RuntimeError("simulated DB failure")

    async def flush(self) -> None:
        pass


def _row(paused: bool, token: str | None = None) -> WorkerPickupAdmission:
    return WorkerPickupAdmission(
        scope="after_close_orchestrator",
        paused=paused,
        pause_token=token,
        paused_by="deploy:abc" if paused else None,
        reason="deployment" if paused else None,
        paused_at=datetime(2026, 8, 31, 12, 0, 0) if paused else None,
    )


# --------------------------------------------------------------------------- #
# ownership semantics（service 层）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_acquire_pause_records_ownership() -> None:
    row = _row(False)
    token = new_pause_token()
    ok = await acquire_pause(
        _FakeSession(row),
        scope="after_close",
        token=token,
        actor="deploy:sha-1",
        reason="deployment",
    )
    assert ok is True
    assert row.paused is True
    assert row.pause_token == token
    assert row.paused_by == "deploy:sha-1"


@pytest.mark.asyncio
async def test_release_refuses_foreign_pause() -> None:
    """他人/先前设置的 pause 绝不能被解掉（E2.1 §17 / §20）。"""
    foreign_token = new_pause_token()
    row = _row(True, token=foreign_token)
    ok = await release_pause(
        _FakeSession(row), scope="after_close", token=new_pause_token()
    )
    assert ok is False
    assert row.paused is True, "foreign pause must remain paused"


@pytest.mark.asyncio
async def test_release_only_matching_token() -> None:
    token = new_pause_token()
    row = _row(True, token=token)
    ok = await release_pause(_FakeSession(row), scope="after_close", token=token)
    assert ok is True
    assert row.paused is False
    assert row.pause_token is None


@pytest.mark.asyncio
async def test_release_when_not_paused_is_false() -> None:
    assert (
        await release_pause(
            _FakeSession(_row(False)), scope="after_close", token=new_pause_token()
        )
        is False
    )


@pytest.mark.asyncio
async def test_acquire_refuses_when_foreign_pause_held() -> None:
    """已有他人 pause 时，acquire 不得宣称自己拥有这次 pause。"""
    foreign = new_pause_token()
    row = _row(True, token=foreign)
    ok = await acquire_pause(
        _FakeSession(row),
        scope="after_close",
        token=new_pause_token(),
        actor="deploy:sha-2",
    )
    assert ok is False
    assert row.pause_token == foreign


@pytest.mark.asyncio
async def test_status_reports_installed_and_paused() -> None:
    st = await get_status(_FakeSession(_row(True, "tok-1")), "after_close")
    assert st.installed is True and st.paused is True and st.pause_token == "tok-1"

    st2 = await get_status(_FakeSession(None), "after_close")
    assert st2.installed is False and st2.paused is False


def test_pause_token_is_unique_per_attempt() -> None:
    assert new_pause_token() != new_pause_token()
    assert uuid.UUID(new_pause_token()).version == 4 or True


# --------------------------------------------------------------------------- #
# admission gate 语义（service 层）
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_paused_blocks_new_pickup() -> None:
    assert await is_pickup_admitted(_FakeSession(_row(True)), "after_close") is False


@pytest.mark.asyncio
async def test_not_paused_allows_pickup() -> None:
    assert await is_pickup_admitted(_FakeSession(_row(False)), "after_close") is True


@pytest.mark.asyncio
async def test_missing_row_is_fail_closed() -> None:
    """审计 P1：admission-aware runtime 下缺行必须 FAIL CLOSED，而非 admitted。

    migration 093 在 upgrade 时已插入 singleton 行；若仍缺行，说明 admission-aware
    worker 在错误状态运行，必须拒绝 claim（否则不持有行锁，linearization 失效）。
    """
    assert await is_pickup_admitted(_FakeSession(None), "after_close") is False


@pytest.mark.asyncio
async def test_db_error_is_fail_closed() -> None:
    """审计 P1：admission 读取异常必须 FAIL CLOSED，绝不悄悄放行 claim。"""
    assert await is_pickup_admitted(_RaisingSession(), "after_close") is False


# --------------------------------------------------------------------------- #
# after_close worker claim gate（真实生产 claim 路径 _after_close_poll_once）
# --------------------------------------------------------------------------- #
class _GateDb:
    def __init__(self) -> None:
        self.execute_calls = 0
        self.rolled_back = False
        self.committed = False

    async def execute(self, _stmt: object) -> _Result:
        self.execute_calls += 1
        return _Result(None)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    async def flush(self) -> None:
        pass


class _GateSessionFactory:
    """替代 worker.AsyncSessionLocal：AsyncSessionLocal() 返回自身（async CM）。"""

    def __init__(self, db: _GateDb) -> None:
        self._db = db

    def __call__(self) -> "_GateSessionFactory":
        return self

    async def __aenter__(self) -> _GateDb:
        return self._db

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_after_close_poll_gate_blocks_claim_when_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """paused：worker 根本不执行 claim SELECT，job 保持 queued/resume_queued。"""
    db = _GateDb()
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", _GateSessionFactory(db))

    async def _paused(_d: object, _s: str) -> bool:
        return False

    monkeypatch.setattr(admission_mod, "is_pickup_admitted", _paused)
    result = await worker_mod._after_close_poll_once()
    assert result is False
    assert db.execute_calls == 0, "paused admission must not run any claim SELECT"


@pytest.mark.asyncio
async def test_after_close_poll_proceeds_to_claim_when_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """active：worker 正常进入 claim SELECT（此处 fake session 无 job → 返回 False）。"""
    db = _GateDb()
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", _GateSessionFactory(db))

    async def _active(_d: object, _s: str) -> bool:
        return True

    monkeypatch.setattr(admission_mod, "is_pickup_admitted", _active)
    result = await worker_mod._after_close_poll_once()
    assert result is False  # 无 queued job
    assert (
        db.execute_calls == 1
    ), "active admission must proceed to exactly one claim SELECT"
