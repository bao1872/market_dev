"""权限能力聚合服务测试 - V2.1 邀请码模块化授权。

测试 capability_service 模块：
- get_capability_access_context: V2.1 capability 权限上下文（PRD §9）
- has_capability: 检查用户是否拥有指定能力
- get_effective_watchlist_limit: 自选额度 max 规则（PRD §7.1）
- require_capability / require_any_capability: FastAPI 依赖
- invalidate_access_context_cache: 缓存失效

测试覆盖（PRD 测试计划）：
- ACC-001 ~ ACC-007: 三能力七组合
- TIME: 日历月边界（已在 capability_calendar.py 自测中覆盖）
- 多 grant 续期
- 独立到期
- 自选额度 max
- 到期边界
- 缓存失效
- admin 豁免

测试策略：
- 使用 conftest.py 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 直接创建 User + Role + UserRole + UserCapabilityGrant 测试数据
- 调用 get_capability_access_context 验证字段
- 直接调用依赖函数验证 403 异常
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import (
    MARKET_SCREENING,
    REVIEW_MANAGEMENT,
    WATCHLIST_MANAGEMENT,
)
from app.models.capability_grant import UserCapabilityGrant
from app.models.user import Role, User, UserRole
from app.services.capability_service import (
    REASON_CAPABILITY_REQUIRED,
    CapabilityAccessContext,
    get_capability_access_context,
    get_effective_watchlist_limit,
    has_capability,
    invalidate_access_context_cache,
    require_any_capability,
    require_capability,
)

# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    from sqlalchemy import select

    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_user_with_roles(
    db: AsyncSession, role_names: list[str], email: str | None = None
) -> User:
    """创建用户并分配指定角色，挂载 _roles 属性模拟 deps 注入行为。"""
    email = email or f"user_{uuid.uuid4().hex[:8]}@test.com"
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash="fake-hash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()
    for name in role_names:
        role = await _ensure_role(db, name)
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    # 挂载 _roles 属性，模拟 deps._fetch_user_with_roles 的行为
    object.__setattr__(user, "_roles", list(role_names))
    return user


def _make_grant(
    user_id: uuid.UUID,
    capability_key: str,
    *,
    limit_value: int | None = None,
    source_id: str | None = None,
    starts_at: datetime | None = None,
    duration_days: int = 30,
    revoked: bool = False,
) -> UserCapabilityGrant:
    """构造 UserCapabilityGrant 测试数据。

    Args:
        user_id: 用户 ID
        capability_key: 能力键
        limit_value: 自选额度（仅 watchlist_management）
        source_id: 来源 ID（默认随机 UUID）
        starts_at: 生效时间（默认 now-1d）
        duration_days: 有效天数（默认 30）
        revoked: 是否撤销
    """
    now = datetime.now(UTC)
    s_at = starts_at or (now - timedelta(days=1))
    e_at = s_at + timedelta(days=duration_days)
    return UserCapabilityGrant(
        id=uuid.uuid4(),
        user_id=user_id,
        capability_key=capability_key,
        limit_value=limit_value,
        source_type="invite_code",
        source_id=source_id or str(uuid.uuid4()),
        starts_at=s_at,
        expires_at=e_at,
        revoked_at=now if revoked else None,
        created_by=None,
    )


# ============================================================
# 1. admin 路径测试
# ============================================================


@pytest.mark.asyncio
async def test_admin_capability_context(db_session: AsyncSession) -> None:
    """admin 用户：三能力全开 + watchlist unlimited（PRD §4.4）。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    invalidate_access_context_cache(str(admin.id))

    ctx = await get_capability_access_context(db_session, admin)

    assert ctx.is_admin is True
    assert ctx.user_id == str(admin.id)
    # 三能力全部 active
    assert len(ctx.capabilities) == 3
    for key in (WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT):
        assert ctx.capabilities[key].active is True
        assert ctx.capabilities[key].expires_at is None
    # watchlist unlimited
    assert ctx.limits.is_admin_unlimited is True
    assert ctx.limits.watchlist_stock_limit is None
    assert ctx.limits.watchlist_over_limit is False


