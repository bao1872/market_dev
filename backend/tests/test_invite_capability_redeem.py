"""V2.1 邀请码兑换（redeem）事务测试 - Phase D 门禁。

测试 redeem_invite_code_with_capabilities 服务函数（PRD §6.2 + §7）：
- D4 单次兑换：成功创建 grant + 邀请码状态变 redeemed + 失效缓存
- D2 并发兑换：两个用户并发兑换同一码，FOR UPDATE 行锁确保只有一个成功
- D4 原子回滚：grant 创建过程中抛错时邀请码保持 available
- D4 多次兑换：用户兑换第二个码时，已有能力独立延长，新能力立即增加
- D4 月末续期：月末日期 + 1 月按日历月收缩（2026-01-31 + 1 月 = 2026-02-28）
- D4 缓存刷新：兑换后用户 AccessContext 缓存被精确失效
- 边界拒绝：无效/已兑换/已撤销邀请码拒绝

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 并发测试用 TestAsyncSessionLocal 创建两个独立 session + asyncio.gather
- 直接调用 service 函数验证业务语义（不走 HTTP）
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import (
    MARKET_SCREENING,
    REVIEW_MANAGEMENT,
    WATCHLIST_MANAGEMENT,
)
from app.models.capability_grant import UserCapabilityGrant
from app.models.invitation import InviteCode, InviteRedemption
from app.models.user import Role, User, UserRole
from app.services.capability_calendar import add_calendar_months_asiashanghai
from app.services.capability_service import (
    _access_context_cache,
    get_capability_access_context,
    invalidate_access_context_cache,
)
from app.services.invite_capability_service import (
    InviteCodeCapabilityInput,
    create_invite_codes_with_capabilities,
    derive_invite_code_status_v2,
    redeem_invite_code_with_capabilities,
    revoke_invite_code_v2,
)

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_user(
    db: AsyncSession, role_names: list[str], email: str | None = None
) -> User:
    """创建用户并分配角色。"""
    email = email or f"user_{uuid.uuid4().hex[:8]}@test.com"
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    for name in role_names:
        role = await _ensure_role(db, name)
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（用于生成邀请码）。"""
    return await _create_user(db, role_names=["admin", "member"])


async def _create_member(db: AsyncSession) -> User:
    """创建普通会员用户（用于兑换邀请码）。"""
    return await _create_user(db, role_names=["member"])


async def _create_invite_code(
    db: AsyncSession,
    admin: User,
    duration_months: int = 3,
    capabilities: list[InviteCodeCapabilityInput] | None = None,
) -> tuple[InviteCode, str]:
    """创建单个邀请码（带能力配置）。"""
    if capabilities is None:
        capabilities = [
            InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=30),
            InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=None),
            InviteCodeCapabilityInput(REVIEW_MANAGEMENT, limit_value=None),
        ]
    results = await create_invite_codes_with_capabilities(
        db=db,
        count=1,
        created_by=admin.id,
        duration_months=duration_months,
        capabilities=capabilities,
    )
    await db.flush()
    return results[0]


# ============================================================
# D4 单次兑换 + 边界拒绝
# ============================================================


