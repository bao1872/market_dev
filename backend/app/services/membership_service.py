"""会员与邀请码服务层 - V1.6 会员系统业务逻辑。

提供：
- generate_invite_codes: 生成邀请码（单个/批量）
- hash_invite_code: 邀请码哈希（SHA256）
- register_with_invite_code: 邀请码注册（原子操作）
- renew_with_invite_code: 邀请码续期
- get_membership_status: 查询会员状态
- revoke_invite_code: 作废邀请码
- list_invite_codes: 邀请码列表
- list_members: 会员账户列表
- get_redemptions_by_user: 用户兑换记录

业务规则：
- 注册：原子操作（锁定邀请码 → 创建账户 → 开通 30 天会员 → 写兑换记录）
- 续期（未到期）：从当前到期日顺延 30 天
- 续期（已到期）：从兑换当天重新计算 30 天
- 邀请码为一次性，status: unused → used / revoked
- 邀请码明文不存储，仅存 SHA256 哈希
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession


def _ensure_aware(dt: datetime) -> datetime:
    """确保 datetime 为时区感知；无时区时视为 UTC。

    SQLite 不保留 DateTime 时区信息，从 DB 读出的 datetime 为 offset-naive，
    与 datetime.now(UTC) 比较会抛 TypeError。此函数统一归一化为 offset-aware。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt

from app.core.security import get_password_hash
from app.models.membership import InviteCode, InviteRedemption, Membership
from app.models.user import Role, User, UserRole

# 邀请码字符集（排除易混淆字符 O/0/I/1/L）
_INVITE_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 邀请码分组：4 组 × 4 字符 = 16 字符
_INVITE_CODE_GROUPS = 4
_INVITE_CODE_GROUP_LEN = 4
# 会员默认天数
_DEFAULT_GRANT_DAYS = 30


def _generate_invite_code() -> str:
    """生成随机邀请码明文。

    格式：XXXX-XXXX-XXXX-XXXX（4 组 × 4 字符，排除易混淆字符）。

    Returns:
        邀请码明文字符串
    """
    groups = []
    for _ in range(_INVITE_CODE_GROUPS):
        group = "".join(
            secrets.choice(_INVITE_CODE_CHARS) for _ in range(_INVITE_CODE_GROUP_LEN)
        )
        groups.append(group)
    return "-".join(groups)


def hash_invite_code(raw_code: str) -> str:
    """计算邀请码的 SHA256 哈希。

    邀请码明文不存储，仅存储哈希用于查找。
    输入会去除前后空格并转为大写，保证一致性。

    Args:
        raw_code: 邀请码明文

    Returns:
        SHA256 哈希字符串（十六进制）
    """
    normalized = raw_code.strip().upper()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def generate_invite_codes(
    db: AsyncSession,
    count: int,
    created_by: uuid.UUID,
    note: str | None = None,
) -> list[tuple[InviteCode, str]]:
    """生成邀请码（批量）。

    Args:
        db: 异步数据库会话
        count: 生成数量
        created_by: 创建者 user_id（管理员）
        note: 批次备注

    Returns:
        list of (InviteCode ORM 对象, 明文邀请码) 元组
    """
    results: list[tuple[InviteCode, str]] = []
    for _ in range(count):
        raw_code = _generate_invite_code()
        code_hash = hash_invite_code(raw_code)
        invite = InviteCode(
            code_hash=code_hash,
            status="unused",
            grant_days=_DEFAULT_GRANT_DAYS,
            note=note,
            created_by=created_by,
        )
        db.add(invite)
        results.append((invite, raw_code))
    await db.flush()
    return results


