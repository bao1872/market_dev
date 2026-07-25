"""权限能力聚合服务 - V2.1 邀请码模块化授权单一真源。

PRD §9 AccessContext 契约 + §10 后端授权边界 + §7 多次兑换与续期规则。

提供：
- get_capability_access_context: 获取 V2.1 capability 权限上下文（PRD §9）
- has_capability: 检查用户是否拥有指定能力（PRD §10.1）
- get_effective_watchlist_limit: 获取有效自选额度（PRD §7.1 max 规则）
- require_capability: FastAPI 依赖，要求指定能力（PRD §10.1）
- require_any_capability: FastAPI 依赖，要求任一能力（PRD §10.1）
- invalidate_access_context_cache: 精确失效用户缓存（PRD §11 步骤 8）

设计原则（PRD §2.3 后端是唯一授权真源）：
- 禁止各 API 自行查询 grant 表，统一从本服务读取
- 禁止 Worker 复制权限规则，统一调用本服务
- 每项能力独立计算 active/expires_at（PRD §7 多次兑换规则）
- 自选额度取所有有效 grant 的 max（PRD §7.1）
- 到期实时计算，不依赖 cron 更新状态（PRD §8.3）

缓存策略：
- per-user 进程内缓存（TTL 默认 60s），避免高频 API 反复查库
- TTL 上限 60s，过期重新查询；不允许过期权限继续有效
- 创建 grant / 兑换 / 撤销 / 自选变动后必须调用 invalidate_access_context_cache
- 缓存键包含 user_id；不缓存管理员路径（管理员权限不依赖 grant）
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.capability_keys import (
    ALL_CAPABILITY_KEYS,
    WATCHLIST_MANAGEMENT,
)
from app.core.deps import _get_user_roles, get_current_active_user
from app.db import get_db
from app.models.capability_grant import UserCapabilityGrant
from app.models.user import User
from app.models.watchlist import UserWatchlistItem

__all__ = [
    "CapabilityAccessContext",
    "CapabilityStatus",
    "WatchlistLimitInfo",
    "get_capability_access_context",
    "has_capability",
    "get_effective_watchlist_limit",
    "require_capability",
    "require_any_capability",
    "invalidate_access_context_cache",
    "REASON_CAPABILITY_REQUIRED",
    "REASON_WATCHLIST_LIMIT_REACHED",
    "REASON_INVITE_ALREADY_REDEEMED",
    "REASON_INVITE_REVOKED",
]

# ---------------------------------------------------------------------------
# 错误 reason_code（PRD §10.2）
# ---------------------------------------------------------------------------

REASON_CAPABILITY_REQUIRED = "CAPABILITY_REQUIRED"
REASON_WATCHLIST_LIMIT_REACHED = "WATCHLIST_LIMIT_REACHED"
REASON_INVITE_ALREADY_REDEEMED = "INVITE_CODE_ALREADY_REDEEMED"
REASON_INVITE_REVOKED = "INVITE_CODE_REVOKED"

# ---------------------------------------------------------------------------
# Pydantic 模型（PRD §9 AccessContext 契约）
# ---------------------------------------------------------------------------


class CapabilityStatus(BaseModel):
    """单项能力状态（PRD §9 capabilities[key]）。

    - active: 当前是否有效（revoked_at IS NULL AND starts_at <= now AND expires_at > now）
    - expires_at: 最晚到期时间（active=false 时为 None）
    """

    model_config = ConfigDict(frozen=True)

    active: bool
    expires_at: datetime | None = None


class WatchlistLimitInfo(BaseModel):
    """自选额度信息（PRD §9 limits）。

    - watchlist_stock_limit: 有效额度（PRD §7.1 max 规则）
      - None: 无 watchlist_management 权限
      - int: 当前有效 grant 的 limit_value 最大值
    - watchlist_current_count: 当前 active 自选数量
    - watchlist_over_limit: 当前数量是否超过额度（PRD §7.1）
    - is_admin_unlimited: admin 标识（前端区分"unlimited"显示，不使用魔法大数）
    """

    model_config = ConfigDict(frozen=True)

    watchlist_stock_limit: int | None
    watchlist_current_count: int
    watchlist_over_limit: bool
    is_admin_unlimited: bool = False


class CapabilityAccessContext(BaseModel):
    """V2.1 capability 权限上下文（PRD §9）。

    字段语义：
    - user_id: 用户 ID（字符串化 UUID，与 JWT sub 一致）
    - is_admin: 是否为管理员（三能力全开 + watchlist unlimited）
    - capabilities: 三能力状态字典（key=capability_key, value=CapabilityStatus）
    - limits: 自选额度信息

    设计要点：
    - frozen=True 不可变
    - 不返回旧 Plan/Subscription 字段（V2.1 grant 为唯一真源）
    - admin 路径：三能力 active=True，watchlist_stock_limit=None + is_admin_unlimited=True
    - 普通用户路径：从 user_capability_grants 表实时聚合
    """

    model_config = ConfigDict(frozen=True)

    user_id: str
    is_admin: bool
    capabilities: dict[str, CapabilityStatus]
    limits: WatchlistLimitInfo


# ---------------------------------------------------------------------------
# 进程内缓存（per-user, TTL=60s）
# ---------------------------------------------------------------------------

_CACHE_TTL_SECONDS: int = 60
_access_context_cache: dict[str, tuple[float, CapabilityAccessContext]] = {}


def invalidate_access_context_cache(user_id: str | None = None) -> None:
    """精确失效用户 AccessContext 缓存（PRD §11 步骤 8）。

    Args:
        user_id: 指定用户 ID（字符串化 UUID）；None 清空全部缓存
    """
    if user_id is None:
        _access_context_cache.clear()
        return
    _access_context_cache.pop(user_id, None)


# ---------------------------------------------------------------------------
# 核心聚合方法
# ---------------------------------------------------------------------------


async def get_capability_access_context(
    db: AsyncSession,
    user: User,
    now: datetime | None = None,
) -> CapabilityAccessContext:
    """获取 V2.1 capability 权限上下文（PRD §9）。

    流程：
    1. 检查缓存（TTL=60s）；命中且未过期直接返回
    2. 从 user._roles 读取角色名列表，计算 is_admin
    3. admin 路径：三能力 active=True，watchlist unlimited
    4. 普通用户路径：
       a. 查询 user_capability_grants 表（revoked_at IS NULL AND starts_at <= now AND expires_at > now）
       b. 每项能力取最晚 expires_at 作为 active=True 的状态
       c. watchlist_management 的 limit_value 取所有有效 grant 的 max
       d. 统计 user_watchlist_items active=true 数量
    5. 缓存结果（TTL=60s）并返回

    Args:
        db: 异步数据库会话
        user: 当前用户对象（需由 deps.get_current_user 注入 _roles 属性）
        now: 当前时间（None=datetime.now(timezone.utc)；用于测试注入）

    Returns:
        CapabilityAccessContext 权限上下文
    """
    now = now if now is not None else datetime.now(UTC)
    user_id_str = str(user.id)

    # 检查缓存
    cached = _access_context_cache.get(user_id_str)
    if cached is not None:
        expires_at, ctx = cached
        if time.time() < expires_at:
            return ctx

    roles = _get_user_roles(user)
    is_admin = "admin" in roles

    if is_admin:
        # admin 路径：三能力全开 + watchlist unlimited（PRD §4.4）
        admin_capabilities: dict[str, CapabilityStatus] = {
            key: CapabilityStatus(active=True, expires_at=None)
            for key in ALL_CAPABILITY_KEYS
        }
        # admin 也需要统计当前自选数量（用于 UI 显示），但不限制额度
        current_count = await _count_active_watchlist(db, user.id)
        limits = WatchlistLimitInfo(
            watchlist_stock_limit=None,
            watchlist_current_count=current_count,
            watchlist_over_limit=False,
            is_admin_unlimited=True,
        )
        ctx = CapabilityAccessContext(
            user_id=user_id_str,
            is_admin=True,
            capabilities=admin_capabilities,
            limits=limits,
        )
        _access_context_cache[user_id_str] = (time.time() + _CACHE_TTL_SECONDS, ctx)
        return ctx

    # 普通用户路径：查询有效 grant（PRD §8.3 实时推导）
    grants = await _fetch_effective_grants(db, user.id, now)

    # 按能力键聚合：每项能力取最晚 expires_at（PRD §7 多次兑换规则）
    per_capability: dict[str, list[UserCapabilityGrant]] = {}
    for grant in grants:
        per_capability.setdefault(grant.capability_key, []).append(grant)

    capabilities: dict[str, CapabilityStatus] = {}
    for key in ALL_CAPABILITY_KEYS:
        grant_list = per_capability.get(key, [])
        if not grant_list:
            capabilities[key] = CapabilityStatus(active=False, expires_at=None)
            continue
        # 取最晚 expires_at（active=True 时返回）
        latest_expires = max(g.expires_at for g in grant_list)
        capabilities[key] = CapabilityStatus(active=True, expires_at=latest_expires)

    # 自选额度：PRD §7.1 max 规则
    watchlist_grants = per_capability.get(WATCHLIST_MANAGEMENT, [])
    if watchlist_grants:
        watchlist_limit = max(g.limit_value for g in watchlist_grants if g.limit_value is not None)
    else:
        watchlist_limit = None

    # 统计当前 active 自选数量
    current_count = await _count_active_watchlist(db, user.id)

    # PRD §7.1：超限判断（仅在有限额时判断）
    over_limit = (
        watchlist_limit is not None
        and current_count > watchlist_limit
    )

    limits = WatchlistLimitInfo(
        watchlist_stock_limit=watchlist_limit,
        watchlist_current_count=current_count,
        watchlist_over_limit=over_limit,
        is_admin_unlimited=False,
    )

    ctx = CapabilityAccessContext(
        user_id=user_id_str,
        is_admin=False,
        capabilities=capabilities,
        limits=limits,
    )
    _access_context_cache[user_id_str] = (time.time() + _CACHE_TTL_SECONDS, ctx)
    return ctx


async def has_capability(
    db: AsyncSession,
    user_id: uuid.UUID,
    capability_key: str,
    now: datetime | None = None,
) -> bool:
    """检查用户是否拥有指定能力（PRD §10.1）。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        capability_key: 能力键（必须在 ALL_CAPABILITY_KEYS 中）
        now: 当前时间（None=datetime.now(timezone.utc)）

    Returns:
        True 如果用户拥有该能力（admin 全开；普通用户查询有效 grant）

    Raises:
        ValueError: capability_key 不在 ALL_CAPABILITY_KEYS 中
    """
    if capability_key not in ALL_CAPABILITY_KEYS:
        raise ValueError(
            f"capability_key 必须在 {ALL_CAPABILITY_KEYS} 中，当前={capability_key!r}"
        )
    now = now if now is not None else datetime.now(UTC)

    # 查询用户 + roles
    user_stmt = (
        select(User)
        .where(User.id == user_id)
    )
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if user is None:
        return False

    roles = _get_user_roles(user)
    if "admin" in roles:
        return True  # admin 三能力全开

    # 普通用户：查询有效 grant
    grants = await _fetch_effective_grants(db, user_id, now)
    return any(g.capability_key == capability_key for g in grants)


async def get_effective_watchlist_limit(
    db: AsyncSession,
    user_id: uuid.UUID,
    now: datetime | None = None,
) -> int | None:
    """获取有效自选额度（PRD §7.1 max 规则）。

    Args:
        db: 异步数据库会话
        user_id: 用户 ID
        now: 当前时间（None=datetime.now(timezone.utc)）

    Returns:
        - None: admin（unlimited）或无 watchlist_management 权限
        - int: 当前有效 watchlist_management grant 的 limit_value 最大值
    """
    now = now if now is not None else datetime.now(UTC)

    # admin 路径
    user_stmt = select(User).where(User.id == user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    if user is None:
        return None

    roles = _get_user_roles(user)
    if "admin" in roles:
        return None  # admin = unlimited

    # 普通用户：取所有有效 watchlist_management grant 的 limit_value max
    grants = await _fetch_effective_grants(db, user_id, now)
    watchlist_limits = [
        g.limit_value
        for g in grants
        if g.capability_key == WATCHLIST_MANAGEMENT and g.limit_value is not None
    ]
    if not watchlist_limits:
        return None
    return max(watchlist_limits)


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------


def require_capability(
    capability_key: str,
) -> Callable[..., Coroutine[Any, Any, CapabilityAccessContext]]:
    """FastAPI 依赖工厂：要求指定能力（PRD §10.1）。

    用法：
        @router.post("/watchlist", dependencies=[Depends(require_capability("watchlist_management"))])
        async def add_watchlist(...): ...

    Args:
        capability_key: 能力键（必须在 ALL_CAPABILITY_KEYS 中）

    Returns:
        FastAPI 依赖函数，校验通过返回 CapabilityAccessContext，否则 403

    Raises:
        ValueError: capability_key 不在 ALL_CAPABILITY_KEYS 中
    """
    if capability_key not in ALL_CAPABILITY_KEYS:
        raise ValueError(
            f"capability_key 必须在 {ALL_CAPABILITY_KEYS} 中，当前={capability_key!r}"
        )

    async def _check_capability(
        db: AsyncSession = Depends(get_db),
        user: User = Depends(get_current_active_user),
    ) -> CapabilityAccessContext:
        """检查用户是否具备指定能力（admin 自动豁免）。"""
        ctx = await get_capability_access_context(db, user)
        # admin 全开
        if ctx.is_admin:
            return ctx
        # 普通用户检查 capability
        cap_status = ctx.capabilities.get(capability_key)
        if cap_status is None or not cap_status.active:
            raise HTTPException(
                status_code=403,
                detail={
                    "reason_code": REASON_CAPABILITY_REQUIRED,
                    "message": f"需要能力: {capability_key}",
                    "capability_key": capability_key,
                },
            )
        return ctx

    return _check_capability


def require_any_capability(
    capability_keys: list[str],
) -> Callable[..., Coroutine[Any, Any, CapabilityAccessContext]]:
    """FastAPI 依赖工厂：要求任一能力（PRD §10.1 基础行情列表 watchlist OR market）。

    用法：
        @router.get("/instruments", dependencies=[
            Depends(require_any_capability(["watchlist_management", "market_screening"]))
        ])
        async def list_instruments(...): ...

    Args:
        capability_keys: 能力键列表（至少 2 个，必须在 ALL_CAPABILITY_KEYS 中）

    Returns:
        FastAPI 依赖函数，校验通过返回 CapabilityAccessContext，否则 403

    Raises:
        ValueError: capability_keys 为空或包含非法键
    """
    if not capability_keys:
        raise ValueError("require_any_capability 需要非空 capability_keys")
    for key in capability_keys:
        if key not in ALL_CAPABILITY_KEYS:
            raise ValueError(
                f"capability_key 必须在 {ALL_CAPABILITY_KEYS} 中，当前={key!r}"
            )

    async def _check_any_capability(
        db: AsyncSession = Depends(get_db),
        user: User = Depends(get_current_active_user),
    ) -> CapabilityAccessContext:
        """检查用户是否具备任一指定能力（admin 自动豁免）。"""
        ctx = await get_capability_access_context(db, user)
        if ctx.is_admin:
            return ctx
        for key in capability_keys:
            cap_status = ctx.capabilities.get(key)
            if cap_status is not None and cap_status.active:
                return ctx
        raise HTTPException(
            status_code=403,
            detail={
                "reason_code": REASON_CAPABILITY_REQUIRED,
                "message": f"需要任一能力: {capability_keys}",
                "capability_keys": capability_keys,
            },
        )

    return _check_any_capability


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


async def _fetch_effective_grants(
    db: AsyncSession,
    user_id: uuid.UUID,
    now: datetime,
) -> list[UserCapabilityGrant]:
    """查询用户当前有效 grant（PRD §8.3 实时推导）。

    有效条件：revoked_at IS NULL AND starts_at <= now AND expires_at > now
    """
    stmt = (
        select(UserCapabilityGrant)
        .where(
            UserCapabilityGrant.user_id == user_id,
            UserCapabilityGrant.revoked_at.is_(None),
            UserCapabilityGrant.starts_at <= now,
            UserCapabilityGrant.expires_at > now,
        )
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def _count_active_watchlist(
    db: AsyncSession,
    user_id: uuid.UUID,
) -> int:
    """统计用户当前 active 自选数量。"""
    stmt = (
        select(func.count(UserWatchlistItem.id))
        .where(
            UserWatchlistItem.user_id == user_id,
            UserWatchlistItem.active.is_(True),
        )
    )
    result = await db.execute(stmt)
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# 顶部 import（避免局部 import 在生产路径中重复）
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    # [CapabilityService] - 描述: 自测入口，验证函数签名与字段（不连接数据库）
    assert callable(get_capability_access_context)
    assert callable(has_capability)
    assert callable(get_effective_watchlist_limit)
    assert callable(require_capability)
    assert callable(require_any_capability)
    assert callable(invalidate_access_context_cache)

    # CapabilityAccessContext 字段
    expected_fields = {"user_id", "is_admin", "capabilities", "limits"}
    assert set(CapabilityAccessContext.model_fields.keys()) == expected_fields

    # CapabilityStatus 字段
    assert set(CapabilityStatus.model_fields.keys()) == {"active", "expires_at"}

    # WatchlistLimitInfo 字段
    assert set(WatchlistLimitInfo.model_fields.keys()) == {
        "watchlist_stock_limit",
        "watchlist_current_count",
        "watchlist_over_limit",
        "is_admin_unlimited",
    }

    # 工厂函数返回可调用依赖
    dep = require_capability("watchlist_management")
    assert callable(dep)
    any_dep = require_any_capability(["watchlist_management", "market_screening"])
    assert callable(any_dep)

    # 非法 capability_key 拒绝
    try:
        require_capability("invalid_key")
        raise AssertionError("非法 capability_key 应拒绝")
    except ValueError:
        pass

    # 缓存失效
    invalidate_access_context_cache("dummy-uuid")
    invalidate_access_context_cache(None)

    # 构造 CapabilityAccessContext 验证字段
    ctx = CapabilityAccessContext(
        user_id="test-uuid",
        is_admin=False,
        capabilities={
            "watchlist_management": CapabilityStatus(active=True, expires_at=None),
            "market_screening": CapabilityStatus(active=False, expires_at=None),
            "review_management": CapabilityStatus(active=False, expires_at=None),
        },
        limits=WatchlistLimitInfo(
            watchlist_stock_limit=30,
            watchlist_current_count=12,
            watchlist_over_limit=False,
        ),
    )
    assert ctx.capabilities["watchlist_management"].active is True
    assert ctx.limits.watchlist_stock_limit == 30
    # frozen=True 不可变
    try:
        ctx.is_admin = True  # type: ignore[misc]
        raise AssertionError("CapabilityAccessContext 应为 frozen")
    except Exception:
        pass

    print(f"CapabilityAccessContext fields={sorted(CapabilityAccessContext.model_fields.keys())}")
    print(f"require_capability('watchlist_management') -> {dep}")
    print(f"require_any_capability(['watchlist_management','market_screening']) -> {any_dep}")
    print("OK: capability_service 函数签名与字段验证通过")
