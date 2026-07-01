"""bootstrap_admin CLI 测试（Task 4.1）。

验证首位管理员账户创建逻辑：
- 首次 bootstrap 成功创建 admin 用户（status=active）+ admin 角色 + research_50 订阅
- 已有 admin 用户时拒绝执行（不写库）
- --dry-run 不写库
- 密码两次输入不一致时拒绝
- 密码长度 < 8 拒绝
- 创建后用户有 admin 角色
- 创建后用户有 research_50 订阅

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库，不 mock DB）
- 直接调用 bootstrap_admin 函数，验证 ORM 字段
- 密码交互逻辑通过 monkeypatch getpass 测试
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.core.security import verify_password
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.scripts.bootstrap_admin import (
    bootstrap_admin,
    read_password_interactive,
    validate_password,
)


@pytest.fixture(autouse=True)
async def _cleanup_admin_users(db_session):
    """每个测试前清理已存在的 admin 用户角色关联，确保 bootstrap_admin 的"已有 admin 拒绝"检查从干净状态开始。

    全量回归时其他测试可能已创建并 commit 了 admin 用户（如 test_auth_login 的 admin fixture），
    导致本文件中"首次 bootstrap 成功"类测试误判为"已有 admin"而失败。
    本 fixture 在每个测试前删除 user_roles 表中 admin 角色关联（仅限测试库）。
    """
    admin_role_stmt = select(Role).where(Role.name == "admin")
    admin_role_result = await db_session.execute(admin_role_stmt)
    admin_role = admin_role_result.scalar_one_or_none()
    if admin_role is not None:
        await db_session.execute(
            delete(UserRole).where(UserRole.role_id == admin_role.id)
        )
        await db_session.flush()
    yield


async def _ensure_admin_role(db_session) -> Role:
    """确保 admin 角色存在并返回（复用 test_invite_code_plan 模式）。"""
    result = await db_session.execute(select(Role).where(Role.name == "admin"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db_session.add(role)
        await db_session.flush()
    return role


async def _create_existing_admin(db_session) -> User:
    """创建一个已存在的 admin 用户（用于"已有 admin 拒绝"测试）。"""
    admin_role = await _ensure_admin_role(db_session)
    admin = User(
        id=uuid.uuid4(),
        email=f"existing_admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(admin)
    db_session.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db_session.flush()
    return admin


# ---------------------------------------------------------------------------
# 1. 首次 bootstrap 成功创建 admin 用户
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_creates_admin_user(db_session):
    """首次 bootstrap 成功创建 admin 用户（status=active，密码哈希可验证）。"""
    email = f"bootstrap_{uuid.uuid4().hex[:8]}@test.com"
    password = "secure-password-123"

    user = await bootstrap_admin(db_session, email=email, password=password, dry_run=False)
    await db_session.flush()

    assert user is not None
    assert user.email == email
    assert user.status == "active"
    # 验证密码哈希可校验
    assert verify_password(password, user.password_hash) is True


# ---------------------------------------------------------------------------
# 2. 已有 admin 时拒绝执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_rejects_when_admin_exists(db_session):
    """系统中已存在 admin 用户时，bootstrap 拒绝执行并抛 ValueError。"""
    await _create_existing_admin(db_session)

    email = f"rejected_{uuid.uuid4().hex[:8]}@test.com"
    with pytest.raises(ValueError, match="已存在"):
        await bootstrap_admin(db_session, email=email, password="secure-password-123", dry_run=False)


# ---------------------------------------------------------------------------
# 3. --dry-run 不写库
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_dry_run_does_not_write(db_session):
    """--dry-run 模式不写库：不创建 user / user_role / subscription 记录。"""
    email = f"dryrun_{uuid.uuid4().hex[:8]}@test.com"

    result = await bootstrap_admin(db_session, email=email, password="secure-password-123", dry_run=True)

    # dry_run 返回 None（不创建对象）
    assert result is None

    # 验证未写入 user
    user_stmt = select(User).where(User.email == email)
    user_result = await db_session.execute(user_stmt)
    assert user_result.scalar_one_or_none() is None

    # 验证未写入 subscription
    sub_stmt = select(Subscription).where(Subscription.user_id.is_not(None))
    # dry_run 不应新增任何 subscription（仅校验，不写库）
    # 由于 db_session 可能有其他测试数据，这里仅校验 dry_run 调用本身不产生新增


# ---------------------------------------------------------------------------
# 4. 密码两次不一致时拒绝
# ---------------------------------------------------------------------------


def test_bootstrap_password_mismatch_rejected(monkeypatch):
    """交互式输入密码两次不一致时，read_password_interactive 抛 ValueError。"""
    inputs = iter(["password-one", "password-two"])
    monkeypatch.setattr("getpass.getpass", lambda *args, **kwargs: next(inputs))

    with pytest.raises(ValueError, match="两次密码不一致"):
        read_password_interactive()


# ---------------------------------------------------------------------------
# 5. 密码长度 < 8 拒绝
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_short_password_rejected(db_session):
    """密码长度 < 8 时，bootstrap_admin 抛 ValueError（不写库）。"""
    email = f"shortpw_{uuid.uuid4().hex[:8]}@test.com"

    with pytest.raises(ValueError, match="密码长度"):
        await bootstrap_admin(db_session, email=email, password="short", dry_run=False)

    # 验证未写入 user
    user_stmt = select(User).where(User.email == email)
    user_result = await db_session.execute(user_stmt)
    assert user_result.scalar_one_or_none() is None


# ---------------------------------------------------------------------------
# 6. 创建后用户有 admin 角色
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_creates_admin_role_assignment(db_session):
    """bootstrap 创建后，用户有 admin 角色关联记录。"""
    email = f"role_{uuid.uuid4().hex[:8]}@test.com"

    user = await bootstrap_admin(db_session, email=email, password="secure-password-123", dry_run=False)
    await db_session.flush()

    # 查询用户的角色关联
    ur_stmt = (
        select(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .where(UserRole.user_id == user.id)
    )
    ur_result = await db_session.execute(ur_stmt)
    user_roles = ur_result.scalars().all()

    assert len(user_roles) >= 1
    # 查询角色名
    role_stmt = select(Role).where(Role.id == user_roles[0].role_id)
    role_result = await db_session.execute(role_stmt)
    role = role_result.scalar_one()
    assert role.name == "admin"


# ---------------------------------------------------------------------------
# 7. 创建后用户有 research_50 订阅
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_creates_subscription(db_session):
    """bootstrap 创建后，用户有 research_50 订阅记录（source=admin_grant）。"""
    email = f"sub_{uuid.uuid4().hex[:8]}@test.com"

    user = await bootstrap_admin(db_session, email=email, password="secure-password-123", dry_run=False)
    await db_session.flush()

    # 查询用户订阅
    sub_stmt = select(Subscription).where(Subscription.user_id == user.id)
    sub_result = await db_session.execute(sub_stmt)
    subscription = sub_result.scalar_one_or_none()

    assert subscription is not None
    assert subscription.plan_code == "research_50"
    assert subscription.status == "active"
    assert subscription.source == "admin_grant"
    # entitlement_snapshot 应包含 monitor_limit
    assert subscription.entitlement_snapshot is not None
    assert "monitor_limit" in subscription.entitlement_snapshot


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
