"""Migration 097 contract — split market_review out of market_data（真实 PostgreSQL 部分）。

[PANJI-REVIEW-CAPABILITY-SPLIT] 097 是纯数据迁移：对每条真实授予（非 admin_revoke）的
``market_data`` 行，为同一 user 补一条 ``market_review`` 行（source='migration_review_split'），
granted_at / expires_at 原样复制、watchlist_limit 与 granted_by 置 NULL；
已存在 market_review 的用户由 NOT EXISTS 跳过；downgrade 只删除本迁移写入的行。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- Case A：invite_code 的 market_data → market_review 精确复制（时间戳 / NULL 字段）；
- Case B：admin_grant 的 market_data → 同样补 market_review；
- Case C：admin_revoke tombstone → 不得补 market_review；
- Case D：无 market_data（仅 self_selection）→ 不得补 market_review；
- Case E：已存在 admin_grant market_review → 不被覆盖、不产生重复（NOT EXISTS 生效）；
- Case F：downgrade 096 只删 migration_review_split 行，admin_grant 的 market_review 存活；
          再 upgrade head 精确重建，仍无重复。

============================================================================
锁安全约束（与 095/096 contract 一致）：
conftest 的 db_session 是 savepoint 模式；本文件所有真实 PG 操作使用
TestAsyncSessionLocal 短事务，且每次 _run_alembic() 前必须无打开连接/事务。
禁止在 db_session 内执行 Alembic DDL。
============================================================================
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = "/app/alembic.ini"
_PARENT_REVISION = "096_retire_legacy_market_review"
_MIGRATION_SOURCE = "migration_review_split"


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


async def _seed() -> dict:
    """创建 5 个受控测试用户及其 capability 行，返回用户 id 与期望时间戳。"""
    from app.models.user import User
    from app.models.user_capability import UserCapability

    now = datetime.now(UTC)
    granted = now - timedelta(days=5)
    expires = now + timedelta(days=25)

    def _mk_user(tag: str) -> User:
        return User(
            id=uuid.uuid4(),
            email=f"revsplit_{tag}_{uuid.uuid4().hex[:8]}@test.local",
            password_hash="not-a-real-hash",
            status="active",
        )

    u_real, u_adm, u_rev, u_none, u_exist = (
        _mk_user("real"),
        _mk_user("adm"),
        _mk_user("rev"),
        _mk_user("none"),
        _mk_user("exist"),
    )
    async with TestAsyncSessionLocal() as s:
        s.add_all([u_real, u_adm, u_rev, u_none, u_exist])
        await s.flush()
        s.add_all(
            [
                # Case A: invite_code 真实授予
                UserCapability(
                    user_id=u_real.id, capability="market_data",
                    granted_at=granted, expires_at=expires, source="invite_code",
                ),
                # Case B: admin_grant 真实授予
                UserCapability(
                    user_id=u_adm.id, capability="market_data",
                    granted_at=granted, expires_at=expires, source="admin_grant",
                ),
                # Case C: admin_revoke tombstone
                UserCapability(
                    user_id=u_rev.id, capability="market_data",
                    granted_at=granted, expires_at=expires, source="admin_revoke",
                ),
                # Case D: 无 market_data（仅 self_selection）
                UserCapability(
                    user_id=u_none.id, capability="self_selection",
                    granted_at=granted, expires_at=expires, source="invite_code", watchlist_limit=20,
                ),
                # Case E: 已存在 admin_grant market_review（expires 与 market_data 不同）
                UserCapability(
                    user_id=u_exist.id, capability="market_data",
                    granted_at=granted, expires_at=expires, source="admin_grant",
                ),
                UserCapability(
                    user_id=u_exist.id, capability="market_review",
                    granted_at=granted - timedelta(days=1),
                    expires_at=expires + timedelta(days=7),
                    source="admin_grant",
                ),
            ]
        )
        await s.commit()

    return {
        "real": u_real.id, "adm": u_adm.id, "rev": u_rev.id,
        "none": u_none.id, "exist": u_exist.id,
        "granted": granted, "expires": expires,
    }


async def _cleanup(ids: dict) -> None:
    """删除本轮种子用户（user_capabilities 依赖 ON DELETE CASCADE 一并清除）。

    按 email 前缀清理，可顺带收敛历史中断运行残留的种子行。
    """
    await _fetch_all("DELETE FROM users WHERE email LIKE 'revsplit_%@test.local'")


async def _review_rows(user_id: uuid.UUID) -> list[tuple]:
    """返回该用户所有 market_review 行 (source, granted_at, expires_at, watchlist_limit, granted_by)。"""
    return await _fetch_all(
        """
        SELECT source, granted_at, expires_at, watchlist_limit, granted_by
        FROM user_capabilities
        WHERE user_id = :uid AND capability = 'market_review'
        ORDER BY source
        """,
        {"uid": str(user_id)},
    )


# ============================================================
# Cases A–E：upgrade 数据拆分
# ============================================================
@pytest.mark.asyncio
async def test_097_upgrade_splits_market_review() -> None:
    ids = await _seed()
    try:
        # 确保处于 097 未应用状态（downgrade 到 096 会清掉本迁移写入的行）
        _run_alembic(["downgrade", _PARENT_REVISION])
        _run_alembic(["upgrade", "head"])

        # Case A：invite_code → 精确复制时间戳，NULL 字段正确
        rows = await _review_rows(ids["real"])
        assert len(rows) == 1, f"Case A: 期望恰好 1 条 market_review，实际 {rows}"
        source, granted_at, expires_at, watchlist_limit, granted_by = rows[0]
        assert source == _MIGRATION_SOURCE, f"Case A: source 应为 {_MIGRATION_SOURCE}，实际 {source}"
        assert granted_at == ids["granted"], f"Case A: granted_at 必须复制，实际 {granted_at}"
        assert expires_at == ids["expires"], f"Case A: expires_at 必须复制，实际 {expires_at}"
        assert watchlist_limit is None, f"Case A: watchlist_limit 必须为 NULL，实际 {watchlist_limit}"
        assert granted_by is None, f"Case A: granted_by 必须为 NULL，实际 {granted_by}"

        # Case B：admin_grant → 同样补 market_review
        rows_adm = await _review_rows(ids["adm"])
        assert len(rows_adm) == 1 and rows_adm[0][0] == _MIGRATION_SOURCE, (
            f"Case B: admin_grant 的 market_data 应补 market_review，实际 {rows_adm}"
        )

        # Case C：admin_revoke → 不得补 market_review
        rows_rev = await _review_rows(ids["rev"])
        assert rows_rev == [], f"Case C: admin_revoke 不得补 market_review，实际 {rows_rev}"

        # Case D：无 market_data → 不得补 market_review
        rows_none = await _review_rows(ids["none"])
        assert rows_none == [], f"Case D: 无 market_data 不得补 market_review，实际 {rows_none}"

        # Case E：已存在 admin_grant market_review → 不被覆盖、不重复
        rows_exist = await _review_rows(ids["exist"])
        assert len(rows_exist) == 1, f"Case E: 不得产生重复 market_review，实际 {rows_exist}"
        src_exist, granted_exist, expires_exist, _, _ = rows_exist[0]
        assert src_exist == "admin_grant", f"Case E: 既有 admin_grant 行不得被覆盖，实际 {src_exist}"
        assert expires_exist == ids["expires"] + timedelta(days=7), (
            f"Case E: 既有 expires_at 必须保留，实际 {expires_exist}"
        )
    finally:
        _run_alembic(["upgrade", "head"])
        await _cleanup(ids)


# ============================================================
# Case F：downgrade 精确回滚 + 再 upgrade 收敛
# ============================================================
@pytest.mark.asyncio
async def test_097_downgrade_only_removes_migration_rows() -> None:
    ids = await _seed()
    try:
        _run_alembic(["downgrade", _PARENT_REVISION])
        _run_alembic(["upgrade", "head"])

        # downgrade 097 → 096：仅删除 migration_review_split 行
        _run_alembic(["downgrade", _PARENT_REVISION])

        assert await _review_rows(ids["real"]) == [], "Case F: migration 行必须被回滚"
        assert await _review_rows(ids["adm"]) == [], "Case F: migration 行必须被回滚"

        rows_exist = await _review_rows(ids["exist"])
        assert len(rows_exist) == 1 and rows_exist[0][0] == "admin_grant", (
            f"Case F: admin_grant 的 market_review 必须存活，实际 {rows_exist}"
        )

        # 再 upgrade head：精确重建，仍无重复
        _run_alembic(["upgrade", "head"])
        rows_real = await _review_rows(ids["real"])
        assert len(rows_real) == 1 and rows_real[0][0] == _MIGRATION_SOURCE, (
            f"Case F: 再 upgrade 后必须精确重建且无重复，实际 {rows_real}"
        )
        rows_exist_again = await _review_rows(ids["exist"])
        assert len(rows_exist_again) == 1 and rows_exist_again[0][0] == "admin_grant", (
            f"Case F: 再 upgrade 不得污染既有 admin_grant 行，实际 {rows_exist_again}"
        )
    finally:
        _run_alembic(["upgrade", "head"])
        await _cleanup(ids)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
