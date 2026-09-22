"""统一权限解析服务 - resolve_effective_access。

功能权限唯一真源：``user_capabilities``。

设计原则（权限模型 V2 统一重构）：
- 功能访问判权唯一依据 ``user_capabilities``，不再由 plan_code / Subscription 决定；
- Subscription 仅记录商业周期（注册/续期），不参与功能判权；
- Plan 仅是展示/销售模板，不作为运行时权限真源；
- 运行时判权不得根据 plan_code 决定功能权限；
- 兼容期允许 legacy plan fallback，但必须显式标记 ``source=legacy_plan_fallback``；
- 所有新注册/新续期必须生成显式 user_capabilities。

统一供以下链路使用：login / register / refresh / /me/access / API route guards /
前端 AuthStore / 管理员用户列表 / 管理员用户详情 / 默认路由计算。

禁止这些模块各自重新推导权限（get_access_context 作为兼容包装，内部必须调用
本服务，不得再次查询和推导）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import _get_user_roles
from app.models.user_capability import (
    ALL_CAPABILITIES,
    CAPABILITY_MARKET_DATA,
    CAPABILITY_MARKET_REVIEW,
    CAPABILITY_RESEARCH_REPLAY,
    CAPABILITY_SELF_SELECTION,
    UserCapability,
)
from app.services.subscription_summary_service import (
    SubscriptionSummary,
    resolve_subscription_summary,
)

# 中文展示名（后台权限概览用）
# [PANJI-REVIEW-CAPABILITY-SPLIT] market_review 已从 market_data 拆分为独立 capability（复盘）
CAPABILITY_LABELS: dict[str, str] = {
    CAPABILITY_SELF_SELECTION: "自选管理",
    CAPABILITY_MARKET_DATA: "行情数据",
    CAPABILITY_MARKET_REVIEW: "复盘分析",
    CAPABILITY_RESEARCH_REPLAY: "竞价分析",
}

# 默认路由矩阵
DEFAULT_ROUTE_ADMIN = "/admin/overview"
DEFAULT_ROUTE_MARKET = "/market"
DEFAULT_ROUTE_MARKET_WATCHLIST = "/market?scope=watchlist"
DEFAULT_ROUTE_REVIEW = "/review"
DEFAULT_ROUTE_AUCTION = "/auction"
DEFAULT_ROUTE_FORBIDDEN = "/forbidden"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _ensure_aware(value: datetime | None) -> datetime | None:
    """统一 naive/aware 时间为 UTC-aware（naive 视为 UTC）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass
class CapabilityState:
    """单条 capability 的规范化状态。"""

    key: str
    active: bool
    granted_at: datetime | None = None
    expires_at: datetime | None = None
    watchlist_limit: int | None = None
    source: str = "none"
    reason: str | None = None


@dataclass
class EffectiveAccessProfile:
    """统一权限画像 - 供登录/路由/后台/前端共用。

    字段：
    - user_id: 用户 ID（字符串化）
    - account_status: user.status（是否允许登录）
    - roles: 角色名列表
    - is_admin: 是否 admin
    - capabilities: {key: CapabilityState}
    - active_capability_keys: active 的 capability key 列表
    - has_any_access: 是否有任一 active capability（admin 恒为 True）
    - default_route: 依据 capabilities 计算的默认入口
    - subscription_summary: 商业记录摘要（只读展示，不参与判权）
    - diagnostics: 诊断信息（source 标记、legacy fallback 警告等）
    """

    user_id: str
    account_status: str
    roles: list[str]
    is_admin: bool = False
    capabilities: dict[str, CapabilityState] = field(default_factory=dict)
    default_route: str = DEFAULT_ROUTE_FORBIDDEN
    subscription_summary: SubscriptionSummary | None = None
    diagnostics: list[str] = field(default_factory=list)

    @property
    def active_capability_keys(self) -> list[str]:
        return [k for k, v in self.capabilities.items() if v.active]

    @property
    def has_any_access(self) -> bool:
        return self.is_admin or bool(self.active_capability_keys)

    @property
    def capability_source(self) -> str:
        """来源：admin / user_capabilities / legacy_plan_fallback / none。"""
        if self.is_admin:
            return "admin"
        sources = {v.source for v in self.capabilities.values() if v.source}
        if not sources:
            return "none"
        if sources == {"legacy_plan_fallback"}:
            return "legacy_plan_fallback"
        return "user_capabilities"


