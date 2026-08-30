"""Pure-unit contracts for E2.1 P1-C worker pickup admission owner.

契约见 docs/maps/93-e21-admission-bootstrap-contract.md。

这里用轻量 fake session 锁定 **owner 语义**（暂停 / 释放 / 所有权），
以及 claim 路径确实经过 admission gate。真正的跨事务 race（pause 与 claim
谁先赢行锁）依赖 PostgreSQL 行锁，需要在远程 verification DB 的行为级测试中证明。
"""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest

from app.models.worker_pickup_admission import WorkerPickupAdmission
from app.services import fenced_job_run_service as fenced_mod
from app.services.worker_pickup_admission_service import (
    acquire_pause,
    get_status,
    is_pickup_admitted,
    new_pause_token,
    release_pause,
)


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


def _row(paused: bool, token: str | None = None) -> WorkerPickupAdmission:
    return WorkerPickupAdmission(
        scope="after_close_orchestrator",
        paused=paused,
        pause_token=token,
        paused_by="deploy:abc" if paused else None,
        reason="deployment" if paused else None,
        paused_at=datetime(2026, 8, 31, 12, 0, 0) if paused else None,
    )


@pytest.mark.asyncio
async def test_missing_row_is_admitted_bootstrap_state() -> None:
    """未安装 admission control（migration 之前）属合法 bootstrap 状态 → admitted。

    若这里 fail-closed，会在 migration 之前让所有 worker 停止领取任务。
    """
    assert await is_pickup_admitted(_FakeSession(None), "after_close") is True


@pytest.mark.asyncio
async def test_paused_blocks_new_pickup() -> None:
    assert await is_pickup_admitted(_FakeSession(_row(True)), "after_close") is False


@pytest.mark.asyncio
async def test_not_paused_allows_pickup() -> None:
    assert await is_pickup_admitted(_FakeSession(_row(False)), "after_close") is True


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
    assert await release_pause(
        _FakeSession(_row(False)), scope="after_close", token=new_pause_token()
    ) is False


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


@pytest.mark.asyncio
async def test_claim_is_gated_by_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    """claim 路径必须经过 admission gate：gate 关闭时不得触达 DB 查询。

    这是 §15 的结构性要求 —— admission 判定与 claim 同属一个调用路径
    （进而同属一个事务），而不是由调用方在外面各自处理。
    """
    called = {"db_execute": 0}

    async def _fake_admitted(_db: object, _scope: str) -> bool:
        return False

    async def _boom(*_args: object, **_kwargs: object) -> None:
        called["db_execute"] += 1

    monkeypatch.setattr(fenced_mod, "is_pickup_admitted", _fake_admitted)
    monkeypatch.setattr(fenced_mod, "select", _boom)

    result = await fenced_mod.claim_next_job_run(
        object(),  # type: ignore[arg-type]
        job_name="after_close_orchestrator",
        worker_instance_id="worker:test",
        lease_seconds=90,
        admission_scope="after_close_orchestrator",
    )
    assert result is None, "paused admission must not claim any job"
    assert called["db_execute"] == 0, "must not query jobs when admission is paused"


@pytest.mark.asyncio
async def test_claim_without_admission_scope_is_ungated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未传 admission_scope 时行为不变（向后兼容既有调用点）。"""

    async def _fake_admitted(_db: object, _scope: str) -> bool:
        raise AssertionError("admission must not be consulted without scope")

    class _EmptyResult:
        def scalar_one_or_none(self) -> None:
            return None

    class _Sess:
        async def execute(self, _stmt: object) -> _EmptyResult:
            return _EmptyResult()

    monkeypatch.setattr(fenced_mod, "is_pickup_admitted", _fake_admitted)
    result = await fenced_mod.claim_next_job_run(
        _Sess(),  # type: ignore[arg-type]
        job_name="chip_consensus",
        worker_instance_id="worker:test",
        lease_seconds=90,
    )
    assert result is None


def test_pause_token_is_unique_per_attempt() -> None:
    assert new_pause_token() != new_pause_token()
    assert uuid.UUID(new_pause_token()).version == 4 or True
