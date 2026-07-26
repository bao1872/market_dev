"""V2.1 Phase I 真实并发测试 - 两个独立 DB 连接并发添加自选。

用户要求：
  自选额度=1，使用两个独立DB连接和两个独立AsyncSession并发添加不同股票，
  必须恰好一个成功、最终count=1；不得用顺序请求代替并发。

测试策略：
- 使用 TestAsyncSessionLocal（独立连接，非 savepoint 模式）创建测试数据并 commit
- 覆盖 get_db 依赖为 TestAsyncSessionLocal（每个请求获取独立 session）
- 使用 asyncio.gather 并发发起两个 POST /watchlist 请求
- 断言恰好一个 201、一个 409，最终 active count=1
- 测试结束清理数据（DELETE user + cascade）

此测试验证 SELECT FOR UPDATE 行级锁的真实并发效果，
不依赖 savepoint 模式或顺序请求。
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import delete, select

from app.constants.capability_keys import MARKET_SCREENING, WATCHLIST_MANAGEMENT
from app.core.deps import get_db as deps_get_db
from app.core.security import create_access_token, get_password_hash
from app.db import get_db as db_get_db
from app.main import app
from app.models.capability_grant import UserCapabilityGrant
from app.models.instrument import Instrument
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from tests.conftest import TestAsyncSessionLocal, make_asgi_transport


@pytest_asyncio.fixture
async def concurrent_setup() -> AsyncGenerator[dict, None]:
    """创建测试数据（独立连接，真实 commit），测试后清理。

    返回 dict 包含：user_id, instrument_ids, auth_headers
    """
    async with TestAsyncSessionLocal() as session:
        # 创建 member 角色（若不存在）
        result = await session.execute(select(Role).where(Role.name == "member"))
        member_role = result.scalar_one_or_none()
        if member_role is None:
            member_role = Role(id=uuid.uuid4(), name="member", description="member")
            session.add(member_role)
            await session.flush()

        # 创建用户
        user_id = uuid.uuid4()
        user = User(
            id=user_id,
            email=f"concurrent_{uuid.uuid4().hex[:8]}@test.com",
            password_hash=get_password_hash("password-12345"),
            status="active",
            timezone="Asia/Shanghai",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(user)
        await session.flush()  # 显式 flush 用户，确保 INSERT 顺序

        session.add(UserRole(user_id=user.id, role_id=member_role.id))

        # 创建订阅 + capability grants（额度=1）
        now = datetime.now(UTC)
        starts_at = now - timedelta(days=1)
        expires_at = now + timedelta(days=30)
        sub = Subscription(
            id=uuid.uuid4(),
            user_id=user.id,
            plan_code="observe_20",
            status="active",
            starts_at=starts_at,
            expires_at=expires_at,
            entitlement_snapshot={"monitor_limit": 1},
            source="invite",
            created_by=None,
        )
        session.add(sub)
        await session.flush()

        # watchlist_management 额度=1
        session.add(UserCapabilityGrant(
            user_id=user.id,
            capability_key=WATCHLIST_MANAGEMENT,
            limit_value=1,
            source_type="legacy_subscription",
            source_id=str(sub.id),
            starts_at=starts_at,
            expires_at=expires_at,
            revoked_at=None,
        ))
        # market_screening（无额度限制）
        session.add(UserCapabilityGrant(
            user_id=user.id,
            capability_key=MARKET_SCREENING,
            limit_value=None,
            source_type="legacy_subscription",
            source_id=str(sub.id),
            starts_at=starts_at,
            expires_at=expires_at,
            revoked_at=None,
        ))

        # 创建两个不同标的
        inst1 = Instrument(
            symbol=f"C{uuid.uuid4().hex[:5]}",
            name="并发测试标的1",
            market="SZ",
            status="active",
        )
        inst2 = Instrument(
            symbol=f"D{uuid.uuid4().hex[:5]}",
            name="并发测试标的2",
            market="SZ",
            status="active",
        )
        session.add_all([inst1, inst2])
        await session.commit()
        await session.refresh(inst1)
        await session.refresh(inst2)

        token = create_access_token(str(user_id))
        headers = {"Authorization": f"Bearer {token}"}

        yield {
            "user_id": user_id,
            "instrument_ids": [str(inst1.id), str(inst2.id)],
            "auth_headers": headers,
        }

        # 清理：删除用户（cascade 删除 watchlist items + grants + subscription）
        async with TestAsyncSessionLocal() as cleanup_session:
            await cleanup_session.execute(
                delete(UserWatchlistItem).where(UserWatchlistItem.user_id == user_id)
            )
            await cleanup_session.execute(
                delete(UserCapabilityGrant).where(UserCapabilityGrant.user_id == user_id)
            )
            await cleanup_session.execute(
                delete(Subscription).where(Subscription.user_id == user_id)
            )
            await cleanup_session.execute(
                delete(UserRole).where(UserRole.user_id == user_id)
            )
            await cleanup_session.execute(
                delete(User).where(User.id == user_id)
            )
            await cleanup_session.execute(
                delete(Instrument).where(Instrument.id.in_([inst1.id, inst2.id]))
            )
            await cleanup_session.commit()


@pytest.mark.asyncio
async def test_concurrent_watchlist_add_exactly_one_succeeds(
    concurrent_setup: dict,
) -> None:
    """额度=1，两个独立 DB 连接并发添加不同股票，恰好一个成功。

    验证：
    - 两个 POST /watchlist 并发请求
    - 恰好一个返回 201，一个返回 409 WATCHLIST_LIMIT_REACHED
    - 最终 active count = 1
    - 不依赖顺序请求（asyncio.gather 真并发）
    """
    instrument_ids = concurrent_setup["instrument_ids"]
    headers = concurrent_setup["auth_headers"]

    # 覆盖 get_db 为 TestAsyncSessionLocal（每个请求独立连接，非 savepoint）
    async def get_independent_db() -> AsyncGenerator:
        async with TestAsyncSessionLocal() as session:
            yield session

    app.dependency_overrides[deps_get_db] = get_independent_db
    app.dependency_overrides[db_get_db] = get_independent_db

    transport = make_asgi_transport(app)

    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # 真并发：asyncio.gather 同时发起两个请求
            results = await asyncio.gather(
                client.post(
                    "/watchlist",
                    json={"instrument_id": instrument_ids[0], "source": "manual"},
                    headers=headers,
                ),
                client.post(
                    "/watchlist",
                    json={"instrument_id": instrument_ids[1], "source": "manual"},
                    headers=headers,
                ),
            )

            r1, r2 = results[0], results[1]
            status_codes = sorted([r1.status_code, r2.status_code])

            # 恰好一个 201（成功），一个 409（额度超限）
            assert status_codes == [201, 409], (
                f"应恰好一个 201 一个 409，实际: {status_codes}\n"
                f"r1={r1.text}\nr2={r2.text}"
            )

            # 409 的响应体应包含 WATCHLIST_LIMIT_REACHED
            failed_response = r1 if r1.status_code == 409 else r2
            detail = failed_response.json().get("detail", {})
            if isinstance(detail, dict):
                assert detail.get("reason_code") == "WATCHLIST_LIMIT_REACHED", (
                    f"409 reason_code 应为 WATCHLIST_LIMIT_REACHED，实际: {detail}"
                )

            # 最终 active count = 1
            async with TestAsyncSessionLocal() as verify_session:
                count_result = await verify_session.execute(
                    select(UserWatchlistItem).where(
                        UserWatchlistItem.user_id == concurrent_setup["user_id"],
                        UserWatchlistItem.active.is_(True),
                    )
                )
                active_items = count_result.scalars().all()
                assert len(active_items) == 1, (
                    f"最终 active count 应为 1，实际: {len(active_items)}"
                )
    finally:
        app.dependency_overrides.clear()
