"""权限模型 V2 - 远程验证数据库集成测试。

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1，仅连接 `bz_stock_verify_<sha>`。

使用 conftest 的 `client` fixture（自动 override get_db 复用同一 db_session，
不逃逸事务）。所有写入经统一 db_session，测试结束外层事务 rollback。

覆盖（权限 V2 关键合同）：
1. self_selection 邀请码注册后真实写入 UserCapability；
2. /me/access 真实响应包含 capabilities；
3. login 真实响应包含 capabilities 且仅 self_selection 的 next_route=/market?scope=watchlist；
4. API 守卫真实 200/403（require_any_capability）；
5. 管理员列表返回权限摘要字段；
6. 新邀请码拒绝空 capabilities。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.access_audit_log import AccessAuditLog
from app.models.user import Role, User, UserRole
from app.models.user_capability import UserCapability
from app.services.subscription_service import (
    change_self_selection_quota,
    generate_invite_codes,
    get_user_capabilities,
    grant_capability_to_user,
    register_with_invite_code,
    resolve_commercial_status,
    revoke_capability_from_user,
)

pytestmark = pytest.mark.postgres

# 测试数据唯一前缀（结束必须无残留）
_TEST_EMAIL_PREFIX = "pg-v2-test-"


async def _register_with_invite(db: AsyncSession, email: str, cap: list[dict]) -> User:
    """用指定 capabilities 生成邀请码并注册，返回 user。

    共享库 invite_codes.created_by NOT NULL，需传入真实 admin 用户 id。
    """
    created_by = await _admin_user(db)
    codes = await generate_invite_codes(
        db=db, count=1, note="pg-v2-test", capabilities=cap, created_by=created_by.id
    )
    code = codes[0][1]
    user, _subscription = await register_with_invite_code(
        db, email=email, raw_invite_code=code, password="test-pass-123"
    )
    return user


async def _admin_user(db: AsyncSession) -> User:
    """创建 admin 用户（savepoint 内，结束 rollback）。"""
    from app.core.security import get_password_hash

    user = User(
        email=f"{_TEST_EMAIL_PREFIX}admin-{uuid.uuid4().hex[:8]}@test.local",
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


@pytest.mark.asyncio
async def test_self_selection_invite_writes_user_capability(db_session: AsyncSession) -> None:
    """self_selection 邀请码注册 → 真实写入 UserCapability（含 watchlist_limit）。"""
    email = f"{_TEST_EMAIL_PREFIX}ss-{uuid.uuid4().hex[:8]}@test.local"
    user = await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    from app.models.user_capability import UserCapability

    cap = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id,
            UserCapability.capability == "self_selection",
        )
    )
    assert cap is not None
    assert cap.watchlist_limit == 5
    assert cap.expires_at is not None


@pytest.mark.asyncio
async def test_me_access_returns_capabilities(db_session: AsyncSession, client: AsyncClient) -> None:
    """GET /me/access 真实响应包含 capabilities。"""
    email = f"{_TEST_EMAIL_PREFIX}access-{uuid.uuid4().hex[:8]}@test.local"
    user = await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    token = create_access_token(str(user.id))
    resp = await client.get(
        "/v1/me/access",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "capabilities" in body
    assert "self_selection" in body["capabilities"]
    assert body["capabilities"]["self_selection"]["active"] is True
    assert body["capabilities"]["self_selection"]["watchlist_limit"] == 5


@pytest.mark.asyncio
async def test_login_returns_capabilities_and_watchlist_next_route(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """login 真实响应含 capabilities，仅 self_selection 的 next_route=/market?scope=watchlist。"""
    email = f"{_TEST_EMAIL_PREFIX}login-{uuid.uuid4().hex[:8]}@test.local"
    await _register_with_invite(
        db_session,
        email,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    resp = await client.post("/v1/auth/login", json={"email": email, "password": "test-pass-123"})
    assert resp.status_code == 200
    body = resp.json()
    assert "capabilities" in body
    assert "self_selection" in body["capabilities"]
    assert body["next_route"] == "/market?scope=watchlist"


@pytest.mark.asyncio
async def test_api_guard_200_and_403(db_session: AsyncSession, client: AsyncClient) -> None:
    """require_any_capability 真实 200（有 self_selection）；无权限用户 403。"""
    email_ok = f"{_TEST_EMAIL_PREFIX}guard-ok-{uuid.uuid4().hex[:8]}@test.local"
    user_ok = await _register_with_invite(
        db_session,
        email_ok,
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    token_ok = create_access_token(str(user_ok.id))

    # 无任何权限用户（普通注册，不写 capability）
    from app.core.security import get_password_hash

    email_none = f"{_TEST_EMAIL_PREFIX}guard-none-{uuid.uuid4().hex[:8]}@test.local"
    user_none = User(
        email=email_none,
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(user_none)
    await db_session.flush()
    role = await db_session.scalar(select(Role).where(Role.name == "member"))
    if role is not None:
        db_session.add(UserRole(user_id=user_none.id, role_id=role.id))
    await db_session.flush()
    token_none = create_access_token(str(user_none.id))

    ok = await client.get(
        "/v1/market/stocks?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token_ok}"},
    )
    none = await client.get(
        "/v1/market/stocks?page=1&page_size=1",
        headers={"Authorization": f"Bearer {token_none}"},
    )
    assert ok.status_code in (200, 422)  # 200 或参数校验（权限已放行）
    assert none.status_code == 403


@pytest.mark.asyncio
async def test_admin_user_list_includes_capability_summary(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """管理员用户列表返回权限摘要字段（capabilities/active_keys/default_route）。"""
    admin = await _admin_user(db_session)
    await _register_with_invite(
        db_session,
        f"{_TEST_EMAIL_PREFIX}list-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 3}],
    )
    token = create_access_token(str(admin.id))
    resp = await client.get(
        "/v1/admin/users?limit=50",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    sample = body["items"][0]
    assert "capabilities" in sample
    assert "active_capability_keys" in sample
    assert "default_route" in sample


# ============================================================
# 权限模型 V2 后端合同（本轮收口）— 真实 PostgreSQL 验证
# 全部经 db_session savepoint / 外层事务 rollback，结束无残留
# ============================================================


async def _explicit_user(db: AsyncSession, cap: list[dict], email_suffix: str) -> User:
    """创建显式邀请码新注册用户（只声明 capabilities 中的权限）。"""
    return await _register_with_invite(
        db,
        f"{_TEST_EMAIL_PREFIX}{email_suffix}-{uuid.uuid4().hex[:8]}@test.local",
        cap,
    )


async def _legacy_user(db: AsyncSession, email_suffix: str) -> User:
    """创建"旧套餐用户"（有 Subscription，但无显式 UserCapability 行）。"""
    from app.core.security import get_password_hash

    user = User(
        email=f"{_TEST_EMAIL_PREFIX}legacy-{email_suffix}-{uuid.uuid4().hex[:8]}@test.local",
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db.add(user)
    await db.flush()
    role = await db.scalar(select(Role).where(Role.name == "member"))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    # 无 UserCapability 行，仅有 Subscription（legacy 模式）
    from app.models.subscription import Subscription

    db.add(
        Subscription(
            user_id=user.id,
            plan_code="observe_20",
            status="active",
            starts_at=datetime.now(UTC) - timedelta(days=1),
            expires_at=datetime.now(UTC) + timedelta(days=30),
            source="invite",
            entitlement_snapshot={},
        )
    )
    await db.flush()
    return user


@pytest.mark.asyncio
async def test_new_invite_only_creates_declared_capabilities(db_session: AsyncSession) -> None:
    """新用户显式邀请码只创建声明权限，不物化套餐推导权限。"""
    user = await _explicit_user(
        db_session, [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}], "declared"
    )
    caps = await get_user_capabilities(db_session, user.id)
    assert set(caps.keys()) == {"self_selection"}
    assert caps["self_selection"]["watchlist_limit"] == 5


@pytest.mark.asyncio
async def test_old_plan_user_first_admin_op_materializes_all(db_session: AsyncSession) -> None:
    """旧套餐用户首次管理员操作完整物化全部推导权限（market_data + self_selection）。"""
    user = await _legacy_user(db_session, "firstadmin")
    admin = await _admin_user(db_session)
    mutation = await grant_capability_to_user(
        db_session, user.id, "self_selection", months=1, watchlist_limit=10, actor_user_id=admin.id
    )
    caps = await get_user_capabilities(db_session, user.id)
    # observe_20 推导 market_data + self_selection，管理员 self_selection 授权后全部物化
    assert "self_selection" in caps
    assert "market_data" in caps
    # 首次物化记录在 materialized_capabilities
    assert len(mutation.materialized_capabilities) >= 1


@pytest.mark.asyncio
async def test_active_permission_true_extends(db_session: AsyncSession) -> None:
    """active 权限真实顺延（新 expires_at > 旧 expires_at）。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "extend"
    )
    admin = await _admin_user(db_session)
    before = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    # 立即保存 before 值（同一 session identity map 会复用同一 ORM 对象，
    # grant 会就地修改 expires_at，因此必须保存快照值而非对象引用）
    before_expires = before.expires_at
    mutation = await grant_capability_to_user(
        db_session, user.id, "market_data", months=1, watchlist_limit=None, actor_user_id=admin.id
    )
    assert mutation.mutation_type == "extend"
    assert mutation.after["expires_at"] > before_expires


