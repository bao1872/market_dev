"""[ARCH-SIMPLIFY-S1] 权限收口行为测试（纯单元，PURE_UNIT_TEST=1，不连库）。

验证 S1 收口后：
- resolve_effective_access 成为权限画像唯一 owner（capability-only 解析零商业 I/O；
  商业摘要由 subscription_service.resolve_subscription_summary 按需解析并可选择性传入）；
- get_access_context 退化为纯 DTO adapter，不再二次查询 Subscription / Plan；
- plan → capability 推导只存在 effective_access_service.infer_capabilities_from_plan 一个实现；
- subscription_summary 仅商业展示，不参与 capability / default_route 决策（反向因果测试）。

通过 mock AsyncSession + SimpleNamespace 驱动服务层，不连接任何数据库；
不依赖生产源码字符串 grep，全部为行为断言。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.plan import Plan
from app.models.subscription import Subscription
from app.models.user_capability import UserCapability
from app.services.access_control_service import AccessContext, get_access_context
from app.services.effective_access_service import (
    EffectiveAccessProfile,
    resolve_effective_access,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _user(roles: list[str]) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="active", _roles=list(roles))


def _sub(
    uid: uuid.UUID,
    plan_code: str,
    status: str = "active",
    starts_at: datetime | None = None,
    expires_at: datetime | None = None,
    source: str = "invite",
) -> SimpleNamespace:
    now = _now()
    starts_at = starts_at or (now - timedelta(days=1))
    expires_at = expires_at or (now + timedelta(days=10))
    return SimpleNamespace(
        user_id=uid,
        plan_code=plan_code,
        status=status,
        starts_at=starts_at,
        expires_at=expires_at,
        source=source,
        entitlement_snapshot={},
    )


def _plan(
    plan_code: str,
    display_name: str,
    monitor_limit: int,
    features: list[str] | None = None,
    notification_channel_limit: int = 1,
    message_retention_days: int = 30,
) -> SimpleNamespace:
    return SimpleNamespace(
        plan_code=plan_code,
        display_name=display_name,
        monitor_limit=monitor_limit,
        notification_channel_limit=notification_channel_limit,
        message_retention_days=message_retention_days,
        features=features or [],
        status="active",
    )


def _cap(
    uid: uuid.UUID,
    capability: str,
    source: str = "invite_code",
    expires_at: datetime | None = None,
    granted_at: datetime | None = None,
    watchlist_limit: int | None = None,
) -> SimpleNamespace:
    now = _now()
    return SimpleNamespace(
        user_id=uid,
        capability=capability,
        source=source,
        expires_at=expires_at or (now + timedelta(days=30)),
        granted_at=granted_at or now,
        watchlist_limit=watchlist_limit,
    )


class _FakeResult:
    """模拟 AsyncSession.execute 返回的 Result（scalar_one_or_none / scalars）。"""

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


class _FakeSession:
    """按 SELECT 的实体类型分发的 mock session（不连库）。"""

    def __init__(self, subs=None, caps=None, plans=None):
        self._subs = list(subs or [])
        self._caps = list(caps or [])
        self._plans = list(plans or [])
        self.calls: list = []
        self.execute = AsyncMock(side_effect=self._execute)

    async def _execute(self, stmt, *a, **k):
        self.calls.append(stmt)
        entity = None
        try:
            entity = stmt.column_descriptions[0]["entity"]
        except Exception:
            entity = None
        if entity is Subscription:
            return _FakeResult(self._subs)
        if entity is UserCapability:
            return _FakeResult(self._caps)
        if entity is Plan:
            return _FakeResult(self._plans)
        return _FakeResult([])


# ============================================================
# S1-1 admin
# ============================================================


@pytest.mark.asyncio
async def test_s1_admin_get_access_context():
    user = _user(["admin"])
    db = _FakeSession()
    ctx = await get_access_context(db, user)
    assert ctx.is_admin is True
    assert {k: v["active"] for k, v in ctx.capabilities.items()} == {
        "self_selection": True,
        "market_data": True,
        "market_review": True,
        "research_replay": True,
    }
    assert ctx.subscription_active is True  # admin 豁免
    assert ctx.default_route == "/admin/overview"
    # admin 路径不查询 Subscription
    sub_calls = [
        s
        for s in db.calls
        if getattr(s, "column_descriptions", [{}]) and s.column_descriptions[0].get("entity") is Subscription
    ]
    assert sub_calls == []


# ============================================================
# S1-2 explicit self_selection + market_data
# ============================================================


@pytest.mark.asyncio
async def test_s1_explicit_self_selection_market_data():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        caps=[_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")],
        subs=[_sub(uid, "research_50")],
        plans=[_plan("research_50", "研究版", 50, ["trend_selection"])],
    )
    profile = await resolve_effective_access(db, user)
    assert set(profile.active_capability_keys) == {"self_selection", "market_data"}
    assert profile.capability_source == "user_capabilities"
    assert profile.default_route == "/market"
    assert profile.capabilities["self_selection"].watchlist_limit == 20
    assert "research_replay" not in profile.active_capability_keys
    # explicit 用户未请求商业摘要时，capability-only resolver 不产出 subscription_summary（零商业 I/O）
    assert profile.subscription_summary is None


# ============================================================
# S1-3 explicit research_replay-only
# ============================================================


@pytest.mark.asyncio
async def test_s1_explicit_research_replay_only():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        caps=[_cap(uid, "research_replay")],
        subs=[_sub(uid, "research_50")],
        plans=[_plan("research_50", "研究版", 50)],
    )
    profile = await resolve_effective_access(db, user)
    assert profile.active_capability_keys == ["research_replay"]
    assert profile.default_route == "/auction"
    assert profile.capability_source == "user_capabilities"


# ============================================================
# S1-4 legacy observe_20
# ============================================================


@pytest.mark.asyncio
async def test_s1_legacy_observe_20():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        subs=[_sub(uid, "observe_20")],
        plans=[_plan("observe_20", "观察版", 20)],
    )
    profile = await resolve_effective_access(db, user)
    assert profile.capability_source == "legacy_plan_fallback"
    assert "legacy_plan_fallback" in profile.diagnostics
    assert set(profile.active_capability_keys) == {"self_selection", "market_data", "market_review"}
    assert profile.capabilities["self_selection"].watchlist_limit == 20
    assert "research_replay" not in profile.capabilities
    assert profile.default_route == "/market"


# ============================================================
# S1-5 legacy research_50
# ============================================================


@pytest.mark.asyncio
async def test_s1_legacy_research_50():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        subs=[_sub(uid, "research_50")],
        plans=[_plan("research_50", "研究版", 50)],
    )
    profile = await resolve_effective_access(db, user)
    assert profile.capability_source == "legacy_plan_fallback"
    assert set(profile.active_capability_keys) == {"self_selection", "market_data", "market_review", "research_replay"}
    assert profile.default_route == "/market"


# ============================================================
# S1-6 expired legacy subscription
# ============================================================


@pytest.mark.asyncio
async def test_s1_expired_legacy_subscription():
    uid = uuid.uuid4()
    user = _user(["member"])
    now = _now()
    db = _FakeSession(
        subs=[_sub(uid, "research_50", starts_at=now - timedelta(days=20), expires_at=now - timedelta(days=1))],
        plans=[_plan("research_50", "研究版", 50)],
    )
    profile = await resolve_effective_access(db, user)
    assert profile.capability_source == "legacy_plan_fallback"
    assert set(profile.capabilities.keys()) == {"self_selection", "market_data", "market_review", "research_replay"}
    # 能力存在但全部 inactive（过期订阅）
    assert profile.active_capability_keys == []
    assert profile.subscription_summary.active is False
    assert profile.subscription_summary.status == "expired"


# ============================================================
# S1-7 no subscription / no capability
# ============================================================


@pytest.mark.asyncio
async def test_s1_no_subscription_no_capability():
    user = _user(["member"])
    db = _FakeSession()  # 无订阅、无能力、无套餐
    profile = await resolve_effective_access(db, user)
    assert profile.capabilities == {}
    assert profile.active_capability_keys == []
    assert profile.capability_source == "none"
    assert profile.default_route == "/forbidden"
    s = profile.subscription_summary
    assert s.status == "none"
    assert s.active is False
    assert s.features == []
    assert s.limits == {}


# ============================================================
# S1-8 admin_revoke tombstone
# ============================================================


@pytest.mark.asyncio
async def test_s1_admin_revoke_tombstone():
    uid = uuid.uuid4()
    user = _user(["member"])
    future = _now() + timedelta(days=10)
    db = _FakeSession(
        caps=[_cap(uid, "self_selection", source="admin_revoke", expires_at=future, watchlist_limit=20)],
        subs=[_sub(uid, "research_50")],
        plans=[_plan("research_50", "研究版", 50)],
    )
    profile = await resolve_effective_access(db, user)
    cap = profile.capabilities["self_selection"]
    assert cap.active is False
    assert cap.reason == "explicitly_revoked"
    assert "self_selection" not in profile.active_capability_keys


# ============================================================
# S1-9 get_access_context 与 resolve_effective_access 权限字段完全一致
# ============================================================


@pytest.mark.asyncio
async def test_s1_get_access_context_matches_resolve():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        subs=[_sub(uid, "research_50")],
        caps=[_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")],
        plans=[_plan("research_50", "研究版", 50, ["trend_selection"])],
    )
    profile: EffectiveAccessProfile = await resolve_effective_access(db, user)
    ctx: AccessContext = await get_access_context(db, user)

    # capability 字段一致性
    assert ctx.default_route == profile.default_route
    assert ctx.active_capability_keys == profile.active_capability_keys
    assert ctx.capability_source == profile.capability_source

    expected_capabilities = {
        k: {
            "active": v.active,
            "granted_at": v.granted_at.isoformat() if v.granted_at else None,
            "expires_at": v.expires_at.isoformat() if v.expires_at else None,
            "watchlist_limit": v.watchlist_limit,
            "source": v.source,
            "reason": v.reason,
        }
        for k, v in profile.capabilities.items()
    }
    assert ctx.capabilities == expected_capabilities

    # 商业展示字段由 get_access_context 内 resolve_subscription_summary 解析，与直接解析一致
    from app.services.subscription_service import resolve_subscription_summary

    summary = await resolve_subscription_summary(db, user.id)
    assert ctx.subscription_active == summary.active
    assert ctx.plan_code == summary.plan_code
    assert ctx.plan_display_name == summary.plan_display_name
    assert ctx.expires_at == summary.expires_at
    assert ctx.features == summary.features
    assert ctx.limits == summary.limits

    # capability-only resolver 不依赖商业 I/O：未传 summary 时 subscription_summary 为 None
    assert profile.subscription_summary is None


# ============================================================
# S1-10 反向因果：相同 explicit capability，改变 plan/features 不影响能力解析
# ============================================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan_code,features,monitor",
    [
        ("observe_20", ["a"], 20),
        ("research_50", ["a", "b"], 50),
        ("none_plan", [], 99),  # 无订阅
    ],
)
async def test_s1_reverse_causality_plan_not_affecting_capabilities(plan_code, features, monitor):
    uid = uuid.uuid4()
    user = _user(["member"])
    caps = [_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")]
    if plan_code == "none_plan":
        subs = []
        plans = []
    else:
        subs = [_sub(uid, plan_code)]
        plans = [_plan(plan_code, plan_code, monitor, features)]

    db = _FakeSession(subs=subs, caps=caps, plans=plans)
    profile = await resolve_effective_access(db, user)

    # 能力解析结果与 plan 无关（capability 判权 owner 唯一）
    assert set(profile.active_capability_keys) == {"self_selection", "market_data"}
    assert profile.capability_source == "user_capabilities"
    assert profile.default_route == "/market"

    # 商业摘要由独立的 resolve_subscription_summary 解析，随 plan 变化（证明 plan 仅影响展示层）
    from app.services.subscription_service import resolve_subscription_summary

    summary = await resolve_subscription_summary(db, user.id)
    if plan_code == "none_plan":
        assert summary.status == "none"
    else:
        assert summary.plan_code == plan_code
        assert summary.features == features
        assert summary.limits["monitor_limit"] == monitor


# ============================================================
# S1-11 调用链证据：get_access_context 不再二次查询 Subscription/Plan
# ============================================================


@pytest.mark.asyncio
async def test_s1_get_access_context_single_subscription_query():
    uid = uuid.uuid4()
    user = _user(["member"])
    db = _FakeSession(
        subs=[_sub(uid, "research_50")],
        caps=[_cap(uid, "self_selection", watchlist_limit=20), _cap(uid, "market_data")],
        plans=[_plan("research_50", "研究版", 50)],
    )
    ctx = await get_access_context(db, user)

    sub_calls = [
        s
        for s in db.calls
        if getattr(s, "column_descriptions", [{}]) and s.column_descriptions[0].get("entity") is Subscription
    ]
    plan_calls = [
        s
        for s in db.calls
        if getattr(s, "column_descriptions", [{}]) and s.column_descriptions[0].get("entity") is Plan
    ]
    # Subscription / Plan 各只查询一次（在 resolve_effective_access 内部），
    # get_access_context 自身不得再次查询。
    assert len(sub_calls) == 1, f"期望恰好 1 次 Subscription 查询，实际 {len(sub_calls)} 次"
    assert len(plan_calls) == 1, f"期望恰好 1 次 Plan 查询，实际 {len(plan_calls)} 次"
    assert ctx.subscription_active is True


if __name__ == "__main__":
    raise SystemExit("run via pytest")
