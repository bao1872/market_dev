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
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.models.user import User

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
# ============================================================


@pytest_asyncio.fixture
async def admin_user(user_factory) -> User:
    """创建真实管理员（InviteCode.created_by 为 NOT NULL 外键，不能为 None）。"""
    return await user_factory(roles=["admin"])


def _run_alembic(args: list[str]) -> None:
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", _ALEMBIC_INI, *args],
        env=env, capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{proc.stderr}")


async def _column_default(db) -> str:
    return str((await db.execute(text(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name='invite_codes' AND column_name='grant_days'"
    ))).scalar())


async def test_093_grant_days_default_is_one(db_session) -> None:
    """093 执行后 invite_codes.grant_days column_default 必须为 1（实际查 information_schema）。"""
    default = await _column_default(db_session)
    assert default == "1", f"093 执行后 grant_days column_default 应为 1，实际 {default!r}"


async def test_093_migration_preserves_existing_rows(db_session, admin_user) -> None:
    """migration 只改变 schema default，不改变历史行：

    - 回退到 092（default=30）插入样本行（不指定 grant_days，依赖旧 default）-> 30
    - 升级回 093（default=1）后，该历史行 grant_days 仍为 30（未被 migration 改写）
    - 升级后新插入行（不指定 grant_days）取新 default=1
    - 最终 column_default 仍为 1
    """
    rc_hist = f"m093hist-{uuid.uuid4().hex}"
    rc_new = f"m093new-{uuid.uuid4().hex}"
    try:
        # 1) 回退到 092（default=30）
        _run_alembic(["downgrade", "-1"])
        # 2) 插入样本行（不指定 grant_days，依赖旧 default=30）
        await db_session.execute(
            text("INSERT INTO invite_codes (code_hash, created_by) VALUES (:rc, :cb)"),
            {"rc": rc_hist, "cb": admin_user.id},
        )
        await db_session.flush()
        before = (await db_session.execute(
            text("SELECT grant_days FROM invite_codes WHERE code_hash=:rc"),
            {"rc": rc_hist},
        )).scalar()
        assert before == 30, f"092 下样本行 grant_days 应为 30（旧 default），实际 {before}"

        # 3) 升级回 093（default=1）
        _run_alembic(["upgrade", "head"])
        # 4) 已存在样本行不应被 migration 改写（只改 default，不改历史行）
        after = (await db_session.execute(
            text("SELECT grant_days FROM invite_codes WHERE code_hash=:rc"),
            {"rc": rc_hist},
        )).scalar()
        assert after == 30, f"migration 后历史行 grant_days 应保持不变（30），实际 {after}"

        # 5) 新插入行（不指定 grant_days）应取新 default=1
        await db_session.execute(
            text("INSERT INTO invite_codes (code_hash, created_by) VALUES (:rc, :cb)"),
            {"rc": rc_new, "cb": admin_user.id},
        )
        await db_session.flush()
        new_default = (await db_session.execute(
            text("SELECT grant_days FROM invite_codes WHERE code_hash=:rc"),
            {"rc": rc_new},
        )).scalar()
        assert new_default == 1, f"093 下新行 grant_days 应取 default=1，实际 {new_default}"

        # 6) 最终 default 仍为 1
        default = await _column_default(db_session)
        assert default == "1", f"最终 column_default 应为 1，实际 {default!r}"
    finally:
        # 始终恢复 093 基线，避免影响同 session 内后续 contract
        try:
            _run_alembic(["upgrade", "head"])
        except Exception:  # noqa: BLE001 - 恢复失败不改变测试结论，仅记录
            pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