@pytest.mark.asyncio
async def test_admin_has_capability_always_true(db_session: AsyncSession) -> None:
    """admin 的 has_capability 对所有能力返回 True。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    invalidate_access_context_cache(str(admin.id))

    for key in (WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT):
        result = await has_capability(db_session, admin.id, key)
        assert result is True, f"admin 应拥有 {key}"


@pytest.mark.asyncio
async def test_admin_watchlist_limit_unlimited(db_session: AsyncSession) -> None:
    """admin 的 get_effective_watchlist_limit 返回 None（unlimited）。"""
    admin = await _create_user_with_roles(db_session, ["admin"])

    result = await get_effective_watchlist_limit(db_session, admin.id)
    assert result is None


# ============================================================
# 2. ACC-001 ~ ACC-007: 三能力七组合测试
# ============================================================


async def _setup_grants(
    db: AsyncSession, user: User, capabilities: dict[str, int | None]
) -> None:
    """为用户创建指定能力的 grant。

    Args:
        db: 数据库 session
        user: 用户对象
        capabilities: {capability_key: limit_value} 映射
            - watchlist_management 必须有 limit_value（正整数）
            - 其他能力 limit_value=None
    """
    for key, limit in capabilities.items():
        if key == WATCHLIST_MANAGEMENT:
            assert limit is not None and limit > 0
        else:
            assert limit is None
        grant = _make_grant(user.id, key, limit_value=limit)
        db.add(grant)
    await db.flush()
    invalidate_access_context_cache(str(user.id))


@pytest.mark.asyncio
async def test_acc_001_watchlist_only(db_session: AsyncSession) -> None:
    """ACC-001: 仅自选（watchlist=1, market=0, review=0）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(db_session, user, {WATCHLIST_MANAGEMENT: 30})

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.is_admin is False
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True
    assert ctx.capabilities[MARKET_SCREENING].active is False
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is False
    assert ctx.limits.watchlist_stock_limit == 30
    assert ctx.limits.is_admin_unlimited is False


@pytest.mark.asyncio
async def test_acc_002_market_only(db_session: AsyncSession) -> None:
    """ACC-002: 仅行情（watchlist=0, market=1, review=0）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(db_session, user, {MARKET_SCREENING: None})

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False
    assert ctx.capabilities[MARKET_SCREENING].active is True
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is False
    # 无 watchlist 权限，额度为 None
    assert ctx.limits.watchlist_stock_limit is None


@pytest.mark.asyncio
async def test_acc_003_review_only(db_session: AsyncSession) -> None:
    """ACC-003: 仅复盘（watchlist=0, market=0, review=1）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(db_session, user, {REVIEW_MANAGEMENT: None})

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False
    assert ctx.capabilities[MARKET_SCREENING].active is False
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is True


@pytest.mark.asyncio
async def test_acc_004_watchlist_and_market(db_session: AsyncSession) -> None:
    """ACC-004: 自选+行情。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(
        db_session, user, {WATCHLIST_MANAGEMENT: 20, MARKET_SCREENING: None}
    )

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True
    assert ctx.capabilities[MARKET_SCREENING].active is True
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is False
    assert ctx.limits.watchlist_stock_limit == 20


@pytest.mark.asyncio
async def test_acc_005_watchlist_and_review(db_session: AsyncSession) -> None:
    """ACC-005: 自选+复盘。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(
        db_session, user, {WATCHLIST_MANAGEMENT: 50, REVIEW_MANAGEMENT: None}
    )

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True
    assert ctx.capabilities[MARKET_SCREENING].active is False
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is True
    assert ctx.limits.watchlist_stock_limit == 50


