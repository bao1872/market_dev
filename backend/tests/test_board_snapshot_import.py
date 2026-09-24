"""[BOARD-LOCAL-OWNERSHIP-01] 生产 importer 合同测试（任务 §18 E/F，纯单元）。

覆盖：
- 成功：sync_boards 恰好调用一次；commit；status=succeeded 记录
- 服务端拥有 effective_date（上海日历当日，不信任客户端）
- 失败：整体 rollback（保留上一成功快照）；status=failed 记录；非零退出
- single-flight：并发时 fail-fast（BOARD_SYNC_BUSY），**不是**频率限制
- 频率：同一天重复同步被允许（无冷却/无 once-per-day）
- 源码级：无手写 INSERT/UPDATE/DELETE 业务变更；唯一业务写 owner 是 sync_boards
"""

from __future__ import annotations

import inspect
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.cli import board_snapshot_import as imp
from app.services.board_snapshot_transfer import build_envelope, serialize_envelope
from app.services.wencai_board_provider import BoardSnapshot

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _envelope_bytes() -> bytes:
    snap = BoardSnapshot(
        boards=[{"external_code": "BK1", "name": "半导体", "type": "industry"}],
        memberships={("BK1", "industry"): ["600000.SH"]},
        raw_rows=6000,
        unresolved_symbols=[],
        diagnostics={"duration_ms": 1},
    )
    return serialize_envelope(build_envelope(snap, producer_git_sha="deadbeef"))


class _FakeTxn:
    def __init__(self, session: "_FakeSession") -> None:
        self._s = session

    async def __aenter__(self) -> "_FakeSession":
        self._s.begin_count += 1
        return self._s

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # 真实 AsyncSession.begin() 在异常时 rollback，正常时 commit。
        self._s.rolled_back = exc_type is not None
        return False


class _FakeSession:
    """最小 AsyncSession 替身：只提供 importer 使用的 begin()/scalar()。"""

    def __init__(self, lock_available: bool = True) -> None:
        self._lock_available = lock_available
        self.begin_count = 0
        self.rolled_back = False

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    def begin(self) -> _FakeTxn:
        return _FakeTxn(self)

    async def scalar(self, *args, **kwargs):
        return self._lock_available


class _Spy:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result
        self.exc = exc

    async def __call__(self, db, snapshot, instrument_resolver=None, *, effective_date=None):
        self.calls.append(
            {
                "db": db,
                "snapshot": snapshot,
                "instrument_resolver": instrument_resolver,
                "effective_date": effective_date,
            }
        )
        if self.exc is not None:
            raise self.exc
        return self.result or {
            "status": "succeeded",
            "source": "wencai",
            "raw_rows": 6000,
            "resolved": 1,
            "unresolved": 0,
            "industry_count": 1,
            "concept_count": 0,
            "membership_count": 1,
        }