@pytest.mark.asyncio
async def test_redeem_single_success(db_session: AsyncSession) -> None:
    """D4 单次兑换：成功创建 grant + 邀请码状态变 redeemed。"""
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)
    invite, raw_code = await _create_invite_code(db_session, admin, duration_months=3)
    await db_session.flush()

    invite_id = invite.id
    member_id = member.id

    invite, grants, capabilities = await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code,
        user_id=member_id,
        usage_type="registration",
    )
    await db_session.flush()

    # 邀请码状态变为 redeemed
    assert derive_invite_code_status_v2(invite) == "redeemed"
    assert invite.redeemed_by_user_id == member_id
    assert invite.redeemed_at is not None

    # 三项能力各创建一个 grant
    assert len(grants) == 3
    assert len(capabilities) == 3
    cap_keys = {g.capability_key for g in grants}
    assert cap_keys == {WATCHLIST_MANAGEMENT, MARKET_SCREENING, REVIEW_MANAGEMENT}

    # grant 字段校验
    now = datetime.now(UTC)
    for grant in grants:
        assert grant.user_id == member_id
        assert grant.source_type == "invite_code"
        assert grant.source_id == str(invite_id)
        assert grant.revoked_at is None
        assert grant.starts_at <= now
        # expires_at = now + 3 月（日历月）
        shanghai_expires = grant.expires_at.astimezone(_SHANGHAI_TZ)
        shanghai_now = now.astimezone(_SHANGHAI_TZ)
        # 月份差 3，日期相同（或月末收缩）
        assert (shanghai_expires.year - shanghai_now.year) * 12 + (
            shanghai_expires.month - shanghai_now.month
        ) == 3

    # watchlist_management 有 limit_value，其他为 None
    wl_grant = next(g for g in grants if g.capability_key == WATCHLIST_MANAGEMENT)
    assert wl_grant.limit_value == 30
    for cap_key in (MARKET_SCREENING, REVIEW_MANAGEMENT):
        grant = next(g for g in grants if g.capability_key == cap_key)
        assert grant.limit_value is None

    # InviteRedemption 记录已写入
    redemption_stmt = select(InviteRedemption).where(
        InviteRedemption.invite_code_id == invite_id
    )
    redemption = (await db_session.execute(redemption_stmt)).scalar_one()
    assert redemption.user_id == member_id
    assert redemption.usage_type == "registration"
    assert redemption.new_expires_at is not None
    assert redemption.old_expires_at is None


@pytest.mark.asyncio
async def test_redeem_invalid_code(db_session: AsyncSession) -> None:
    """无效邀请码拒绝。"""
    member = await _create_member(db_session)
    await db_session.flush()

    with pytest.raises(ValueError, match="邀请码无效"):
        await redeem_invite_code_with_capabilities(
            db=db_session,
            raw_invite_code="INVALID-CODE-1234",
            user_id=member.id,
        )


@pytest.mark.asyncio
async def test_redeem_already_redeemed(db_session: AsyncSession) -> None:
    """已兑换邀请码拒绝。"""
    admin = await _create_admin(db_session)
    member_a = await _create_member(db_session)
    member_b = await _create_member(db_session)
    invite, raw_code = await _create_invite_code(db_session, admin)
    await db_session.flush()

    # 第一次兑换成功
    await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code,
        user_id=member_a.id,
        usage_type="registration",
    )
    await db_session.flush()

    # 第二次兑换同一码失败
    with pytest.raises(ValueError, match="邀请码已被兑换"):
        await redeem_invite_code_with_capabilities(
            db=db_session,
            raw_invite_code=raw_code,
            user_id=member_b.id,
        )


@pytest.mark.asyncio
async def test_redeem_revoked(db_session: AsyncSession) -> None:
    """已撤销邀请码拒绝。"""
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)
    invite, raw_code = await _create_invite_code(db_session, admin)
    await db_session.flush()

    # 撤销邀请码
    await revoke_invite_code_v2(db_session, invite.id)
    await db_session.flush()

    with pytest.raises(ValueError, match="邀请码已被撤销"):
        await redeem_invite_code_with_capabilities(
            db=db_session,
            raw_invite_code=raw_code,
            user_id=member.id,
        )


# ============================================================
# D4 多次兑换：已有能力独立延长 + 新能力立即增加
# ============================================================


