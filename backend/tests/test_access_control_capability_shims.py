"""权限 resolver 行为门禁（纯单元，不依赖 PG）。

验证 2f5f18d 后 capability-first 的真实语义：
- capability-only 用户按能力放行/拒绝，quota 取自 capability.watchlist_limit（非凭空映射）；
- 旧 Subscription active 但无 capability → 按新模型拒绝（require_active_subscription 兼容 shim 不模糊 account-wide active）；
- 不同 capability 不同 expiry → 细粒度校验，不因为某个有效就把全部功能视为 active；
- require_feature("trend_selection") 映射 self_selection OR research_replay（不再读 plan features）。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.services.access_control_service import (
    AccessContext,
    require_active_subscription,
    require_capability,
    require_feature,
    require_quota,
)


def _ctx(**overrides: object) -> AccessContext:
    base: dict[str, object] = dict(
        user_id="u",
        account_status="active",
        roles=["member"],
        is_admin=False,
        is_member=True,
        subscription_active=False,
        features=[],
        limits={},
        capabilities={},
        default_route="/forbidden",
        active_capability_keys=[],
    )
    base.update(overrides)
    return AccessContext(**base)  # type: ignore[arg-type]


async def test_require_active_subscription_passes_when_capability_active() -> None:
    ctx = _ctx(active_capability_keys=["self_selection"])
    assert await require_active_subscription(ctx) is ctx


async def test_require_active_subscription_403_when_no_capability_and_no_subscription() -> None:
    ctx = _ctx()
    with pytest.raises(HTTPException) as exc:
        await require_active_subscription(ctx)
    assert exc.value.status_code == 403


async def test_require_active_subscription_403_when_subscription_active_but_no_capability() -> None:
    """Case 6: 旧 Subscription active 但无 capability → 仍按新模型拒绝（不模糊 account-wide active）。"""
    ctx = _ctx(subscription_active=True, plan_code="research_50")
    with pytest.raises(HTTPException) as exc:
        await require_active_subscription(ctx)
    assert exc.value.status_code == 403


async def test_require_feature_trend_selection_maps_to_self_selection_or_research_replay() -> None:
    """trend_selection = self_selection OR research_replay；market_data 不算（PA-13）。"""
    ok_self = _ctx(
        active_capability_keys=["self_selection"],
        capabilities={"self_selection": {"active": True}},
    )
    assert await require_feature("trend_selection")(ok_self) is ok_self

    ok_research = _ctx(
        active_capability_keys=["research_replay"],
        capabilities={"research_replay": {"active": True}},
    )
    assert await require_feature("trend_selection")(ok_research) is ok_research

    only_market = _ctx(
        active_capability_keys=["market_data"],
        capabilities={"market_data": {"active": True}},
    )
    with pytest.raises(HTTPException):
        await require_feature("trend_selection")(only_market)


async def test_require_quota_monitor_limit_from_capability() -> None:
    """quota 真源 = capability.watchlist_limit（非凭空映射）。"""
    ctx = _ctx(
        active_capability_keys=["self_selection"],
        capabilities={"self_selection": {"active": True, "watchlist_limit": 15}},
    )
    assert await require_quota("monitor_limit")(ctx) == 15


async def test_require_quota_monitor_limit_admin_none() -> None:
    ctx = _ctx(is_admin=True, roles=["admin"])
    assert await require_quota("monitor_limit")(ctx) is None


async def test_require_quota_monitor_limit_403_when_no_capability() -> None:
    with pytest.raises(HTTPException):
        await require_quota("monitor_limit")(_ctx())


async def test_capability_expired_blocks() -> None:
    """Case 6: capability 已过期（active=False）→ 拒绝。"""
    ctx = _ctx(capabilities={"self_selection": {"active": False}}, active_capability_keys=[])
    with pytest.raises(HTTPException):
        await require_feature("trend_selection")(ctx)


async def test_require_capability_respects_specific_expiry_not_account_wide() -> None:
    """Case 7: 不同 capability 不同 expiry → 细粒度校验，不因为某个有效就把全部视为 active。"""
    ctx = _ctx(
        capabilities={
            "self_selection": {"active": False},
            "research_replay": {"active": True},
        },
        active_capability_keys=["research_replay"],
    )
    with pytest.raises(HTTPException):
        await require_capability("self_selection")(ctx)
    assert await require_capability("research_replay")(ctx) is ctx