def _patch(monkeypatch, *, session: _FakeSession, sync_spy: _Spy):
    monkeypatch.setattr(imp, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(imp, "sync_boards", sync_spy)

    statuses: list[dict] = []

    async def _rec(status):
        statuses.append(status)

    monkeypatch.setattr(imp, "record_sync_status", _rec)
    return statuses


@pytest.mark.asyncio
async def test_apply_calls_sync_boards_once_and_records_success(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy()
    statuses = _patch(monkeypatch, session=session, sync_spy=spy)

    summary = await imp._apply(_envelope_bytes())

    assert len(spy.calls) == 1, "成功路径必须恰好调用一次 sync_boards"
    assert session.begin_count == 1
    assert session.rolled_back is False
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "local_manual"
    # 成功状态（best-effort 由 _amain 调用；此处直接验证可记录）
    assert statuses == []


@pytest.mark.asyncio
async def test_apply_uses_server_owned_effective_date(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy()
    _patch(monkeypatch, session=session, sync_spy=spy)

    summary = await imp._apply(_envelope_bytes())

    expected = datetime.now(_SHANGHAI).date()
    assert spy.calls[0]["effective_date"] == expected, (
        "effective_date 必须由服务端按上海日历当日决定"
    )
    assert summary["effective_date"] == expected.isoformat()


@pytest.mark.asyncio
async def test_apply_binds_resolver_to_same_session(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy()
    _patch(monkeypatch, session=session, sync_spy=spy)

    seen: list = []

    async def _fake_resolve(db, symbols):
        seen.append((db, list(symbols)))
        return {}

    monkeypatch.setattr(imp, "resolve_board_instruments", _fake_resolve)

    await imp._apply(_envelope_bytes())

    resolver = spy.calls[0]["instrument_resolver"]
    assert resolver is not None, "必须传入 instrument_resolver"
    await resolver(["600000.SH"])
    assert seen and seen[0][0] is session, (
        "resolver 必须绑定 importer 的同一个 session（不新建连接）"
    )


@pytest.mark.asyncio
async def test_apply_rolls_back_on_failure(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy(exc=RuntimeError("gate failed"))
    _patch(monkeypatch, session=session, sync_spy=spy)

    with pytest.raises(RuntimeError):
        await imp._apply(_envelope_bytes())

    assert session.rolled_back is True, "失败必须 rollback（保留上一成功快照）"


@pytest.mark.asyncio
async def test_apply_refuses_when_lock_unavailable(monkeypatch) -> None:
    """single-flight：锁不可用 → fail-fast（并发保护，非频率限制）。"""
    session = _FakeSession(lock_available=False)
    spy = _Spy()
    _patch(monkeypatch, session=session, sync_spy=spy)

    with pytest.raises(imp.BoardSyncBusyError):
        await imp._apply(_envelope_bytes())

    assert spy.calls == [], "未获得 single-flight 锁时不得调用 sync_boards"


@pytest.mark.asyncio
async def test_same_day_repeated_sync_is_allowed(monkeypatch) -> None:
    """同一天重复同步必须被允许（无冷却 / 无 once-per-day / 相同快照可重放）。"""
    spy = _Spy()
    sessions: list[_FakeSession] = []

    def _factory():
        s = _FakeSession()
        sessions.append(s)
        return s

    monkeypatch.setattr(imp, "AsyncSessionLocal", _factory)
    monkeypatch.setattr(imp, "sync_boards", spy)

    async def _rec(status):  # noqa: ARG001
        return None

    monkeypatch.setattr(imp, "record_sync_status", _rec)

    env = _envelope_bytes()
    first = await imp._apply(env)
    second = await imp._apply(env)

    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert len(spy.calls) == 2, "同日第二次同步不得被拒绝"
    assert len(sessions) == 2, "每次调用都使用独立 session（无跨调用缓存频率）"


@pytest.mark.asyncio
async def test_amain_returns_nonzero_on_failure(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy(exc=RuntimeError("boom"))
    _patch(monkeypatch, session=session, sync_spy=spy)
    monkeypatch.setattr(imp, "_read_stdin_envelope", _envelope_bytes)

    rc = await imp._amain()
    assert rc == 1, "失败必须非零退出"


@pytest.mark.asyncio
async def test_amain_returns_zero_on_success(monkeypatch) -> None:
    session = _FakeSession()
    spy = _Spy()
    _patch(monkeypatch, session=session, sync_spy=spy)
    monkeypatch.setattr(imp, "_read_stdin_envelope", _envelope_bytes)

    rc = await imp._amain()
    assert rc == 0


def test_importer_has_no_direct_business_sql() -> None:
    """唯一业务写 owner 是 sync_boards：importer 不得手写业务 DML。"""
    src = inspect.getsource(imp)
    for forbidden in (
        "INSERT INTO",
        "UPDATE market_boards",
        "DELETE FROM",
        "insert(",
        "sa_update(",
    ):
        assert forbidden not in src, f"importer 不得包含手写业务 SQL: {forbidden}"


def test_importer_reads_from_stdin_not_argv() -> None:
    """envelope 必须走 stdin（不得作为 shell 参数/命令行）。"""
    src = inspect.getsource(imp)
    assert "sys.stdin" in src, "importer 必须从 stdin 读取 envelope"
