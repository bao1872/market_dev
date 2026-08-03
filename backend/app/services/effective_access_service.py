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

禁止这些模块各自重新推导权限。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

CAP_SELF_SELECTION = "self_selection"
CAP_MARKET_DATA = "market_data"
CAP_RESEARCH_REPLAY = "research_replay"
ALL_CAPABILITIES = (CAP_SELF_SELECTION, CAP_MARKET_DATA, CAP_RESEARCH_REPLAY)

# 中文展示名（后台权限概览用）
CAPABILITY_LABELS: dict[str, str] = {
    CAP_SELF_SELECTION: "自选管理",
    CAP_MARKET_DATA: "行情数据",
    CAP_RESEARCH_REPLAY: "复盘与竞价",
}

# 默认路由矩阵
DEFAULT_ROUTE_ADMIN = "/admin/overview"
DEFAULT_ROUTE_MARKET = "/market"
DEFAULT_ROUTE_MARKET_WATCHLIST = "/market?scope=watchlist"
DEFAULT_ROUTE_REVIEW = "/review"
DEFAULT_ROUTE_FORBIDDEN = "/forbidden"
DEFAULT_ROUTE_EXPIRED = "/subscription-expired"


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
    subscription_summary: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)

    @property
    def active_capability_keys(self) -> list[str]:
        return [k for k, v in self.capabilities.items() if v.active]

    @property
    def has_any_access(self) -> bool:
        return self.is_admin or bool(self.active_capability_keys)


def _compute_default_route(
    is_admin: bool,
    capabilities: dict[str, CapabilityState],
) -> str:
    """依据 capabilities 计算默认入口（不与 Subscription 状态耦合）。

    规则（权限模型 V2）：
    - admin → /admin/overview
    - 无 active capability → /forbidden
    - 仅 research_replay → /review
    - self_selection + market_data（含 research_replay）→ /market
    - 仅 self_selection → /market?scope=watchlist
    - 仅 market_data → /market
    - research_replay 加其他权限 → /market
    """
    if is_admin:
        return DEFAULT_ROUTE_ADMIN
    active = {k for k, v in capabilities.items() if v.active}
    if not active:
        return DEFAULT_ROUTE_FORBIDDEN
    if active == {CAP_RESEARCH_REPLAY}:
        return DEFAULT_ROUTE_REVIEW
    if active == {CAP_SELF_SELECTION}:
        return DEFAULT_ROUTE_MARKET_WATCHLIST
    if active == {CAP_MARKET_DATA}:
        return DEFAULT_ROUTE_MARKET
    # self_selection + market_data（可含 research_replay）→ /market
    if CAP_SELF_SELECTION in active and CAP_MARKET_DATA in active:
        return DEFAULT_ROUTE_MARKET
    # 其他组合：research_replay 加任一 → /market
    if CAP_RESEARCH_REPLAY in active:
        return DEFAULT_ROUTE_MARKET
    return DEFAULT_ROUTE_FORBIDDEN


async def resolve_effective_access(db: Any, user: Any) -> EffectiveAccessProfile:
    """唯一权限解析入口。

    从 user_capabilities 解析 capabilities（唯一真源），旧用户无显式行时
    显式标记 legacy plan fallback。不因 plan_code 决定功能权限。

    Args:
        db: 异步 DB 会话
        user: User 对象（需挂载 _roles）

    Returns:
        EffectiveAccessProfile
    """
    from app.models.user_capability import UserCapability

    user_id = str(user.id)
    roles = list(getattr(user, "_roles", []) or [])
    is_admin = "admin" in roles

    # 查询显式 user_capabilities（唯一真源）
    cap_rows: list[UserCapability] = []
    if not is_admin:
        stmt = (
            "SELECT capability, granted_at, expires_at, watchlist_limit, source "
            "FROM user_capabilities WHERE user_id = :uid"
        )
        result = await db.execute(
            stmt,
            {"uid": user.id},
        )
        cap_rows = result.fetchall() if hasattr(result, "fetchall") else result.scalars().all()

    capabilities: dict[str, CapabilityState] = {}
    diagnostics: list[str] = []
    now_utc = datetime.utcnow()

    if is_admin:
        # admin 全权限豁免
        for key in ALL_CAPABILITIES:
            capabilities[key] = CapabilityState(
                key=key, active=True, granted_at=None, expires_at=None,
                watchlist_limit=None, source="admin",
            )
    elif cap_rows:
        for row in cap_rows:
            cap_key = getattr(row, "capability", None) or "unknown"
            granted_at = getattr(row, "granted_at", None)
            expires_at = getattr(row, "expires_at", None)
            watchlist_limit = getattr(row, "watchlist_limit", None)
            source = getattr(row, "source", None) or "user_capabilities"
            active = bool(expires_at and expires_at > now_utc)
            capabilities[cap_key] = CapabilityState(
                key=cap_key,
                active=active,
                granted_at=granted_at,
                expires_at=expires_at,
                watchlist_limit=watchlist_limit,
                source=source,
                reason="expired" if (expires_at and not active) else ("active" if active else "no_expiry"),
            )
    else:
        # legacy plan fallback（兼容期，显式标记 source，不得静默混入正常用户）
        from app.models.subscription import Subscription
        from app.services.access_control_service import _infer_capabilities_from_plan

        sub_stmt = select(Subscription).where(Subscription.user_id == user.id)
        sub_row = (await db.execute(sub_stmt)).scalars().first()
        plan_code = sub_row.plan_code if sub_row else None
        plan_monitor_limit = None
        expires_at = sub_row.expires_at if sub_row else None
        sub_active = bool(
            sub_row
            and sub_row.status == "active"
            and sub_row.starts_at <= now_utc
            and sub_row.expires_at > now_utc
        )
        if plan_code:
            from app.services.plan_service import get_plan
            plan = await get_plan(db, plan_code)
            plan_monitor_limit = plan.monitor_limit if plan else None
        inferred = _infer_capabilities_from_plan(
            plan_code, plan_monitor_limit, expires_at, sub_active
        )
        for key, info in inferred.items():
            capabilities[key] = CapabilityState(
                key=key,
                active=bool(info.get("active")),
                expires_at=info.get("expires_at"),
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
        default_route=_compute_default_route(is_admin, capabilities),
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
