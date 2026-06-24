"""认证 API 路由 - 登录、注册、续期、token 刷新、当前用户信息、会员状态。

端点：
- POST /auth/login: 用户登录，返回 access_token + refresh_token + 会员状态
- POST /auth/register: 邀请码注册，原子操作创建账户 + 开通 30 天会员
- POST /auth/renew: 邀请码续期，未到期顺延 / 已到期从当天计算
- POST /auth/refresh: 使用 refresh token 刷新
- GET /me: 获取当前用户信息（含角色列表）
- GET /me/membership: 获取当前用户会员状态

设计说明：
- 登录使用 email + password，验证 bcrypt 哈希
- 仅 active 状态用户可登录（disabled/pending 拒绝）
- 会员到期后允许登录，但返回 membership_expired=true，前端跳转续期页
- 注册需邀请码，原子操作：锁定邀请码 → 创建账户 → 开通会员 → 写兑换记录
- 续期需邀请码，未到期顺延 30 天，已到期从当天计算 30 天
- refresh token 类型校验：仅 refresh 类型可刷新
- /me 和 /me/membership 通过 get_current_active_user 注入当前用户
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from jose import JWTError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.deps import _fetch_user_with_roles, get_current_active_user
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    verify_password,
)
from app.db import get_db
from app.models.event_recipient import StrategyEventRecipient
from app.models.strategy_event import StrategyEvent
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.schemas.membership import (
    InviteCodeRenew,
    LoginResponse,
    MembershipResponse,
    RegisterSuccessResponse,
    RenewSuccessResponse,
    UserRegister,
)
from app.schemas.user import (
    TokenResponse,
    UserLogin,
    UserResponse,
)
from app.services.membership_service import (
    _ensure_aware,
    get_membership_status,
    get_renewal_count,
    register_with_invite_code,
    renew_with_invite_code,
)

router = APIRouter(tags=["auth"])
_settings = get_settings()


def _user_to_response(user: User) -> UserResponse:
    """将 User ORM 对象转换为 UserResponse（含角色列表）。

    角色名列表从 _fetch_user_with_roles 挂载的 _roles 属性获取。
    """
    roles = getattr(user, "_roles", []) or []
    return UserResponse(
        id=user.id,
        email=user.email,
        status=user.status,
        timezone=user.timezone,
        roles=list(roles),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.post("/auth/login", response_model=LoginResponse)
async def login(
    payload: UserLogin,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    """用户登录 - 验证邮箱密码，返回 access + refresh token + 会员状态。

    流程：
    1. 按 email 查询用户
    2. 验证密码（bcrypt 常量时间比较）
    3. 检查用户状态为 active
    4. 查询会员状态，判断是否到期
    5. 生成 access + refresh token

    会员到期后允许登录，但返回 membership_expired=true，前端跳转续期页。
    管理员停用账户（status=disabled）拒绝登录，与会员到期是两个独立状态。

    Args:
        payload: 登录请求（email + password）
        db: 异步数据库会话

    Returns:
        LoginResponse（access_token + refresh_token + membership_expired）

    Raises:
        HTTPException 401: 邮箱不存在/密码错误/用户状态非 active
    """
    # 按 email 查询用户
    stmt = select(User).where(User.email == payload.email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    # 用户不存在或密码错误统一返回 401（避免泄露用户是否存在）
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )

    # 验证密码
    try:
        password_ok = verify_password(payload.password, user.password_hash)
    except ValueError as e:
        # 哈希格式异常，补上下文后抛 401（不吞没）
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"密码验证失败: {e}",
        ) from e

    if not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )

    # 检查用户状态
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"用户状态非 active（当前: {user.status}），禁止登录",
        )

    # 查询会员状态
    membership = await get_membership_status(db, user.id)
    membership_expired = membership is None or membership.status == "expired"

    # 生成 token
    user_id_str = str(user.id)
    access_token = create_access_token(user_id_str)
    refresh_token = create_refresh_token(user_id_str)

    return LoginResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=_settings.jwt_access_ttl_seconds,
        membership_expired=membership_expired,
    )


@router.post("/auth/refresh", response_model=TokenResponse)
async def refresh_token(
    refresh_token: str,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """使用 refresh token 刷新，返回新的 access + refresh token。

    流程：
    1. 解码 refresh token，验证签名与过期
    2. 校验 token 类型为 refresh（access token 不可用于刷新）
    3. 查询用户，检查状态为 active
    4. 生成新的 access + refresh token

    Args:
        refresh_token: refresh token 字符串（query/body 参数）
        db: 异步数据库会话

    Returns:
        TokenResponse（新的 access_token + refresh_token）

    Raises:
        HTTPException 401: token 无效/过期/类型错误/用户不存在或非 active
    """
    # 解码 refresh token
    try:
        token_payload = decode_token(refresh_token)
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"refresh token 无效或过期: {e}",
        ) from e

    # 校验 token 类型
    if token_payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 类型错误，需要 refresh token",
        )

    # 提取 user_id
    sub = token_payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 缺少 sub 声明",
        )

    try:
        user_id = UUID(sub)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"token sub 声明不是有效的 UUID: {e}",
        ) from e

    # 查询用户
    user = await _fetch_user_with_roles(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token 对应的用户不存在",
        )

    # 检查用户状态
    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"用户状态非 active（当前: {user.status}），禁止刷新",
        )

    # 生成新的 token 对
    user_id_str = str(user.id)
    new_access_token = create_access_token(user_id_str)
    new_refresh_token = create_refresh_token(user_id_str)

    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=_settings.jwt_access_ttl_seconds,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(
    current_user: User = Depends(get_current_active_user),
) -> UserResponse:
    """获取当前用户信息（含角色列表）。

    user_id 由 JWT token 上下文注入，不接受客户端传入。
    需要有效的 access token + active 状态用户。

    Args:
        current_user: 当前用户（由 get_current_active_user 注入）

    Returns:
        UserResponse（含 id/email/status/timezone/roles/时间戳）
    """
    return _user_to_response(current_user)


@router.post("/auth/register", response_model=RegisterSuccessResponse)
async def register(
    payload: UserRegister,
    db: AsyncSession = Depends(get_db),
) -> RegisterSuccessResponse:
    """邀请码注册 - 原子操作创建账户 + 开通 30 天会员。

    流程：
    1. 校验邀请码（哈希查找，状态必须为 unused）
    2. 检查邮箱未被注册
    3. 创建用户（status=active）+ 分配 user 角色
    4. 创建会员记录（30 天）
    5. 更新邀请码状态为 used
    6. 写入兑换记录
    7. 生成 access + refresh token

    Args:
        payload: 注册请求（email + password + invite_code）
        db: 异步数据库会话

    Returns:
        RegisterSuccessResponse（token + 会员开始/到期时间）

    Raises:
        HTTPException 400: 邀请码无效/已使用/已作废，或邮箱已注册
    """
    try:
        user, membership = await register_with_invite_code(
            db=db,
            email=payload.email,
            password=payload.password,
            raw_invite_code=payload.invite_code,
            timezone=payload.timezone,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await db.commit()

    # 生成 token
    user_id_str = str(user.id)
    access_token = create_access_token(user_id_str)
    refresh_token = create_refresh_token(user_id_str)

    return RegisterSuccessResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=_settings.jwt_access_ttl_seconds,
        membership_started_at=membership.started_at,
        membership_expires_at=membership.expires_at,
    )


@router.post("/auth/renew", response_model=RenewSuccessResponse)
async def renew(
    payload: InviteCodeRenew,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> RenewSuccessResponse:
    """邀请码续期 - 未到期顺延 30 天 / 已到期从当天计算 30 天。

    需要有效的 access token（登录状态）。
    会员到期后允许登录但只能访问续期相关端点，此端点允许到期用户调用。

    Args:
        payload: 续期请求（invite_code）
        current_user: 当前用户（由 get_current_active_user 注入）
        db: 异步数据库会话

    Returns:
        RenewSuccessResponse（会员状态 + 新到期时间 + 剩余天数）

    Raises:
        HTTPException 400: 邀请码无效/已使用/已作废，或会员记录不存在
    """
    try:
        membership, old_expires_at, new_expires_at = await renew_with_invite_code(
            db=db,
            user_id=current_user.id,
            raw_invite_code=payload.invite_code,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await db.commit()

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    remaining_days = (_ensure_aware(new_expires_at) - now).days

    return RenewSuccessResponse(
        membership_status="active",
        started_at=membership.started_at,
        old_expires_at=old_expires_at,
        new_expires_at=new_expires_at,
        remaining_days=remaining_days,
    )


@router.get("/me/membership", response_model=MembershipResponse)
async def get_my_membership(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> MembershipResponse:
    """获取当前用户会员状态。

    返回会员状态、到期时间、剩余天数、累计续期次数。
    如果用户无会员记录（如管理员），返回 404。

    Args:
        current_user: 当前用户（由 get_current_active_user 注入）
        db: 异步数据库会话

    Returns:
        MembershipResponse

    Raises:
        HTTPException 404: 用户无会员记录
    """
    membership = await get_membership_status(db, current_user.id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="用户无会员记录",
        )

    from datetime import UTC, datetime

    now = datetime.now(UTC)
    remaining_days = (_ensure_aware(membership.expires_at) - now).days
    renewal_count = await get_renewal_count(db, current_user.id)

    return MembershipResponse(
        status=membership.status,
        started_at=membership.started_at,
        expires_at=membership.expires_at,
        remaining_days=remaining_days,
        renewal_count=renewal_count,
    )


@router.get("/me/events/summary")
async def get_my_events_summary(
    date_param: date = Query(..., alias="date", description="查询日期 YYYY-MM-DD"),
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """查询当前用户指定日期的策略事件汇总。

    优先通过 strategy_event_recipients 表统计用户作为接收人的事件数；
    若 recipients 表无数据，则回退到统计用户自选股相关的 StrategyEvent。

    Args:
        date_param: 查询日期
        current_user: 当前用户
        db: 异步数据库会话

    Returns:
        汇总信息：date / total_events / instruments_with_events / last_event_at
    """
    day_start = datetime(date_param.year, date_param.month, date_param.day)
    from datetime import timedelta
    day_end = day_start + timedelta(days=1)

    # 优先：通过 strategy_event_recipients 统计
    recipient_count_stmt = (
        select(func.count(StrategyEventRecipient.id))
        .join(StrategyEvent, StrategyEventRecipient.event_id == StrategyEvent.id)
        .where(
            StrategyEventRecipient.user_id == current_user.id,
            StrategyEvent.event_time >= day_start,
            StrategyEvent.event_time < day_end,
        )
    )
    recipient_result = await db.execute(recipient_count_stmt)
    total_events = recipient_result.scalar() or 0

    if total_events > 0:
        # 有 recipients 数据：统计涉及股票数和最后事件时间
        instruments_stmt = (
            select(func.count(func.distinct(StrategyEvent.instrument_id)))
            .join(StrategyEventRecipient, StrategyEventRecipient.event_id == StrategyEvent.id)
            .where(
                StrategyEventRecipient.user_id == current_user.id,
                StrategyEvent.event_time >= day_start,
                StrategyEvent.event_time < day_end,
            )
        )
        inst_result = await db.execute(instruments_stmt)
        instruments_with_events = inst_result.scalar() or 0

        last_event_stmt = (
            select(func.max(StrategyEvent.event_time))
            .join(StrategyEventRecipient, StrategyEventRecipient.event_id == StrategyEvent.id)
            .where(
                StrategyEventRecipient.user_id == current_user.id,
                StrategyEvent.event_time >= day_start,
                StrategyEvent.event_time < day_end,
            )
        )
        last_result = await db.execute(last_event_stmt)
        last_event_at = last_result.scalar()
    else:
        # 回退：统计用户自选股相关的 StrategyEvent
        watchlist_stmt = (
            select(UserWatchlistItem.instrument_id)
            .where(
                UserWatchlistItem.user_id == current_user.id,
                UserWatchlistItem.active.is_(True),
            )
        )
        wl_result = await db.execute(watchlist_stmt)
        instrument_ids = [row[0] for row in wl_result.all()]

        if instrument_ids:
            event_count_stmt = (
                select(func.count(StrategyEvent.id))
                .where(
                    StrategyEvent.instrument_id.in_(instrument_ids),
                    StrategyEvent.event_time >= day_start,
                    StrategyEvent.event_time < day_end,
                )
            )
            count_result = await db.execute(event_count_stmt)
            total_events = count_result.scalar() or 0

            inst_count_stmt = (
                select(func.count(func.distinct(StrategyEvent.instrument_id)))
                .where(
                    StrategyEvent.instrument_id.in_(instrument_ids),
                    StrategyEvent.event_time >= day_start,
                    StrategyEvent.event_time < day_end,
                )
            )
            inst_result2 = await db.execute(inst_count_stmt)
            instruments_with_events = inst_result2.scalar() or 0

            last_event_stmt2 = (
                select(func.max(StrategyEvent.event_time))
                .where(
                    StrategyEvent.instrument_id.in_(instrument_ids),
                    StrategyEvent.event_time >= day_start,
                    StrategyEvent.event_time < day_end,
                )
            )
            last_result2 = await db.execute(last_event_stmt2)
            last_event_at = last_result2.scalar()
        else:
            instruments_with_events = 0
            last_event_at = None

    return {
        "date": date_param.isoformat(),
        "total_events": total_events,
        "instruments_with_events": instruments_with_events,
        "last_event_at": last_event_at.isoformat() if last_event_at else None,
    }


if __name__ == "__main__":
    # 自测入口：验证路由注册
    paths = [r.path for r in router.routes]
    print(f"router.routes={paths}")
    assert "/auth/login" in paths
    assert "/auth/register" in paths
    assert "/auth/renew" in paths
    assert "/auth/refresh" in paths
    assert "/me" in paths
    assert "/me/membership" in paths
    assert "/me/events/summary" in paths
    print("OK")
