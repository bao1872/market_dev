"""V2.1 邀请码兑换 API 端点测试 - /auth/redeem-v2。

测试 PRD §6.2 + §7 + Phase E5 错误合同：
- 成功兑换：200 + grants 列表
- 未认证：401
- 无效邀请码：400
- 已兑换：409 INVITE_CODE_ALREADY_REDEEMED
- 已撤销：409 INVITE_CODE_REVOKED

测试策略：
- 使用 conftest 的 client + user_factory fixture（HTTP 端到端）
- 通过 admin API 创建 V2.1 邀请码（带能力配置）
- 验证响应状态码、reason_code、grants 字段
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient

from app.core.security import create_access_token, get_password_hash
from app.models.user import User
from tests.conftest import AsyncFactory


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_user_v2(user_factory: AsyncFactory[User]) -> User:
    """创建管理员测试用户（用于创建 V2.1 邀请码）。"""
    return await user_factory(
        email=f"admin_redeem_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


@pytest_asyncio.fixture
async def member_user_v2(user_factory: AsyncFactory[User]) -> User:
    """创建普通会员测试用户（用于兑换邀请码）。"""
    return await user_factory(
        email=f"member_redeem_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("member-password-123"),
        roles=["member"],
    )


async def _create_v2_invite_code(
    client: AsyncClient,
    admin: User,
    duration_months: int = 3,
    capabilities: list[dict] | None = None,
) -> str:
    """通过 admin API 创建 V2.1 邀请码，返回明文。"""
    if capabilities is None:
        capabilities = [
            {"capability_key": "watchlist_management", "limit_value": 30},
            {"capability_key": "market_screening", "limit_value": None},
            {"capability_key": "review_management", "limit_value": None},
        ]
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=_auth_headers(admin.id),
        json={
            "count": 1,
            "duration_months": duration_months,
            "capabilities": capabilities,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()[0]["code"]


# ============================================================
# 成功兑换
# ============================================================


@pytest.mark.asyncio
async def test_redeem_v2_success(
    client: AsyncClient,
    admin_user_v2: User,
    member_user_v2: User,
) -> None:
    """成功兑换 V2.1 邀请码：200 + grants 列表。"""
    raw_code = await _create_v2_invite_code(
        client, admin_user_v2, duration_months=3
    )

    response = await client.post(
        "/auth/redeem-v2",
        headers=_auth_headers(member_user_v2.id),
        json={"invite_code": raw_code},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "invite_code_id" in data
    assert "redeemed_at" in data
    assert len(data["grants"]) == 3

    cap_keys = {g["capability_key"] for g in data["grants"]}
    assert cap_keys == {
        "watchlist_management",
        "market_screening",
        "review_management",
    }

    # watchlist_management 有 limit_value
    wl_grant = next(
        g for g in data["grants"] if g["capability_key"] == "watchlist_management"
    )
    assert wl_grant["limit_value"] == 30
    for cap_key in ("market_screening", "review_management"):
        grant = next(g for g in data["grants"] if g["capability_key"] == cap_key)
        assert grant["limit_value"] is None

    # 所有 grant 应有 starts_at 和 expires_at
    for grant in data["grants"]:
        assert "starts_at" in grant
        assert "expires_at" in grant


# ============================================================
# 未认证
# ============================================================


@pytest.mark.asyncio
async def test_redeem_v2_unauthenticated(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """未认证用户兑换：401。"""
    raw_code = await _create_v2_invite_code(client, admin_user_v2)

    response = await client.post(
        "/auth/redeem-v2",
        json={"invite_code": raw_code},
    )
    assert response.status_code == 401


# ============================================================
# 无效邀请码
# ============================================================


@pytest.mark.asyncio
async def test_redeem_v2_invalid_code(
    client: AsyncClient,
    member_user_v2: User,
) -> None:
    """无效邀请码：400。"""
    response = await client.post(
        "/auth/redeem-v2",
        headers=_auth_headers(member_user_v2.id),
        json={"invite_code": "INVALID-CODE-1234"},
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "邀请码无效" in str(detail)


# ============================================================
# 已兑换：409 INVITE_CODE_ALREADY_REDEEMED
# ============================================================


@pytest.mark.asyncio
async def test_redeem_v2_already_redeemed(
    client: AsyncClient,
    admin_user_v2: User,
    member_user_v2: User,
    user_factory: AsyncFactory[User],
) -> None:
    """REDEEM-002 同码重复：第二次返回 409 + reason_code。"""
    raw_code = await _create_v2_invite_code(client, admin_user_v2)

    # 第一个用户兑换成功
    first_response = await client.post(
        "/auth/redeem-v2",
        headers=_auth_headers(member_user_v2.id),
        json={"invite_code": raw_code},
    )
    assert first_response.status_code == 200

    # 第二个用户兑换同一码 → 409
    other_user = await user_factory(
        email=f"other_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("password-123"),
        roles=["member"],
    )
    second_response = await client.post(
        "/auth/redeem-v2",
        headers=_auth_headers(other_user.id),
        json={"invite_code": raw_code},
    )
    assert second_response.status_code == 409
    detail = second_response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason_code"] == "INVITE_CODE_ALREADY_REDEEMED"
    assert "已被兑换" in detail["message"]


# ============================================================
# 已撤销：409 INVITE_CODE_REVOKED
# ============================================================


@pytest.mark.asyncio
async def test_redeem_v2_revoked(
    client: AsyncClient,
    admin_user_v2: User,
    member_user_v2: User,
) -> None:
    """已撤销邀请码：409 + reason_code。"""
    raw_code = await _create_v2_invite_code(client, admin_user_v2)

    # 查询邀请码列表获取 ID
    list_response = await client.get(
        "/admin/v2/invite-codes",
        headers=_auth_headers(admin_user_v2.id),
    )
    assert list_response.status_code == 200
    invite_id = list_response.json()["items"][0]["id"]

    # 撤销邀请码
    revoke_response = await client.post(
        f"/admin/v2/invite-codes/{invite_id}/revoke",
        headers=_auth_headers(admin_user_v2.id),
    )
    assert revoke_response.status_code == 200

    # 兑换已撤销邀请码 → 409
    response = await client.post(
        "/auth/redeem-v2",
        headers=_auth_headers(member_user_v2.id),
        json={"invite_code": raw_code},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert isinstance(detail, dict)
    assert detail["reason_code"] == "INVITE_CODE_REVOKED"
    assert "已被撤销" in detail["message"]
