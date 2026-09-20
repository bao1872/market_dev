"""F1B projection persistence — 纯单元结构 / batch / guard（不连 DB）。

覆盖：
- bulk insert 分批（<= PERSIST_BATCH_SIZE；2501 -> 1000/1000/501）
- writer 要求 fresh session；空 market_records / 非法 market dates 拒绝（begin 之前）
- 源码合同：LOCK TABLE ... IN SHARE ROW EXCLUSIVE MODE 在 DELETE 之前；无 TRUNCATE /
  ACCESS EXCLUSIVE / ON CONFLICT / 裸 session.commit(；单一 commit boundary
- manifest 登记回归（复用 load_evidence_manifest）

注意：本文件**不得**出现 conftest 的 PG 源码标志，否则整模块会被判为 postgres 并在 PURE_UNIT 下跳过。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.models.market_dashboard import MarketDashboardScopeDaily
from app.repositories.market_dashboard_projection_repository import (
    PERSIST_BATCH_SIZE,
    _bulk_insert,
    replace_dashboard_projection,
)

_REPO_FILE = (
    Path(__file__).parent.parent
    / "app"
    / "repositories"
    / "market_dashboard_projection_repository.py"
)

# 复用仓库既有 manifest loader（scripts/verify，纯 stdlib，无 app 依赖）
_VERIFY_DIR = Path(__file__).resolve().parents[2] / "scripts" / "verify"
if str(_VERIFY_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFY_DIR))

from evidence_manifest import load_evidence_manifest  # noqa: E402


class _FakeSession:
    """仅用于 begin 之前的守卫 / 纯分批逻辑（不执行真实 SQL）。"""

    def __init__(self, *, in_tx: bool = False) -> None:
        self._in_tx = in_tx
        self.payloads: list[object] = []

    def in_transaction(self) -> bool:
        return self._in_tx

    async def execute(self, statement, parameters=None):  # noqa: ANN001
        self.payloads.append(parameters)
        return None


def _executable_source(path: Path) -> str:
    """剥离注释与 docstring，只留可执行代码（避免 docstring 里的"禁止 X"被误判为回潮）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    return ast.unparse(tree)


def _mkt(d):  # noqa: ANN001
    return {"trade_date": d}


# ---------------------------------------------------------------
# batch regression
# ---------------------------------------------------------------
def test_bulk_insert_batches_at_persist_batch_size():
    rows = [{"ma5_valid_count": 1} for _ in range(2501)]
    session = _FakeSession()

    async def _run():
        return await _bulk_insert(session, MarketDashboardScopeDaily.__table__, rows)

    total = asyncio.run(_run())
    sizes = [len(p) for p in session.payloads]  # type: ignore[arg-type]
    assert total == 2501
    assert sizes == [1000, 1000, 501]
    assert all(size <= PERSIST_BATCH_SIZE for size in sizes)
    assert PERSIST_BATCH_SIZE == 1000


# ---------------------------------------------------------------
# writer guards（begin 之前）
# ---------------------------------------------------------------
def test_writer_requires_fresh_session():
    session = _FakeSession(in_tx=True)
    with pytest.raises(RuntimeError):
        asyncio.run(
            replace_dashboard_projection(
                session,  # type: ignore[arg-type]
                market_records=[_mkt(date(2026, 9, 18))],
                scope_chunks=[],
                expected_membership_versions={},
            )
        )


def test_writer_rejects_empty_market_records():
    with pytest.raises(ValueError):
        asyncio.run(
            replace_dashboard_projection(
                _FakeSession(),  # type: ignore[arg-type]
                market_records=[],
                scope_chunks=[],
                expected_membership_versions={},
            )
        )


def test_writer_rejects_duplicate_market_dates():
    d = date(2026, 9, 18)
    with pytest.raises(ValueError):
        asyncio.run(
            replace_dashboard_projection(
                _FakeSession(),  # type: ignore[arg-type]
                market_records=[_mkt(d), _mkt(d)],
                scope_chunks=[],
                expected_membership_versions={},
            )
        )


def test_writer_rejects_too_many_market_dates():
    end = date(2026, 9, 18)
    dates = [end - timedelta(days=i) for i in range(251)]
    with pytest.raises(ValueError):
        asyncio.run(
            replace_dashboard_projection(
                _FakeSession(),  # type: ignore[arg-type]
                market_records=[_mkt(d) for d in dates],
                scope_chunks=[],
                expected_membership_versions={},
            )
        )


def test_writer_rejects_non_date_market_trade_date():
    with pytest.raises(ValueError):
        asyncio.run(
            replace_dashboard_projection(
                _FakeSession(),  # type: ignore[arg-type]
                market_records=[{"trade_date": "2026-09-18"}],
                scope_chunks=[],
                expected_membership_versions={},
            )
        )


# ---------------------------------------------------------------
# source contract（可执行代码）
# ---------------------------------------------------------------
def test_writer_lock_and_single_transaction_source_contract():
    code = _executable_source(_REPO_FILE)
    assert "LOCK TABLE" in code
    assert "IN SHARE ROW EXCLUSIVE MODE" in code
    # 禁止项（只看可执行代码；docstring 里的"禁止 X"不算回潮）
    assert "ACCESS EXCLUSIVE" not in code
    assert "TRUNCATE" not in code
    assert "ON CONFLICT" not in code.upper()
    assert "session.commit(" not in code  # 单一 commit boundary（session.begin）
    assert code.count("session.begin(") == 1
    # lock 必须先于 DELETE
    assert code.index("await session.execute(_WRITE_LOCK_SQL)") < code.index(
        "delete(MarketDashboardScopeDaily)"
    )


# ---------------------------------------------------------------
# manifest registration regression
# ---------------------------------------------------------------
def test_manifest_registers_atomic_persistence_contract():
    manifest = load_evidence_manifest(
        _VERIFY_DIR / "evidence_manifest.json",
        repo_root=Path(__file__).resolve().parents[2],
    )
    by_id = {c.contract_id: c for c in manifest.contracts}
    contract = by_id.get("market_dashboard_projection_atomic_persistence")
    assert contract is not None, "F1B 原子持久化 contract 必须登记进 evidence_manifest.json"
    assert contract.required is True
    assert "targeted-pg" in contract.gates
    assert contract.test_selectors == (
        "tests/test_market_dashboard_projection_persistence_pg.py",
    ), f"selector 必须精确为该 PG 文件: {contract.test_selectors}"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
