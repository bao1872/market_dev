"""Migration 094 contract — invite_codes.code_ciphertext (TEXT NULL)。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- 094 执行后 invite_codes.code_ciphertext 存在，data_type=text，is_nullable=YES；
- 094 → 093 downgrade 后该列被删除，其他 invite_codes 数据/字段不受影响；
- 再 upgrade head 恢复该列；
- 历史 invite_codes 行不因该 migration 被改写（只 add/drop column）。

同时包含源码级静态契约检查（revision 链 / 只 add_column / 不 UPDATE/DELETE 历史行），
与 086/087/093 migration contract 同范式。

============================================================================
锁安全约束（与 093 contract 一致）：
conftest 的 db_session 是 savepoint 模式，测试期间持有外层未提交事务。若在该事务打开时
执行 `alembic upgrade/downgrade`（ALTER TABLE 需 ACCESS EXCLUSIVE），会形成
pytest 等 alembic / alembic 等 pytest 的 lock-wait 死环。
因此本文件所有真实 PG 操作使用 TestAsyncSessionLocal 短事务（open→execute→commit→close），
且每次 _run_alembic() 前必须无打开连接/事务。禁止在 db_session 内执行 Alembic DDL。
============================================================================
"""
from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "094_invite_code_ciphertext.py"
)
_ALEMBIC_INI = "/app/alembic.ini"


# ============================================================
# 源码级静态契约（不连库）
# ============================================================
def _migration_source() -> str:
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def test_migration_file_exists() -> None:
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_migration_revision_chain() -> None:
    src = _migration_source()
    assert re.search(
        r'\brevision\b\s*(?::[^=]+)?=\s*"094_invite_code_ciphertext"', src
    ), "revision 必须为 094_invite_code_ciphertext"
    assert re.search(
        r'\bdown_revision\b\s*(?::[^=]+)?=\s*"093_invite_grant_days_default"', src
    ), "down_revision 必须为 093_invite_grant_days_default"


def test_upgrade_only_adds_ciphertext_column() -> None:
    src = _migration_source()
    up_body = src[src.index("def upgrade"):src.index("def downgrade")]
    assert re.search(
        r'op\.add_column\(\s*"invite_codes"', up_body,
    ), "upgrade 必须 add_column invite_codes"
    assert '"code_ciphertext"' in up_body, "必须新增 code_ciphertext 列"
    assert "sa.Text()" in up_body, "code_ciphertext 必须为 TEXT"
    assert "nullable=True" in up_body, "code_ciphertext 必须 nullable（历史行不 backfill）"
    # 只 add column：不得 drop/alter/create table，不得改写 code_hash
    for verb in ("op.drop_column", "op.alter_column", "op.create_table", "op.drop_table"):
        assert verb not in up_body, f"upgrade 不得含 {verb}"
    assert "UPDATE invite_codes" not in up_body, "upgrade 不得 UPDATE 历史行"
    assert "DELETE FROM invite_codes" not in up_body, "upgrade 不得 DELETE 历史行"


def test_downgrade_drops_ciphertext_column() -> None:
    src = _migration_source()
    dn_body = src[src.index("def downgrade"):]
    assert re.search(
        r'op\.drop_column\(\s*"invite_codes"\s*,\s*"code_ciphertext"', dn_body,
    ), "downgrade 必须 drop_column invite_codes.code_ciphertext"
    assert "UPDATE invite_codes" not in dn_body, "downgrade 不得 UPDATE 历史行"
    assert "DELETE FROM invite_codes" not in dn_body, "downgrade 不得 DELETE 历史行"


def test_migration_module_imports_and_constants() -> None:
    spec = importlib.util.spec_from_file_location("m094_contract_check", _MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module.revision == "094_invite_code_ciphertext"
    assert module.down_revision == "093_invite_grant_days_default"
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


# ============================================================
# 真实 PG 验证
# ============================================================
def _run_alembic(args: list[str]) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        env=dict(os.environ), capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr}")


async def _read_column_info() -> tuple[str, str] | None:
    """返回 (data_type, is_nullable)；列不存在返回 None。短事务。"""
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT data_type, is_nullable FROM information_schema.columns "
                "WHERE table_name='invite_codes' AND column_name='code_ciphertext'"
            )
        )
        row = result.first()
        if row is None:
            return None
        return str(row[0]), str(row[1])


@pytest.mark.asyncio
async def test_094_column_exists_text_nullable() -> None:
    """094 执行后 code_ciphertext 存在、TEXT、nullable。"""
    info = await _read_column_info()
    assert info is not None, "invite_codes.code_ciphertext 必须存在（094 已 upgrade head）"
    data_type, is_nullable = info
    assert data_type == "text", f"code_ciphertext 应为 text，实际 {data_type}"
    assert is_nullable == "YES", f"code_ciphertext 必须 nullable，实际 {is_nullable}"


@pytest.mark.asyncio
async def test_094_downgrade_drops_then_upgrade_restores() -> None:
    """094 → 093 删除该列；再 head 恢复；期间其它列不受影响。"""
    try:
        # 1) downgrade -1 → 093：列必须消失
        _run_alembic(["downgrade", "-1"])
        assert await _read_column_info() is None, "downgrade 后 code_ciphertext 必须被删除"

        # 2) 其它既有列仍在（downgrade 不得误删其它字段）
        async with TestAsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='invite_codes' AND column_name IN "
                    "('code_hash','grant_days','capabilities','monitor_limit')"
                )
            )
            cols = {r[0] for r in result.fetchall()}
        assert cols == {"code_hash", "grant_days", "capabilities", "monitor_limit"}, (
            f"downgrade 不应影响其它列，缺失: {cols}"
        )

        # 3) upgrade head → 094：列恢复且仍为 TEXT NULL
        _run_alembic(["upgrade", "head"])
        info = await _read_column_info()
        assert info is not None, "upgrade head 后 code_ciphertext 必须恢复"
        assert info[0] == "text" and info[1] == "YES"
    finally:
        # 无论如何恢复到正式 head（此刻无打开事务）
        _run_alembic(["upgrade", "head"])


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