async def register_with_invite_code(
    db: AsyncSession,
    email: str,
    password: str,
    raw_invite_code: str,
    timezone: str = "Asia/Shanghai",
) -> tuple[User, Membership]:
    """邀请码注册 - 原子操作。

    流程：
    1. 哈希邀请码并查找
    2. 校验邀请码状态为 unused
    3. 检查邮箱未被注册
    4. 创建用户（status=active）
    5. 创建会员记录（30 天）
    6. 更新邀请码状态为 used
    7. 写入兑换记录
    8. flush（由调用方 commit）

    Args:
        db: 异步数据库会话
        email: 用户邮箱
        password: 明文密码
        raw_invite_code: 邀请码明文
        timezone: 用户时区

    Returns:
        (User, Membership) 元组

    Raises:
        ValueError: 邀请码无效/已使用/已作废，或邮箱已注册
    """
    # 1. 哈希邀请码并查找
    code_hash = hash_invite_code(raw_invite_code)
    invite_stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
    invite_result = await db.execute(invite_stmt)
    invite = invite_result.scalar_one_or_none()

    if invite is None:
        raise ValueError("邀请码无效")

    if invite.status == "used":
        raise ValueError("邀请码已被使用")
    if invite.status == "revoked":
        raise ValueError("邀请码已被作废")

    # 2. 检查邮箱未被注册
    email_check = select(User).where(User.email == email)
    email_result = await db.execute(email_check)
    if email_result.scalar_one_or_none() is not None:
        raise ValueError(f"邮箱已被注册: {email}")

    # 3. 创建用户
    now = datetime.now(UTC)
    user = User(
        email=email,
        password_hash=get_password_hash(password),
        status="active",
        timezone=timezone,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()  # 获取 user.id

    # 4. 分配 user 角色
    role_stmt = select(Role).where(Role.name == "user")
    role_result = await db.execute(role_stmt)
    user_role = role_result.scalar_one_or_none()
    if user_role is not None:
        db.add(UserRole(user_id=user.id, role_id=user_role.id))

    # 5. 创建会员记录（30 天）
    expires_at = now + timedelta(days=invite.grant_days)
    membership = Membership(
        user_id=user.id,
        status="active",
        started_at=now,
        expires_at=expires_at,
        updated_at=now,
    )
    db.add(membership)

    # 6. 更新邀请码状态
    invite.status = "used"
    invite.used_by = user.id
    invite.used_at = now
    invite.usage_type = "registration"

    # 7. 写入兑换记录
    redemption = InviteRedemption(
        invite_code_id=invite.id,
        user_id=user.id,
        usage_type="registration",
        old_expires_at=None,
        new_expires_at=expires_at,
        redeemed_at=now,
    )
    db.add(redemption)
    await db.flush()

    return user, membership


async def renew_with_invite_code(
    db: AsyncSession,
    user_id: uuid.UUID,
    raw_invite_code: str,
) -> tuple[Membership, datetime | None, datetime]:
    """邀请码续期。

    业务规则：
    - 未到期续期：从当前到期日顺延 grant_days 天
    - 已到期续期：从兑换当天重新计算 grant_days 天

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        raw_invite_code: 邀请码明文

    Returns:
        (Membership, old_expires_at, new_expires_at) 元组

    Raises:
        ValueError: 邀请码无效/已使用/已作废，或用户不存在，或会员记录不存在
    """
    # 1. 哈希邀请码并查找
    code_hash = hash_invite_code(raw_invite_code)
    invite_stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
    invite_result = await db.execute(invite_stmt)
    invite = invite_result.scalar_one_or_none()

    if invite is None:
        raise ValueError("邀请码无效")

    if invite.status == "used":
        raise ValueError("邀请码已被使用")
    if invite.status == "revoked":
        raise ValueError("邀请码已被作废")

    # 2. 查找用户会员记录
    membership_stmt = select(Membership).where(Membership.user_id == user_id)
    membership_result = await db.execute(membership_stmt)
    membership = membership_result.scalar_one_or_none()

    if membership is None:
        raise ValueError(f"用户会员记录不存在: {user_id}")

    # 3. 计算新的到期时间
    now = datetime.now(UTC)
    old_expires_at = membership.expires_at

    if _ensure_aware(membership.expires_at) > now:
        # 未到期：从当前到期日顺延
        new_expires_at = membership.expires_at + timedelta(days=invite.grant_days)
    else:
        # 已到期：从兑换当天重新计算
        new_expires_at = now + timedelta(days=invite.grant_days)

    # 4. 更新会员记录
    membership.status = "active"
    membership.expires_at = new_expires_at
    membership.updated_at = now

    # 5. 更新邀请码状态
    invite.status = "used"
    invite.used_by = user_id
    invite.used_at = now
    invite.usage_type = "renewal"

    # 6. 写入兑换记录
    redemption = InviteRedemption(
        invite_code_id=invite.id,
        user_id=user_id,
        usage_type="renewal",
        old_expires_at=old_expires_at,
        new_expires_at=new_expires_at,
        redeemed_at=now,
    )
    db.add(redemption)
    await db.flush()

    return membership, old_expires_at, new_expires_at


async def get_membership_status(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Membership | None:
    """查询用户会员状态。

    同时检查并更新过期状态（如果 expires_at 已过但 status 仍为 active）。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        Membership 对象或 None（用户无会员记录）
    """
    stmt = select(Membership).where(Membership.user_id == user_id)
    result = await db.execute(stmt)
    membership = result.scalar_one_or_none()

    if membership is None:
        return None

    # 检查是否需要更新过期状态
    now = datetime.now(UTC)
    if membership.status == "active" and _ensure_aware(membership.expires_at) <= now:
        membership.status = "expired"
        membership.updated_at = now
        await db.flush()

    return membership


async def get_renewal_count(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """查询用户累计续期次数。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        续期次数（usage_type='renewal' 的记录数）
    """
    stmt = (
        select(func.count())
        .select_from(InviteRedemption)
        .where(
            InviteRedemption.user_id == user_id,
            InviteRedemption.usage_type == "renewal",
        )
    )
    result = await db.execute(stmt)
    return result.scalar_one()


async def revoke_invite_code(
    db: AsyncSession,
    invite_code_id: uuid.UUID,
) -> InviteCode:
    """作废邀请码（仅 unused 状态可作废）。

    Args:
        db: 异步数据库会话
        invite_code_id: 邀请码 ID

    Returns:
        更新后的 InviteCode 对象

    Raises:
        ValueError: 邀请码不存在或状态非 unused
    """
    stmt = select(InviteCode).where(InviteCode.id == invite_code_id)
    result = await db.execute(stmt)
    invite = result.scalar_one_or_none()

    if invite is None:
        raise ValueError(f"邀请码不存在: {invite_code_id}")

    if invite.status != "unused":
        raise ValueError(f"仅未使用邀请码可作废（当前状态: {invite.status}）")

    invite.status = "revoked"
    await db.flush()
    return invite


async def list_invite_codes(
    db: AsyncSession,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[InviteCode], int]:
    """查询邀请码列表。

    Args:
        db: 异步数据库会话
        status: 状态筛选（unused/used/revoked），None 为全部
        limit: 分页大小
        offset: 分页偏移

    Returns:
        (邀请码列表, 总数) 元组
    """
    base_stmt = select(InviteCode)
    count_stmt = select(func.count()).select_from(InviteCode)

    if status is not None:
        base_stmt = base_stmt.where(InviteCode.status == status)
        count_stmt = count_stmt.where(InviteCode.status == status)

    base_stmt = base_stmt.order_by(InviteCode.created_at.desc()).limit(limit).offset(offset)

    result = await db.execute(base_stmt)
    items = list(result.scalars().all())

    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    return items, total


async def list_members(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """查询会员账户列表（JOIN users + memberships）。

    Args:
        db: 异步数据库会话
        limit: 分页大小
        offset: 分页偏移

    Returns:
        (会员列表 dict, 总数) 元组
    """
    # 查询总数
    count_stmt = select(func.count()).select_from(User)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # 查询用户 + 会员信息
    stmt = (
        select(User, Membership)
        .outerjoin(Membership, Membership.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)

    now = datetime.now(UTC)
    members: list[dict] = []
    for row in result.all():
        user = row[0]
        membership = row[1]

        if membership is not None:
            # 检查是否需要更新过期状态
            if membership.status == "active" and _ensure_aware(membership.expires_at) <= now:
                membership.status = "expired"

            remaining_days = (_ensure_aware(membership.expires_at) - now).days
            renewal_count = await get_renewal_count(db, user.id)
            members.append({
                "user_id": user.id,
                "email": user.email,
                "account_status": user.status,
                "membership_status": membership.status,
                "started_at": membership.started_at,
                "expires_at": membership.expires_at,
                "remaining_days": remaining_days,
                "renewal_count": renewal_count,
                "created_at": user.created_at,
            })
        else:
            members.append({
                "user_id": user.id,
                "email": user.email,
                "account_status": user.status,
                "membership_status": None,
                "started_at": None,
                "expires_at": None,
                "remaining_days": None,
                "renewal_count": 0,
                "created_at": user.created_at,
            })

    return members, total


async def get_redemptions_by_user(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> list[InviteRedemption]:
    """查询用户兑换记录。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        兑换记录列表
    """
    stmt = (
        select(InviteRedemption)
        .where(InviteRedemption.user_id == user_id)
        .order_by(InviteRedemption.redeemed_at.desc())
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


if __name__ == "__main__":
    # 自测入口：验证邀请码生成与哈希
    code = _generate_invite_code()
    print(f"generated code: {code}")
    assert len(code) == 19  # 4*4 + 3 dashes
    assert code.count("-") == 3

    h1 = hash_invite_code(code)
    h2 = hash_invite_code(code.lower())
    h3 = hash_invite_code(f" {code} ")
    assert h1 == h2 == h3, "哈希应一致（忽略大小写和空格）"
    print(f"hash: {h1[:20]}...")

    # 验证不同邀请码哈希不同
    code2 = _generate_invite_code()
    assert code != code2, "两次生成的邀请码应不同"
    assert hash_invite_code(code) != hash_invite_code(code2)
    print("different codes hash differently")

    print("OK")