def compute_default_route(
    is_admin: bool,
    capabilities: dict[str, CapabilityState],
) -> str:
    """依据 capabilities 计算默认入口（公开函数，供登录/路由/后台共用）。

    规则（权限模型 V2；[PANJI-REVIEW-CAPABILITY-SPLIT] market_review 为独立 capability）：
    - admin → /admin/overview
    - 无 active capability → /forbidden
    - 显式优先级（首个命中即返回）：market_data > self_selection > market_review > research_replay
        - market_data → /market（行情）
        - self_selection → /market?scope=watchlist（自选）
        - market_review → /review（复盘）
        - research_replay → /auction（竞价）
    四个 capability 之间无任何隐式授权：某一 capability 存在不会自动授予其他 capability 的默认入口。
    """
    if is_admin:
        return DEFAULT_ROUTE_ADMIN
    active = {k for k, v in capabilities.items() if v.active}
    if not active:
        return DEFAULT_ROUTE_FORBIDDEN
    if CAPABILITY_MARKET_DATA in active:
        return DEFAULT_ROUTE_MARKET
    if CAPABILITY_SELF_SELECTION in active:
        return DEFAULT_ROUTE_MARKET_WATCHLIST
    if CAPABILITY_MARKET_REVIEW in active:
        return DEFAULT_ROUTE_REVIEW
    if CAPABILITY_RESEARCH_REPLAY in active:
        return DEFAULT_ROUTE_AUCTION
    return DEFAULT_ROUTE_FORBIDDEN


def infer_capabilities_from_plan(
    plan_code: str | None,
    plan_monitor_limit: int | None,
    expires_at: datetime | None,
    subscription_active: bool,
) -> dict[str, dict[str, Any]]:
    """Legacy plan fallback 适配器（公开函数）。

    仅用于无显式 user_capabilities 的旧用户兼容期。调用方必须显式标记
    ``source=legacy_plan_fallback``，不得静默混入正常用户。
    """
    expires_at = _ensure_aware(expires_at)
    if plan_code == "observe_20":
        return {
            CAPABILITY_SELF_SELECTION: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": plan_monitor_limit},
            CAPABILITY_MARKET_DATA: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": None},
            CAPABILITY_MARKET_REVIEW: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": None},
        }
    if plan_code == "research_50":
        return {
            CAPABILITY_SELF_SELECTION: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": plan_monitor_limit},
            CAPABILITY_MARKET_DATA: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": None},
            CAPABILITY_MARKET_REVIEW: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": None},
            CAPABILITY_RESEARCH_REPLAY: {"active": subscription_active, "expires_at": expires_at, "watchlist_limit": None},
        }
    return {}


async def resolve_effective_access(
    db: AsyncSession,
    user: Any,
    *,
    subscription_summary: SubscriptionSummary | None = None,
) -> EffectiveAccessProfile:
    """唯一权限解析入口（权限画像唯一 owner）。

    capability 只由 explicit ``user_capabilities`` 或 ``legacy_plan_fallback`` 决定；
    商业展示摘要 ``subscription_summary`` 是附加事实，不是 authorization 前置条件：

    - admin：立即返回，零 DB I/O（不查询 Subscription / Plan / UserCapability）；
    - non-admin 先查 UserCapability（唯一真源）：
        - 有 explicit 行：直接解析权限，**不查询 Subscription / Plan**
          （除非调用方已传入 subscription_summary 供展示复用）；
        - 无 explicit 行（legacy）：仅此时按需查询 commercial facts
          （复用传入 summary，或调用 resolve_subscription_summary）。

    legacy fallback 显式标记 ``source=legacy_plan_fallback``，行为不变。
    商业状态语义唯一 owner：subscription_summary_service.resolve_commercial_status，
    禁止在此复制 ``status=='active' and starts_at<=now and expires_at>now`` 判断。

    ``subscription_summary`` 是调用方已拥有的事实依赖（typed object），
    不是控制行为的 boolean flag；capability-only caller 可不传入而独立成功。
    """
    user_id = str(user.id)
    roles = list(_get_user_roles(user))
    is_admin = "admin" in roles
    now = _utcnow()

    capabilities: dict[str, CapabilityState] = {}
    diagnostics: list[str] = []

    # admin fast path：零 DB I/O，绝不查询 Subscription / Plan / UserCapability
    if is_admin:
        for key in ALL_CAPABILITIES:
            capabilities[key] = CapabilityState(
                key=key, active=True, granted_at=None, expires_at=None,
                watchlist_limit=None, source="admin", reason="admin",
            )
        return EffectiveAccessProfile(
            user_id=user_id, account_status=user.status, roles=roles, is_admin=True,
            capabilities=capabilities, default_route=compute_default_route(True, capabilities),
            subscription_summary=subscription_summary,
        )

    # non-admin：先查 UserCapability（唯一真源，ORM select，禁止原始 SQL 字符串）
    stmt = select(UserCapability).where(UserCapability.user_id == user.id)
    cap_rows = (await db.execute(stmt)).scalars().all()

    if cap_rows:
        # 显式 capability 用户：直接解析，不查询 Subscription / Plan
        for row in cap_rows:
            cap_expires_at = _ensure_aware(row.expires_at)
            granted_at = _ensure_aware(row.granted_at)
            source = row.source or "user_capabilities"
            # [权限模型 V2 PV2-B04] admin_revoke 记录无论 expires_at 是否在未来，
            # 一律解析为 active=False（撤销 tombstone 优先级最高）
            if source == "admin_revoke":
                active = False
                reason = "explicitly_revoked"
            else:
                active = bool(cap_expires_at and cap_expires_at > now)
                reason = "expired" if (cap_expires_at and not active) else ("active" if active else "no_expiry")
            capabilities[row.capability] = CapabilityState(
                key=row.capability,
                active=active,
                granted_at=granted_at,
                expires_at=cap_expires_at,
                watchlist_limit=row.watchlist_limit,
                source=source,
                reason=reason,
            )
        # subscription_summary 保持调用方传入值（None 即不展示商业摘要，零商业 I/O）
    else:
        # legacy plan fallback：仅在无 explicit 行时才需要 commercial facts
        if subscription_summary is None:
            subscription_summary = await resolve_subscription_summary(db, user.id)
        plan_code = subscription_summary.plan_code
        plan_monitor_limit = (
            subscription_summary.limits.get("monitor_limit") if subscription_summary.limits else None
        )
        sub_active = subscription_summary.active
        expires_at = subscription_summary.expires_at
        inferred = infer_capabilities_from_plan(
            plan_code, plan_monitor_limit, expires_at, sub_active
        )
        for key, info in inferred.items():
            capabilities[key] = CapabilityState(
                key=key,
                active=bool(info.get("active")),
                expires_at=_ensure_aware(info.get("expires_at")),
                watchlist_limit=info.get("watchlist_limit"),
                source="legacy_plan_fallback",
                reason="legacy_plan_fallback",
            )
        diagnostics.append("legacy_plan_fallback")

    return EffectiveAccessProfile(
        user_id=user_id,
        account_status=user.status,
        roles=roles,
        is_admin=is_admin,
        capabilities=capabilities,
        default_route=compute_default_route(is_admin, capabilities),
        subscription_summary=subscription_summary,
        diagnostics=diagnostics,
    )


