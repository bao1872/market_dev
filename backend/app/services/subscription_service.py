"""订阅与邀请码服务层 - V1.6 订阅系统业务逻辑 + plans 表套餐权限。

提供：
- generate_invite_codes: 生成邀请码（单个/批量，绑定 plan_code/grant_months）
- hash_invite_code: 邀请码哈希（SHA256）
- register_with_invite_code: 邀请码注册（原子操作，写入套餐快照到 Subscription）
- renew_with_invite_code: 邀请码续期（更新套餐，按 30 天周期顺延到期日）
- get_subscription_status: 查询订阅记录（纯只读，返回 Subscription 对象）
- get_effective_subscription_status: 只读查询订阅有效状态（active/expired/none）
- revoke_invite_code: 作废邀请码
- list_invite_codes: 邀请码列表
- list_subscribers: 订阅账户列表（JOIN users + subscriptions，展示用 status 实时计算不写库）
- get_redemptions_by_user: 用户兑换记录

业务规则（plans 表套餐权限）：
- 生成邀请码：从 plans 表读取 monitor_limit 快照，写入 plan_code/monitor_limit/grant_months
- 注册：创建 Subscription（source='invite'），到期日按 grant_months × 30 天计算
- 续期（未到期）：从当前到期日顺延 grant_months × 30 天，同时更新 plan_code/entitlement_snapshot
- 续期（已到期）：从兑换当天计算 grant_months × 30 天
- 邀请码为一次性，status: unused → used / revoked
- 邀请码明文不存储，仅存 SHA256 哈希
- grant_months 按 30 天周期计算（1 个月 = 30 天，N 个月 = N×30 天），grant_days 保留兼容性

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
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

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
# 订阅默认天数（旧字段 grant_days，保留兼容性；新逻辑优先使用 grant_months）
_DEFAULT_GRANT_DAYS = 30
# 默认 grant_months（管理员未指定时，1 个月 = 30 天近似）
_DEFAULT_GRANT_MONTHS = 1


def _compute_expires_at_from_months(base: datetime, grant_months: int | None) -> datetime:
    """按 grant_months × 30 天计算到期时间（固定 30 天周期）。

    Args:
        base: 基准时间
        grant_months: 30 天周期数（1 = 30 天，2 = 60 天）

    Returns:
        到期时间（时区感知）
    """
    if grant_months is not None and grant_months > 0:
        return base + timedelta(days=30 * grant_months)
    # 兼容旧逻辑（未提供 grant_months 时回退 30 天）
    return base + timedelta(days=_DEFAULT_GRANT_DAYS)


def _compute_expires_at(base: datetime, invite: InviteCode) -> datetime:
    """根据邀请码的 grant_months 或 grant_days 计算到期时间。

    优先使用 grant_months（30 天周期），兼容旧邀请码的 grant_days（天数）。
    - grant_months 为正数：使用 30 × grant_months 天
    - grant_months 为空且 grant_days 为正数：使用原 grant_days 天
    - 两者都无效：默认 30 天
    30 天周期：1 个月 = 30 天，2 个月 = 60 天，跨月/跨年按天数计算。

    Args:
        base: 基准时间（注册时为 now，续期未到期时为 old_expires_at）
        invite: 邀请码对象（含 grant_months/grant_days）

    Returns:
        到期时间（时区感知）
    """
    if invite.grant_months is not None and invite.grant_months > 0:
        return _compute_expires_at_from_months(base, invite.grant_months)
    if invite.grant_days is not None and invite.grant_days > 0:
        return base + timedelta(days=invite.grant_days)
    return base + timedelta(days=_DEFAULT_GRANT_DAYS)


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
    capabilities: list[dict[str, Any]] | None = None,
) -> list[tuple[InviteCode, str]]:
    """生成邀请码（批量，支持 capability 组合或 plan_code 兼容）。

    两种模式：
    1. 新模式（PA-20）：提供 capabilities 列表，存储到 InviteCode.capabilities JSONB
    2. 旧模式（兼容）：capabilities=None，从 plans 表读取 monitor_limit 快照

    Args:
        db: 异步数据库会话
        count: 生成数量
        created_by: 创建者 user_id（管理员）
        note: 批次备注
        plan_code: 套餐代码（旧模式），默认 observe_20
        grant_months: 兑换后增加的 30 天周期数（旧模式），默认 1
        capabilities: capability 组合（PA-20 新模式）；提供时优先于 plan_code

    Returns:
        list of (InviteCode ORM 对象, 明文邀请码) 元组

    Raises:
        ValueError: plan_code 不在 plans 表中，或 grant_months 非法，或 capabilities 非法
    """
    if grant_months < 1:
        raise ValueError(f"grant_months 必须 >= 1，实际: {grant_months}")

    # 旧模式：从 plans 表查询 monitor_limit
    monitor_limit = await get_monitor_limit_async(db, plan_code)

    # 新模式：capabilities 序列化为 JSONB 兼容格式
    capabilities_json: list[dict[str, Any]] | None = None
    if capabilities is not None:
        if not isinstance(capabilities, list) or len(capabilities) == 0:
            raise ValueError("capabilities 必须是非空列表")
        # 验证每个 capability 配置
        for cap in capabilities:
            cap_name = cap.get("capability")
            if cap_name not in ("self_selection", "market_data", "research_replay"):
                raise ValueError(f"无效 capability: {cap_name}")
            if cap_name == "self_selection" and cap.get("watchlist_limit") is None:
                raise ValueError("self_selection 必须指定 watchlist_limit")
        capabilities_json = capabilities

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
            capabilities=capabilities_json,
            note=note,
            created_by=created_by,
        )
        db.add(invite)
        results.append((invite, raw_code))
    await db.flush()
    return results


async def _grant_capabilities_from_invite(
    db: AsyncSession,
    user_id: uuid.UUID,
    invite_code: InviteCode,
) -> None:
    """从邀请码创建/更新 user_capabilities 行（PRD60 PA-20 新模式）。

    如果 invite_code.capabilities 不为 None，为每个 capability 创建独立授权行。
    如果为 None（旧模式），不创建（fallback 到 plan_code 推断，兼容期）。

    per-capability 独立 expires_at（PA-03 30 天周期）：
    - 从兑换时间 + months × 30 天计算，不继承 Subscription.expires_at
    - 已有该 capability 时取较晚的 expires_at（不降权）
    """
    if invite_code.capabilities is None:
        return  # 旧模式，不创建 user_capabilities

    from app.models.user_capability import UserCapability

    now_utc = datetime.now(UTC)
    for cap_config in invite_code.capabilities:
        cap_name = cap_config.get("capability")
        cap_months = cap_config.get("months", 1)
        cap_watchlist_limit = cap_config.get("watchlist_limit")
        cap_expires_at = now_utc + timedelta(days=30 * cap_months)

        # 查询是否已有该 capability
        existing = await db.execute(
            select(UserCapability).where(
                UserCapability.user_id == user_id,
                UserCapability.capability == cap_name,
            )
        )
        existing_row = existing.scalar_one_or_none()
        if existing_row:
            # 已有：取较晚的 expires_at（不降权），更新 watchlist_limit
            existing_row.expires_at = max(existing_row.expires_at, cap_expires_at)
            if cap_watchlist_limit is not None:
                existing_row.watchlist_limit = cap_watchlist_limit
        else:
            # 新建
            new_cap = UserCapability(
                user_id=user_id,
                capability=cap_name,
                watchlist_limit=cap_watchlist_limit,
                granted_at=now_utc,
                expires_at=cap_expires_at,
                source="invite_code",
                granted_by=None,
            )
            db.add(new_cap)
    await db.flush()


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

    # 5. 创建订阅记录（按 grant_months × 30 天计算到期日，写入套餐快照到 entitlement_snapshot）
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

    # [Phase 5B-2 PRD60 PA-20] 从邀请码创建 user_capabilities（新模式）
    await _grant_capabilities_from_invite(db, user.id, invite)

    await db.flush()

    return user, subscription


async def renew_with_invite_code(
    db: AsyncSession,
    user_id: uuid.UUID,
    raw_invite_code: str,
) -> tuple[Subscription, datetime | None, datetime]:
    """邀请码续期 - 同时更新套餐（plan_code/entitlement_snapshot）和按 30 天周期顺延到期日。

    业务规则：
    - 未到期续期：从当前到期日顺延 grant_months × 30 天
    - 已到期续期：从兑换当天计算 grant_months × 30 天
    - 无 subscription 用户：视为首次开通，从当天计算到期日并新建 subscription
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
        ValueError: 邀请码无效/已使用/已作废，或用户不存在
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

    # 2. 查找用户订阅记录；无记录时本次邀请码兑换视为首次开通（兼容 no-subscription member 续期）
    subscription_stmt = select(Subscription).where(Subscription.user_id == user_id)
    subscription_result = await db.execute(subscription_stmt)
    subscription = subscription_result.scalar_one_or_none()
    is_new_subscription = subscription is None

    # 3. 计算新的到期时间（按 grant_months × 30 天，兼容旧 grant_days）
    # old_expires_at 归一化为时区感知，确保与 new_expires_at（基于 now=UTC）一致，
    # 避免 API 响应中 old/new 一个 naive 一个 aware 导致前端解析失败
    now = datetime.now(UTC)
    if is_new_subscription:
        old_expires_at = None
        new_expires_at = _compute_expires_at(now, invite)
    else:
        assert subscription is not None  # 由 is_new_subscription 保证
        old_expires_at = _ensure_aware(subscription.expires_at)
        if old_expires_at > now:
            # 未到期：从当前到期日顺延
            new_expires_at = _compute_expires_at(old_expires_at, invite)
        else:
            # 已到期：从兑换当天重新计算
            new_expires_at = _compute_expires_at(now, invite)

    # 4. 更新或新建订阅记录（同时更新套餐与到期日 + 刷新 entitlement_snapshot）
    new_plan_code = invite.plan_code or DEFAULT_PLAN_CODE
    # [PlanService] - 描述: 从 plans 表查询套餐构造 entitlement_snapshot 快照
    plan = await get_plan_async(db, new_plan_code)
    entitlement_snapshot = _build_entitlement_snapshot(plan)
    if is_new_subscription:
        subscription = Subscription(
            user_id=user_id,
            plan_code=new_plan_code,
            status="active",
            starts_at=now,
            expires_at=new_expires_at,
            entitlement_snapshot=entitlement_snapshot,
            source="invite",
            created_by=None,
            updated_at=now,
        )
        db.add(subscription)
    else:
        assert subscription is not None  # 由 is_new_subscription 保证
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

    # [Phase 5B-2 PRD60 PA-20] 从邀请码创建/更新 user_capabilities（新模式）
    await _grant_capabilities_from_invite(db, user_id, invite)

    await db.flush()

    assert subscription is not None  # 新建或续期分支均保证 subscription 非空
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
    - 到期日按 grant_months × 30 天计算

    Args:
        db: 异步数据库会话
        user_id: 被授权用户 ID
        plan_code: 套餐代码
        grant_months: 授予 30 天周期数
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
    """管理员为用户续期订阅（按 30 天周期顺延或从当前时间重新计算）。

    业务规则：
    - 未到期：从当前 expires_at 顺延 grant_months × 30 天
    - 已到期：从当前时间重新计算 grant_months × 30 天
    - 管理员（admin 角色）不续期

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        grant_months: 续期 30 天周期数
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
        grant_months: 授予/续期 30 天周期数
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


# ===== [Gate2 PRD60 PA-20] Capability 管理（管理员直接授予/撤销/修改）=====


async def get_user_capabilities(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> dict[str, dict[str, Any]]:
    """查询用户 capabilities（per-capability 独立 expires_at/watchlist_limit）。

    返回结构与 AccessContext.capabilities 对齐：
        {"self_selection": {"active": bool, "expires_at": datetime|None, "watchlist_limit": int|None}, ...}

    无 user_capabilities 行时返回空 dict（由调用方 fallback 到 plan_code 推断）。
    """
    from app.models.user_capability import UserCapability

    stmt = select(UserCapability).where(UserCapability.user_id == user_id)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    now_utc = datetime.now(UTC)
    capabilities: dict[str, dict[str, Any]] = {}
    for row in rows:
        cap_active = row.expires_at > now_utc if row.expires_at else False
        capabilities[row.capability] = {
            "active": cap_active,
            "expires_at": row.expires_at,
            "watchlist_limit": row.watchlist_limit,
        }
    return capabilities


async def grant_capability_to_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    capability: str,
    months: int,
    watchlist_limit: int | None,
    granted_by: uuid.UUID,
) -> dict[str, Any]:
    """管理员直接授予/修改用户 capability（PRD60 PA-20）。

    行为：
    - 已有该 capability：取较晚的 expires_at（不降权），如提供 watchlist_limit 则更新
    - 无该 capability：新建行，source='admin_grant'，granted_by=管理员 ID
    - expires_at 按 30 天周期计算（PA-03，months × 30 天），从 now 起

    Args:
        db: 异步数据库会话
        user_id: 目标用户 ID
        capability: 权限类型 self_selection/market_data/research_replay
        months: 30 天周期有效期（1-36，1 = 30 天）
        watchlist_limit: 自选数量上限（仅 self_selection 必填）
        granted_by: 管理员 user_id

    Returns:
        更新后的 capability 状态 dict（与 AccessContext.capabilities[cap] 对齐）

    Raises:
        ValueError: capability 非法或 self_selection 未提供 watchlist_limit
    """
    from app.models.user_capability import ALL_CAPABILITIES, UserCapability

    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"无效 capability: {capability}，允许: {ALL_CAPABILITIES}")
    if capability == "self_selection" and watchlist_limit is None:
        raise ValueError("self_selection capability 必须指定 watchlist_limit（PA-02）")
    if capability != "self_selection" and watchlist_limit is not None:
        raise ValueError(f"{capability} 不支持 watchlist_limit（仅 self_selection）")
    if months < 1 or months > 36:
        raise ValueError(f"months 必须在 1-36 之间，当前: {months}")

    now_utc = datetime.now(UTC)
    new_expires_at = now_utc + timedelta(days=30 * months)

    # 查询是否已有该 capability
    existing = await db.execute(
        select(UserCapability).where(
            UserCapability.user_id == user_id,
            UserCapability.capability == capability,
        )
    )
    existing_row = existing.scalar_one_or_none()

    if existing_row:
        # 已有：取较晚的 expires_at（不降权），更新 watchlist_limit
        existing_row.expires_at = max(existing_row.expires_at, new_expires_at)
        if watchlist_limit is not None:
            existing_row.watchlist_limit = watchlist_limit
        existing_row.granted_by = granted_by
        existing_row.source = "admin_grant"
        await db.flush()
        row = existing_row
    else:
        # 新建
        new_cap = UserCapability(
            user_id=user_id,
            capability=capability,
            watchlist_limit=watchlist_limit,
            granted_at=now_utc,
            expires_at=new_expires_at,
            source="admin_grant",
            granted_by=granted_by,
        )
        db.add(new_cap)
        await db.flush()
        row = new_cap

    cap_active = row.expires_at > now_utc if row.expires_at else False
    return {
        "active": cap_active,
        "expires_at": row.expires_at,
        "watchlist_limit": row.watchlist_limit,
    }


async def revoke_capability_from_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    capability: str,
) -> bool:
    """管理员撤销用户 capability（PRD60 PA-20）。

    行为：硬删除 user_capabilities 行（撤销即失去该 capability）。
    旧 plan_code fallback 仍可能为用户提供该 capability（兼容期），
    若需完全禁止，管理员应同时调整 subscription 状态。

    Args:
        db: 异步数据库会话
        user_id: 目标用户 ID
        capability: 权限类型 self_selection/market_data/research_replay

    Returns:
        True 如果删除了行；False 如果原本就没有该 capability 行

    Raises:
        ValueError: capability 非法
    """
    from app.models.user_capability import ALL_CAPABILITIES, UserCapability

    if capability not in ALL_CAPABILITIES:
        raise ValueError(f"无效 capability: {capability}，允许: {ALL_CAPABILITIES}")

    existing = await db.execute(
        select(UserCapability).where(
            UserCapability.user_id == user_id,
            UserCapability.capability == capability,
        )
    )
    existing_row = existing.scalar_one_or_none()
    if existing_row is None:
        return False

    await db.delete(existing_row)
    await db.flush()
    return True


async def list_subscribers_with_capabilities(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """查询订阅账户列表（含 capabilities，PRD60 PA-01）。

    在 list_subscribers 基础上追加 capabilities 字段，避免修改原函数签名。
    """
    members, total = await list_subscribers(db=db, limit=limit, offset=offset)
    for member in members:
        user_id = member["user_id"]
        if isinstance(user_id, str):
            try:
                user_id = uuid.UUID(user_id)
            except ValueError:
                pass
        member["capabilities"] = await get_user_capabilities(db, user_id)
        # [权限模型 V2] 补充统一权限摘要（复用 resolve_effective_access 唯一解析）
        try:
            from app.models.user import User as _User
            from app.services.effective_access_service import (
                _ensure_aware,
                capabilities_to_serializable,
                resolve_effective_access,
            )
            user_obj = await db.get(_User, user_id)
            if user_obj is not None:
                roles = member.get("roles") or []
                user_obj._roles = roles  # type: ignore[attr-defined]
                profile = await resolve_effective_access(db, user_obj)
                active_expiries = [
                    _ensure_aware(cap.expires_at)
                    for cap in profile.capabilities.values()
                    if cap.active and cap.expires_at is not None
                ]
                member["capabilities"] = capabilities_to_serializable(profile.capabilities)
                member["active_capability_keys"] = profile.active_capability_keys
                member["has_any_access"] = profile.has_any_access
                member["default_route"] = profile.default_route
                member["capability_source"] = profile.capability_source
                member["diagnostics"] = profile.diagnostics
                if active_expiries:
                    member["nearest_capability_expires_at"] = min(
                        e for e in active_expiries if e is not None
                    ).isoformat()
                else:
                    member["nearest_capability_expires_at"] = None
                member["legacy_fallback"] = profile.capability_source == "legacy_plan_fallback"
        except Exception as exc:  # noqa: BLE001
            # [权限模型 V2] 权限解析异常不得静默吞掉：记录明确日志并在 member 标记解析失败
            logger.exception(
                "list_subscribers_with_capabilities resolve_effective_access failed user_id=%s: %s",
                user_id,
                exc,
            )
            member["permission_resolution_error"] = str(exc)
            member["diagnostics"] = member.get("diagnostics") or []
            member["diagnostics"].append("permission_resolution_error")
    return members, total


logger = logging.getLogger(__name__)


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
