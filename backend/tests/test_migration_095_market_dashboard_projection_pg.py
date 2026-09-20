"""Migration 095 contract — Market Dashboard projection schema（真实 PostgreSQL 部分）。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- 095 执行后两张表存在；
- market PK = trade_date；scope PK = (board_id, trade_date)；
- scope FK board_id -> market_boards.id ON DELETE CASCADE（confdeltype='c'）；
- scope 有 trade_date 索引；
- 两张表都不存在 ratio / normalized_index / delta / scope name/type/level 列；
- CHECK 约束实际存在并覆盖 above <= valid <= member、valid_return <= member；
- 095 → 094 downgrade 删除两表，再 upgrade 095 恢复（完整回滚）。

============================================================================
锁安全约束（与 093/094 contract 一致）：
conftest 的 db_session 是 savepoint 模式，测试期间持有外层未提交事务。若在该事务打开时
执行 `alembic upgrade/downgrade`（ALTER TABLE/DROP TABLE 需 ACCESS EXCLUSIVE），会形成
pytest 等 alembic / alembic 等 pytest 的 lock-wait 死环。
因此本文件所有真实 PG 操作使用 TestAsyncSessionLocal 短事务（open→execute→commit→close），
且每次 _run_alembic() 前必须无打开连接/事务。禁止在 db_session 内执行 Alembic DDL。
============================================================================
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = "/app/alembic.ini"

_MARKET_TABLE = "market_dashboard_market_daily"
_SCOPE_TABLE = "market_dashboard_scope_daily"
_TABLES = (_MARKET_TABLE, _SCOPE_TABLE)

_WINDOWS = (5, 10, 20, 50, 120)

_FORBIDDEN_COLUMNS = frozenset(
    {"ratio", "normalized_index", "scope_name", "scope_type", "hierarchy_level"}
)


# ============================================================
# helpers
# ============================================================
def _run_alembic(args: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr}")


async def _fetch_all(sql: str, params: dict | None = None) -> list[tuple]:
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(text(sql), params or {})
        rows = list(result.fetchall())
        await session.commit()
        return rows


def _pg_char_text(value: object) -> str:
    """归一 asyncpg 对 PG ``"char"`` 类型的返回值。

    asyncpg 会把 ``pg_constraint.confdeltype`` 这类 ``"char"`` 读成 ``bytes``（如 ``b"c"``），
    归一为 ``str`` 供断言。``confdeltype`` 是单字符代码，ASCII 解码足够。
    """
    if isinstance(value, bytes):
        return value.decode("ascii")
    return str(value)


# ============================================================
# A. 两张表存在
# ============================================================
@pytest.mark.asyncio
async def test_095_tables_exist() -> None:
    rows = await _fetch_all(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_name IN "
        "(:t1, :t2)",
        {"t1": _MARKET_TABLE, "t2": _SCOPE_TABLE},
    )
    found = {r[0] for r in rows}
    assert found == set(_TABLES), f"095 后两表必须存在，实际 {found}"


# ============================================================
# B. market PK = trade_date
# ============================================================
@pytest.mark.asyncio
async def test_095_market_pk_is_trade_date() -> None:
    rows = await _fetch_all(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = CAST(:tbl AS regclass) AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        {"tbl": _MARKET_TABLE},
    )
    pk = [r[0] for r in rows]
    assert pk == ["trade_date"], f"market PK 必须为 [trade_date]，实际 {pk}"


# ============================================================
# C. scope PK = (board_id, trade_date)
# ============================================================
@pytest.mark.asyncio
async def test_095_scope_pk_is_board_and_trade_date() -> None:
    rows = await _fetch_all(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = CAST(:tbl AS regclass) AND i.indisprimary
        ORDER BY array_position(i.indkey, a.attnum)
        """,
        {"tbl": _SCOPE_TABLE},
    )
    pk = [r[0] for r in rows]
    assert pk == ["board_id", "trade_date"], f"scope PK 必须为 [board_id, trade_date]，实际 {pk}"


# ============================================================
# D. scope FK -> market_boards.id ON DELETE CASCADE
# ============================================================
@pytest.mark.asyncio
async def test_095_scope_fk_cascade() -> None:
    rows = await _fetch_all(
        """
        SELECT confrelid::regclass::text AS ref_table, confdeltype
        FROM pg_constraint
        WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'f'
        """,
        {"tbl": _SCOPE_TABLE},
    )
    assert rows, "scope 表必须有外键"
    ref_tables = {r[0] for r in rows}
    assert ref_tables == {"market_boards"}, f"FK 目标必须为 market_boards，实际 {ref_tables}"
    deltypes = {_pg_char_text(r[1]) for r in rows}
    assert deltypes == {"c"}, f"FK ON DELETE 必须为 CASCADE（confdeltype='c'），实际 {deltypes}"