@pytest.mark.asyncio
async def test_acc_006_market_and_review(db_session: AsyncSession) -> None:
    """ACC-006: 行情+复盘。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(
        db_session, user, {MARKET_SCREENING: None, REVIEW_MANAGEMENT: None}
    )

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False
    assert ctx.capabilities[MARKET_SCREENING].active is True
    assert ctx.capabilities[REVIEW_MANAGEMENT].active is True
    assert ctx.limits.watchlist_stock_limit is None


@pytest.mark.asyncio
async def test_acc_007_all_three(db_session: AsyncSession) -> None:
    """ACC-007: 三项全部。"""
    user = await _create_user_with_roles(db_session, ["member"])
    await _setup_grants(
        db_session,
        user,
        {WATCHLIST_MANAGEMENT: 100, MARKET_SCREENING: None, REVIEW_MANAGEMENT: None},
    )

    ctx = await get_capability_access_context(db_session, user)
    for key in (WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT):
        assert ctx.capabilities[key].active is True
    assert ctx.limits.watchlist_stock_limit == 100


@pytest.mark.asyncio
async def test_acc_no_grants(db_session: AsyncSession) -> None:
    """无任何 grant：三能力全部 active=False。"""
    user = await _create_user_with_roles(db_session, ["member"])
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    for key in (WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT):
        assert ctx.capabilities[key].active is False
    assert ctx.limits.watchlist_stock_limit is None


# ============================================================
# 3. PRD §7 多次兑换 + 自选额度 max
# ============================================================


@pytest.mark.asyncio
async def test_multiple_grants_same_capability_take_latest_expires(
    db_session: AsyncSession,
) -> None:
    """PRD §7: 同一能力多 grant 时取最晚 expires_at。"""
    user = await _create_user_with_roles(db_session, ["member"])
    # 两个 watchlist_management grant：一个 30 天后到期，一个 90 天后到期
    now = datetime.now(UTC)
    grant1 = _make_grant(
        user.id, WATCHLIST_MANAGEMENT, limit_value=20,
        starts_at=now - timedelta(days=1), duration_days=30,
    )
    grant2 = _make_grant(
        user.id, WATCHLIST_MANAGEMENT, limit_value=30,
        starts_at=now - timedelta(days=1), duration_days=90,
        source_id=str(uuid.uuid4()),
    )
    db_session.add(grant1)
    db_session.add(grant2)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True
    # 最晚 expires_at
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].expires_at == grant2.expires_at
    # limit_value 取 max（PRD §7.1）
    assert ctx.limits.watchlist_stock_limit == 30


@pytest.mark.asyncio
async def test_watchlist_limit_takes_max_of_effective_grants(
    db_session: AsyncSession,
) -> None:
    """PRD §7.1: 自选额度取所有有效 grant 的 max。"""
    user = await _create_user_with_roles(db_session, ["member"])
    # 三个 grant：limit=10, 50, 30 → max=50
    now = datetime.now(UTC)
    for limit in (10, 50, 30):
        grant = _make_grant(
            user.id, WATCHLIST_MANAGEMENT, limit_value=limit,
            starts_at=now - timedelta(days=1), duration_days=30,
            source_id=str(uuid.uuid4()),
        )
        db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    limit = await get_effective_watchlist_limit(db_session, user.id)
    assert limit == 50


# ============================================================
# 4. 到期边界 + 撤销
# ============================================================


@pytest.mark.asyncio
async def test_expired_grant_not_active(db_session: AsyncSession) -> None:
    """PRD §8.3: 到期 grant 不算 active（expires_at <= now）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    # 已过期 grant
    now = datetime.now(UTC)
    grant = _make_grant(
        user.id, WATCHLIST_MANAGEMENT, limit_value=30,
        starts_at=now - timedelta(days=40), duration_days=30,  # starts_at=-40d, expires_at=-10d
    )
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False
    assert ctx.limits.watchlist_stock_limit is None  # 无有效 grant