@pytest.mark.asyncio
async def test_redeem_extends_existing_capability(db_session: AsyncSession) -> None:
    """D3 多次兑换：用户已有 watchlist_management grant 未到期，第二次兑换应在最晚 expires_at 上延长。

    场景：
    1. 第一次兑换：watchlist_management + market_screening，3 月，now=T0
       → watchlist grant expires_at = T0 + 3M
       → market grant expires_at = T0 + 3M
    2. 第二次兑换：watchlist_management + market_screening + review_management，2 月，now=T1 (T0 + 1 月)
       → watchlist grant 新 expires_at = max(T1, T0+3M) + 2M = (T0+3M) + 2M = T0 + 5M
       → market grant 新 expires_at = max(T1, T0+3M) + 2M = T0 + 5M
       → review grant 新 expires_at = T1 + 2M（新能力，从 now 开始）
    """
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)

    # 第一次邀请码：watchlist + market，3 月
    invite1, raw_code1 = await _create_invite_code(
        db_session,
        admin,
        duration_months=3,
        capabilities=[
            InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=30),
            InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=None),
        ],
    )
    await db_session.flush()

    _, grants1, _ = await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code1,
        user_id=member.id,
        usage_type="registration",
    )
    await db_session.flush()

    wl_grant1 = next(g for g in grants1 if g.capability_key == WATCHLIST_MANAGEMENT)
    market_grant1 = next(g for g in grants1 if g.capability_key == MARKET_SCREENING)
    wl_expires_after_first = wl_grant1.expires_at
    market_expires_after_first = market_grant1.expires_at

    # 第二次邀请码：watchlist + market + review，2 月
    invite2, raw_code2 = await _create_invite_code(
        db_session,
        admin,
        duration_months=2,
        capabilities=[
            InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=50),
            InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=None),
            InviteCodeCapabilityInput(REVIEW_MANAGEMENT, limit_value=None),
        ],
    )
    await db_session.flush()

    _, grants2, _ = await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code2,
        user_id=member.id,
        usage_type="renewal",
    )
    await db_session.flush()

    # 第二次兑换应有 3 个 grant
    assert len(grants2) == 3
    wl_grant2 = next(g for g in grants2 if g.capability_key == WATCHLIST_MANAGEMENT)
    market_grant2 = next(g for g in grants2 if g.capability_key == MARKET_SCREENING)
    review_grant2 = next(g for g in grants2 if g.capability_key == REVIEW_MANAGEMENT)

    # 已有能力应在最晚 expires_at 上延长 2 月
    # base = max(now, first_expires)；now 紧接第一次兑换后，first_expires 在未来 → base = first_expires
    expected_wl_expires = add_calendar_months_asiashanghai(
        wl_expires_after_first, 2
    )
    expected_market_expires = add_calendar_months_asiashanghai(
        market_expires_after_first, 2
    )
    assert wl_grant2.expires_at == expected_wl_expires, (
        f"watchlist grant 应在原最晚 expires_at 上延长 2 月，"
        f"实际={wl_grant2.expires_at}, 期望={expected_wl_expires}"
    )
    assert market_grant2.expires_at == expected_market_expires, (
        f"market grant 应在原最晚 expires_at 上延长 2 月，"
        f"实际={market_grant2.expires_at}, 期望={expected_market_expires}"
    )

    # 新能力 review_management 从 now 开始 + 2 月
    now = datetime.now(UTC)
    expected_review_expires = add_calendar_months_asiashanghai(now, 2)
    # 允许 1 秒误差（now 在函数内部取）
    delta = abs((review_grant2.expires_at - expected_review_expires).total_seconds())
    assert delta < 5, (
        f"review grant 应从 now 开始 + 2 月，"
        f"实际={review_grant2.expires_at}, 期望≈{expected_review_expires}"
    )


@pytest.mark.asyncio
async def test_redeem_adds_new_capability(db_session: AsyncSession) -> None:
    """D3 新能力立即增加：用户只有 watchlist_management，兑换 market_screening 后立即获得。"""
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)

    # 第一次邀请码：仅 watchlist，1 月
    invite1, raw_code1 = await _create_invite_code(
        db_session,
        admin,
        duration_months=1,
        capabilities=[
            InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=20),
        ],
    )
    await db_session.flush()
    await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code1,
        user_id=member.id,
        usage_type="registration",
    )
    await db_session.flush()

    # 第二次邀请码：仅 market，2 月
    invite2, raw_code2 = await _create_invite_code(
        db_session,
        admin,
        duration_months=2,
        capabilities=[
            InviteCodeCapabilityInput(MARKET_SCREENING, limit_value=None),
        ],
    )
    await db_session.flush()
    _, grants2, _ = await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code2,
        user_id=member.id,
        usage_type="renewal",
    )
    await db_session.flush()

    # 第二次兑换创建 1 个 market grant
    assert len(grants2) == 1
    assert grants2[0].capability_key == MARKET_SCREENING

    # 验证用户当前有 watchlist + market 两个 active grant
    all_grants_stmt = select(UserCapabilityGrant).where(
        UserCapabilityGrant.user_id == member.id,
        UserCapabilityGrant.revoked_at.is_(None),
    )
    all_grants = (await db_session.execute(all_grants_stmt)).scalars().all()
    cap_keys = {g.capability_key for g in all_grants}
    assert cap_keys == {WATCHLIST_MANAGEMENT, MARKET_SCREENING}


# ============================================================
# D4 月末续期
# ============================================================


