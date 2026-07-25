"""V2.1 邀请码能力配置 API 集成测试。

测试 /admin/v2/invite-codes 端点：
- POST 创建邀请码（带能力配置）
- GET 列表查询（含能力配置 + 状态推导）
- POST 撤销邀请码

测试覆盖：
- 创建成功（单码/批量，三能力组合）
- 非法参数拒绝（capability_key / limit_value / duration_months / 重复键）
- 列表查询（全部 / 按 status 筛选）
- 撤销成功 + 撤销已撤销/已兑换拒绝
- RBAC 越权（普通用户 403）
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select as sa_select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.capability_grant import InviteCodeCapability
from app.models.invitation import InviteCode
from app.models.user import User
from tests.conftest import AsyncFactory


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def admin_user_v2(user_factory: AsyncFactory[User]) -> User:
    """创建管理员测试用户。"""
    return await user_factory(
        email=f"admin_v2_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


@pytest_asyncio.fixture
async def member_user_v2(user_factory: AsyncFactory[User]) -> User:
    """创建普通会员测试用户。"""
    return await user_factory(
        email=f"member_v2_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("member-password-123"),
        roles=["member"],
    )


# ============================================================
# 1. 创建邀请码 - 成功场景
# ============================================================


@pytest.mark.asyncio
async def test_create_invite_code_single_watchlist_only(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """创建单个邀请码：仅 watchlist_management 能力。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 3,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": 30}
            ],
            "note": "watchlist-only batch",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    item = data[0]
    assert "code" in item
    assert len(item["code"]) > 0
    assert item["duration_months"] == 3
    assert len(item["capabilities"]) == 1
    assert item["capabilities"][0]["capability_key"] == "watchlist_management"
    assert item["capabilities"][0]["limit_value"] == 30
    assert item["note"] == "watchlist-only batch"


@pytest.mark.asyncio
async def test_create_invite_code_all_three_capabilities(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """创建邀请码：三能力组合。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 6,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": 50},
                {"capability_key": "market_screening", "limit_value": None},
                {"capability_key": "review_management", "limit_value": None},
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    caps = {c["capability_key"]: c["limit_value"] for c in data[0]["capabilities"]}
    assert caps == {
        "watchlist_management": 50,
        "market_screening": None,
        "review_management": None,
    }


@pytest.mark.asyncio
async def test_create_invite_code_batch(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """批量创建邀请码。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 5,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 5
    # 每个邀请码明文唯一
    codes = [item["code"] for item in data]
    assert len(set(codes)) == 5


# ============================================================
# 2. 创建邀请码 - 非法参数拒绝
# ============================================================


@pytest.mark.asyncio
async def test_create_invite_code_invalid_capability_key(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """非法 capability_key 拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "invalid_key", "limit_value": None}
            ],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_invite_code_watchlist_missing_limit(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """watchlist_management 缺少 limit_value 拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": None}
            ],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_invite_code_watchlist_zero_limit(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """watchlist_management limit_value=0 拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": 0}
            ],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_invite_code_duplicate_capability_key(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """重复能力键拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": 30},
                {"capability_key": "watchlist_management", "limit_value": 50},
            ],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_invite_code_duration_months_over_limit(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """duration_months 超过上限拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 121,  # MAX_DURATION_MONTHS=120
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_create_invite_code_empty_capabilities(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """空 capabilities 列表拒绝（422）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [],
        },
    )
    assert response.status_code == 422, response.text


# ============================================================
# 3. 列表查询
# ============================================================


