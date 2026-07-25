"""POST /watchlist 监控数量额度校验测试（SubTask 4.11）。

验证场景：
- 普通用户 observe_20：加第 21 只返回 409 "监控数量已达上限 20"
- 普通用户 research_50：加第 51 只返回 409 "监控数量已达上限 50"
- 管理员：可以超过 50 只（绕过额度限制）
- 降级不删除：research_50 用户有 30 只，降级到 observe_20 后 30 只保留但禁止新增
- 恢复软删除前校验额度：用户有 20 只 active + 1 只 inactive，恢复 inactive 时返回 409
- 低于额度时正常加入

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用 POST /watchlist
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.instrument import Instrument
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.watchlist import UserWatchlistItem
from app.services.subscription_service import generate_invite_codes, register_with_invite_code
from tests.conftest import make_asgi_transport


def _assert_watchlist_limit_reached(detail: object, expected_limit: int | None = None) -> None:
    """[V2.1] 断言 409 WATCHLIST_LIMIT_REACHED detail（兼容 dict 和字符串格式）。"""
    if isinstance(detail, dict):
        assert detail.get("reason_code") == "WATCHLIST_LIMIT_REACHED"
        msg = detail.get("message", "")
        assert "监控数量已达上限" in msg
        if expected_limit is not None:
            assert str(expected_limit) in msg
    else:
        # 兼容旧字符串格式
        assert "监控数量已达上限" in str(detail)
        if expected_limit is not None:
            assert str(expected_limit) in str(detail)


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（admin 角色，无 membership 记录）。"""
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


async def _create_user_with_plan(
    db: AsyncSession, plan_code: str, grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建普通用户 + 订阅记录。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db, count=1, created_by=admin.id,
        plan_code=plan_code, grant_months=grant_months,
    )
    await db.flush()
    email = f"user_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db, email=email, password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_instruments(db: AsyncSession, count: int) -> list[Instrument]:
    """创建若干测试标的。"""
    instruments = []
    for _ in range(count):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name="测试标的",
            market="SZ",
            status="active",
        )
        db.add(inst)
        instruments.append(inst)
    await db.flush()
    return instruments


async def _add_active_watchlist(
    db: AsyncSession, user: User, instruments: list[Instrument], count: int
) -> None:
    """为用户添加 count 个 active 自选股。"""
    for i in range(count):
        item = UserWatchlistItem(
            user_id=user.id,
            instrument_id=instruments[i].id,
            source="manual",
            active=True,
        )
        db.add(item)
    await db.flush()


@pytest_asyncio.fixture
async def watchlist_client(db_session: AsyncSession) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。

    Mock session.commit 为 flush，避免关闭 conftest 的嵌套事务（SAVEPOINT）。
    watchlist POST 端点会调用 db.commit()，若直接提交会破坏测试隔离。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    original_commit = db_session.commit

    async def mock_commit():
        await db_session.flush()

    db_session.commit = mock_commit  # type: ignore[method-assign]

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    db_session.commit = original_commit  # type: ignore[method-assign]
    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 监控数量额度校验测试
# ============================================================


@pytest.mark.asyncio
async def test_watchlist_observe_20_limit_blocked(watchlist_client):
    """observe_20 用户已有 20 只 active，加第 21 只返回 409。"""
    client, db = watchlist_client
    user, _ = await _create_user_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 21)
    await _add_active_watchlist(db, user, instruments, count=20)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[20].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    _assert_watchlist_limit_reached(detail, expected_limit=20)


@pytest.mark.asyncio
async def test_watchlist_research_50_limit_blocked(watchlist_client):
    """research_50 用户已有 50 只 active，加第 51 只返回 409。"""
    client, db = watchlist_client
    user, _ = await _create_user_with_plan(db, "research_50")
    instruments = await _create_instruments(db, 51)
    await _add_active_watchlist(db, user, instruments, count=50)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[50].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    _assert_watchlist_limit_reached(detail, expected_limit=50)