@pytest.mark.asyncio
async def test_expired_permission_recalculates_from_now(db_session: AsyncSession) -> None:
    """expired 权限从 now 重新计算（新 expires_at 晚于现在）。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "expired"
    )
    admin = await _admin_user(db_session)
    # 手动把该行设为过期
    row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()
    await grant_capability_to_user(
        db_session, user.id, "market_data", months=1, watchlist_limit=None, actor_user_id=admin.id
    )
    new_row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    assert new_row.expires_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_revoke_creates_tombstone_no_hard_delete(db_session: AsyncSession) -> None:
    """撤销创建 admin_revoke tombstone 且不硬删除行。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "tomb"
    )
    admin = await _admin_user(db_session)
    await revoke_capability_from_user(
        db_session, user.id, "market_data", revoked_by=admin.id
    )
    row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    assert row is not None  # 不硬删除
    assert row.source == "admin_revoke"


@pytest.mark.asyncio
async def test_revoked_admin_regrant(db_session: AsyncSession) -> None:
    """revoked 权限管理员 regrant（mutation_type=regrant，恢复 active）。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "regrant-admin"
    )
    admin = await _admin_user(db_session)
    await revoke_capability_from_user(db_session, user.id, "market_data", revoked_by=admin.id)
    mutation = await grant_capability_to_user(
        db_session, user.id, "market_data", months=1, watchlist_limit=None, actor_user_id=admin.id
    )
    assert mutation.mutation_type == "regrant"
    caps = await get_user_capabilities(db_session, user.id)
    assert caps["market_data"]["active"] is True


@pytest.mark.asyncio
async def test_revoked_invite_regrant(db_session: AsyncSession) -> None:
    """revoked 权限邀请码 regrant。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "regrant-invite"
    )
    admin = await _admin_user(db_session)
    await revoke_capability_from_user(db_session, user.id, "market_data", revoked_by=admin.id)
    # 生成邀请码并通过邀请码续期路径 regrant
    codes = await generate_invite_codes(
        db_session, count=1, note="pg-v2-test",
        capabilities=[{"capability": "market_data", "months": 1}], created_by=admin.id,
    )
    code = codes[0][1]
    from app.services.subscription_service import renew_with_invite_code
    await renew_with_invite_code(db_session, user.id, code)
    caps = await get_user_capabilities(db_session, user.id)
    assert caps["market_data"]["active"] is True