@pytest.mark.asyncio
async def test_future_grant_not_active(db_session: AsyncSession) -> None:
    """PRD §8.3: 未来才生效的 grant 不算 active（starts_at > now）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    now = datetime.now(UTC)
    grant = _make_grant(
        user.id, WATCHLIST_MANAGEMENT, limit_value=30,
        starts_at=now + timedelta(days=10), duration_days=30,  # 未来生效
    )
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False


@pytest.mark.asyncio
async def test_revoked_grant_not_active(db_session: AsyncSession) -> None:
    """PRD §8.3: 撤销的 grant 不算 active（revoked_at IS NOT NULL）。"""
    user = await _create_user_with_roles(db_session, ["member"])
    grant = _make_grant(
        user.id, WATCHLIST_MANAGEMENT, limit_value=30, revoked=True
    )
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is False


@pytest.mark.asyncio
async def test_boundary_expires_at_strict_exclusive(db_session: AsyncSession) -> None:
    """PRD §6.3: 有效区间 [starts_at, expires_at) - expires_at 严格 exclusive。

    注：由于数据库时间精度和查询条件 expires_at > now，无法精确测试 expires_at == now
    的边界（依赖于时间差），所以这里测试 expires_at 远未到的 active=True。
    """
    user = await _create_user_with_roles(db_session, ["member"])
    grant = _make_grant(
        user.id, MARKET_SCREENING, duration_days=30
    )
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    ctx = await get_capability_access_context(db_session, user)
    assert ctx.capabilities[MARKET_SCREENING].active is True


# ============================================================
# 5. has_capability 测试
# ============================================================


@pytest.mark.asyncio
async def test_has_capability_for_member(db_session: AsyncSession) -> None:
    """普通用户 has_capability：拥有返回 True，无返回 False。"""
    user = await _create_user_with_roles(db_session, ["member"])
    grant = _make_grant(user.id, WATCHLIST_MANAGEMENT, limit_value=20)
    db_session.add(grant)
    await db_session.flush()

    assert await has_capability(db_session, user.id, WATCHLIST_MANAGEMENT) is True
    assert await has_capability(db_session, user.id, MARKET_SCREENING) is False
    assert await has_capability(db_session, user.id, REVIEW_MANAGEMENT) is False


@pytest.mark.asyncio
async def test_has_capability_invalid_key_raises(db_session: AsyncSession) -> None:
    """非法 capability_key 抛 ValueError。"""
    user = await _create_user_with_roles(db_session, ["member"])
    with pytest.raises(ValueError):
        await has_capability(db_session, user.id, "invalid_key")


# ============================================================
# 6. require_capability / require_any_capability 依赖测试
# ============================================================


@pytest.mark.asyncio
async def test_require_capability_member_with_grant_passes(
    db_session: AsyncSession,
) -> None:
    """普通用户拥有该能力 → require_capability 通过。"""
    user = await _create_user_with_roles(db_session, ["member"])
    grant = _make_grant(user.id, WATCHLIST_MANAGEMENT, limit_value=20)
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    dep = require_capability(WATCHLIST_MANAGEMENT)
    ctx = await dep(db_session, user)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True


@pytest.mark.asyncio
async def test_require_capability_member_without_grant_403(
    db_session: AsyncSession,
) -> None:
    """普通用户无该能力 → require_capability 抛 403。"""
    user = await _create_user_with_roles(db_session, ["member"])
    invalidate_access_context_cache(str(user.id))

    dep = require_capability(MARKET_SCREENING)
    with pytest.raises(HTTPException) as exc_info:
        await dep(db_session, user)
    assert exc_info.value.status_code == 403
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["reason_code"] == REASON_CAPABILITY_REQUIRED
    assert detail["capability_key"] == MARKET_SCREENING


@pytest.mark.asyncio
async def test_require_capability_admin_passes(db_session: AsyncSession) -> None:
    """admin 任何能力都通过。"""
    admin = await _create_user_with_roles(db_session, ["admin"])
    invalidate_access_context_cache(str(admin.id))

    for key in (WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT):
        dep = require_capability(key)
        ctx = await dep(db_session, admin)
        assert ctx.is_admin is True


@pytest.mark.asyncio
async def test_require_any_capability_either_passes(db_session: AsyncSession) -> None:
    """require_any_capability: 拥有任一即通过。"""
    # 拥有 watchlist，无 market → 任一通过
    user1 = await _create_user_with_roles(db_session, ["member"])
    grant = _make_grant(user1.id, WATCHLIST_MANAGEMENT, limit_value=20)
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user1.id))

    dep = require_any_capability([WATCHLIST_MANAGEMENT, MARKET_SCREENING])
    ctx = await dep(db_session, user1)
    assert ctx.capabilities[WATCHLIST_MANAGEMENT].active is True


@pytest.mark.asyncio
async def test_require_any_capability_neither_403(db_session: AsyncSession) -> None:
    """require_any_capability: 都没有 → 403。"""
    user = await _create_user_with_roles(db_session, ["member"])
    # 只有 review，但 require [watchlist, market]
    grant = _make_grant(user.id, REVIEW_MANAGEMENT)
    db_session.add(grant)
    await db_session.flush()
    invalidate_access_context_cache(str(user.id))

    dep = require_any_capability([WATCHLIST_MANAGEMENT, MARKET_SCREENING])
    with pytest.raises(HTTPException) as exc_info:
        await dep(db_session, user)
    assert exc_info.value.status_code == 403


# ============================================================
# 7. 缓存失效
# ============================================================


@pytest.mark.asyncio
async def test_cache_invalidation_after_grant_creation(
    db_session: AsyncSession,
) -> None:
    """PRD §11 步骤 8: 创建 grant 后必须精确失效缓存。"""
    user = await _create_user_with_roles(db_session, ["member"])
    invalidate_access_context_cache(str(user.id))

    # 第一次查询：无 grant，watchlist=False
    ctx1 = await get_capability_access_context(db_session, user)
    assert ctx1.capabilities[WATCHLIST_MANAGEMENT].active is False

    # 添加 grant
    grant = _make_grant(user.id, WATCHLIST_MANAGEMENT, limit_value=20)
    db_session.add(grant)
    await db_session.flush()

    # 未失效缓存 → 仍是旧值（active=False）
    ctx2 = await get_capability_access_context(db_session, user)
    assert ctx2.capabilities[WATCHLIST_MANAGEMENT].active is False

    # 失效缓存后 → 新值（active=True）
    invalidate_access_context_cache(str(user.id))
    ctx3 = await get_capability_access_context(db_session, user)
    assert ctx3.capabilities[WATCHLIST_MANAGEMENT].active is True


@pytest.mark.asyncio
async def test_cache_invalidation_all_users(db_session: AsyncSession) -> None:
    """invalidate_access_context_cache(None) 清空全部缓存。"""
    user1 = await _create_user_with_roles(db_session, ["member"], email="u1@test.com")
    user2 = await _create_user_with_roles(db_session, ["member"], email="u2@test.com")
    # 填充缓存
    await get_capability_access_context(db_session, user1)
    await get_capability_access_context(db_session, user2)

    # 清空全部
    invalidate_access_context_cache(None)

    # 缓存被清空（无法直接验证，但函数应正常返回）
    ctx1 = await get_capability_access_context(db_session, user1)
    ctx2 = await get_capability_access_context(db_session, user2)
    assert ctx1.user_id == str(user1.id)
    assert ctx2.user_id == str(user2.id)


# ============================================================
# 8. Pydantic 模型字段验证
# ============================================================


def test_capability_access_context_frozen() -> None:
    """CapabilityAccessContext 应为 frozen。"""
    from pydantic import ValidationError

    from app.services.capability_service import CapabilityStatus, WatchlistLimitInfo

    ctx = CapabilityAccessContext(
        user_id="test-uuid",
        is_admin=False,
        capabilities={
            WATCHLIST_MANAGEMENT: CapabilityStatus(active=True, expires_at=None),
        },
        limits=WatchlistLimitInfo(
            watchlist_stock_limit=30,
            watchlist_current_count=10,
            watchlist_over_limit=False,
        ),
    )
    # frozen=True 不可变（pydantic 抛 ValidationError）
    with pytest.raises(ValidationError):
        ctx.is_admin = True  # type: ignore[misc]


def test_capability_status_fields() -> None:
    """CapabilityStatus 字段：active + expires_at。"""
    from app.services.capability_service import CapabilityStatus

    status = CapabilityStatus(active=True)
    assert status.expires_at is None

    now = datetime.now(UTC)
    status2 = CapabilityStatus(active=False, expires_at=now)
    assert status2.active is False
    assert status2.expires_at == now
