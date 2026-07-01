"""V1.6 订阅与邀请码系统测试。

测试内容：
1. 邀请码注册（成功/邀请码无效/已使用/已作废/邮箱已注册）
2. 邀请码续期（未到期顺延/已到期从当天计算）
3. 订阅状态查询（active/expired/无记录）
4. 登录订阅到期拦截（到期返回 subscription_active=false，Phase 2 Task 2.4）
5. 管理员邀请码管理（生成/作废/列表）
6. 管理员订阅列表（含订阅状态/到期时间/续期次数）
7. RBAC 越权访问（普通用户不能访问 admin 端点）

测试策略：
- 使用 PostgreSQL 测试库 + Alembic 迁移结构
- 通过 conftest.py 的 client/user_factory/role_factory/subscription_factory/invite_code_factory 构造测试数据
- client fixture 自动覆盖 get_db 注入当前测试 session
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.subscription import Subscription
from app.models.user import User
from app.services.subscription_service import hash_invite_code


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_user(user_factory: Callable[..., User]) -> User:
    """创建管理员测试用户。"""
    return await user_factory(
        email="admin@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


# ============================================================
# 邀请码生成测试
# ============================================================


@pytest.mark.asyncio
async def test_generate_invite_codes_single(
    client: AsyncClient,
    admin_user: User,
) -> None:
    """测试管理员生成单个邀请码。"""
    headers = _auth_headers(admin_user.id)
    response = await client.post(
        "/admin/invite-codes",
        headers=headers,
        json={"count": 1, "note": "test batch"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert "code" in data[0]
    assert len(data[0]["code"]) > 0
    assert data[0]["grant_days"] == 30
    assert data[0]["note"] == "test batch"


@pytest.mark.asyncio
async def test_generate_invite_codes_batch(
    client: AsyncClient,
    admin_user: User,
) -> None:
    """测试管理员批量生成邀请码。"""
    headers = _auth_headers(admin_user.id)
    response = await client.post(
        "/admin/invite-codes",
        headers=headers,
        json={"count": 5, "note": "batch of 5"},
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5
    # 验证每个邀请码不同
    codes = [item["code"] for item in data]
    assert len(set(codes)) == 5


@pytest.mark.asyncio
async def test_generate_invite_codes_normal_user_forbidden(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试普通用户不能生成邀请码。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id, note="for register")
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "newuser-password-123",
            "invite_code": raw_code,
        },
    )
    assert reg_resp.status_code == 200
    new_user_token = reg_resp.json()["access_token"]

    # 新用户尝试生成邀请码（应被拒绝）
    response = await client.post(
        "/admin/invite-codes",
        headers={"Authorization": f"Bearer {new_user_token}"},
        json={"count": 1},
    )
    assert response.status_code == 403


# ============================================================
# 邀请码注册测试
# ============================================================


@pytest.mark.asyncio
async def test_register_success(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试邀请码注册成功。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id, note="for register")
    response = await client.post(
        "/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "newuser-password-123",
            "invite_code": raw_code,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert "membership_started_at" in data
    assert "membership_expires_at" in data


@pytest.mark.asyncio
async def test_register_invalid_invite_code(client: AsyncClient) -> None:
    """测试无效邀请码注册失败。"""
    response = await client.post(
        "/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "newuser-password-123",
            "invite_code": "INVALID-CODE-1234",
        },
    )
    assert response.status_code == 400
    assert "邀请码无效" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_used_invite_code(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试已使用邀请码注册失败。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 第一次注册成功
    await client.post(
        "/auth/register",
        json={
            "email": "user1@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    # 第二次使用同一邀请码注册失败
    response = await client.post(
        "/auth/register",
        json={
            "email": "user2@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    assert response.status_code == 400
    assert "已被使用" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_revoked_invite_code(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试已作废邀请码注册失败。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 作废邀请码
    headers = _auth_headers(admin_user.id)
    revoke_resp = await client.post(
        f"/admin/invite-codes/{invite.id}/revoke", headers=headers
    )
    assert revoke_resp.status_code == 200
    assert revoke_resp.json()["status"] == "revoked"

    # 使用已作废邀请码注册失败
    response = await client.post(
        "/auth/register",
        json={
            "email": "newuser@example.com",
            "password": "newuser-password-123",
            "invite_code": raw_code,
        },
    )
    assert response.status_code == 400
    assert "已被作废" in response.json()["detail"]


@pytest.mark.asyncio
async def test_register_duplicate_email(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试邮箱已注册时注册失败。"""
    invite1, raw_code1 = await invite_code_factory(created_by=admin_user.id)
    invite2, raw_code2 = await invite_code_factory(created_by=admin_user.id)

    # 第一次注册成功
    await client.post(
        "/auth/register",
        json={
            "email": "dup@example.com",
            "password": "password-12345",
            "invite_code": raw_code1,
        },
    )
    # 第二次用同一邮箱注册失败
    response = await client.post(
        "/auth/register",
        json={
            "email": "dup@example.com",
            "password": "password-12345",
            "invite_code": raw_code2,
        },
    )
    assert response.status_code == 400
    assert "已被注册" in response.json()["detail"]