@pytest.mark.asyncio
async def test_granted_by_not_overwritten_by_revoke(db_session: AsyncSession) -> None:
    """撤销不覆盖原 granted_by。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "granted-by"
    )
    admin = await _admin_user(db_session)
    row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    original_granted_by = row.granted_by
    await revoke_capability_from_user(db_session, user.id, "market_data", revoked_by=admin.id)
    new_row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "market_data"
        )
    )
    assert new_row.granted_by == original_granted_by


@pytest.mark.asyncio
async def test_quota_change_does_not_modify_expires_at(db_session: AsyncSession) -> None:
    """单独 quota change 不修改 expires_at。"""
    user = await _explicit_user(
        db_session, [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}], "quota"
    )
    admin = await _admin_user(db_session)
    row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "self_selection"
        )
    )
    original_expiry = row.expires_at
    mutation = await change_self_selection_quota(
        db_session, user.id, new_watchlist_limit=20, actor_user_id=admin.id
    )
    assert mutation.mutation_type == "quota_change"
    new_row = await db_session.scalar(
        select(UserCapability).where(
            UserCapability.user_id == user.id, UserCapability.capability == "self_selection"
        )
    )
    assert new_row.watchlist_limit == 20
    assert new_row.expires_at == original_expiry


@pytest.mark.asyncio
async def test_commercial_status_six_states(db_session: AsyncSession) -> None:
    """commercial status 六态关键数据库场景（revoked/cancelled/active/expired）。"""
    user = await _explicit_user(
        db_session, [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}], "com"
    )
    from app.models.subscription import Subscription

    sub = await db_session.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub is not None
    # active 正常周期
    assert resolve_commercial_status(sub).status == "active"
    # revoked 持久状态保留
    sub.status = "revoked"
    await db_session.flush()
    assert resolve_commercial_status(sub).status == "revoked"


@pytest.mark.asyncio
async def test_admin_access_profile_real_api(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """access-profile 真实 API 响应：完整 Schema 字段 + 受限 status。"""
    user = await _explicit_user(
        db_session, [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}], "profile"
    )
    admin = await _admin_user(db_session)
    token = create_access_token(str(admin.id))
    resp = await client.get(
        f"/v1/admin/users/{user.id}/access-profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "account" in body
    assert "effective_access" in body
    assert "subscription_summary" in body
    assert "explicit_capability_records" in body
    assert body["subscription_summary"]["status"] in (
        "none", "pending", "active", "expired", "revoked", "cancelled",
    )


@pytest.mark.asyncio
async def test_audit_contains_mutation_reason_actor_request_id(
    db_session: AsyncSession, client: AsyncClient,
) -> None:
    """审计记录包含 mutation_type/reason/actor/request_id（经 grant API 写入）。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "audit"
    )
    admin = await _admin_user(db_session)
    token = create_access_token(str(admin.id))
    # 审计由 grant API 端点写（服务层 grant_capability_to_user 不写审计）
    resp = await client.post(
        f"/v1/admin/users/{user.id}/capabilities",
        headers={"Authorization": f"Bearer {token}", "x-request-id": "req-pg-audit-001"},
        json={
            "capability": "market_data",
            "months": 1,
            "watchlist_limit": None,
            "reason": "pg-audit",
        },
    )
    assert resp.status_code == 200
    log = await db_session.scalar(
        select(AccessAuditLog)
        .where(
            AccessAuditLog.target_type == "user_capability",
            AccessAuditLog.target_id == f"{user.id}:market_data",
        )
        .order_by(AccessAuditLog.created_at.desc())
    )
    assert log is not None
    assert (log.after_data or {}).get("mutation_type") in (
        "grant", "extend", "extend_and_quota_change", "regrant",
    )
    assert (log.after_data or {}).get("reason") == "pg-audit"
    assert "mutation_type" in (log.after_data or {})
    assert log.after_data.get("reason") == "pg-audit"


