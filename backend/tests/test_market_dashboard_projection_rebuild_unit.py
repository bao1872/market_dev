"""F1C rebuild orchestration — 纯单元（session ownership / guard / invariants / manifest）。

不连 DB：用 fake session factory + spy 锁死事件顺序与 fail-closed 行为。
覆盖：
- session ownership 顺序：read_close < write_open；membership_lock < membership_compare < writer；
  writer 期间 guard 仍持锁（writer < guard_end）
- membership mismatch（version 变更 / board 新增 / board 缺失）→ ProjectionInputChangedError 且 writer=0
- 成功路径：prepare/build/iter/writer 各 1；writer 用 fresh session；scope 仍是 iterator；参数原样
- context invariants：重复 board_id / keys 不一致 → fail closed
- manifest 登记回归

注意：本文件不得出现 conftest 记录的 PG 建连源码标志（DB session 工厂名 / 独立引擎名），
否则整模块会被判为 postgres 并在 PURE_UNIT 下跳过。
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

import app.services.market_dashboard_projection_service as proj
import app.services.market_dashboard_service as svc
from app.repositories.market_dashboard_projection_repository import ProjectionWriteResult
from app.services import market_dashboard_projection_rebuild_service as rebuild

_VERIFY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "verify"
if str(_VERIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFY_DIR))

from evidence_manifest import load_evidence_manifest  # noqa: E402


# ---------------------------------------------------------------- fakes
class _FakeResult:
    def __init__(self, rows) -> None:  # noqa: ANN001
        self._rows = list(rows)

    def all(self):  # noqa: ANN201
        return list(self._rows)

    def scalar(self):  # noqa: ANN201
        return self._rows[0] if self._rows else None


class _FakeTx:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> _FakeTx:
        self.session.events.append(f"{self.session.name}_begin")
        self.session.in_tx = True
        return self

    async def __aexit__(self, *_exc) -> bool:  # noqa: ANN002
        self.session.events.append(f"{self.session.name}_end")
        self.session.in_tx = False
        return False


class _FakeSession:
    def __init__(self, name: str, events: list[str], *, result_rows=()) -> None:  # noqa: ANN001
        self.name = name
        self.events = events
        self.in_tx = False
        self._result_rows = list(result_rows)

    def in_transaction(self) -> bool:
        return self.in_tx

    def begin(self) -> _FakeTx:
        return _FakeTx(self)

    async def execute(self, statement, parameters=None):  # noqa: ANN001, ANN201
        if "LOCK TABLE" in str(statement):
            self.events.append("membership_lock")
        self.events.append(f"{self.name}_execute")
        return _FakeResult(self._result_rows)

    async def __aenter__(self) -> _FakeSession:
        self.events.append(f"{self.name}_open")
        return self

    async def __aexit__(self, *_exc) -> bool:  # noqa: ANN002
        self.events.append(f"{self.name}_close")
        return False


def _factory(events: list[str]):
    names = iter(["read", "guard", "write", "extra"])

    def make() -> _FakeSession:
        return _FakeSession(next(names), events)

    return make


def _context(board_ids, membership_versions) -> proj.ProjectionContext:  # noqa: ANN001
    return proj.ProjectionContext(
        projection_trade_date=date(2026, 9, 18),
        display_dates=[],
        stock_facts=svc._compute_stock_facts_long(None),
        membership_long=svc._build_membership_long({}),
        board_ids=list(board_ids),
        membership_versions=dict(membership_versions),
    )


def _noop_writer():
    async def _writer(*_a, **_k):  # noqa: ANN002, ANN003
        return ProjectionWriteResult(
            projection_trade_date=date(2026, 9, 18), market_rows=1, scope_rows=0
        )

    return _writer


# ---------------------------------------------------------------
# session ownership order
# ---------------------------------------------------------------
def test_orchestration_session_ownership_order(monkeypatch):
    events: list[str] = []
    b1 = uuid4()
    ctx = _context([b1], {b1: "mv1"})

    async def _prepare(_session, _end_date):
        events.append("prepare")
        return ctx

    def _build(_context):
        events.append("build_market")
        return [{"trade_date": date(2026, 9, 18)}]

    def _iter(_context, **_kw):
        events.append("iter_scope")
        return iter([["row"]])

    async def _writer(session, *, market_records, scope_chunks, expected_membership_versions):
        events.append("writer")
        assert session.in_transaction() is False  # F1B 要求 fresh session
        assert session.name == "write"
        return ProjectionWriteResult(
            projection_trade_date=date(2026, 9, 18), market_rows=1, scope_rows=1
        )

    async def _current(_session):
        events.append("membership_compare")
        return {b1: "mv1"}

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)
    monkeypatch.setattr(proj, "build_market_records", _build)
    monkeypatch.setattr(proj, "iter_scope_record_chunks", _iter)
    monkeypatch.setattr(rebuild, "replace_dashboard_projection", _writer)
    monkeypatch.setattr(rebuild, "_current_membership_versions", _current)

    result = asyncio.run(
        rebuild.rebuild_market_dashboard_projection(
            date(2026, 9, 18), session_factory=_factory(events)
        )
    )
    assert isinstance(result, ProjectionWriteResult)
    # read transaction/session 必须先完全结束，write 才开始
    assert events.index("read_close") < events.index("write_open")
    # guard SHARE lock → compare → writer（顺序不可颠倒）
    assert events.index("membership_lock") < events.index("membership_compare")
    assert events.index("membership_compare") < events.index("writer")
    # writer 在 guard transaction 结束之前执行（lock 仍持有）
    assert events.index("writer") < events.index("guard_end")
    assert events.index("guard_end") < events.index("guard_close")


# ---------------------------------------------------------------
# success delegation
# ---------------------------------------------------------------
def test_orchestration_success_delegates_to_writer(monkeypatch):
    events: list[str] = []
    b1, b2 = uuid4(), uuid4()
    ctx = _context([b1, b2], {b1: "mvA", b2: "mvB"})
    captured: dict[str, object] = {}
    sentinel_scope = iter([["r1"], ["r2"]])

    async def _prepare(_session, _end_date):
        return ctx

    def _build(context):
        captured["build_ctx"] = context
        return ["MARKET"]

    def _iter(context, **_kw):
        captured["iter_ctx"] = context
        return sentinel_scope

    async def _writer(session, *, market_records, scope_chunks, expected_membership_versions):
        captured["writer_session"] = session
        captured["market_records"] = market_records
        captured["scope_chunks"] = scope_chunks
        captured["expected"] = expected_membership_versions
        return ProjectionWriteResult(
            projection_trade_date=date(2026, 9, 18), market_rows=1, scope_rows=2
        )

    async def _current(_session):
        return {b1: "mvA", b2: "mvB"}

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)
    monkeypatch.setattr(proj, "build_market_records", _build)
    monkeypatch.setattr(proj, "iter_scope_record_chunks", _iter)
    monkeypatch.setattr(rebuild, "replace_dashboard_projection", _writer)
    monkeypatch.setattr(rebuild, "_current_membership_versions", _current)

    result = asyncio.run(
        rebuild.rebuild_market_dashboard_projection(
            date(2026, 9, 18), session_factory=_factory(events)
        )
    )
    assert captured["build_ctx"] is ctx
    assert captured["iter_ctx"] is ctx
    assert captured["market_records"] == ["MARKET"]
    # scope 必须是 iterator，绝不是全量 list
    assert captured["scope_chunks"] is sentinel_scope
    assert not isinstance(captured["scope_chunks"], list)
    assert isinstance(captured["scope_chunks"], Iterator)
    assert captured["expected"] == {b1: "mvA", b2: "mvB"}
    assert captured["writer_session"].name == "write"  # type: ignore[union-attr]
    assert captured["writer_session"].in_transaction() is False  # type: ignore[union-attr]
    assert isinstance(result, ProjectionWriteResult)


# ---------------------------------------------------------------
# membership mismatch → fail closed（绝不写）
# ---------------------------------------------------------------
@pytest.mark.parametrize("scenario", ["version_changed", "added", "missing"])
def test_orchestration_membership_mismatch_fails_closed(monkeypatch, scenario):
    events: list[str] = []
    b1 = uuid4()
    if scenario == "version_changed":
        ctx_versions = {b1: "mv1"}
        current = {b1: "mv2"}
    elif scenario == "added":
        ctx_versions = {b1: "mv1"}
        current = {b1: "mv1", uuid4(): "mvX"}
    else:  # missing
        b2 = uuid4()
        ctx_versions = {b1: "mv1", b2: "mv1"}
        current = {b1: "mv1"}
    ctx = _context(list(ctx_versions), ctx_versions)
    writer_calls: list[int] = []

    async def _prepare(_session, _end_date):
        return ctx

    def _build(_context):
        return ["MARKET"]

    def _iter(_context, **_kw):
        return iter([])

    async def _writer(*_a, **_k):  # noqa: ANN002, ANN003
        writer_calls.append(1)
        return ProjectionWriteResult(
            projection_trade_date=date(2026, 9, 18), market_rows=1, scope_rows=0
        )

    async def _current(_session):
        return dict(current)

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)
    monkeypatch.setattr(proj, "build_market_records", _build)
    monkeypatch.setattr(proj, "iter_scope_record_chunks", _iter)
    monkeypatch.setattr(rebuild, "replace_dashboard_projection", _writer)
    monkeypatch.setattr(rebuild, "_current_membership_versions", _current)

    with pytest.raises(rebuild.ProjectionInputChangedError):
        asyncio.run(
            rebuild.rebuild_market_dashboard_projection(
                date(2026, 9, 18), session_factory=_factory(events)
            )
        )
    assert writer_calls == []  # 绝不写 projection


# ---------------------------------------------------------------
# context invariants
# ---------------------------------------------------------------
@pytest.mark.parametrize("kind", ["duplicate", "key_mismatch"])
def test_orchestration_rejects_invalid_context(monkeypatch, kind):
    events: list[str] = []
    b1, b2 = uuid4(), uuid4()
    ctx = (
        _context([b1, b1], {b1: "mv1"}) if kind == "duplicate" else _context([b1, b2], {b1: "mv1"})
    )
    writer_calls: list[int] = []

    async def _prepare(_session, _end_date):
        return ctx

    def _build(_context):
        return ["MARKET"]

    async def _writer(*_a, **_k):  # noqa: ANN002, ANN003
        writer_calls.append(1)
        return None

    monkeypatch.setattr(proj, "prepare_projection_context", _prepare)
    monkeypatch.setattr(proj, "build_market_records", _build)
    monkeypatch.setattr(rebuild, "replace_dashboard_projection", _writer)

    with pytest.raises(ValueError):
        asyncio.run(
            rebuild.rebuild_market_dashboard_projection(
                date(2026, 9, 18), session_factory=_factory(events)
            )
        )
    assert writer_calls == []


# ---------------------------------------------------------------
# manifest registration regression
# ---------------------------------------------------------------
def test_manifest_registers_rebuild_orchestration_contract():
    manifest = load_evidence_manifest(
        _VERIFY_DIR / "evidence_manifest.json",
        repo_root=Path(__file__).resolve().parents[2],
    )
    by_id = {c.contract_id: c for c in manifest.contracts}
    contract = by_id.get("market_dashboard_projection_rebuild_orchestration")
    assert contract is not None, "F1C orchestration contract 必须登记进 evidence_manifest.json"
    assert contract.required is True
    assert "targeted-pg" in contract.gates
    assert contract.test_selectors == ("tests/test_market_dashboard_projection_rebuild_pg.py",), (
        f"selector 必须精确为该 PG 文件: {contract.test_selectors}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
