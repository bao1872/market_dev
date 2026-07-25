"""V2.1 API 权限矩阵测试 - AUTH-001~009。

测试 PRD §10.2 API 矩阵 + §10.1 统一依赖 + E5 错误合同：
- AUTH-001: 未认证 → 401
- AUTH-002: watchlist-only → 基础行情列表/自选 200；详情/K线/指标/DSA 403
- AUTH-003: market-only → 基础行情列表/详情/K线/指标/DSA 200(或非403)；自选 403
- AUTH-004: review-only → 行情/自选 403（复盘 API 不存在，仅验证 403）
- AUTH-005: 组合权限 → 取并集
- AUTH-006: 过期 grant → 403
- AUTH-007: 管理员 → 全部不 403
- AUTH-008: 403/409 reason_code 稳定

测试策略：
- 直接在 DB 插入 UserCapabilityGrant 创建不同能力组合的用户
- HTTP 端到端验证状态码和 reason_code
- 403 测试使用任意 UUID（依赖在 handler 前执行）
- 非 403 测试验证响应不是 403（可能是 404/422/200）
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from app.constants.capability_keys import (
    MARKET_SCREENING,
    REVIEW_MANAGEMENT,
    WATCHLIST_MANAGEMENT,
)
from app.core.security import create_access_token, get_password_hash
from app.models.capability_grant import UserCapabilityGrant
from app.models.instrument import Instrument
from app.models.user import User
from tests.conftest import AsyncFactory


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _grant_capability(
    db,
    user: User,
    capability_key: str,
    limit_value: int | None = None,
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    source_id: str | None = None,
) -> UserCapabilityGrant:
    """直接在 DB 插入 UserCapabilityGrant（测试辅助）。"""
    now = datetime.now(UTC)
    grant = UserCapabilityGrant(
        user_id=user.id,
        capability_key=capability_key,
        limit_value=limit_value,
        source_type="invite_code",
        source_id=source_id or f"test-{uuid.uuid4().hex[:8]}",
        starts_at=starts_at or now,
        expires_at=expires_at or (now + timedelta(days=90)),
        revoked_at=None,
    )
    db.add(grant)
    await db.flush()
    return grant


@pytest_asyncio.fixture
async def admin_user(user_factory: AsyncFactory[User]) -> User:
    """管理员用户（三能力全开，由 capability_service 自动推导）。"""
    return await user_factory(
        email=f"admin_matrix_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("admin-pass-123"),
        roles=["admin"],
    )


@pytest_asyncio.fixture
async def watchlist_only_user(
    user_factory: AsyncFactory[User],
    db_session,
) -> User:
    """仅 watchlist_management 能力的用户。"""
    user = await user_factory(
        email=f"wl_only_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("pass-123"),
        roles=["member"],
    )
    await _grant_capability(
        db_session, user, WATCHLIST_MANAGEMENT, limit_value=30,
    )
    return user


@pytest_asyncio.fixture
async def market_only_user(
    user_factory: AsyncFactory[User],
    db_session,
) -> User:
    """仅 market_screening 能力的用户。"""
    user = await user_factory(
        email=f"mkt_only_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("pass-123"),
        roles=["member"],
    )
    await _grant_capability(db_session, user, MARKET_SCREENING)
    return user


@pytest_asyncio.fixture
async def review_only_user(
    user_factory: AsyncFactory[User],
    db_session,
) -> User:
    """仅 review_management 能力的用户。"""
    user = await user_factory(
        email=f"rev_only_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("pass-123"),
        roles=["member"],
    )
    await _grant_capability(db_session, user, REVIEW_MANAGEMENT)
    return user


@pytest_asyncio.fixture
async def watchlist_market_user(
    user_factory: AsyncFactory[User],
    db_session,
) -> User:
    """watchlist + market 组合权限用户。"""
    user = await user_factory(
        email=f"wl_mkt_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("pass-123"),
        roles=["member"],
    )
    await _grant_capability(
        db_session, user, WATCHLIST_MANAGEMENT, limit_value=30,
    )
    await _grant_capability(db_session, user, MARKET_SCREENING)
    return user


@pytest_asyncio.fixture
async def expired_watchlist_user(
    user_factory: AsyncFactory[User],
    db_session,
) -> User:
    """watchlist 能力已过期的用户。"""
    user = await user_factory(
        email=f"expired_wl_{uuid.uuid4().hex[:6]}@example.com",
        password_hash=get_password_hash("pass-123"),
        roles=["member"],
    )
    now = datetime.now(UTC)
    await _grant_capability(
        db_session,
        user,
        WATCHLIST_MANAGEMENT,
        limit_value=30,
        starts_at=now - timedelta(days=100),
        expires_at=now - timedelta(days=1),  # 已过期
    )
    return user


@pytest_asyncio.fixture
async def test_instrument(db_session) -> Instrument:
    """创建测试标的（用于详情/K线/指标端点）。"""
    stmt = select(Instrument).limit(1)
    result = await db_session.execute(stmt)
    inst = result.scalar_one_or_none()
    if inst is not None:
        return inst
    # 若 DB 无标的，创建一个
    inst = Instrument(
        symbol=f"TEST{uuid.uuid4().hex[:4]}",
        name="测试股票",
        market="SZ",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    return inst


# ============================================================
# AUTH-001: 未认证 → 401
# ============================================================


@pytest.mark.asyncio
async def test_auth_001_unauthenticated_401(client: AsyncClient) -> None:
    """AUTH-001: 未认证访问私有端点 → 401。"""
    endpoints = [
        ("/watchlist", "GET"),
        ("/watchlist", "POST"),
        ("/watchlist/monitor-status", "GET"),
        ("/market/stocks?scope=market", "GET"),
        ("/market/stocks?scope=watchlist", "GET"),
        (f"/api/v1/instruments/{uuid.uuid4()}/bars", "GET"),
        (f"/api/v1/instruments/{uuid.uuid4()}/indicators", "GET"),
        (f"/api/v1/instruments/{uuid.uuid4()}/chart-snapshot", "GET"),
        ("/api/v1/stocks/TEST/context", "GET"),
        ("/me/access", "GET"),
    ]
    for path, method in endpoints:
        if method == "GET":
            response = await client.get(path)
        else:
            response = await client.post(path, json={"instrument_id": str(uuid.uuid4()), "source": "market"})
        assert response.status_code == 401, f"{method} {path} 期望 401，实际 {response.status_code}"


# ============================================================
# AUTH-002: watchlist-only → 基础列表/自选 200；详情/K线/指标/DSA 403
# ============================================================


@pytest.mark.asyncio
async def test_auth_002_watchlist_only_access(
    client: AsyncClient,
    watchlist_only_user: User,
    test_instrument: Instrument,
) -> None:
    """AUTH-002: watchlist-only 用户权限边界。"""
    headers = _auth_headers(watchlist_only_user.id)

    # 基础行情列表（market scope）→ 200（或非 403）
    response = await client.get("/market/stocks?scope=market", headers=headers)
    assert response.status_code != 403, f"watchlist-only 基础行情列表不应 403，实际 {response.status_code}"

    # 基础行情列表（watchlist scope）→ 200（或非 403）
    response = await client.get("/market/stocks?scope=watchlist", headers=headers)
    assert response.status_code != 403, f"watchlist-only watchlist scope 不应 403，实际 {response.status_code}"

    # 自选列表 → 200（或非 403）
    response = await client.get("/watchlist", headers=headers)
    assert response.status_code != 403, f"watchlist-only 自选列表不应 403，实际 {response.status_code}"

    # 自选监控状态 → 200（或非 403）
    response = await client.get("/watchlist/monitor-status", headers=headers)
    assert response.status_code != 403, f"watchlist-only 监控状态不应 403，实际 {response.status_code}"

    # 个股详情 K线 → 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/bars", headers=headers
    )
    assert response.status_code == 403, f"watchlist-only /bars 应 403，实际 {response.status_code}"

    # 指标 → 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/indicators", headers=headers
    )
    assert response.status_code == 403, f"watchlist-only /indicators 应 403，实际 {response.status_code}"

    # Chart Snapshot → 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/chart-snapshot", headers=headers
    )
    assert response.status_code == 403, f"watchlist-only /chart-snapshot 应 403，实际 {response.status_code}"

    # Stock Context → 403
    response = await client.get(
        f"/api/v1/stocks/{test_instrument.symbol}/context", headers=headers
    )
    assert response.status_code == 403, f"watchlist-only /context 应 403，实际 {response.status_code}"


# ============================================================
# AUTH-003: market-only → 详情/K线/指标/DSA 不 403；自选 403
# ============================================================


@pytest.mark.asyncio
async def test_auth_003_market_only_access(
    client: AsyncClient,
    market_only_user: User,
    test_instrument: Instrument,
) -> None:
    """AUTH-003: market-only 用户权限边界。"""
    headers = _auth_headers(market_only_user.id)

    # 基础行情列表（market scope）→ 不 403
    response = await client.get("/market/stocks?scope=market", headers=headers)
    assert response.status_code != 403, f"market-only 基础行情列表不应 403，实际 {response.status_code}"

    # 基础行情列表（watchlist scope）→ 403（额外要求 watchlist_management）
    response = await client.get("/market/stocks?scope=watchlist", headers=headers)
    assert response.status_code == 403, f"market-only watchlist scope 应 403，实际 {response.status_code}"

    # 自选列表 → 403
    response = await client.get("/watchlist", headers=headers)
    assert response.status_code == 403, f"market-only 自选列表应 403，实际 {response.status_code}"

    # 自选监控状态 → 403
    response = await client.get("/watchlist/monitor-status", headers=headers)
    assert response.status_code == 403, f"market-only 监控状态应 403，实际 {response.status_code}"

    # K线 → 不 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/bars", headers=headers
    )
    assert response.status_code != 403, f"market-only /bars 不应 403，实际 {response.status_code}"

    # 指标 → 不 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/indicators", headers=headers
    )
    assert response.status_code != 403, f"market-only /indicators 不应 403，实际 {response.status_code}"

    # Chart Snapshot → 不 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/chart-snapshot", headers=headers
    )
    assert response.status_code != 403, f"market-only /chart-snapshot 不应 403，实际 {response.status_code}"

    # Stock Context → 不 403
    response = await client.get(
        f"/api/v1/stocks/{test_instrument.symbol}/context", headers=headers
    )
    assert response.status_code != 403, f"market-only /context 不应 403，实际 {response.status_code}"


# ============================================================
# AUTH-004: review-only → 行情/自选 403
# ============================================================


@pytest.mark.asyncio
async def test_auth_004_review_only_access(
    client: AsyncClient,
    review_only_user: User,
) -> None:
    """AUTH-004: review-only 用户权限边界（复盘 API 不存在，验证 403）。"""
    headers = _auth_headers(review_only_user.id)

    # 基础行情列表 → 403（需 watchlist 或 market，review 不满足）
    response = await client.get("/market/stocks?scope=market", headers=headers)
    assert response.status_code == 403, f"review-only 基础行情列表应 403，实际 {response.status_code}"

    # 自选列表 → 403
    response = await client.get("/watchlist", headers=headers)
    assert response.status_code == 403, f"review-only 自选列表应 403，实际 {response.status_code}"


# ============================================================
# AUTH-005: 组合权限 → 取并集
# ============================================================


@pytest.mark.asyncio
async def test_auth_005_combined_permissions(
    client: AsyncClient,
    watchlist_market_user: User,
    test_instrument: Instrument,
) -> None:
    """AUTH-005: watchlist + market 组合权限取并集。"""
    headers = _auth_headers(watchlist_market_user.id)

    # 基础行情列表（market scope）→ 不 403
    response = await client.get("/market/stocks?scope=market", headers=headers)
    assert response.status_code != 403

    # 基础行情列表（watchlist scope）→ 不 403
    response = await client.get("/market/stocks?scope=watchlist", headers=headers)
    assert response.status_code != 403

    # 自选列表 → 不 403
    response = await client.get("/watchlist", headers=headers)
    assert response.status_code != 403

    # K线 → 不 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/bars", headers=headers
    )
    assert response.status_code != 403

    # Chart Snapshot → 不 403
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/chart-snapshot", headers=headers
    )
    assert response.status_code != 403


# ============================================================
# AUTH-006: 过期 grant → 403
# ============================================================


@pytest.mark.asyncio
async def test_auth_006_expired_grant(
    client: AsyncClient,
    expired_watchlist_user: User,
) -> None:
    """AUTH-006: 过期 grant 实时 403。"""
    headers = _auth_headers(expired_watchlist_user.id)

    # 自选列表 → 403（grant 已过期）
    response = await client.get("/watchlist", headers=headers)
    assert response.status_code == 403, f"过期 grant 自选列表应 403，实际 {response.status_code}"

    # 基础行情列表 → 403（无有效能力）
    response = await client.get("/market/stocks?scope=market", headers=headers)
    assert response.status_code == 403, f"过期 grant 基础行情列表应 403，实际 {response.status_code}"


# ============================================================
# AUTH-007: 管理员 → 全部不 403
# ============================================================


@pytest.mark.asyncio
async def test_auth_007_admin_bypass(
    client: AsyncClient,
    admin_user: User,
    test_instrument: Instrument,
) -> None:
    """AUTH-007: 管理员三能力全开，全部端点不 403。"""
    headers = _auth_headers(admin_user.id)

    endpoints_get = [
        "/market/stocks?scope=market",
        "/market/stocks?scope=watchlist",
        "/watchlist",
        "/watchlist/monitor-status",
        f"/api/v1/instruments/{test_instrument.id}/bars",
        f"/api/v1/instruments/{test_instrument.id}/indicators",
        f"/api/v1/instruments/{test_instrument.id}/chart-snapshot",
        f"/api/v1/stocks/{test_instrument.symbol}/context",
    ]
    for path in endpoints_get:
        response = await client.get(path, headers=headers)
        assert response.status_code != 403, f"admin {path} 不应 403，实际 {response.status_code}"


# ============================================================
# AUTH-008: 403/409 reason_code 稳定
# ============================================================


@pytest.mark.asyncio
async def test_auth_008_reason_code_stable(
    client: AsyncClient,
    watchlist_only_user: User,
    test_instrument: Instrument,
) -> None:
    """AUTH-008: 403 响应包含稳定 reason_code=CAPABILITY_REQUIRED。"""
    headers = _auth_headers(watchlist_only_user.id)

    # K线 → 403 + reason_code
    response = await client.get(
        f"/api/v1/instruments/{test_instrument.id}/bars", headers=headers
    )
    assert response.status_code == 403
    detail = response.json()["detail"]
    # detail 可能是 dict（require_capability 返回 dict detail）
    if isinstance(detail, dict):
        assert detail.get("reason_code") == "CAPABILITY_REQUIRED"
        assert "capability_key" in detail
    else:
        # 如果是字符串，说明实现不一致
        pytest.fail(f"403 detail 应为 dict 包含 reason_code，实际: {detail}")


# ============================================================
# AUTH-009: /me/access 返回 V2.1 capabilities + watchlist_limits
# ============================================================


@pytest.mark.asyncio
async def test_auth_009_me_access_v2_fields(
    client: AsyncClient,
    watchlist_only_user: User,
) -> None:
    """AUTH-009: /me/access 返回 V2.1 capabilities 和 watchlist_limits 字段。"""
    headers = _auth_headers(watchlist_only_user.id)
    response = await client.get("/me/access", headers=headers)
    assert response.status_code == 200, response.text
    data = response.json()

    # V1 字段保留
    assert "user_id" in data
    assert "is_admin" in data
    assert "subscription_active" in data

    # V2.1 字段存在
    assert "capabilities" in data
    assert "watchlist_limits" in data

    # capabilities 包含三项
    caps = data["capabilities"]
    assert "watchlist_management" in caps
    assert "market_screening" in caps
    assert "review_management" in caps

    # watchlist-only 用户：watchlist_management active=True
    assert caps["watchlist_management"]["active"] is True
    assert caps["watchlist_management"]["expires_at"] is not None

    # market_screening active=False
    assert caps["market_screening"]["active"] is False
    assert caps["market_screening"]["expires_at"] is None

    # watchlist_limits
    limits = data["watchlist_limits"]
    assert limits["watchlist_stock_limit"] == 30
    assert limits["watchlist_current_count"] == 0
    assert limits["watchlist_over_limit"] is False
    assert limits["is_admin_unlimited"] is False


@pytest.mark.asyncio
async def test_auth_009_admin_me_access_unlimited(
    client: AsyncClient,
    admin_user: User,
) -> None:
    """AUTH-009: admin /me/access 返回三能力全开 + unlimited。"""
    headers = _auth_headers(admin_user.id)
    response = await client.get("/me/access", headers=headers)
    assert response.status_code == 200
    data = response.json()

    caps = data["capabilities"]
    assert caps["watchlist_management"]["active"] is True
    assert caps["market_screening"]["active"] is True
    assert caps["review_management"]["active"] is True

    limits = data["watchlist_limits"]
    assert limits["is_admin_unlimited"] is True
    assert limits["watchlist_stock_limit"] is None