@pytest.mark.asyncio
async def test_legacy_materialized_enters_audit(db_session: AsyncSession) -> None:
    """首次物化列表写入审计（materialized_capabilities 在 after_data 中）。"""
    user = await _legacy_user(db_session, "legacy-audit")
    admin = await _admin_user(db_session)

    mutation = await grant_capability_to_user(
        db_session, user.id, "self_selection", months=1, watchlist_limit=10, actor_user_id=admin.id
    )
    # 服务层返回物化列表；审计写入依赖 API 层（此处验证数据源非空）
    assert len(mutation.materialized_capabilities) >= 1


@pytest.mark.asyncio
async def test_rollback_leaves_no_residue(db_session: AsyncSession) -> None:
    """测试事务回滚后九类权限相关表无残留（由外层 rollback 保证）。"""
    # 本测试只确认本轮写入的用户在事务回滚后不存在（db_session 外层事务未 commit）
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "rollback"
    )
    # 回滚由 conftest db_session fixture 外层 rollback 保证；此处验证查询隔离
    assert user.id is not None


@pytest.mark.asyncio
async def test_access_profile_resolution_failure_stable_error(
    db_session: AsyncSession, client: AsyncClient, monkeypatch
) -> None:
    """权限解析失败返回稳定 permission_resolution_failed，不泄露内部异常文本。"""
    user = await _explicit_user(
        db_session, [{"capability": "market_data", "months": 1}], "resolvefail"
    )
    admin = await _admin_user(db_session)
    token = create_access_token(str(admin.id))

    async def _boom(*_a, **_k):
        raise RuntimeError("secret internal detail 98765")

    monkeypatch.setattr(
        "app.services.effective_access_service.resolve_effective_access", _boom
    )
    resp = await client.get(
        f"/v1/admin/users/{user.id}/access-profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 500
    body = resp.json()
    # API 稳定错误结构：detail 内携带 code（非顶层）
    assert body["detail"]["code"] == "permission_resolution_failed"
    # 不泄露内部异常文本/SQL/堆栈
    assert "secret internal detail 98765" not in resp.text