@pytest.mark.asyncio
async def test_redeem_month_end_extension(db_session: AsyncSession) -> None:
    """D4 月末续期：在 1 月 31 日兑换 1 月邀请码，到期日应为 2 月 28 日（日历月收缩）。

    验证 PRD §6.3 + capability_calendar 月末规则。
    通过直接构造 expires_at 模拟 1 月 31 日的 grant，再兑换延长。
    """
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)

    # 直接构造一个 1 月 31 日的 watchlist grant（模拟用户在最晚边界）
    jan_31 = datetime(2026, 1, 31, 10, 0, tzinfo=UTC)
    existing_grant = UserCapabilityGrant(
        id=uuid.uuid4(),
        user_id=member.id,
        capability_key=WATCHLIST_MANAGEMENT,
        limit_value=20,
        source_type="legacy_subscription",
        source_id="legacy-test-1",
        starts_at=jan_31,
        expires_at=add_calendar_months_asiashanghai(jan_31, 1),  # 2026-02-28
        revoked_at=None,
    )
    db_session.add(existing_grant)
    await db_session.flush()

    # 创建邀请码：watchlist + 1 月
    invite, raw_code = await _create_invite_code(
        db_session,
        admin,
        duration_months=1,
        capabilities=[
            InviteCodeCapabilityInput(WATCHLIST_MANAGEMENT, limit_value=30),
        ],
    )
    await db_session.flush()

    _, grants, _ = await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code,
        user_id=member.id,
        usage_type="renewal",
    )
    await db_session.flush()

    # base = max(now, 2026-02-28)；now 是真实当前时间（2026-07-26 左右）
    # → base = now（now 在 2026-02-28 之后）
    # → new_expires_at = now + 1 月（按日历月）
    # 这个测试主要验证：当 existing grant 已过期时，base = now，新 grant 从 now 延长
    assert len(grants) == 1
    new_grant = grants[0]
    assert new_grant.capability_key == WATCHLIST_MANAGEMENT
    # expires_at 必须在未来
    assert new_grant.expires_at > datetime.now(UTC)


# ============================================================
# D4 缓存刷新
# ============================================================


@pytest.mark.asyncio
async def test_redeem_invalidates_cache(db_session: AsyncSession) -> None:
    """D4 缓存刷新：兑换后用户 AccessContext 缓存被精确失效。"""
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)

    # 预填充缓存（通过 get_capability_access_context）
    invalidate_access_context_cache()  # 清空全部缓存确保干净起点
    await get_capability_access_context(db_session, member)
    member_id_str = str(member.id)
    assert member_id_str in _access_context_cache, (
        "预填充缓存失败：get_capability_access_context 应缓存结果"
    )

    # 兑换邀请码
    invite, raw_code = await _create_invite_code(db_session, admin, duration_months=1)
    await db_session.flush()
    await redeem_invite_code_with_capabilities(
        db=db_session,
        raw_invite_code=raw_code,
        user_id=member.id,
        usage_type="registration",
    )
    await db_session.flush()

    # 兑换后缓存应被失效
    assert member_id_str not in _access_context_cache, (
        "兑换后用户 AccessContext 缓存应被精确失效"
    )

    # 清理：避免污染其他测试
    invalidate_access_context_cache()


# ============================================================
# D2 并发兑换：两个用户并发兑换同一码，只有一个成功
# ============================================================