# ============================================================
# 登录会员到期拦截测试
# ============================================================


@pytest.mark.asyncio
async def test_login_membership_active(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试会员有效时登录返回 subscription_active=True（替代旧 membership_expired=false）。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 注册
    await client.post(
        "/auth/register",
        json={
            "email": "active@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    # 登录
    response = await client.post(
        "/auth/login",
        json={"email": "active@example.com", "password": "password-12345"},
    )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: subscription_active 替代旧 membership_expired（语义等价取反）
    assert data["subscription_active"] is True


@pytest.mark.asyncio
async def test_login_membership_expired(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
    db_session: AsyncSession,
) -> None:
    """测试会员到期后登录返回 subscription_active=False（替代旧 membership_expired=true）。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 注册
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "expired@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    assert reg_resp.status_code == 200

    # 手动将会员到期时间设为过去（status 不持久化 expired，仅通过 expires_at<now 表示）
    user_stmt = sa_select(User).where(User.email == "expired@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    subscription_stmt = sa_select(Subscription).where(Subscription.user_id == user.id)
    subscription_result = await db_session.execute(subscription_stmt)
    subscription = subscription_result.scalar_one()
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.commit()

    # 登录应返回 subscription_active=False
    response = await client.post(
        "/auth/login",
        json={"email": "expired@example.com", "password": "password-12345"},
    )
    assert response.status_code == 200
    data = response.json()
    # [Auth] - 描述: subscription_active 替代旧 membership_expired（语义等价取反）
    assert data["subscription_active"] is False


# ============================================================
# 会员状态查询测试
# ============================================================


@pytest.mark.asyncio
async def test_get_subscription_status(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试查询会员状态。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 注册
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "status@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 查询会员状态
    response = await client.get(
        "/me/membership", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "active"
    assert data["remaining_days"] > 0
    assert data["renewal_count"] == 0


@pytest.mark.asyncio
async def test_get_membership_no_record(
    client: AsyncClient,
    admin_user: User,
) -> None:
    """测试无会员记录的用户查询会员状态返回 404。"""
    headers = _auth_headers(admin_user.id)
    response = await client.get("/me/membership", headers=headers)
    assert response.status_code == 404


# ============================================================
# 邀请码续期测试
# ============================================================


@pytest.mark.asyncio
async def test_renew_membership_active(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试未到期续期 - 从当前到期日顺延 30 天。"""
    invite1, raw_code1 = await invite_code_factory(created_by=admin_user.id)
    invite2, raw_code2 = await invite_code_factory(created_by=admin_user.id)

    # 注册
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "renew@example.com",
            "password": "password-12345",
            "invite_code": raw_code1,
        },
    )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 续期
    renew_resp = await client.post(
        "/auth/renew",
        headers={"Authorization": f"Bearer {token}"},
        json={"invite_code": raw_code2},
    )
    assert renew_resp.status_code == 200
    renew_data = renew_resp.json()
    assert renew_data["membership_status"] == "active"
    assert renew_data["old_expires_at"] is not None

    # 验证续期后到期时间顺延
    from datetime import datetime as dt

    old_expires = dt.fromisoformat(renew_data["old_expires_at"].replace("Z", "+00:00"))
    new_expires = dt.fromisoformat(renew_data["new_expires_at"].replace("Z", "+00:00"))
    diff = new_expires - old_expires
    assert abs(diff.days - 30) <= 1  # 允许 1 天误差（时区）


@pytest.mark.asyncio
async def test_renew_membership_expired(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
    db_session: AsyncSession,
) -> None:
    """测试已到期续期 - 从兑换当天重新计算 30 天。"""
    invite1, raw_code1 = await invite_code_factory(created_by=admin_user.id)
    invite2, raw_code2 = await invite_code_factory(created_by=admin_user.id)

    # 注册
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "renew2@example.com",
            "password": "password-12345",
            "invite_code": raw_code1,
        },
    )
    assert reg_resp.status_code == 200
    token = reg_resp.json()["access_token"]

    # 手动将会员到期时间设为过去（status 不持久化 expired）
    user_stmt = sa_select(User).where(User.email == "renew2@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    subscription_stmt = sa_select(Subscription).where(Subscription.user_id == user.id)
    subscription_result = await db_session.execute(subscription_stmt)
    subscription = subscription_result.scalar_one()
    subscription.expires_at = datetime.now(UTC) - timedelta(days=5)
    await db_session.commit()

    # 续期
    renew_resp = await client.post(
        "/auth/renew",
        headers={"Authorization": f"Bearer {token}"},
        json={"invite_code": raw_code2},
    )
    assert renew_resp.status_code == 200
    renew_data = renew_resp.json()
    assert renew_data["membership_status"] == "active"
    assert renew_data["remaining_days"] > 0


# ============================================================
# 邀请码作废测试
# ============================================================


@pytest.mark.asyncio
async def test_revoke_invite_code(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试作废未使用邀请码。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    headers = _auth_headers(admin_user.id)
    response = await client.post(
        f"/admin/invite-codes/{invite.id}/revoke", headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"


@pytest.mark.asyncio
async def test_revoke_used_invite_code_fails(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试作废已使用邀请码失败。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 先注册使用邀请码
    await client.post(
        "/auth/register",
        json={
            "email": "used@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    # 尝试作废已使用邀请码
    headers = _auth_headers(admin_user.id)
    response = await client.post(
        f"/admin/invite-codes/{invite.id}/revoke", headers=headers
    )
    assert response.status_code == 400
    assert "仅未使用" in response.json()["detail"]


# ============================================================
# 管理员列表查询测试
# ============================================================


@pytest.mark.asyncio
async def test_list_invite_codes(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试查询邀请码列表。"""
    for _ in range(3):
        await invite_code_factory(created_by=admin_user.id)

    headers = _auth_headers(admin_user.id)
    response = await client.get("/admin/invite-codes", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3
    assert len(data["items"]) >= 3


@pytest.mark.asyncio
async def test_list_invite_codes_by_status(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试按状态筛选邀请码列表。"""
    for _ in range(2):
        await invite_code_factory(created_by=admin_user.id)

    headers = _auth_headers(admin_user.id)
    response = await client.get(
        "/admin/invite-codes?status=unused", headers=headers
    )
    assert response.status_code == 200
    data = response.json()
    assert all(item["status"] == "unused" for item in data["items"])


@pytest.mark.asyncio
async def test_list_subscribers(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
) -> None:
    """测试查询会员账户列表。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 注册一个新用户
    await client.post(
        "/auth/register",
        json={
            "email": "member@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )

    # 查询会员列表
    headers = _auth_headers(admin_user.id)
    response = await client.get("/admin/members", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 2  # admin + 新注册用户
    # 验证新注册用户有会员信息
    member_emails = [m["email"] for m in data["items"]]
    assert "member@example.com" in member_emails


@pytest.mark.asyncio
async def test_get_member_redemptions(
    client: AsyncClient,
    admin_user: User,
    invite_code_factory: Callable[..., tuple],
    db_session: AsyncSession,
) -> None:
    """测试查询用户兑换记录。"""
    invite, raw_code = await invite_code_factory(created_by=admin_user.id)

    # 注册
    reg_resp = await client.post(
        "/auth/register",
        json={
            "email": "redemption@example.com",
            "password": "password-12345",
            "invite_code": raw_code,
        },
    )
    assert reg_resp.status_code == 200

    # 查找用户
    user_stmt = sa_select(User).where(User.email == "redemption@example.com")
    user_result = await db_session.execute(user_stmt)
    user = user_result.scalar_one()

    # 查询兑换记录
    headers = _auth_headers(admin_user.id)
    response = await client.get(
        f"/admin/members/{user.id}/redemptions", headers=headers
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert data[0]["usage_type"] == "registration"


# ============================================================
# 邀请码哈希一致性测试
# ============================================================


def test_hash_invite_code_consistency() -> None:
    """测试邀请码哈希一致性（忽略大小写和空格）。"""
    code = "ABCD-EFGH-IJKL-MNOP"
    h1 = hash_invite_code(code)
    h2 = hash_invite_code(code.lower())
    h3 = hash_invite_code(f" {code} ")
    assert h1 == h2 == h3


def test_hash_invite_code_different() -> None:
    """测试不同邀请码哈希不同。"""
    h1 = hash_invite_code("ABCD-EFGH-IJKL-MNOP")
    h2 = hash_invite_code("DCBA-HGFE-LKJI-PONM")
    assert h1 != h2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
