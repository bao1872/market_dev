"""[ARCH-SIMPLIFY-S1-C1] 隔离性纠正测试（纯单元，PURE_UNIT_TEST=1，不连库）。

验证 S1 收口后的关键隔离合同（独立审计发现的依赖/扇出回退纠正）：

- admin resolve 零 DB I/O（在任何 Subscription/Plan/UserCapability 查询之前返回）；
- explicit capability 用户的 capability 解析不依赖商业 Subscription/Plan 查询成功；
- explicit 用户 capability-only 解析不触发商业 I/O（subscription_summary 为 None）；
- get_access_context（API 需商业字段）对 explicit / legacy 各仅 1 次 Subscription + 1 次 Plan；
- legacy fallback 复用已传入的 summary，不二次查询；
- admin access-profile 商业摘要来自 canonical resolver，不被 admin fast-path 抹除。

通过 mock AsyncSession + SimpleNamespace 驱动服务层，不连接任何数据库。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.plan import Plan
from app.models.subscription import Subscription
from app.models.user_capability import ALL_CAPABILITIES, UserCapability
from app.services.access_control_service import AccessContext, get_access_context
from app.services.effective_access_service import (
    EffectiveAccessProfile,
    resolve_effective_access,
)
from app.services.subscription_service import resolve_subscription_summary


def _now() -> datetime:
    return datetime.now(UTC)


def _user(roles: list[str]) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="active", _roles=list(roles))


def _sub(uid: uuid.UUID, plan_code: str, status: str = "active",
         starts_at: datetime | None = None, expires_at: datetime | None = None,
         source: str = "invite") -> SimpleNamespace:
    now = _now()
    starts_at = starts_at or (now - timedelta(days=1))
    expires_at = expires_at or (now + timedelta(days=10))
    return SimpleNamespace(
        user_id=uid, plan_code=plan_code, status=status,
        starts_at=starts_at, expires_at=expires_at, source=source,
        entitlement_snapshot={},
    )


def _plan(plan_code: str, display_name: str, monitor_limit: int,
          features: list[str] | None = None,
          notification_channel_limit: int = 1, message_retention_days: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        plan_code=plan_code, display_name=display_name, monitor_limit=monitor_limit,
        notification_channel_limit=notification_channel_limit,
        message_retention_days=message_retention_days,
        features=features or [], status="active",
    )


def _cap(uid: uuid.UUID, capability: str, source: str = "invite_code",
         expires_at: datetime | None = None, granted_at: datetime | None = None,
         watchlist_limit: int | None = None) -> SimpleNamespace:
    now = _now()
    return SimpleNamespace(
        user_id=uid, capability=capability, source=source,
        expires_at=expires_at or (now + timedelta(days=30)),
        granted_at=granted_at or now, watchlist_limit=watchlist_limit,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)
        self._value = self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def all(self):
        return self._rows

    def scalars(self):
        rows = self._rows
        value = self._value

        class _Scalars:
            def all(self):
                return rows

            def first(self):
                return value

        return _Scalars()

    def first(self):
        return self._value


def _entity(stmt):
    try:
        return stmt.column_descriptions[0]["entity"]
    except Exception:
        return None


def _count(calls, entity):
    return sum(1 for c in calls if _entity(c) is entity)


class _RaiseSession:
    """mock session：对指定实体查询直接 raise；其余按实体类型返回 fake 结果。"""

    def __init__(self, *, raise_on=None, subs=None, caps=None, plans=None):
        self._raise_on = set(raise_on or set())
        self._subs = list(subs or [])
        self._caps = list(caps or [])
        self._plans = list(plans or [])
        self.calls: list = []
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt, *a, **k):
        self.calls.append(stmt)
        entity = _entity(stmt)
        if entity in self._raise_on:
            raise AssertionError(f"unexpected query on {entity}")
        if entity is Subscription:
            return _FakeResult(self._subs)
        if entity is UserCapability:
            return _FakeResult(self._caps)
        if entity is Plan:
            return _FakeResult(self._plans)
        return _FakeResult([])


# ============================================================
# C1-1 direct admin resolver fast path：零 DB I/O
# ============================================================


@pytest.mark.asyncio
async def test_c1_admin_resolve_zero_db_io():
    user = _user(["admin"])
    # 任何 execute 都直接 raise：admin resolve 必须在查询前返回
    db = _RaiseSession(raise_on={Subscription, Plan, UserCapability})
    profile = await resolve_effective_access(db, user)
    assert profile.is_admin is True
    assert {k: v.active for k, v in profile.capabilities.items()} == dict.fromkeys(ALL_CAPABILITIES, True)
    assert profile.default_route == "/admin/overview"
    assert db.calls == []  # 零 DB I/O


# ============================================================
# C1-2 explicit capability 与商业失败隔离
# ============================================================


@pytest.mark.asyncio
async def test_c1_explicit_isolated_from_commercial_failure():
    uid = uuid.uuid4()
    user = _user(["member"])
    # Subscription / Plan 查询若发生立即 raise；UserCapability 正常返回
    db = _RaiseSession(
        raise_on={Subscription, Plan},
        caps=[_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")],
    )
    profile = await resolve_effective_access(db, user)
    assert set(profile.active_capability_keys) == {"self_selection", "market_data"}
    assert profile.default_route == "/market"
    # 确认未查询 Subscription / Plan
    assert [c for c in db.calls if _entity(c) in (Subscription, Plan)] == []
    assert _count(db.calls, UserCapability) == 1


# ============================================================
# C1-3 full access context explicit user：各 1 次查询
# ============================================================


@pytest.mark.asyncio
async def test_c1_get_access_context_explicit_query_counts():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _RaiseSession(
        subs=[_sub(uid, "research_50")],
        caps=[_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")],
        plans=[_plan("research_50", "研究版", 50, ["trend_selection"])],
    )
    ctx: AccessContext = await get_access_context(db, user)
    assert _count(db.calls, UserCapability) == 1
    assert _count(db.calls, Subscription) == 1
    assert _count(db.calls, Plan) == 1
    # 输出与 BASE 一致
    assert ctx.subscription_active is True
    assert ctx.plan_code == "research_50"
    assert ctx.plan_display_name == "研究版"
    assert set(ctx.active_capability_keys) == {"self_selection", "market_data"}
    assert ctx.default_route == "/market"


# ============================================================
# C1-4 full access context legacy user：summary 一次，legacy 不二次查询
# ============================================================


@pytest.mark.asyncio
async def test_c1_get_access_context_legacy_single_summary():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _RaiseSession(
        subs=[_sub(uid, "observe_20")],
        plans=[_plan("observe_20", "观察版", 20)],
    )
    ctx: AccessContext = await get_access_context(db, user)
    assert _count(db.calls, UserCapability) == 1
    assert _count(db.calls, Subscription) == 1
    assert _count(db.calls, Plan) == 1
    assert set(ctx.active_capability_keys) == {"self_selection", "market_data", "market_review"}
    assert ctx.capability_source == "legacy_plan_fallback"
    assert ctx.subscription_active is True
    assert ctx.plan_code == "observe_20"


# ============================================================
# C1-5 direct explicit profile 不需要 summary
# ============================================================


@pytest.mark.asyncio
async def test_c1_explicit_profile_no_summary_when_not_requested():
    uid = uuid.uuid4()
    user = _user(["member"])
    # 商业查询会 raise：证明 explicit 解析完全不依赖商业 I/O
    db = _RaiseSession(raise_on={Subscription, Plan}, caps=[_cap(uid, "self_selection", watchlist_limit=10)])
    profile: EffectiveAccessProfile = await resolve_effective_access(db, user)
    assert profile.subscription_summary is None
    assert profile.active_capability_keys == ["self_selection"]


# ============================================================
# C1-6 admin access-profile 保持原语义（商业摘要来自 canonical resolver）
# ============================================================


@pytest.mark.asyncio
async def test_c1_admin_access_profile_keeps_commercial_info():
    uid = uuid.uuid4()
    sub = _sub(uid, "research_50")
    plans = [_plan("research_50", "研究版", 50, ["trend_selection"])]

    # access-profile 需要 effective_access + 商业摘要
    admin_user = _user(["admin"])
    db = _RaiseSession(subs=[sub], plans=plans)
    summary = await resolve_subscription_summary(db, uid)
    assert summary.status == "active"
    assert summary.plan_code == "research_50"
    assert summary.active is True

    profile = await resolve_effective_access(db, admin_user, subscription_summary=summary)
    # admin fast-path 不抹除已传入的商业摘要
    assert profile.is_admin is True
    assert profile.subscription_summary is summary
    assert profile.subscription_summary.plan_code == "research_50"

    # member target 同样由 canonical resolver 提供商业摘要
    mdb = _RaiseSession(subs=[sub], plans=[_plan("research_50", "研究版", 50)])
    msummary = await resolve_subscription_summary(mdb, uid)
    assert msummary.status == "active"
    assert msummary.plan_code == "research_50"


if __name__ == "__main__":
    raise SystemExit("run via pytest")