def test_pg_char_text_normalizes_asyncpg_bytes() -> None:
    """asyncpg 将 PG "char" 类型返回为 bytes（如 b'c'）；helper 必须归一为 str。"""
    assert _pg_char_text(b"c") == "c"
    assert _pg_char_text("c") == "c"


# ============================================================
# E. scope 有 trade_date 索引
# ============================================================
@pytest.mark.asyncio
async def test_095_scope_trade_date_index() -> None:
    rows = await _fetch_all(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname='public' AND tablename = :tbl",
        {"tbl": _SCOPE_TABLE},
    )
    names = {r[0] for r in rows}
    assert "ix_market_dashboard_scope_daily_trade_date" in names, (
        f"缺少 trade_date 索引，实际索引: {names}"
    )
    match = [r[1] for r in rows if r[0] == "ix_market_dashboard_scope_daily_trade_date"]
    assert match and "trade_date" in match[0], "索引必须建立在 trade_date 上"


# ============================================================
# F. 禁止列不存在
# ============================================================
@pytest.mark.asyncio
async def test_095_no_forbidden_columns() -> None:
    rows = await _fetch_all(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name IN (:t1, :t2)",
        {"t1": _MARKET_TABLE, "t2": _SCOPE_TABLE},
    )
    cols = {r[1] for r in rows}
    bad = {c for c in cols if c in _FORBIDDEN_COLUMNS or c.endswith("_delta") or "ratio" in c}
    assert not bad, f"存在禁止列: {bad}"


# ============================================================
# G. CHECK 约束存在且覆盖不变量
# ============================================================
async def _check_defs(table: str) -> dict[str, str]:
    rows = await _fetch_all(
        """
        SELECT conname, pg_get_constraintdef(oid) AS def
        FROM pg_constraint
        WHERE conrelid = CAST(:tbl AS regclass) AND contype = 'c'
        """,
        {"tbl": table},
    )
    return {r[0]: r[1] for r in rows}


@pytest.mark.asyncio
async def test_095_market_check_constraints_cover_invariants() -> None:
    defs = await _check_defs(_MARKET_TABLE)
    joined = " ".join(defs.values())
    assert "member_count >= 0" in joined, "缺少 member_count >= 0"
    assert "valid_return_count >= 0" in joined, "缺少 valid_return_count >= 0"
    assert "valid_return_count <= member_count" in joined, "缺少 valid_return <= member"
    for k in _WINDOWS:
        assert f"ma{k}_above_count <= ma{k}_valid_count" in joined, (
            f"缺少 ma{k}_above <= ma{k}_valid"
        )
        assert f"ma{k}_valid_count <= member_count" in joined, f"缺少 ma{k}_valid <= member"


@pytest.mark.asyncio
async def test_095_scope_check_constraints_cover_invariants() -> None:
    defs = await _check_defs(_SCOPE_TABLE)
    joined = " ".join(defs.values())
    assert "member_count >= 0" in joined, "缺少 member_count >= 0"
    assert "valid_return_count <= member_count" in joined, "缺少 valid_return <= member"
    for k in _WINDOWS:
        assert f"ma{k}_above_count <= ma{k}_valid_count" in joined, (
            f"缺少 ma{k}_above <= ma{k}_valid"
        )
        assert f"ma{k}_valid_count <= member_count" in joined, f"缺少 ma{k}_valid <= member"


# ============================================================
# downgrade / upgrade 完整回滚
# ============================================================
@pytest.mark.asyncio
async def test_095_downgrade_then_upgrade_restores_tables() -> None:
    """显式 downgrade 094 删除两表，再 upgrade 095 恢复；每一步前无打开事务。"""
    try:
        _run_alembic(["downgrade", "094_invite_code_ciphertext"])
        rows = await _fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN (:t1, :t2)",
            {"t1": _MARKET_TABLE, "t2": _SCOPE_TABLE},
        )
        assert not rows, "downgrade 后两表必须被删除"

        _run_alembic(["upgrade", "095_market_dashboard_projection"])
        rows = await _fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN (:t1, :t2)",
            {"t1": _MARKET_TABLE, "t2": _SCOPE_TABLE},
        )
        assert {r[0] for r in rows} == set(_TABLES), "upgrade 095 后两表必须恢复"
    finally:
        # 无论如何恢复到正式 head（此刻无打开事务）
        _run_alembic(["upgrade", "head"])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