@pytest.mark.asyncio
async def test_watchlist_admin_exceeds_50_allowed(watchlist_client):
    """管理员：已有 50 只 active，加第 51 只成功（绕过额度限制）。"""
    client, db = watchlist_client
    admin = await _create_admin(db)
    instruments = await _create_instruments(db, 51)
    await _add_active_watchlist(db, admin, instruments, count=50)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[50].id), "source": "manual"},
        headers=_auth_headers(admin.id),
    )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_watchlist_downgrade_does_not_delete(watchlist_client):
    """降级不删除：research_50 用户有 30 只 active，降级到 observe_20 后 30 只保留但禁止新增。

    [V2.1] 权限真源是 capability grants。降级必须撤销旧 grants 并创建新 grants，
    否则聚合规则取 max(limit_value) 仍为 50。本测试模拟 V2.1 正式降级流程。

    场景：
    1. research_50 用户添加 30 只自选股（< 50，允许）
    2. V2.1 降级：撤销 research_50 grants，创建 observe_20 grants（limit=20）
    3. 30 只自选股仍然保留（不删除）
    4. 新增第 31 只返回 409（超过 observe_20 的 20 上限）
    """
    from app.constants.capability_keys import WATCHLIST_MANAGEMENT
    from app.models.capability_grant import UserCapabilityGrant

    client, db = watchlist_client
    # 1. research_50 用户添加 30 只自选股
    user, subscription = await _create_user_with_plan(db, "research_50")
    instruments = await _create_instruments(db, 31)
    await _add_active_watchlist(db, user, instruments, count=30)
    await db.flush()

    # 2. V2.1 降级：撤销旧 grants + 修改 subscription + 创建新 grants（limit=20）
    subscription.plan_code = "observe_20"
    subscription.entitlement_snapshot = {"monitor_limit": 20}
    # 撤销所有 watchlist_management grants（模拟正式降级流程）
    grant_result = await db.execute(
        select(UserCapabilityGrant).where(
            UserCapabilityGrant.user_id == user.id,
            UserCapabilityGrant.capability_key == WATCHLIST_MANAGEMENT,
            UserCapabilityGrant.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for grant in grant_result.scalars():
        grant.revoked_at = now
    # 创建新 grant（observe_20，limit=20）
    # source_id 使用新 UUID 模拟新邀请码来源，避免与原 grant 的 UNIQUE 约束冲突
    db.add(UserCapabilityGrant(
        user_id=user.id,
        capability_key=WATCHLIST_MANAGEMENT,
        limit_value=20,
        source_type="legacy_subscription",
        source_id=str(uuid.uuid4()),
        starts_at=now,
        expires_at=subscription.expires_at,
        revoked_at=None,
    ))
    await db.flush()

    # 3. 验证 30 只自选股仍保留
    count_stmt = select(UserWatchlistItem).where(
        UserWatchlistItem.user_id == user.id,
        UserWatchlistItem.active.is_(True),
    )
    result = await db.execute(count_stmt)
    active_items = result.scalars().all()
    assert len(active_items) == 30, "降级后已有自选股应保留不删除"

    # 4. 新增第 31 只返回 409
    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[30].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 409
    _assert_watchlist_limit_reached(resp.json()["detail"], expected_limit=20)


@pytest.mark.asyncio
async def test_watchlist_restore_soft_deleted_checks_limit(watchlist_client):
    """恢复软删除前校验额度：用户有 20 只 active + 1 只 inactive，恢复 inactive 时返回 409。

    场景：
    1. observe_20 用户添加 21 只自选股（直接写入 DB，绕过 API 校验）
       - 20 只 active + 1 只 inactive
    2. 通过 API 恢复 inactive 记录 → 返回 409（恢复后将为 21 只，超过 20 上限）
    """
    client, db = watchlist_client
    user, _ = await _create_user_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 21)
    # 20 只 active
    for i in range(20):
        db.add(UserWatchlistItem(
            user_id=user.id, instrument_id=instruments[i].id,
            source="manual", active=True,
        ))
    # 1 只 inactive（软删除）
    db.add(UserWatchlistItem(
        user_id=user.id, instrument_id=instruments[20].id,
        source="manual", active=False, removed_at=datetime.now(UTC),
    ))
    await db.flush()

    # 通过 API 恢复 inactive 记录 → 409
    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[20].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 409
    _assert_watchlist_limit_reached(resp.json()["detail"], expected_limit=20)


@pytest.mark.asyncio
async def test_watchlist_under_limit_allowed(watchlist_client):
    """低于额度时正常加入：observe_20 用户有 19 只 active，加第 20 只成功。"""
    client, db = watchlist_client
    user, _ = await _create_user_with_plan(db, "observe_20")
    instruments = await _create_instruments(db, 20)
    await _add_active_watchlist(db, user, instruments, count=19)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[19].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )

    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_watchlist_no_membership_user_blocked(watchlist_client):
    """无会员记录的普通用户：加入自选返回 403 或 409（无额度权限）。

    无会员记录意味着没有 monitor_limit，应拒绝加入（不能默认无限）。
    """
    client, db = watchlist_client
    # 创建无会员记录的普通用户
    user = User(
        id=uuid.uuid4(),
        email=f"nomember_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    user_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=user_role.id))
    instruments = await _create_instruments(db, 1)
    await db.flush()

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    # 无会员记录应拒绝（403 无权限或 409 额度不足）
    assert resp.status_code in (403, 409)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
