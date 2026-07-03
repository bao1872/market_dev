"""bootstrap_admin CLI - 创建第一个管理员账户。

用法：
    python -m app.scripts.bootstrap_admin --email admin@example.com [--dry-run] [--password PASSWORD]

功能：
    - 创建 admin 用户（status=active）+ 分配 admin 角色
    - 已有 admin 时拒绝执行（不写库）
    - --dry-run 不写库，仅打印将要执行的操作
    - 密码两次输入（交互式或通过 --password 传入）
    - 密码长度 >= 8
    - 事务失败全部回滚

设计说明：
    - admin 角色不存在时自动创建（首位管理员 bootstrap 场景）
    - [Phase9] admin 不创建 subscription：admin 豁免订阅校验，AccessContext 直接返回 subscription_active=True
    - admin 不绑定 research_50，不受 monitor_limit 限制（limits={} 表示无限制）
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.db import AsyncSessionLocal
from app.models.user import Role, User, UserRole

# 密码最小长度
PASSWORD_MIN_LENGTH = 8


def validate_password(password: str) -> None:
    """校验密码强度，不合法抛 ValueError。

    Args:
        password: 明文密码

    Raises:
        ValueError: 密码长度 < PASSWORD_MIN_LENGTH
    """
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(f"密码长度必须 >= {PASSWORD_MIN_LENGTH}")


def read_password_interactive() -> str:
    """交互式读取密码两次，不一致抛 ValueError。

    通过 getpass.getpass 读取密码，避免明文回显。
    两次输入必须一致，否则抛 ValueError。

    Returns:
        确认后的明文密码

    Raises:
        ValueError: 两次密码不一致
    """
    password = getpass.getpass("密码: ")
    confirm = getpass.getpass("确认密码: ")
    if password != confirm:
        raise ValueError("两次密码不一致")
    return password


async def _admin_exists(db: AsyncSession) -> bool:
    """检查系统中是否已存在 admin 用户（任意用户绑定 admin 角色）。

    Args:
        db: 异步数据库会话

    Returns:
        True 表示已存在 admin 用户
    """
    stmt = (
        select(UserRole.user_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(Role.name == "admin")
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _get_or_create_admin_role(db: AsyncSession) -> Role:
    """获取或创建 admin 角色（首位管理员 bootstrap 时角色可能不存在）。

    Args:
        db: 异步数据库会话

    Returns:
        admin Role 对象
    """
    result = await db.execute(select(Role).where(Role.name == "admin"))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db.add(role)
        await db.flush()
    return role


async def bootstrap_admin(
    db: AsyncSession,
    email: str,
    password: str,
    dry_run: bool = False,
) -> User | None:
    """创建第一个管理员账户。

    流程：
    1. 校验密码强度
    2. 检查是否已有 admin 用户（有则拒绝，抛 ValueError）
    3. 获取或创建 admin 角色
    4. dry_run 时返回 None（不写库）
    5. 创建用户（status=active）
    6. 分配 admin 角色
    7. flush（由调用方决定 commit 或 rollback）

    [Phase9] admin 不创建 subscription：admin 豁免订阅校验，
    AccessContext 直接返回 subscription_active=True, plan_code=None, limits={}。

    Args:
        db: 异步数据库会话
        email: 管理员邮箱
        password: 明文密码
        dry_run: True 时仅校验不写库

    Returns:
        创建的 User 对象；dry_run 时返回 None

    Raises:
        ValueError: 密码过短，或系统中已存在 admin 用户
    """
    # 1. 校验密码强度
    validate_password(password)

    # 2. 检查是否已有 admin 用户
    if await _admin_exists(db):
        raise ValueError("系统中已存在 admin 用户，拒绝执行 bootstrap")

    # 3. 获取或创建 admin 角色
    admin_role = await _get_or_create_admin_role(db)

    # 4. dry_run 不写库
    if dry_run:
        return None

    # 5. 创建用户
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=get_password_hash(password),
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()  # 获取 user.id

    # 6. 分配 admin 角色
    db.add(UserRole(user_id=user.id, role_id=admin_role.id))

    # [Phase9] - 描述: admin 不创建 subscription（admin 豁免订阅校验，AccessContext 直接返回 subscription_active=True）
    await db.flush()

    return user


async def _run(email: str, password: str, dry_run: bool) -> int:
    """执行 bootstrap（管理事务边界）。

    dry_run 时 rollback，正常时 commit；异常时 rollback 并返回非零退出码。

    Args:
        email: 管理员邮箱
        password: 明文密码
        dry_run: 是否仅打印不写库

    Returns:
        进程退出码（0 成功，1 失败）
    """
    async with AsyncSessionLocal() as db:
        try:
            user = await bootstrap_admin(db, email, password, dry_run)
            if dry_run:
                print(f"[DRY-RUN] 将创建管理员: {email}（不写库）")
                await db.rollback()
                return 0
            await db.commit()
            assert user is not None
            print(f"管理员创建成功: {email} (id={user.id})")
            return 0
        except Exception as e:
            await db.rollback()
            print(f"错误：{e}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。

    Args:
        argv: 命令行参数，None 时从 sys.argv 读取

    Returns:
        进程退出码（0 成功，1 失败）
    """
    parser = argparse.ArgumentParser(description="创建第一个管理员账户")
    parser.add_argument("--email", required=True, help="管理员邮箱")
    parser.add_argument("--password", help="管理员密码（不传则交互式输入两次）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印将要执行的操作，不写库")
    args = parser.parse_args(argv)

    # 密码处理：未传入 --password 时交互式输入两次
    password = args.password
    if password is None:
        try:
            password = read_password_interactive()
        except ValueError as e:
            print(f"错误：{e}", file=sys.stderr)
            return 1

    # 密码强度校验
    try:
        validate_password(password)
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1

    return asyncio.run(_run(args.email, password, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
