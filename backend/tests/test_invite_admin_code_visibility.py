"""PANJI-BIZ-FIX-20260918 Commit B — 邀请码 admin 可见性 PG 证据。

证明：
- 新邀请码：code_hash 仍是兑换 SSOT；code_ciphertext 只存 Fernet 密文，可解密回显；
- admin list：新邀请码 code == raw；capability self_selection.watchlist_limit 是新模式额度真源；
- 历史邀请码（code_ciphertext=NULL）：code is None，不伪造明文。

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1（仅 bz_stock_verify_<sha>）；
本地 PURE_UNIT_TEST=1 由 conftest 自动 skip（禁止自带 CI skipif）。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, decrypt_secret, get_password_hash
from app.models.invitation import InviteCode
from app.models.user import Role, User, UserRole
from app.services.subscription_service import generate_invite_codes, hash_invite_code

pytestmark = pytest.mark.postgres

_PREFIX = "invite-vis-"


async def _admin(db: AsyncSession) -> User:
    user = User(
        email=f"{_PREFIX}admin-{uuid.uuid4().hex[:8]}@test.local",
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db.add(user)
    await db.flush()
    role = await db.scalar(select(Role).where(Role.name == "admin"))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


@pytest.mark.asyncio
async def test_new_invite_code_ciphertext_roundtrip_and_admin_visibility(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """新邀请码：密文可解密回显 + hash 仍是兑换真源 + capability 额度真源。"""
    admin = await _admin(db_session)
    codes = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        note="invite-vis",
        capabilities=[
            {"capability": "self_selection", "days": 30, "watchlist_limit": 5}
        ],
    )
    invite, raw_code = codes[0]

    # 1. hash 仍是兑换 SSOT；密文不是明文
    assert invite.code_hash == hash_invite_code(raw_code)
    assert invite.code_hash != raw_code
    assert invite.code_ciphertext is not None
    assert invite.code_ciphertext != raw_code

    # 2. 密文可解密回显
    assert decrypt_secret(invite.code_ciphertext) == raw_code

    # 3. monitor_limit 仍是旧快照（20），capability 才是新模式额度真源（5）
    assert invite.monitor_limit == 20
    caps = invite.capabilities or []
    ss = [c for c in caps if c.get("capability") == "self_selection"][0]
    assert ss["watchlist_limit"] == 5

    # 4. admin list API：code == raw
    resp = await client.get("/v1/admin/invite-codes", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    row = [i for i in items if i["id"] == str(invite.id)][0]
    assert row["code"] == raw_code
    row_ss = [c for c in (row.get("capabilities") or []) if c.get("capability") == "self_selection"][0]
    assert row_ss["watchlist_limit"] == 5


@pytest.mark.asyncio
async def test_legacy_invite_code_without_ciphertext_returns_none_code(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """历史邀请码（code_ciphertext=NULL）→ code is None，不伪造明文。"""
    admin = await _admin(db_session)
    legacy = InviteCode(
        code_hash=hash_invite_code(f"LEGACY-{uuid.uuid4().hex[:12]}"),
        code_ciphertext=None,
        status="unused",
        grant_days=30,
        plan_code="observe_20",
        monitor_limit=20,
        capabilities=None,
        note="legacy",
        created_by=admin.id,
    )
    db_session.add(legacy)
    await db_session.flush()

    resp = await client.get("/v1/admin/invite-codes", headers=_auth(admin))
    assert resp.status_code == 200, resp.text
    row = [i for i in resp.json()["items"] if i["id"] == str(legacy.id)][0]
    assert row["code"] is None
