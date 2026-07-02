"""订阅与邀请码服务层 - V1.6 订阅系统业务逻辑 + plans 表套餐权限。

提供：
- generate_invite_codes: 生成邀请码（单个/批量，绑定 plan_code/grant_months）
- hash_invite_code: 邀请码哈希（SHA256）
- register_with_invite_code: 邀请码注册（原子操作，写入套餐快照到 Subscription）
- renew_with_invite_code: 邀请码续期（更新套餐，按自然月顺延到期日）
- get_subscription_status: 查询订阅记录（纯只读，返回 Subscription 对象）
- get_effective_subscription_status: 只读查询订阅有效状态（active/expired/none）
- revoke_invite_code: 作废邀请码
- list_invite_codes: 邀请码列表
- list_subscribers: 订阅账户列表（JOIN users + subscriptions，展示用 status 实时计算不写库）
- get_redemptions_by_user: 用户兑换记录

业务规则（plans 表套餐权限）：
- 生成邀请码：从 plans 表读取 monitor_limit 快照，写入 plan_code/monitor_limit/grant_months
- 注册：创建 Subscription（source='invite'），到期日按 grant_months 自然月计算
- 续期（未到期）：从当前到期日顺延 grant_months 个自然月，同时更新 plan_code/entitlement_snapshot
- 续期（已到期）：从兑换当天计算 grant_months 个自然月
- 邀请码为一次性，status: unused → used / revoked
- 邀请码明文不存储，仅存 SHA256 哈希
- grant_months 优先用于自然月计算（dateutil.relativedelta），grant_days 保留兼容性

Phase 8 调整：
- status 不持久化 'expired'：到期由 get_effective_subscription_status 实时计算
  （DB CheckConstraint 仅允许 active/revoked/cancelled）
- get_subscription_status 改为纯只读，不再写 status='expired'
- list_subscribers 用局部变量计算展示用 status（active 且过期 -> 'expired'），不写库
- 已删除 mark_expired_subscription（不再需要持久化 expired）

Phase 2 Task 2.2：由 membership_service.py 重命名为 subscription_service.py，
所有 Membership 引用改为 Subscription，函数名按 subscription 重命名。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.plan_codes import DEFAULT_PLAN_CODE
from app.core.security import get_password_hash
from app.models.invitation import InviteCode, InviteRedemption
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.plan_service import get_monitor_limit as get_monitor_limit_async
from app.services.plan_service import get_plan as get_plan_async


def _ensure_aware(dt: datetime) -> datetime:
    """确保 datetime 为时区感知；无时区时视为 UTC。

    SQLite 不保留 DateTime 时区信息，从 DB 读出的 datetime 为 offset-naive，
    与 datetime.now(UTC) 比较会抛 TypeError。此函数统一归一化为 offset-aware。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# 邀请码字符集（排除易混淆字符 O/0/I/1/L）
_INVITE_CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
# 邀请码分组：4 组 × 4 字符 = 16 字符
_INVITE_CODE_GROUPS = 4
_INVITE_CODE_GROUP_LEN = 4
# 会员默认天数（旧字段 grant_days，保留兼容性；新逻辑优先使用 grant_months）
_DEFAULT_GRANT_DAYS = 30
# 默认 grant_months（管理员未指定时，1 个月 = 30 天近似）
_DEFAULT_GRANT_MONTHS = 1


def _compute_expires_at_from_months(base: datetime, grant_months: int | None) -> datetime:
    """按 grant_months 自然月计算到期时间。

    Args:
        base: 基准时间
        grant_months: 自然月数

    Returns:
        到期时间（时区感知）
    """
    if grant_months is not None and grant_months > 0:
        return base + relativedelta(months=grant_months)
    # 兼容旧逻辑（未提供 grant_months 时回退 30 天）
    return base + timedelta(days=_DEFAULT_GRANT_DAYS)


def _compute_expires_at(base: datetime, invite: InviteCode) -> datetime:
    """根据邀请码的 grant_months 或 grant_days 计算到期时间。

    优先使用 grant_months（自然月，用 relativedelta），兼容旧邀请码的 grant_days（天数）。
    自然月计算：1月31日 + 1月 = 2月28日（非闰年），避免 30 天近似的跨月错误。

    Args:
        base: 基准时间（注册时为 now，续期未到期时为 old_expires_at）
        invite: 邀请码对象（含 grant_months/grant_days）

    Returns:
        到期时间（时区感知）
    """
    return _compute_expires_at_from_months(base, invite.grant_months)


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


