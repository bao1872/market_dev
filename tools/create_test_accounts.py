"""一次性脚本：创建测试账号（普通用户 + 管理员）+ 开通会员。

用法：
    DATABASE_URL="postgresql+psycopg://bz:***@127.0.0.1:5432/bz_stock" \
    backend/.venv/bin/python tools/create_test_accounts.py

事实源：
- backend/app/models/user.py: User / Role / UserRole ORM
- backend/app/models/membership.py: Membership ORM
- backend/app/core/security.py: get_password_hash

约束：
- 仅在测试环境使用，不修改现有用户密码
- 不启用 MFA（系统当前无 MFA 字段，此约束自动满足）
- 测试账号邮箱：test-user@market.dev / test-admin@market.dev
- 测试账号需开通会员，否则前端会跳转到续期页（membership_expired=true）
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta

# 将 backend 目录加入 sys.path
_BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..", "backend")
_BACKEND_DIR = os.path.abspath(_BACKEND_DIR)
sys.path.insert(0, _BACKEND_DIR)

from sqlalchemy import select  # noqa: E402

from app.core.security import get_password_hash  # noqa: E402
from app.db import AsyncSessionLocal  # noqa: E402
from app.models.membership import Membership  # noqa: E402
from app.models.user import Role, User, UserRole  # noqa: E402

# 测试账号配置（密码禁止硬编码，通过 TEST_USER_PASSWORD / TEST_ADMIN_PASSWORD 传入）
_TEST_ACCOUNTS = [
    {
        "email": "test-user@market.dev",
        "password": os.environ.get("TEST_USER_PASSWORD"),
        "role_names": ["user"],
        "description": "测试普通用户",
    },
    {
        "email": "test-admin@market.dev",
        "password": os.environ.get("TEST_ADMIN_PASSWORD"),
        "role_names": ["admin", "user"],
        "description": "测试管理员",
    },
]

# 测试账号会员有效期（365 天，确保测试期间不会过期）
_TEST_MEMBERSHIP_DAYS = 365


async def _upsert_test_account(
    db,
    email: str,
    password: str,
    role_names: list[str],
    description: str,
) -> str:
    """创建或更新测试账号（已存在则跳过密码修改，仅补全角色与会员）。

    Args:
        db: 异步数据库会话
        email: 邮箱
        password: 明文密码
        role_names: 角色名列表
        description: 账号描述

    Returns:
        操作结果描述
    """
    # 查询现有用户
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if user is None:
        # 创建新用户
        user = User(
            email=email,
            password_hash=get_password_hash(password),
            status="active",
            timezone="Asia/Shanghai",
        )
        db.add(user)
        await db.flush()
        action = f"创建 {description}（id={user.id}）"
    else:
        # 已存在：不修改密码（任务约束），仅补全角色与会员
        action = f"已存在 {description}（id={user.id}），跳过密码修改"

    # 查询角色并补全关联
    for role_name in role_names:
        role_stmt = select(Role).where(Role.name == role_name)
        role_result = await db.execute(role_stmt)
        role = role_result.scalar_one_or_none()
        if role is None:
            print(f"  [WARN] 角色 {role_name} 不存在，跳过")
            continue

        # 检查是否已关联
        ur_stmt = select(UserRole).where(
            UserRole.user_id == user.id, UserRole.role_id == role.id
        )
        ur_result = await db.execute(ur_stmt)
        if ur_result.scalar_one_or_none() is None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
            action += f"，补全角色 {role_name}"

    # 补全会员记录（确保测试账号可访问用户端功能）
    membership_stmt = select(Membership).where(Membership.user_id == user.id)
    membership_result = await db.execute(membership_stmt)
    membership = membership_result.scalar_one_or_none()
    now = datetime.now(UTC)
    if membership is None:
        membership = Membership(
            user_id=user.id,
            status="active",
            started_at=now,
            expires_at=now + timedelta(days=_TEST_MEMBERSHIP_DAYS),
        )
        db.add(membership)
        action += f"，开通会员 {_TEST_MEMBERSHIP_DAYS} 天"
    elif membership.expires_at < now + timedelta(days=30):
        # 临近到期则顺延，避免测试期间过期
        membership.expires_at = now + timedelta(days=_TEST_MEMBERSHIP_DAYS)
        membership.status = "active"
        action += f"，顺延会员至 {membership.expires_at.isoformat()}"

    return action


async def main() -> int:
    """主入口：创建测试账号。"""
    print("=" * 60)
    print("测试账号创建脚本")
    print(f"时间: {datetime.now(UTC).isoformat()}")
    print("=" * 60)

    missing = [
        account["email"]
        for account in _TEST_ACCOUNTS
        if not account["password"]
    ]
    if missing:
        print(
            f"ERROR: 以下测试账号未设置密码，请通过环境变量传入：\n"
            f"  TEST_USER_PASSWORD / TEST_ADMIN_PASSWORD\n"
            f"  缺失密码的账号：{', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    async with AsyncSessionLocal() as db:
        for account in _TEST_ACCOUNTS:
            action = await _upsert_test_account(
                db=db,
                email=account["email"],
                password=account["password"],
                role_names=account["role_names"],
                description=account["description"],
            )
            print(f"  [{account['email']}] {action}")

        await db.commit()

    # 验证
    print("\n验证账号：")
    async with AsyncSessionLocal() as db:
        for account in _TEST_ACCOUNTS:
            stmt = (
                select(User, Role.name)
                .select_from(User)
                .outerjoin(UserRole, UserRole.user_id == User.id)
                .outerjoin(Role, Role.id == UserRole.role_id)
                .where(User.email == account["email"])
            )
            result = await db.execute(stmt)
            rows = result.all()
            if not rows:
                print(f"  [FAIL] {account['email']} 不存在")
                continue
            user = rows[0][0]
            roles = [r[1] for r in rows if r[1] is not None]
            print(
                f"  [OK] {user.email} status={user.status} "
                f"timezone={user.timezone} roles={roles}"
            )

    print("\n完成 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
