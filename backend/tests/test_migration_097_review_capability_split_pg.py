"""Migration 097 contract — split market_review out of market_data（真实 PostgreSQL 部分）。

[PANJI-REVIEW-CAPABILITY-SPLIT] 097 是纯数据迁移，含两个独立 part：

Part 1 ``user_capabilities``：仅对**当前仍然有效**（``expires_at > now()``）的真实授予
``market_data`` 行，为同一 user 补一条 ``market_review`` 行（source='migration_review_split'），
granted_at / expires_at 原样复制、watchlist_limit 与 granted_by 置 NULL；
已过期 market_data 不补；admin_revoke tombstone 不补；已存在 market_review 的用户由
NOT EXISTS 跳过（不覆盖、不重复）。

Part 2 ``invite_codes``：迁移时刻仍 ``status='unused'`` 且同时「含 market_data / 不含
market_review」的历史邀请码，一次性追加 market_review 条目，期限单位原样复制
（days→days / months→months），不复制 watchlist_limit。``used`` / ``revoked`` 邀请码一律不改；
``capabilities IS NULL`` 的旧邀请码不制造 JSON（继续走 legacy plan fallback）。

downgrade（**数据破坏性**）：删除全部 ``market_review`` 授权行（不论来源）；unused 邀请码
移除 market_review 条目，若变为空数组则用现有状态机置 ``status='revoked'``；
used/revoked 邀请码 JSON 不重写。

真实 PostgreSQL 验证（PANJI_REMOTE_VERIFY_DB_TEST=1, plan=targeted-pg）：
- Case A ：invite_code 的 active market_data → market_review 精确复制（时间戳 / NULL 字段）；
- Case A'：admin_grant 的 active market_data → 同样补 market_review；
- Case B ：**expired** market_data → 不得补 market_review；
- Case C ：admin_revoke tombstone → 不得补 market_review；
- Case D ：无 market_data（仅 self_selection）→ 不得补 market_review；
- Case E ：已存在 admin_grant market_review → 不被覆盖、不产生重复（NOT EXISTS 生效）；
- Case E1：unused 历史邀请码 market_data days=30 → 追加 market_review days=30（不带 watchlist_limit）；
- Case E2：unused 历史邀请码 market_data months=2 → 追加 market_review months=2（单位不篡改）；
- Case E3：unused 邀请码已同时含 market_review → 不重复追加、不改写；
- Case E4：unused 邀请码仅 market_review → 不改写（market_review-only 邀请码是合法形态）；
- Case E5：unused 邀请码 capabilities IS NULL → 不制造 JSON；
- Case E6：unused 邀请码仅 self_selection → 不改写；
- Case F ：used / revoked 邀请码 → capabilities 一律不变；
- Case G ：downgrade 删除全部 market_review 授权行；unused 邀请码移除 market_review；
           review-only unused 邀请码变为 revoked；used 邀请码 JSON 不变。

注：``user_capabilities.source`` 在 068 DDL 中为 NOT NULL，故 ``source IS NULL`` 在当前
schema 下不可达；097 的 ``source IS DISTINCT FROM 'admin_revoke'`` 是防御式写法，
其排除语义由 Case C（admin_revoke 被排除）与非撤销来源（Case A/A'）两个方向覆盖。

============================================================================
锁安全约束（与 095/096 contract 一致）：
conftest 的 db_session 是 savepoint 模式；本文件所有真实 PG 操作使用
TestAsyncSessionLocal 短事务，且每次 _run_alembic() 前必须无打开连接/事务。
禁止在 db_session 内执行 Alembic DDL。
另外：097 的 downgrade 是**全局破坏性**的，因此所有 fixture 在
``downgrade 096`` **之后**才播种（保证播种状态即 096 状态）。
============================================================================
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres

_ALEMBIC_INI = "/app/alembic.ini"
_PARENT_REVISION = "096_retire_legacy_market_review"
_MIGRATION_SOURCE = "migration_review_split"
_INVITE_NOTE = "revsplit_fixture"
_USER_EMAIL_PREFIX = "revsplit_"
_USER_EMAIL_DOMAIN = "@test.local"


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


async def _execute_dml(sql: str, params: dict | None = None) -> None:
    """执行不返回结果集的 DML（DELETE/UPDATE）。

    `_fetch_all` 只能用于 SELECT：对 DELETE 调 fetchall() 会抛
    sqlalchemy.exc.ResourceClosedError("This result object does not return rows")。
    """
    async with TestAsyncSessionLocal() as session:
        await session.execute(text(sql), params or {})
        await session.commit()


# --------------------------------------------------------------------------- #
# 用户 fixture
# --------------------------------------------------------------------------- #
async def _seed_users() -> dict:
    """创建 6 个受控测试用户及其 capability 行（须在 096 状态下播种）。"""
    from app.models.user import User
    from app.models.user_capability import UserCapability

    now = datetime.now(UTC)
    granted = now - timedelta(days=5)
    active = now + timedelta(days=25)   # 仍有效
    expired = now - timedelta(days=1)   # 已过期

    def _mk_user(tag: str) -> User:
        return User(
            id=uuid.uuid4(),
            email=f"{_USER_EMAIL_PREFIX}{tag}_{uuid.uuid4().hex[:8]}{_USER_EMAIL_DOMAIN}",
            password_hash="not-a-real-hash",
            status="active",
        )

    u_invite, u_admin, u_expired, u_revoke, u_none, u_exist = (
        _mk_user("invite"),
        _mk_user("admin"),
        _mk_user("expired"),
        _mk_user("revoke"),
        _mk_user("none"),
        _mk_user("exist"),
    )
    async with TestAsyncSessionLocal() as s:
        s.add_all([u_invite, u_admin, u_expired, u_revoke, u_none, u_exist])
        await s.flush()
        s.add_all(
            [
                # Case A: invite_code 真实授予（active）
                UserCapability(
                    user_id=u_invite.id, capability="market_data",
                    granted_at=granted, expires_at=active, source="invite_code",
                ),
                # Case A': admin_grant 真实授予（active）
                UserCapability(
                    user_id=u_admin.id, capability="market_data",
                    granted_at=granted, expires_at=active, source="admin_grant",
                ),
                # Case B: 已过期 market_data → 不得补
                UserCapability(
                    user_id=u_expired.id, capability="market_data",
                    granted_at=granted, expires_at=expired, source="invite_code",
                ),
                # Case C: admin_revoke tombstone → 不得补
                UserCapability(
                    user_id=u_revoke.id, capability="market_data",
                    granted_at=granted, expires_at=active, source="admin_revoke",
                ),
                # Case D: 无 market_data（仅 self_selection）→ 不得补
                UserCapability(
                    user_id=u_none.id, capability="self_selection",
                    granted_at=granted, expires_at=active, source="invite_code", watchlist_limit=20,
                ),
                # Case E: 已存在 admin_grant market_review（expires 与 market_data 不同）
                UserCapability(
                    user_id=u_exist.id, capability="market_data",
                    granted_at=granted, expires_at=active, source="admin_grant",
                ),
                UserCapability(
                    user_id=u_exist.id, capability="market_review",
                    granted_at=granted - timedelta(days=1),
                    expires_at=active + timedelta(days=7),
                    source="admin_grant",
                ),
            ]
        )
        await s.commit()

    return {
        "invite": u_invite.id, "admin": u_admin.id, "expired": u_expired.id,
        "revoke": u_revoke.id, "none": u_none.id, "exist": u_exist.id,
        "owner": u_admin.id,
        "granted": granted, "active": active,
    }


async def _seed_invites(owner_id: uuid.UUID) -> dict:
    """创建受控邀请码 fixture（须在 096 状态下播种）。"""
    from app.models.invitation import InviteCode

    def _mk(status: str, caps: list[dict[str, Any]] | None) -> InviteCode:
        return InviteCode(
            id=uuid.uuid4(),
            code_hash=f"revsplit_{uuid.uuid4().hex}{uuid.uuid4().hex}",
            code_ciphertext=None,
            status=status,
            grant_days=30,
            plan_code=None,
            monitor_limit=None,
            grant_months=None,
            capabilities=caps,
            note=_INVITE_NOTE,
            created_by=owner_id,
        )

    fixtures = {
        # Case E1：unused market_data days=30（附带 watchlist_limit，验证不被复制）
        "e1": _mk("unused", [{"capability": "market_data", "days": 30, "watchlist_limit": 999}]),
        # Case E2：unused market_data months=2（历史单位，必须原样保留）
        "e2": _mk("unused", [{"capability": "market_data", "months": 2}]),
        # Case E3：unused 已含 market_review → 不重复追加
        "e3": _mk("unused", [
            {"capability": "market_data", "days": 10},
            {"capability": "market_review", "days": 5},
        ]),
        # Case E4：unused 仅 market_review（合法形态，不改写）
        "e4": _mk("unused", [{"capability": "market_review", "days": 5}]),
        # Case E5：unused capabilities IS NULL（旧邀请码，不制造 JSON）
        "e5": _mk("unused", None),
        # Case E6：unused 仅 self_selection（无 market_data，不改写）
        "e6": _mk("unused", [{"capability": "self_selection", "days": 30, "watchlist_limit": 20}]),
        # Case F：used / revoked 历史 JSON 不得改写
        "f_used": _mk("used", [{"capability": "market_data", "days": 30}]),
        "f_revoked": _mk("revoked", [{"capability": "market_data", "days": 30}]),
    }
    async with TestAsyncSessionLocal() as s:
        s.add_all(list(fixtures.values()))
        await s.commit()
    return {k: v.id for k, v in fixtures.items()}


async def _cleanup() -> None:
    """删除本轮 fixture（先删邀请码 —— created_by FK 无级联，再删用户级联清 capability）。"""
    await _execute_dml(f"DELETE FROM invite_codes WHERE note = '{_INVITE_NOTE}'")
    await _execute_dml(
        f"DELETE FROM users WHERE email LIKE '{_USER_EMAIL_PREFIX}%{_USER_EMAIL_DOMAIN}'"
    )


# --------------------------------------------------------------------------- #
# 读取 helpers
# --------------------------------------------------------------------------- #
async def _review_rows(user_id: uuid.UUID) -> list[tuple]:
    """该用户所有 market_review 行 (source, granted_at, expires_at, watchlist_limit, granted_by)。"""
    return await _fetch_all(
        """
        SELECT source, granted_at, expires_at, watchlist_limit, granted_by
        FROM user_capabilities
        WHERE user_id = :uid AND capability = 'market_review'
        ORDER BY source
        """,
        {"uid": str(user_id)},
    )


async def _review_count_total() -> int:
    rows = await _fetch_all("SELECT count(*) FROM user_capabilities WHERE capability = 'market_review'")
    return int(rows[0][0])


async def _invite_state(invite_id: uuid.UUID) -> tuple[str, Any]:
    """返回 (status, capabilities-as-python-object|None)。"""
    rows = await _fetch_all(
        "SELECT status, capabilities::text FROM invite_codes WHERE id = :id",
        {"id": str(invite_id)},
    )
    assert len(rows) == 1, f"邀请码 {invite_id} 应恰好存在 1 行，实际 {rows}"
    status, caps_text = rows[0]
    return status, (json.loads(caps_text) if caps_text is not None else None)


async def _reset_stack() -> None:
    """把 DB 置于 096 状态（097.downgrade 为全局破坏性，必须在播种前调用）。"""
    _run_alembic(["downgrade", _PARENT_REVISION])


# ============================================================
# Case A / A' / B / C / D / E：user_capabilities 拆分
# ============================================================
@pytest.mark.asyncio
async def test_097_upgrade_splits_active_market_data() -> None:
    await _reset_stack()
    ids = await _seed_users()
    try:
        _run_alembic(["upgrade", "head"])

        # Case A：invite_code active → 精确复制时间戳，NULL 字段正确
        rows = await _review_rows(ids["invite"])
        assert len(rows) == 1, f"Case A: 期望恰好 1 条 market_review，实际 {rows}"
        source, granted_at, expires_at, watchlist_limit, granted_by = rows[0]
        assert source == _MIGRATION_SOURCE, f"Case A: source 应为 {_MIGRATION_SOURCE}，实际 {source}"
        assert granted_at == ids["granted"], f"Case A: granted_at 必须复制，实际 {granted_at}"
        assert expires_at == ids["active"], f"Case A: expires_at 必须复制，实际 {expires_at}"
        assert watchlist_limit is None, f"Case A: watchlist_limit 必须为 NULL，实际 {watchlist_limit}"
        assert granted_by is None, f"Case A: granted_by 必须为 NULL，实际 {granted_by}"

        # Case A'：admin_grant active → 同样补 market_review
        rows_admin = await _review_rows(ids["admin"])
        assert len(rows_admin) == 1 and rows_admin[0][0] == _MIGRATION_SOURCE, (
            f"Case A': admin_grant 的 active market_data 应补 market_review，实际 {rows_admin}"
        )

        # Case B：expired market_data → 不得补
        rows_expired = await _review_rows(ids["expired"])
        assert rows_expired == [], f"Case B: 已过期 market_data 不得补 market_review，实际 {rows_expired}"

        # Case C：admin_revoke → 不得补
        rows_rev = await _review_rows(ids["revoke"])
        assert rows_rev == [], f"Case C: admin_revoke 不得补 market_review，实际 {rows_rev}"

        # Case D：无 market_data → 不得补
        rows_none = await _review_rows(ids["none"])
        assert rows_none == [], f"Case D: 无 market_data 不得补 market_review，实际 {rows_none}"

        # Case E：已存在 admin_grant market_review → 不被覆盖、不重复
        rows_exist = await _review_rows(ids["exist"])
        assert len(rows_exist) == 1, f"Case E: 不得产生重复 market_review，实际 {rows_exist}"
        src_exist, _, expires_exist, _, _ = rows_exist[0]
        assert src_exist == "admin_grant", f"Case E: 既有 admin_grant 行不得被覆盖，实际 {src_exist}"
        assert expires_exist == ids["active"] + timedelta(days=7), (
            f"Case E: 既有 expires_at 必须保留，实际 {expires_exist}"
        )
    finally:
        _run_alembic(["upgrade", "head"])
        await _cleanup()


# ============================================================
# Case E1–E6 / F：invite_codes JSONB 一次性兼容
# ============================================================
@pytest.mark.asyncio
async def test_097_upgrade_backfills_unused_invites_only() -> None:
    await _reset_stack()
    users = await _seed_users()
    inv = await _seed_invites(users["owner"])
    try:
        _run_alembic(["upgrade", "head"])

        # Case E1：days=30 原样复制；watchlist_limit 不得被复制
        status_e1, caps_e1 = await _invite_state(inv["e1"])
        assert status_e1 == "unused", f"Case E1: status 不得改变，实际 {status_e1}"
        assert caps_e1 == [
            {"capability": "market_data", "days": 30, "watchlist_limit": 999},
            {"capability": "market_review", "days": 30},
        ], f"Case E1: 应追加 market_review days=30 且不带 watchlist_limit，实际 {caps_e1}"

        # Case E2：months=2 单位不篡改
        _, caps_e2 = await _invite_state(inv["e2"])
        assert caps_e2 == [
            {"capability": "market_data", "months": 2},
            {"capability": "market_review", "months": 2},
        ], f"Case E2: 必须保留 months 单位，实际 {caps_e2}"

        # Case E3：已含 market_review → 不重复追加
        _, caps_e3 = await _invite_state(inv["e3"])
        assert caps_e3 == [
            {"capability": "market_data", "days": 10},
            {"capability": "market_review", "days": 5},
        ], f"Case E3: 不得重复追加或改写既有 market_review，实际 {caps_e3}"

        # Case E4：仅 market_review（market_review-only 邀请码合法，不改写）
        _, caps_e4 = await _invite_state(inv["e4"])
        assert caps_e4 == [{"capability": "market_review", "days": 5}], (
            f"Case E4: market_review-only 邀请码不得被改写，实际 {caps_e4}"
        )

        # Case E5：capabilities IS NULL → 不制造 JSON
        _, caps_e5 = await _invite_state(inv["e5"])
        assert caps_e5 is None, f"Case E5: NULL capabilities 不得被制造成 JSON，实际 {caps_e5}"

        # Case E6：仅 self_selection → 不改写
        _, caps_e6 = await _invite_state(inv["e6"])
        assert caps_e6 == [{"capability": "self_selection", "days": 30, "watchlist_limit": 20}], (
            f"Case E6: 无 market_data 的邀请码不得被改写，实际 {caps_e6}"
        )

        # Case F：used / revoked → 历史 JSON 一律不变
        status_used, caps_used = await _invite_state(inv["f_used"])
        assert status_used == "used", f"Case F: used status 不得改变，实际 {status_used}"
        assert caps_used == [{"capability": "market_data", "days": 30}], (
            f"Case F: used 邀请码 JSON 不得改写，实际 {caps_used}"
        )
        status_revoked, caps_revoked = await _invite_state(inv["f_revoked"])
        assert status_revoked == "revoked", f"Case F: revoked status 不得改变，实际 {status_revoked}"
        assert caps_revoked == [{"capability": "market_data", "days": 30}], (
            f"Case F: revoked 邀请码 JSON 不得改写，实际 {caps_revoked}"
        )
    finally:
        _run_alembic(["upgrade", "head"])
        await _cleanup()


# ============================================================
# Case G：downgrade 真正恢复旧三权限可执行状态（数据破坏性）
# ============================================================
@pytest.mark.asyncio
async def test_097_downgrade_removes_all_market_review() -> None:
    await _reset_stack()
    users = await _seed_users()
    inv = await _seed_invites(users["owner"])
    try:
        _run_alembic(["upgrade", "head"])
        # 前置：确认升级后确有 market_review 存在
        assert await _review_rows(users["invite"]) != [], "前置：invite_code active 应已补 market_review"
        assert await _review_rows(users["exist"]) != [], "前置：既有 admin_grant market_review 应存在"

        _run_alembic(["downgrade", _PARENT_REVISION])

        # Case G-1：全部 market_review 授权行被删除（含管理员的）
        assert await _review_count_total() == 0, "Case G: downgrade 后不得残留任何 market_review 授权行"

        # Case G-2：unused 邀请码移除 market_review 条目，且不破坏原 market_data 条目
        _, caps_e1 = await _invite_state(inv["e1"])
        assert caps_e1 == [{"capability": "market_data", "days": 30, "watchlist_limit": 999}], (
            f"Case G: 应只移除 market_review，实际 {caps_e1}"
        )
        _, caps_e3 = await _invite_state(inv["e3"])
        assert caps_e3 == [{"capability": "market_data", "days": 10}], (
            f"Case G: 应只移除 market_review，实际 {caps_e3}"
        )

        # Case G-3：review-only unused 邀请码 → 变 revoked（不留「可兑换但无权限」）
        status_e4, caps_e4 = await _invite_state(inv["e4"])
        assert status_e4 == "revoked", f"Case G: review-only 邀请码应变 revoked，实际 {status_e4}"
        assert caps_e4 == [], f"Case G: 移除后应为空数组，实际 {caps_e4}"

        # Case G-4：used 邀请码 JSON 不得重写
        status_used, caps_used = await _invite_state(inv["f_used"])
        assert status_used == "used", f"Case G: used status 不得改变，实际 {status_used}"
        assert caps_used == [{"capability": "market_data", "days": 30}], (
            f"Case G: used 邀请码 JSON 不得重写，实际 {caps_used}"
        )

        # Case G-5：再 upgrade head 可精确重建（幂等收敛）
        _run_alembic(["upgrade", "head"])
        rows_real = await _review_rows(users["invite"])
        assert len(rows_real) == 1 and rows_real[0][0] == _MIGRATION_SOURCE, (
            f"Case G: 再 upgrade 后必须精确重建，实际 {rows_real}"
        )
    finally:
        _run_alembic(["upgrade", "head"])
        await _cleanup()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