def capabilities_to_serializable(
    capabilities: dict[str, CapabilityState],
) -> dict[str, dict[str, Any]]:
    """把 CapabilityState 转成可序列化 dict（含 source/reason）。"""
    return {
        key: {
            "active": state.active,
            "granted_at": state.granted_at.isoformat() if state.granted_at else None,
            "expires_at": state.expires_at.isoformat() if state.expires_at else None,
            "watchlist_limit": state.watchlist_limit,
            "source": state.source,
            "reason": state.reason,
        }
        for key, state in capabilities.items()
    }


async def ensure_explicit_capability_mode(
    db: AsyncSession,
    user_id: Any,
    actor_user_id: Any | None = None,
) -> list[dict]:
    """[权限模型 V2] 确保用户进入显式 capability 模式（legacy → explicit 安全转换）。

    合同：
    1. 查询用户全部显式 UserCapability 记录，已存在任意显式记录 → 不重复物化，直接返回。
    2. 不存在显式记录：查询当前 Subscription 与 Plan，按 legacy 规则计算全部推导 Capability，
       一次性写为显式记录（source=legacy_materialized），保留每项当前 expires_at，
       self_selection 保留 watchlist_limit，granted_by=当前管理员，granted_at=当前时间。
    3. 当前 legacy 权限为空：不创建无意义 active 记录，返回空。

    任何管理员首次 grant/extend/quota change/revoke 前必须先调用本服务。
    这样避免：旧 research_50 用户只修改 self_selection → market_data/research_replay 消失。
    """
    from app.models.subscription import Subscription
    from app.models.user_capability import UserCapability
    from app.services.plan_service import get_plan

    now = _utcnow()

    # 1. 已存在任意显式记录 → 不重复物化，返回空（本次未发生物化）
    stmt = select(UserCapability).where(UserCapability.user_id == user_id)
    existing = (await db.execute(stmt)).scalars().all()
    if existing:
        return []

    # 2. 无显式记录：按 legacy 规则计算推导 capability
    sub_stmt = select(Subscription).where(Subscription.user_id == user_id)
    sub_row = (await db.execute(sub_stmt)).scalars().first()
    if sub_row is None:
        return []
    plan_code = sub_row.plan_code
    plan_monitor_limit = None
    expires_at = _ensure_aware(sub_row.expires_at)
    sub_active = bool(
        sub_row.status == "active"
        and _ensure_aware(sub_row.starts_at) is not None
        and (_ensure_aware(sub_row.starts_at) or now) <= now
        and expires_at is not None
        and expires_at > now
    )
    if plan_code:
        plan = await get_plan(db, plan_code)
        plan_monitor_limit = plan.monitor_limit if plan else None
    inferred = infer_capabilities_from_plan(plan_code, plan_monitor_limit, expires_at, sub_active)

    # 3. 一次性写为显式记录（source=legacy_materialized）
    materialized: list[dict] = []
    for key, info in inferred.items():
        row = UserCapability(
            user_id=user_id,
            capability=key,
            watchlist_limit=info.get("watchlist_limit"),
            granted_at=now,
            expires_at=_ensure_aware(info.get("expires_at")),
            source="legacy_materialized",
            granted_by=actor_user_id,
        )
        db.add(row)
        materialized.append(
            {
                "capability": key,
                "expires_at": _ensure_aware(info.get("expires_at")),
                "source": "legacy_materialized",
            }
        )
    await db.flush()
    return materialized