def _build_entitlement_snapshot(plan) -> dict:
    """从 Plan ORM 对象构造 entitlement_snapshot JSONB 快照。

    快照字段：monitor_limit/notification_channel_limit/message_retention_days/features
    """
    return {
        "monitor_limit": int(plan.monitor_limit),
        "notification_channel_limit": int(plan.notification_channel_limit),
        "message_retention_days": int(plan.message_retention_days),
        "features": list(plan.features) if plan.features else [],
    }


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
    plan_code: str = DEFAULT_PLAN_CODE,
    grant_months: int = _DEFAULT_GRANT_MONTHS,
) -> list[tuple[InviteCode, str]]:
    """生成邀请码（批量，绑定 plan_code/grant_months）。

    从 plans 表读取 monitor_limit 快照写入邀请码，作为不可变的套餐快照。
    grant_months 用于注册/续期时按自然月计算到期日。

    Args:
        db: 异步数据库会话
        count: 生成数量
        created_by: 创建者 user_id（管理员）
        note: 批次备注
        plan_code: 套餐代码（observe_20/research_50），默认 observe_20
        grant_months: 兑换后增加的自然月数，默认 1

    Returns:
        list of (InviteCode ORM 对象, 明文邀请码) 元组

    Raises:
        ValueError: plan_code 不在 plans 表中，或 grant_months 非法
    """
    if grant_months < 1:
        raise ValueError(f"grant_months 必须 >= 1，实际: {grant_months}")

    # [PlanService] - 描述: 从 plans 表查询 monitor_limit，未知 plan_code 抛 ValueError
    monitor_limit = await get_monitor_limit_async(db, plan_code)

    results: list[tuple[InviteCode, str]] = []
    for _ in range(count):
        raw_code = _generate_invite_code()
        code_hash = hash_invite_code(raw_code)
        invite = InviteCode(
            code_hash=code_hash,
            status="unused",
            grant_days=_DEFAULT_GRANT_DAYS,
            plan_code=plan_code,
            monitor_limit=monitor_limit,
            grant_months=grant_months,
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
) -> tuple[User, Subscription]:
    """邀请码注册 - 原子操作（悲观锁防止并发一码多用）。

    流程：
    1. 哈希邀请码并查找（SELECT ... FOR UPDATE 行级锁，串行化并发请求）
    2. 校验邀请码状态为 unused
    3. 检查邮箱未被注册
    4. 创建用户（status=active）
    5. 创建订阅记录（source='invite'，含 entitlement_snapshot 套餐快照）
    6. 更新邀请码状态为 used
    7. 写入兑换记录
    8. flush（由调用方 commit，提交后释放行锁）

    并发安全：with_for_update() 在 PostgreSQL 生成 SELECT ... FOR UPDATE，
    第二个并发请求会阻塞直到第一个事务提交，然后读到 status=used 失败。
    SQLite 忽略 with_for_update（不支持行级锁）。

    Args:
        db: 异步数据库会话
        email: 用户邮箱
        password: 明文密码
        raw_invite_code: 邀请码明文
        timezone: 用户时区

    Returns:
        (User, Subscription) 元组

    Raises:
        ValueError: 邀请码无效/已使用/已作废，或邮箱已注册
    """
    # 1. 哈希邀请码并查找（FOR UPDATE 行锁防止并发注册同一邀请码）
    code_hash = hash_invite_code(raw_invite_code)
    invite_stmt = (
        select(InviteCode)
        .where(InviteCode.code_hash == code_hash)
        .with_for_update()
    )
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

    # 4. 分配 member 角色（不存在则自动创建，保证注册路径自洽）
    role_stmt = select(Role).where(Role.name == "member")
    role_result = await db.execute(role_stmt)
    member_role = role_result.scalar_one_or_none()
    if member_role is None:
        member_role = Role(id=uuid.uuid4(), name="member", description="普通会员")
        db.add(member_role)
        await db.flush()
    db.add(UserRole(user_id=user.id, role_id=member_role.id))

    # 5. 创建订阅记录（按 grant_months 自然月计算到期日，写入套餐快照到 entitlement_snapshot）
    expires_at = _compute_expires_at(now, invite)
    # [PlanService] - 描述: 从 plans 表查询套餐构造 entitlement_snapshot 快照
    plan = await get_plan_async(db, invite.plan_code or DEFAULT_PLAN_CODE)
    entitlement_snapshot = _build_entitlement_snapshot(plan)
    subscription = Subscription(
        user_id=user.id,
        plan_code=invite.plan_code or DEFAULT_PLAN_CODE,
        status="active",
        starts_at=now,
        expires_at=expires_at,
        entitlement_snapshot=entitlement_snapshot,
        source="invite",
        created_by=None,
        updated_at=now,
    )
    db.add(subscription)

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

    return user, subscription