@pytest.mark.asyncio
async def test_list_invite_codes_all(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """列表查询全部邀请码。"""
    headers = _auth_headers(admin_user_v2.id)
    # 先创建 2 个邀请码
    await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 2,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    # 查询列表
    response = await client.get("/admin/v2/invite-codes", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] >= 2
    assert len(data["items"]) >= 2
    # 每个项有 status + capabilities
    for item in data["items"]:
        assert item["status"] in ("available", "redeemed", "revoked")
        assert isinstance(item["capabilities"], list)
        assert "duration_months" in item


@pytest.mark.asyncio
async def test_list_invite_codes_status_filter_available(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """按 status=available 筛选。"""
    headers = _auth_headers(admin_user_v2.id)
    # 创建 1 个邀请码
    create_resp = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    assert create_resp.status_code == 200

    # 查询 available 状态
    response = await client.get(
        "/admin/v2/invite-codes",
        headers=headers,
        params={"status": "available"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] >= 1
    for item in data["items"]:
        assert item["status"] == "available"


@pytest.mark.asyncio
async def test_list_invite_codes_invalid_status_filter(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """非法 status 筛选值拒绝（400）。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.get(
        "/admin/v2/invite-codes",
        headers=headers,
        params={"status": "invalid_status"},
    )
    assert response.status_code == 400, response.text


# ============================================================
# 4. 撤销邀请码
# ============================================================


@pytest.mark.asyncio
async def test_revoke_invite_code_success(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """撤销 available 状态邀请码成功。"""
    headers = _auth_headers(admin_user_v2.id)
    # 创建邀请码
    create_resp = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    assert create_resp.status_code == 200
    invite_id = create_resp.json()[0]["id"]

    # 撤销
    response = await client.post(
        f"/admin/v2/invite-codes/{invite_id}/revoke",
        headers=headers,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "revoked"
    assert data["revoked_at"] is not None
    assert data["redeemed_at"] is None


@pytest.mark.asyncio
async def test_revoke_invite_code_already_revoked(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """撤销已撤销邀请码拒绝（400）。"""
    headers = _auth_headers(admin_user_v2.id)
    # 创建并撤销
    create_resp = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    invite_id = create_resp.json()[0]["id"]
    # 第一次撤销
    revoke_resp = await client.post(
        f"/admin/v2/invite-codes/{invite_id}/revoke",
        headers=headers,
    )
    assert revoke_resp.status_code == 200
    # 第二次撤销应失败
    response = await client.post(
        f"/admin/v2/invite-codes/{invite_id}/revoke",
        headers=headers,
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_revoke_invite_code_not_found(
    client: AsyncClient,
    admin_user_v2: User,
) -> None:
    """撤销不存在的邀请码返回 400。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        f"/admin/v2/invite-codes/{uuid.uuid4()}/revoke",
        headers=headers,
    )
    assert response.status_code == 400, response.text


# ============================================================
# 5. RBAC 越权
# ============================================================


@pytest.mark.asyncio
async def test_member_cannot_access_v2_admin_endpoints(
    client: AsyncClient,
    member_user_v2: User,
) -> None:
    """普通会员不能访问 V2 admin 端点（403）。"""
    headers = _auth_headers(member_user_v2.id)
    # POST 创建
    resp1 = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 1,
            "capabilities": [
                {"capability_key": "market_screening", "limit_value": None}
            ],
        },
    )
    assert resp1.status_code == 403, resp1.text
    # GET 列表
    resp2 = await client.get("/admin/v2/invite-codes", headers=headers)
    assert resp2.status_code == 403, resp2.text


@pytest.mark.asyncio
async def test_unauthenticated_cannot_access_v2_admin(
    client: AsyncClient,
) -> None:
    """未认证不能访问 V2 admin 端点（401）。"""
    response = await client.get("/admin/v2/invite-codes")
    assert response.status_code == 401, response.text


# ============================================================
# 6. 数据库一致性验证
# ============================================================


@pytest.mark.asyncio
async def test_create_invite_code_db_persistence(
    client: AsyncClient,
    admin_user_v2: User,
    db_session: AsyncSession,
) -> None:
    """创建邀请码后数据库持久化验证。"""
    headers = _auth_headers(admin_user_v2.id)
    response = await client.post(
        "/admin/v2/invite-codes",
        headers=headers,
        json={
            "count": 1,
            "duration_months": 3,
            "capabilities": [
                {"capability_key": "watchlist_management", "limit_value": 30},
                {"capability_key": "market_screening", "limit_value": None},
            ],
            "note": "db-persistence-test",
        },
    )
    assert response.status_code == 200
    invite_id = response.json()[0]["id"]

    # 查询数据库验证
    result = await db_session.execute(
        sa_select(InviteCode).where(InviteCode.id == invite_id)
    )
    invite = result.scalar_one()
    assert invite.duration_months == 3
    assert invite.revoked_at is None
    assert invite.redeemed_at is None
    assert invite.redeemed_by_user_id is None
    assert invite.note == "db-persistence-test"

    # 验证能力配置
    cap_result = await db_session.execute(
        sa_select(InviteCodeCapability)
        .where(InviteCodeCapability.invite_code_id == invite_id)
        .order_by(InviteCodeCapability.capability_key)
    )
    caps = list(cap_result.scalars().all())
    assert len(caps) == 2
    cap_keys = [c.capability_key for c in caps]
    assert "market_screening" in cap_keys
    assert "watchlist_management" in cap_keys
    # watchlist_management 有 limit_value=30
    wl_cap = next(c for c in caps if c.capability_key == "watchlist_management")
    assert wl_cap.limit_value == 30
    # market_screening limit_value=None
    ms_cap = next(c for c in caps if c.capability_key == "market_screening")
    assert ms_cap.limit_value is None