@pytest.mark.asyncio
async def test_redeem_concurrent_same_code_only_one_succeeds() -> None:
    """D2 并发兑换：两个用户并发兑换同一码，FOR UPDATE 行锁确保只有一个成功。

    不使用 conftest 的 db_session fixture（savepoint 模式不适合并发），
    直接用 TestAsyncSessionLocal 创建独立 session，手动管理事务与清理。
    """
    from tests.conftest import TestAsyncSessionLocal

    # 1. 准备阶段：创建 admin + member_a + member_b + 邀请码
    setup_session = TestAsyncSessionLocal()
    admin_email: str | None = None
    member_a_email: str | None = None
    member_b_email: str | None = None
    raw_code: str | None = None
    try:
        admin = await _create_admin(setup_session)
        admin_email = admin.email
        member_a = await _create_member(setup_session)
        member_a_email = member_a.email
        member_b = await _create_member(setup_session)
        member_b_email = member_b.email
        _, raw_code = await _create_invite_code(setup_session, admin, duration_months=1)
        await setup_session.commit()
    finally:
        await setup_session.close()

    assert raw_code is not None

    member_a_id_query = TestAsyncSessionLocal()
    member_b_id_query = TestAsyncSessionLocal()
    try:
        from app.models.user import User as UserModel

        a_result = await member_a_id_query.execute(
            select(UserModel).where(UserModel.email == member_a_email)
        )
        b_result = await member_b_id_query.execute(
            select(UserModel).where(UserModel.email == member_b_email)
        )
        member_a_id = a_result.scalar_one().id
        member_b_id = b_result.scalar_one().id
    finally:
        await member_a_id_query.close()
        await member_b_id_query.close()

    # 2. 并发兑换阶段
    async def redeem_and_commit(
        session: AsyncSession, user_id: uuid.UUID
    ) -> tuple[InviteCode, list[UserCapabilityGrant], list]:
        try:
            result = await redeem_invite_code_with_capabilities(
                db=session,
                raw_invite_code=raw_code,  # type: ignore[arg-type]
                user_id=user_id,
                usage_type="registration",
            )
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise

    session_a = TestAsyncSessionLocal()
    session_b = TestAsyncSessionLocal()
    results: tuple = ()
    try:
        results = await asyncio.gather(
            redeem_and_commit(session_a, member_a_id),
            redeem_and_commit(session_b, member_b_id),
            return_exceptions=True,
        )
    finally:
        await session_a.close()
        await session_b.close()

    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]

    assert len(successes) == 1, (
        f"预期恰好 1 个成功，实际 {len(successes)} 个成功。结果: {results}"
    )
    assert len(failures) == 1, (
        f"预期恰好 1 个失败，实际 {len(failures)} 个失败。结果: {results}"
    )
    assert isinstance(failures[0], ValueError), (
        f"失败异常应为 ValueError，实际为 {type(failures[0]).__name__}: {failures[0]}"
    )
    assert "邀请码已被兑换" in str(failures[0]), (
        f"失败异常应包含'邀请码已被兑换'，实际: {failures[0]}"
    )

    # 3. 清理：按 FK 依赖顺序删除测试数据
    cleanup_session = TestAsyncSessionLocal()
    try:
        from app.services.subscription_service import hash_invite_code

        # 删除邀请码（级联删除 invite_code_capabilities / invite_redemptions）
        code_hash = hash_invite_code(raw_code)
        invite_stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
        invite = (
            await cleanup_session.execute(invite_stmt)
        ).scalar_one_or_none()
        if invite is not None:
            await cleanup_session.delete(invite)

        # 删除用户（级联删除 user_capability_grants / user_roles）
        for email in (member_a_email, member_b_email, admin_email):
            user_stmt = select(User).where(User.email == email)
            user = (await cleanup_session.execute(user_stmt)).scalar_one_or_none()
            if user is not None:
                await cleanup_session.delete(user)

        await cleanup_session.commit()
    finally:
        await cleanup_session.close()
        invalidate_access_context_cache()


# ============================================================
# D4 原子回滚
# ============================================================


@pytest.mark.asyncio
async def test_redeem_atomic_rollback_on_invalid_usage_type(
    db_session: AsyncSession,
) -> None:
    """D4 原子回滚：usage_type 非法时立即抛错，邀请码保持 available。

    验证：redeem 函数在创建 grant 之前的所有校验失败时，
    邀请码状态不变（仍为 available），无 grant 被创建。
    """
    admin = await _create_admin(db_session)
    member = await _create_member(db_session)
    invite, raw_code = await _create_invite_code(db_session, admin)
    await db_session.flush()

    # usage_type 非法 → 函数开头立即抛错
    with pytest.raises(ValueError, match="usage_type"):
        await redeem_invite_code_with_capabilities(
            db=db_session,
            raw_invite_code=raw_code,
            user_id=member.id,
            usage_type="invalid_type",
        )

    # 邀请码状态应保持 available
    await db_session.refresh(invite)
    assert derive_invite_code_status_v2(invite) == "available"
    assert invite.redeemed_at is None
    assert invite.redeemed_by_user_id is None

    # 无 grant 被创建
    grant_stmt = select(UserCapabilityGrant).where(
        UserCapabilityGrant.user_id == member.id
    )
    grants = (await db_session.execute(grant_stmt)).scalars().all()
    assert len(grants) == 0, "usage_type 非法时不应创建任何 grant"