async def renew_with_invite_code(
    db: AsyncSession,
    user_id: uuid.UUID,
    raw_invite_code: str,
) -> tuple[Subscription, datetime | None, datetime]:
    """邀请码续期 - 同时更新套餐（plan_code/entitlement_snapshot）和按自然月顺延到期日。

    业务规则：
    - 未到期续期：从当前到期日顺延 grant_months 个自然月
    - 已到期续期：从兑换当天计算 grant_months 个自然月
    - 续期时更新 subscription.plan_code/entitlement_snapshot 为邀请码的套餐快照
    - 兼容旧邀请码（grant_months 为 NULL 时回退 grant_days 天数计算）

    并发安全：与 register_with_invite_code 一致，使用 SELECT ... FOR UPDATE
    行级锁串行化并发续期请求，防止一码多用。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        raw_invite_code: 邀请码明文

    Returns:
        (Subscription, old_expires_at, new_expires_at) 元组

    Raises:
        ValueError: 邀请码无效/已使用/已作废，或用户不存在，或订阅记录不存在
    """
    # 1. 哈希邀请码并查找（FOR UPDATE 行锁防止并发续期同一邀请码）
    code_hash = hash_invite_code(raw_invite_code)
    invite_stmt = (
        select(InviteCode)
        .where(InviteCode.code_hash == code_hash)
        .with_for_update()
    )
    invite_result = await db.execute(invite_stmt)
    invite = invite_result.scalar_one_or_none()

    if invite is None:
        raise ValueError("邀请码无效")

    if invite.status == "used":
        raise ValueError("邀请码已被使用")
    if invite.status == "revoked":
        raise ValueError("邀请码已被作废")

    # 2. 查找用户订阅记录
    subscription_stmt = select(Subscription).where(Subscription.user_id == user_id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()

    if subscription is None:
        raise ValueError(f"用户订阅记录不存在: {user_id}")

    # 3. 计算新的到期时间（按 grant_months 自然月，兼容旧 grant_days）
    # old_expires_at 归一化为时区感知，确保与 new_expires_at（基于 now=UTC）一致，
    # 避免 API 响应中 old/new 一个 naive 一个 aware 导致前端解析失败
    now = datetime.now(UTC)
    old_expires_at = _ensure_aware(subscription.expires_at)

    if old_expires_at > now:
        # 未到期：从当前到期日顺延
        new_expires_at = _compute_expires_at(old_expires_at, invite)
    else:
        # 已到期：从兑换当天重新计算
        new_expires_at = _compute_expires_at(now, invite)

    # 4. 更新订阅记录（同时更新套餐与到期日 + 刷新 entitlement_snapshot）
    new_plan_code = invite.plan_code or DEFAULT_PLAN_CODE
    # [PlanService] - 描述: 从 plans 表查询套餐构造 entitlement_snapshot 快照
    plan = await get_plan_async(db, new_plan_code)
    entitlement_snapshot = _build_entitlement_snapshot(plan)
    subscription.status = "active"
    subscription.expires_at = new_expires_at
    subscription.plan_code = new_plan_code
    subscription.entitlement_snapshot = entitlement_snapshot
    subscription.updated_at = now

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

    return subscription, old_expires_at, new_expires_at


async def get_effective_subscription_status(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> tuple[Literal["active", "expired", "none"], datetime | None]:
    """只读查询用户订阅有效状态。

    不修改、不 flush 数据库。根据当前时间判断 status 语义：
    - 无订阅记录 -> ("none", None)
    - 有订阅且未过期 -> ("active", expires_at)
    - 有订阅但已过期 -> ("expired", expires_at)

    有效订阅实时计算（不缓存到登录态）：
        status = 'active' AND starts_at <= now AND expires_at > now

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        (状态字符串, expires_at) 元组
    """
    stmt = select(Subscription).where(Subscription.user_id == user_id)
    result = await db.execute(stmt)
    subscription = result.scalar_one_or_none()

    if subscription is None:
        return "none", None

    now = datetime.now(UTC)
    expires_at = _ensure_aware(subscription.expires_at)
    if expires_at <= now:
        return "expired", expires_at
    return "active", expires_at


async def get_subscription_status(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> Subscription | None:
    """查询用户订阅记录（纯只读，不写 DB）。

    返回 Subscription 对象，status 为持久化的生命周期状态
    （active/revoked/cancelled）。到期判断由调用方通过
    get_effective_subscription_status 或比较 expires_at 实时计算，
    本函数不持久化 'expired'。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID

    Returns:
        Subscription 对象或 None（用户无订阅记录）
    """
    stmt = select(Subscription).where(Subscription.user_id == user_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _is_admin_user(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """检查用户是否拥有 admin 角色。"""
    role_stmt = (
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.name == "admin")
    )
    result = await db.execute(role_stmt)
    return result.scalar_one_or_none() is not None


async def grant_subscription_to_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    plan_code: str,
    grant_months: int,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """管理员授予用户订阅（source='admin_grant'）。

    业务规则：
    - 管理员（admin 角色）不绑定套餐，禁止授予
    - 用户已存在 subscription 时失败（避免覆盖）
    - 从 plans 表读取 entitlement_snapshot 快照
    - 到期日按 grant_months 自然月计算

    Args:
        db: 异步数据库会话
        user_id: 被授权用户 ID
        plan_code: 套餐代码
        grant_months: 授予自然月数
        actor_user_id: 操作管理员 ID（可选）

    Returns:
        新创建的 Subscription 对象

    Raises:
        ValueError: 用户不存在、是 admin、已存在 subscription、或 plan_code 未知
    """
    if grant_months < 1:
        raise ValueError(f"grant_months 必须 >= 1，实际: {grant_months}")

    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"用户不存在: {user_id}")

    if await _is_admin_user(db, user_id):
        raise ValueError("admin 角色不绑定套餐，禁止授予 subscription")

    existing_stmt = select(Subscription).where(Subscription.user_id == user_id)
    existing_result = await db.execute(existing_stmt)
    if existing_result.scalar_one_or_none() is not None:
        raise ValueError(f"用户已存在 subscription: {user_id}")

    plan = await get_plan_async(db, plan_code)
    entitlement_snapshot = _build_entitlement_snapshot(plan)

    now = datetime.now(UTC)
    expires_at = _compute_expires_at_from_months(now, grant_months)
    subscription = Subscription(
        user_id=user_id,
        plan_code=plan_code,
        status="active",
        starts_at=now,
        expires_at=expires_at,
        entitlement_snapshot=entitlement_snapshot,
        source="admin_grant",
        created_by=actor_user_id,
        updated_at=now,
    )
    db.add(subscription)
    await db.flush()
    return subscription


async def renew_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    grant_months: int,
    actor_user_id: uuid.UUID | None = None,
) -> tuple[Subscription, datetime, datetime]:
    """管理员为用户续期订阅（按自然月顺延或从当前时间重新计算）。

    业务规则：
    - 未到期：从当前 expires_at 顺延 grant_months 个自然月
    - 已到期：从当前时间重新计算 grant_months 个自然月
    - 管理员（admin 角色）不续期

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        grant_months: 续期自然月数
        actor_user_id: 操作管理员 ID（可选）

    Returns:
        (Subscription, old_expires_at, new_expires_at)

    Raises:
        ValueError: 用户不存在、是 admin、或无 subscription
    """
    if grant_months < 1:
        raise ValueError(f"grant_months 必须 >= 1，实际: {grant_months}")

    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    if user_result.scalar_one_or_none() is None:
        raise ValueError(f"用户不存在: {user_id}")

    if await _is_admin_user(db, user_id):
        raise ValueError("admin 角色不绑定套餐，禁止续期 subscription")

    subscription_stmt = select(Subscription).where(Subscription.user_id == user_id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()
    if subscription is None:
        raise ValueError(f"用户订阅记录不存在: {user_id}")

    now = datetime.now(UTC)
    old_expires_at = _ensure_aware(subscription.expires_at)

    if old_expires_at > now:
        new_expires_at = _compute_expires_at_from_months(old_expires_at, grant_months)
    else:
        new_expires_at = _compute_expires_at_from_months(now, grant_months)

    subscription.status = "active"
    subscription.expires_at = new_expires_at
    subscription.updated_at = now
    await db.flush()
    return subscription, old_expires_at, new_expires_at


async def revoke_subscription(
    db: AsyncSession,
    user_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """管理员撤销用户订阅（status='revoked'）。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        actor_user_id: 操作管理员 ID（可选）

    Returns:
        更新后的 Subscription 对象

    Raises:
        ValueError: 用户不存在或无 subscription
    """
    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    if user_result.scalar_one_or_none() is None:
        raise ValueError(f"用户不存在: {user_id}")

    subscription_stmt = select(Subscription).where(Subscription.user_id == user_id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()
    if subscription is None:
        raise ValueError(f"用户订阅记录不存在: {user_id}")

    subscription.status = "revoked"
    subscription.updated_at = datetime.now(UTC)
    await db.flush()
    return subscription


async def change_subscription_plan(
    db: AsyncSession,
    user_id: uuid.UUID,
    plan_code: str,
    grant_months: int,
    actor_user_id: uuid.UUID | None = None,
) -> Subscription:
    """管理员修改用户套餐（无 subscription 时创建，有时更新并续期）。

    业务规则：
    - 用户无 subscription：按 admin_grant 创建新 subscription
    - 用户有 subscription：更新 plan_code/entitlement_snapshot，并按 grant_months
      从当前到期日或当前时间顺延
    - 管理员（admin 角色）不绑定套餐

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        plan_code: 目标套餐代码
        grant_months: 授予/续期自然月数
        actor_user_id: 操作管理员 ID（可选）

    Returns:
        Subscription 对象

    Raises:
        ValueError: 用户不存在、是 admin、或 plan_code 未知
    """
    if grant_months < 1:
        raise ValueError(f"grant_months 必须 >= 1，实际: {grant_months}")

    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if user is None:
        raise ValueError(f"用户不存在: {user_id}")

    if await _is_admin_user(db, user_id):
        raise ValueError("admin 角色不绑定套餐，禁止修改 subscription")

    plan = await get_plan_async(db, plan_code)
    entitlement_snapshot = _build_entitlement_snapshot(plan)

    subscription_stmt = select(Subscription).where(Subscription.user_id == user_id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()

    now = datetime.now(UTC)
    if subscription is None:
        expires_at = _compute_expires_at_from_months(now, grant_months)
        subscription = Subscription(
            user_id=user_id,
            plan_code=plan_code,
            status="active",
            starts_at=now,
            expires_at=expires_at,
            entitlement_snapshot=entitlement_snapshot,
            source="admin_grant",
            created_by=actor_user_id,
            updated_at=now,
        )
        db.add(subscription)
    else:
        old_expires_at = _ensure_aware(subscription.expires_at)
        base = old_expires_at if old_expires_at > now else now
        new_expires_at = _compute_expires_at_from_months(base, grant_months)
        subscription.plan_code = plan_code
        subscription.entitlement_snapshot = entitlement_snapshot
        subscription.expires_at = new_expires_at
        subscription.status = "active"
        subscription.updated_at = now

    await db.flush()
    return subscription


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


async def list_subscribers(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """查询订阅账户列表（JOIN users + subscriptions）。

    Args:
        db: 异步数据库会话
        limit: 分页大小
        offset: 分页偏移

    Returns:
        (订阅列表 dict, 总数) 元组
    """
    # 查询总数
    count_stmt = select(func.count()).select_from(User)
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # 查询用户 + 订阅信息
    stmt = (
        select(User, Subscription)
        .outerjoin(Subscription, Subscription.user_id == User.id)
        .order_by(User.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    result = await db.execute(stmt)

    now = datetime.now(UTC)
    subscribers: list[dict] = []
    for row in result.all():
        user = row[0]
        subscription = row[1]

        if subscription is not None:
            # 计算展示用 status（不写库）：active 且已过期 -> 'expired'，
            # 其余沿用持久化状态（active/revoked/cancelled）
            display_status = subscription.status
            if subscription.status == "active" and _ensure_aware(subscription.expires_at) <= now:
                display_status = "expired"

            remaining_days = (_ensure_aware(subscription.expires_at) - now).days
            renewal_count = await get_renewal_count(db, user.id)
            subscribers.append({
                "user_id": user.id,
                "email": user.email,
                "account_status": user.status,
                "membership_status": display_status,
                "started_at": subscription.starts_at,
                "expires_at": subscription.expires_at,
                "remaining_days": remaining_days,
                "renewal_count": renewal_count,
                "created_at": user.created_at,
            })
        else:
            subscribers.append({
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

    return subscribers, total


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

    # 验证函数签名
    assert callable(register_with_invite_code)
    assert callable(renew_with_invite_code)
    assert callable(get_effective_subscription_status)
    assert callable(get_subscription_status)
    assert callable(list_subscribers)
    print("OK")
