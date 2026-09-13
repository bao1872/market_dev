"""Migration 093 contract — invite_codes.grant_days server_default 30 -> 1。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg / full-closure）：
- 093 执行后 invite_codes.grant_days column_default = 1（实际查 information_schema）
- migration 前已有样本行 grant_days=X，migration 后仍为 X（只改变 schema default，不改变历史行）
- 093 只改 invite_codes.grant_days 的 server_default，不触碰 subscriptions / expires_at

同时包含源码级静态契约检查（revision 链、server_default 值、只改 invite_codes、无 UPDATE/DELETE
历史行），与本仓库 086/087 migration contract 范式一致（不连库部分可在 PURE_UNIT_TEST=1 下收集）。

实现（真实 PG 部分）：验证库已被 alembic upgrade head 置于 093，本文件再做一次
downgrade -> 092（default=30）插入样本行 -> upgrade head -> 093（default=1），
证明默认由 30 变为 1，且已存在的样本行 grant_days 不被 migration 改写。

============================================================================
锁安全约束（来自 GitHub review）：
本项目 conftest 的 db_session 是 savepoint 模式——它在整个测试期间持有
「外层未提交事务」，对 invite_codes 持 RowExclusive 写锁。若在该事务仍打开时
另起进程执行 `alembic upgrade/downgrade`（ALTER TABLE 需 ACCESS EXCLUSIVE 锁），
会形成 pytest 等 alembic / alembic 等 pytest 的 lock-wait 死环，最终 timeout 失败。

因此本文件所有真实 PG 操作都使用 TestAsyncSessionLocal 的「短事务」：
    open session -> execute -> commit -> close
并在每一次 _run_alembic() 调用之前，保证没有任何打开的数据库连接/事务。
禁止在 db_session（savepoint fixture）内部执行 Alembic DDL。
============================================================================
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models.user import User
from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_MIGRATION_FILE = (
    Path(__file__).parent.parent
    / "alembic"
    / "versions"
    / "093_invite_grant_days_default.py"
)
# 远程验证容器内 Live Mount 根；仅真实 PG 测试（postgres + REMOTE_VERIFY）会用到。
_ALEMBIC_INI = "/app/alembic.ini"


# ============================================================
# 源码级静态契约（不连库，与 086/087 migration contract 同范式）
# ============================================================


def _migration_source() -> str:
    return _MIGRATION_FILE.read_text(encoding="utf-8")


def test_migration_file_exists():
    assert _MIGRATION_FILE.exists(), f"迁移文件不存在: {_MIGRATION_FILE}"


def test_migration_revision_chain():
    src = _migration_source()
    assert 'revision = "093_invite_grant_days_default"' in src, "revision 必须为 093_invite_grant_days_default"
    assert 'down_revision = "092_review_core_only_identity"' in src, (
        "down_revision 必须为 092_review_core_only_identity"
    )


def test_upgrade_only_changes_invite_grant_days_default():
    """upgrade 只改 invite_codes.grant_days 的 server_default，不得触碰其它表。"""
    src = _migration_source()
    up_body = src[src.index("def upgrade"):src.index("def downgrade")]
    assert 'op.alter_column("invite_codes", "grant_days"' in up_body, (
        "upgrade 必须 alter invite_codes.grant_days"
    )
    assert "server_default=sa.text(\"1\")" in up_body, "upgrade 必须 server_default=1"
    assert "subscriptions" not in up_body, "093 不得触碰 subscriptions（expires_at 不动）"
    for verb in ("op.add_column", "op.drop_column", "op.create_table", "op.drop_table"):
        assert verb not in up_body, f"upgrade 不得含 {verb}（只改 default）"


def test_downgrade_restores_default_thirty():
    """downgrade 必须回退 server_default 为 30。"""
    src = _migration_source()
    dn_body = src[src.index("def downgrade"):]
    assert "server_default=sa.text(\"30\")" in dn_body, "downgrade 必须回退 server_default=30"


def test_upgrade_does_not_modify_history_rows():
    """upgrade 不得修改任何 invite_codes 历史业务记录。"""
    src = _migration_source()
    up_body = src[src.index("def upgrade"):src.index("def downgrade")]
    assert "UPDATE invite_codes" not in up_body, "upgrade 不得 UPDATE invite_codes 历史行"
    assert "DELETE FROM invite_codes" not in up_body, "upgrade 不得 DELETE invite_codes 历史行"


def test_downgrade_does_not_modify_history_rows():
    """downgrade 不得修改任何 invite_codes 历史业务记录。"""
    src = _migration_source()
    dn_body = src[src.index("def downgrade"):]
    assert "UPDATE invite_codes" not in dn_body, "downgrade 不得 UPDATE invite_codes 历史行"
    assert "DELETE FROM invite_codes" not in dn_body, "downgrade 不得 DELETE invite_codes 历史行"


def test_migration_module_imports_and_constants():
    """迁移模块可在不连库情况下导入，且 revision 常量正确。"""
    spec = importlib.util.spec_from_file_location("m093_contract_check", _MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    assert module.revision == "093_invite_grant_days_default"
    assert module.down_revision == "092_review_core_only_identity"
    assert callable(getattr(module, "upgrade", None))
    assert callable(getattr(module, "downgrade", None))


# ============================================================
# 真实 PG 验证（postgres + PANJI_REMOTE_VERIFY_DB_TEST=1）
# ------------------------------------------------------------
# 所有 DB 操作使用 TestAsyncSessionLocal 短事务：open -> execute -> commit -> close。
# 禁止在 db_session（savepoint fixture）事务内执行 Alembic DDL（RowExclusive ↔
# ALTER TABLE 的 lock-wait 死环）。每次 _run_alembic() 前必须无打开事务。
# ============================================================


def _run_alembic(args: list[str]) -> None:
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        env=env, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr}")


async def _create_verify_user() -> uuid.UUID:
    """创建真实用户（InviteCode.created_by 为 NOT NULL 外键），短事务 commit+close。"""
    async with TestAsyncSessionLocal() as session:
        user = User(
            email=f"m093-{uuid.uuid4().hex}@test.local",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        session.add(user)
        await session.commit()
        return user.id


async def _insert_invite(*, code_hash: str, created_by: uuid.UUID) -> int:
    """插入一行 invite_codes（不指定 grant_days，依赖当前 schema default），返回实际 grant_days。

    短事务：open -> execute(RETURNING grant_days) -> commit -> close。
    """
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "INSERT INTO invite_codes (code_hash, created_by) "
                "VALUES (:code_hash, :created_by) RETURNING grant_days"
            ),
            {"code_hash": code_hash, "created_by": created_by},
        )
        grant_days = int(result.scalar_one())
        await session.commit()
        return grant_days


async def _read_grant_days(code_hash: str) -> int:
    """读回指定 code_hash 的 grant_days（短事务，commit 后独立连接）。"""
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT grant_days FROM invite_codes WHERE code_hash = :code_hash"
            ),
            {"code_hash": code_hash},
        )
        return int(result.scalar_one())


async def _read_column_default() -> str:
    """实际查 information_schema，返回 invite_codes.grant_days 的 column_default。"""
    async with TestAsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_name='invite_codes' AND column_name='grant_days'"
            )
        )
        return str(result.scalar())


async def _cleanup_verify_rows(
    *,
    created_by: uuid.UUID,
    code_hashes: tuple[str, str],
) -> None:
    """精确清理本测试自己 commit 的 fixtures（验证隔离，防止 full-closure 阶段污染）。

    仅按本测试自己的 exact code_hash / exact user_id 删除：
    禁止 LIKE / TRUNCATE / 全表 DELETE / 跨测试清理。
    删除顺序：invite_codes 在前（其 created_by -> users.id 有外键），users 在后。
    短事务：open -> execute -> commit -> close；执行前无打开事务。
    """
    async with TestAsyncSessionLocal() as session:
        await session.execute(
            text(
                "DELETE FROM invite_codes WHERE code_hash IN (:hist, :new)"
            ),
            {"hist": code_hashes[0], "new": code_hashes[1]},
        )
        await session.execute(
            text("DELETE FROM users WHERE id = :user_id"),
            {"user_id": created_by},
        )
        await session.commit()


@pytest.mark.asyncio
async def test_093_grant_days_default_is_one() -> None:
    """093 执行后 invite_codes.grant_days column_default 必须为 1（实际查 information_schema）。"""
    default = await _read_column_default()
    assert default == "1", f"093 执行后 grant_days column_default 应为 1，实际 {default!r}"


@pytest.mark.asyncio
async def test_093_migration_preserves_existing_rows() -> None:
    """migration 只改变 schema default，不改变历史行：

    当前 verify DB = 093
    → 创建 test user（COMMIT+CLOSE）
    → alembic downgrade -1（现在 = 092，default=30）
    → INSERT 历史样本行（不指定 grant_days，依赖旧 default）→ 30（COMMIT+CLOSE）
    → alembic upgrade head（现在 = 093，default=1）
    → 重新读取历史样本行仍 = 30（未被 migration 改写）（COMMIT+CLOSE）
    → INSERT 新行（不指定 grant_days）取新 default=1（COMMIT+CLOSE）
    → 最终 column_default = 1

    每一步 Alembic 调用前均无打开事务，避免 RowExclusive ↔ ALTER TABLE 锁死环。
    """
    created_by = await _create_verify_user()
    rc_hist = f"m093hist-{uuid.uuid4().hex}"
    rc_new = f"m093new-{uuid.uuid4().hex}"
    try:
        # 1) 回退到 092（default=30）；此刻无打开事务
        _run_alembic(["downgrade", "-1"])
        # 2) 092 下插入样本行（依赖旧 default=30），短事务 commit+close
        hist_before = await _insert_invite(code_hash=rc_hist, created_by=created_by)
        assert hist_before == 30, f"092 下样本行 grant_days 应为 30（旧 default），实际 {hist_before}"

        # 3) 升级回 093（default=1）；此刻无打开事务
        _run_alembic(["upgrade", "head"])
        # 4) 已存在样本行不应被 migration 改写（只改 default，不改历史行）
        hist_after = await _read_grant_days(rc_hist)
        assert hist_after == 30, f"migration 后历史行 grant_days 应保持不变（30），实际 {hist_after}"

        # 5) 093 下新插入行（不指定 grant_days）应取新 default=1，短事务 commit+close
        new_default = await _insert_invite(code_hash=rc_new, created_by=created_by)
        assert new_default == 1, f"093 下新行 grant_days 应取 default=1，实际 {new_default}"

        # 6) 最终 column_default 仍为 1
        default = await _read_column_default()
        assert default == "1", f"最终 column_default 应为 1，实际 {default!r}"
    finally:
        # 先恢复 schema 到正式 head（此刻无打开事务）
        try:
            _run_alembic(["upgrade", "head"])
        finally:
            # 再精确清理本测试自己 commit 的两条 invite + 一个 user（遵守 FK 顺序，invite 先删）
            await _cleanup_verify_rows(
                created_by=created_by,
                code_hashes=(rc_hist, rc_new),
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
