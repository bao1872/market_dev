"""[PANJI-REVIEW-CAPABILITY-SPLIT] 复盘 capability 独立化验收矩阵（纯单元，PURE_UNIT_TEST=1，不连库）。

背景：
复盘（Market Dashboard / /review*）此前复用 ``market_data`` capability 守卫，
导致「有行情=有复盘」的隐式授权。本任务把 ``market_review`` 拆为第 4 个独立 capability。

本文件锁死以下合同（全部为行为/依赖断言，不做真实请求、不连库）：

1. SSOT：capability 常量唯一真源为 ``app.models.user_capability``；
   ``effective_access_service`` 不得再定义或再导出分叉常量（CAP_* / ALL_CAPABILITIES）。
2. 默认路由：``market_review`` 独立映射 ``/review``；
   4 者显式优先级 market_data > self_selection > market_review > research_replay。
3. 无隐式授权验收矩阵：任一 capability 只放行自身守卫，其余三者一律 403；
   admin 豁免全部。
4. 路由守卫：``/v1/market-dashboard/*`` 全部绑定 ``market_review``，且不再出现 ``market_data``。
5. legacy plan fallback：observe_20 → self_selection+market_data+market_review；
   research_50 → 再加 research_replay。
6. 邀请码「声明即授予」（硬合同）：``_grant_capabilities_from_invite`` 严格按邀请码 JSONB
   声明的 capability 授予，**绝不推导/补发附加权限**（曾在 353e3fb7 中存在的
   market_data → market_review 运行时隐式继承已被删除；历史兼容改为一次性 migration 097）。
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.market_dashboard import router as market_dashboard_router
from app.models import user_capability as cap_mod
from app.models.user_capability import (
    ALL_CAPABILITIES,
    CAPABILITY_MARKET_DATA,
    CAPABILITY_MARKET_REVIEW,
    CAPABILITY_RESEARCH_REPLAY,
    CAPABILITY_SELF_SELECTION,
)
from app.services import effective_access_service as eas
from app.services.access_control_service import AccessContext, require_capability
from app.services.effective_access_service import (
    DEFAULT_ROUTE_ADMIN,
    DEFAULT_ROUTE_AUCTION,
    DEFAULT_ROUTE_FORBIDDEN,
    DEFAULT_ROUTE_MARKET,
    DEFAULT_ROUTE_MARKET_WATCHLIST,
    DEFAULT_ROUTE_REVIEW,
    CapabilityState,
    compute_default_route,
    infer_capabilities_from_plan,
)

_EXPIRES = datetime.now(UTC) + timedelta(days=30)

_ALL_CAPS = (
    CAPABILITY_SELF_SELECTION,
    CAPABILITY_MARKET_DATA,
    CAPABILITY_MARKET_REVIEW,
    CAPABILITY_RESEARCH_REPLAY,
)


# ============================================================
# 夹具
# ============================================================


def _cap_state(active: bool = True) -> CapabilityState:
    return CapabilityState(
        key="x",
        active=active,
        expires_at=_EXPIRES if active else _EXPIRES - timedelta(days=60),
    )


def _cap_dict(active: bool = True) -> dict:
    return {"active": active, "expires_at": _EXPIRES, "watchlist_limit": None}


def _ctx(*, capabilities: dict | None = None, is_admin: bool = False) -> AccessContext:
    """构造最小 AccessContext（仅填充权限判定必需字段；其余走默认值）。"""
    return AccessContext(
        user_id=str(uuid.uuid4()),
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        default_route=DEFAULT_ROUTE_ADMIN if is_admin else DEFAULT_ROUTE_FORBIDDEN,
        capabilities=capabilities or {},
    )


def _collect_capability_strings(dep: Any) -> set[str]:
    """递归收集某个路由依赖树中出现的字符串常量（capability 机器值）。"""
    found: set[str] = set()
    fn = getattr(dep, "call", None)
    for cell in getattr(fn, "__closure__", None) or ():
        try:
            value = cell.cell_contents
        except ValueError:  # pragma: no cover - 空 cell
            continue
        if isinstance(value, str):
            found.add(value)
        elif isinstance(value, (list, tuple, set, frozenset)):
            found.update(str(v) for v in value if isinstance(v, str))
    for sub in getattr(dep, "dependencies", None) or ():
        found |= _collect_capability_strings(sub)
    return found


# ============================================================
# 1. SSOT：capability 常量唯一真源
# ============================================================


class TestCapabilitySingleSourceOfTruth:
    def test_all_capabilities_is_exactly_four(self) -> None:
        assert tuple(ALL_CAPABILITIES) == (
            "self_selection",
            "market_data",
            "market_review",
            "research_replay",
        )
        assert len(set(ALL_CAPABILITIES)) == 4

    def test_market_review_machine_value(self) -> None:
        """复盘机器值必须是 ``market_review``，且 research_replay 未被改名。"""
        assert CAPABILITY_MARKET_REVIEW == "market_review"
        assert CAPABILITY_RESEARCH_REPLAY == "research_replay"
        assert CAPABILITY_MARKET_REVIEW in ALL_CAPABILITIES

    def test_effective_access_service_does_not_fork_constants(self) -> None:
        """effective_access_service 不得再定义分叉常量（旧名 CAP_* 必须消失）。"""
        for name in ("CAP_SELF_SELECTION", "CAP_MARKET_DATA", "CAP_RESEARCH_REPLAY"):
            assert not hasattr(eas, name), f"effective_access_service 不应再定义 {name}（SSOT 分叉）"

    def test_effective_access_service_reuses_canonical_constants(self) -> None:
        """导入的常量必须是 user_capability 的同一对象（不是副本）。"""
        assert eas.ALL_CAPABILITIES is cap_mod.ALL_CAPABILITIES
        assert eas.CAPABILITY_MARKET_REVIEW is cap_mod.CAPABILITY_MARKET_REVIEW

    def test_labels_cover_all_capabilities(self) -> None:
        assert set(eas.CAPABILITY_LABELS) == set(ALL_CAPABILITIES)

    def test_route_constants_include_review(self) -> None:
        assert eas.DEFAULT_ROUTE_REVIEW == "/review"
        assert eas.DEFAULT_ROUTE_AUCTION == "/auction"


# ============================================================
# 2. 默认路由：market_review 独立映射 /review
# ============================================================


class TestDefaultRouteIndependence:
    @pytest.mark.parametrize(
        "is_admin,active_keys,expected",
        [
            (True, [], DEFAULT_ROUTE_ADMIN),
            (False, [], DEFAULT_ROUTE_FORBIDDEN),
            (False, [CAPABILITY_SELF_SELECTION], DEFAULT_ROUTE_MARKET_WATCHLIST),
            (False, [CAPABILITY_MARKET_DATA], DEFAULT_ROUTE_MARKET),
            (False, [CAPABILITY_MARKET_REVIEW], DEFAULT_ROUTE_REVIEW),
            (False, [CAPABILITY_RESEARCH_REPLAY], DEFAULT_ROUTE_AUCTION),
            # 显式优先级 market_data > self_selection > market_review > research_replay
            (False, [CAPABILITY_MARKET_REVIEW, CAPABILITY_MARKET_DATA], DEFAULT_ROUTE_MARKET),
            (False, [CAPABILITY_MARKET_REVIEW, CAPABILITY_SELF_SELECTION], DEFAULT_ROUTE_MARKET_WATCHLIST),
            (False, [CAPABILITY_MARKET_REVIEW, CAPABILITY_RESEARCH_REPLAY], DEFAULT_ROUTE_REVIEW),
            (False, [CAPABILITY_SELF_SELECTION, CAPABILITY_RESEARCH_REPLAY], DEFAULT_ROUTE_MARKET_WATCHLIST),
        ],
    )
    def test_route_matrix(self, is_admin: bool, active_keys: list[str], expected: str) -> None:
        caps = {k: _cap_state(True) for k in active_keys}
        assert compute_default_route(is_admin, caps) == expected


# ============================================================
# 3. 无隐式授权验收矩阵（依赖层，等价 API 守卫真实实现）
# ============================================================


class TestNoImplicitAuthorization:
    """任一 capability 只放行自身守卫；其余三者一律 403。"""

    @pytest.mark.parametrize("granted", _ALL_CAPS)
    @pytest.mark.asyncio
    async def test_single_capability_grants_only_itself(self, granted: str) -> None:
        ctx = _ctx(capabilities={granted: _cap_dict()})
        for cap in _ALL_CAPS:
            dep = require_capability(cap)
            if cap == granted:
                assert await dep(ctx=ctx) is ctx, f"{cap} 应放行（已授予 {granted}）"
            else:
                with pytest.raises(HTTPException) as exc:
                    await dep(ctx=ctx)
                assert exc.value.status_code == 403, f"授予 {granted} 不得隐式放行 {cap}"

    @pytest.mark.asyncio
    async def test_market_data_only_is_denied_review(self) -> None:
        """market_data-only 用户访问复盘守卫 → 403（本轮核心隔离目标）。"""
        ctx = _ctx(capabilities={CAPABILITY_MARKET_DATA: _cap_dict()})
        with pytest.raises(HTTPException) as exc:
            await require_capability(CAPABILITY_MARKET_REVIEW)(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_market_review_only_is_denied_market_data(self) -> None:
        """market_review-only 用户访问行情守卫 → 403（反向隔离）。"""
        ctx = _ctx(capabilities={CAPABILITY_MARKET_REVIEW: _cap_dict()})
        with pytest.raises(HTTPException) as exc:
            await require_capability(CAPABILITY_MARKET_DATA)(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_market_review_only_allows_review(self) -> None:
        ctx = _ctx(capabilities={CAPABILITY_MARKET_REVIEW: _cap_dict()})
        assert await require_capability(CAPABILITY_MARKET_REVIEW)(ctx=ctx) is ctx

    @pytest.mark.asyncio
    async def test_research_replay_untouched_by_review(self) -> None:
        """research_replay（竞价）机器值语义不变：无 replay → 403，有 → 放行。"""
        without = _ctx(capabilities={CAPABILITY_MARKET_REVIEW: _cap_dict()})
        with pytest.raises(HTTPException) as exc:
            await require_capability(CAPABILITY_RESEARCH_REPLAY)(ctx=without)
        assert exc.value.status_code == 403

        with_replay = _ctx(capabilities={CAPABILITY_RESEARCH_REPLAY: _cap_dict()})
        assert await require_capability(CAPABILITY_RESEARCH_REPLAY)(ctx=with_replay) is with_replay

    @pytest.mark.asyncio
    async def test_expired_market_review_is_denied(self) -> None:
        ctx = _ctx(capabilities={CAPABILITY_MARKET_REVIEW: _cap_dict(active=False)})
        with pytest.raises(HTTPException) as exc:
            await require_capability(CAPABILITY_MARKET_REVIEW)(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_bypasses_all_guards(self) -> None:
        ctx = _ctx(is_admin=True, capabilities={})
        for cap in _ALL_CAPS:
            assert await require_capability(cap)(ctx=ctx) is ctx


# ============================================================
# 4. 路由守卫：/v1/market-dashboard/* 绑定 market_review
# ============================================================


class TestMarketDashboardGuardUsesReview:
    def test_every_dashboard_route_binds_market_review(self) -> None:
        routes = [r for r in market_dashboard_router.routes if hasattr(r, "dependant")]
        assert routes, "market-dashboard 路由不应为空"
        for route in routes:
            names = _collect_capability_strings(route.dependant)
            path = getattr(route, "path", "")
            assert CAPABILITY_MARKET_REVIEW in names, f"{path} 未绑定 market_review：{names}"

    def test_dashboard_route_does_not_bind_market_data(self) -> None:
        """复盘守卫不得再回退到 market_data（否则隔离目标未达成）。"""
        routes = [r for r in market_dashboard_router.routes if hasattr(r, "dependant")]
        for route in routes:
            names = _collect_capability_strings(route.dependant)
            path = getattr(route, "path", "")
            assert CAPABILITY_MARKET_DATA not in names, f"{path} 仍绑定 market_data：{names}"


# ============================================================
# 5. legacy plan fallback：4 类 capability 兼容映射
# ============================================================


class TestLegacyPlanFallback:
    def test_observe_20_grants_review_not_replay(self) -> None:
        caps = infer_capabilities_from_plan("observe_20", 20, _EXPIRES, True)
        assert set(caps) == {
            CAPABILITY_SELF_SELECTION,
            CAPABILITY_MARKET_DATA,
            CAPABILITY_MARKET_REVIEW,
        }
        assert CAPABILITY_RESEARCH_REPLAY not in caps
        assert caps[CAPABILITY_SELF_SELECTION]["watchlist_limit"] == 20
        assert caps[CAPABILITY_MARKET_REVIEW]["watchlist_limit"] is None

    def test_research_50_grants_all_four(self) -> None:
        caps = infer_capabilities_from_plan("research_50", 50, _EXPIRES, True)
        assert set(caps) == set(ALL_CAPABILITIES)

    def test_inactive_subscription_all_inactive(self) -> None:
        caps = infer_capabilities_from_plan("research_50", 50, _EXPIRES, False)
        assert set(caps) == set(ALL_CAPABILITIES)
        assert all(info["active"] is False for info in caps.values())

    def test_unknown_plan_yields_no_capability(self) -> None:
        assert infer_capabilities_from_plan("unknown_plan", None, None, True) == {}


# ============================================================
# 6. 邀请码「声明即授予」：禁止任何隐式继承
# ============================================================


class _FakeDb:
    """最小 AsyncSession 替身：仅满足 _grant_capabilities_from_invite 的 await db.flush()。"""

    def __init__(self) -> None:
        self.flush_calls = 0

    async def flush(self) -> None:
        self.flush_calls += 1


async def _run_invite_redemption(monkeypatch: pytest.MonkeyPatch, capabilities: Any) -> list[dict]:
    """以 monkeypatch 截获 apply_capability_grant，返回实际发出的授权调用列表。"""
    from app.services import subscription_service as ss

    calls: list[dict] = []

    async def _fake_apply(
        db: Any,
        *,
        user_id: Any,
        capability: str,
        grant_days: int,
        watchlist_limit: int | None,
        source: str,
        materialize_legacy: bool,
        actor_user_id: Any = None,
        reason: str | None = None,
        now: Any = None,
    ) -> None:
        calls.append(
            {
                "capability": capability,
                "grant_days": grant_days,
                "watchlist_limit": watchlist_limit,
                "source": source,
                "actor_user_id": actor_user_id,
            }
        )

    monkeypatch.setattr(ss, "apply_capability_grant", _fake_apply)

    invite = SimpleNamespace(capabilities=capabilities)
    db = _FakeDb()
    await ss._grant_capabilities_from_invite(db, uuid.uuid4(), invite, materialize_legacy=False)
    # capabilities=None 走旧模式提前返回，不产生 flush
    expected_flush = 0 if capabilities is None else 1
    assert db.flush_calls == expected_flush, (
        f"flush 次数应为 {expected_flush}，实际 {db.flush_calls}"
    )
    return calls


class TestInviteRedemptionDeclaresOnlyWhatItSays:
    """[A9] 永久阻止「权限继承」重新出现。"""

    @pytest.mark.asyncio
    async def test_market_data_only_invite_never_grants_market_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """升级后新建的 market_data-only 邀请码，兑换结果必须严格只有 market_data。"""
        calls = await _run_invite_redemption(
            monkeypatch, [{"capability": "market_data", "days": 30}]
        )
        assert [c["capability"] for c in calls] == ["market_data"], (
            f"market_data-only 邀请码不得推导任何附加权限，实际 {calls}"
        )
        assert len(calls) == 1, f"必须只调用一次 apply_capability_grant，实际 {calls}"
        assert not any(c["capability"] == CAPABILITY_MARKET_REVIEW for c in calls), (
            "不得出现 market_review 隐式继承"
        )

    @pytest.mark.asyncio
    async def test_market_review_only_invite_grants_only_review(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = await _run_invite_redemption(
            monkeypatch, [{"capability": "market_review", "days": 30}]
        )
        assert [c["capability"] for c in calls] == ["market_review"], f"实际 {calls}"

    @pytest.mark.asyncio
    async def test_explicit_both_grants_both_separately(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = await _run_invite_redemption(
            monkeypatch,
            [
                {"capability": "market_data", "days": 30},
                {"capability": "market_review", "days": 30},
            ],
        )
        assert [c["capability"] for c in calls] == ["market_data", "market_review"], f"实际 {calls}"

    @pytest.mark.asyncio
    async def test_self_selection_invite_never_grants_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """self_selection 邀请码不得推导 market_data / market_review / research_replay。"""
        calls = await _run_invite_redemption(
            monkeypatch,
            [{"capability": "self_selection", "days": 30, "watchlist_limit": 20}],
        )
        assert [c["capability"] for c in calls] == ["self_selection"], f"实际 {calls}"
        assert calls[0]["watchlist_limit"] == 20, "self_selection 必须保留 watchlist_limit"

    @pytest.mark.asyncio
    async def test_legacy_months_unit_is_converted_to_days(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """历史 months 邀请码仍按 ×30 天兑换（授权天数口径不变）。"""
        calls = await _run_invite_redemption(
            monkeypatch, [{"capability": "market_data", "months": 2}]
        )
        assert [c["capability"] for c in calls] == ["market_data"], f"实际 {calls}"
        assert calls[0]["grant_days"] == 60, f"months=2 应折算 60 天，实际 {calls}"

    @pytest.mark.asyncio
    async def test_null_capabilities_invite_grants_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """旧邀请码（capabilities=NULL）不创建 user_capabilities，走 legacy plan fallback。"""
        calls = await _run_invite_redemption(monkeypatch, None)
        assert calls == [], f"NULL capabilities 不得授予任何 capability，实际 {calls}"

    def test_no_inheritance_bridge_remains_in_source(self) -> None:
        """结构性锁：兑换函数源码中不得再出现隐式继承桥的痕迹。"""
        import inspect

        from app.services import subscription_service as ss

        src = inspect.getsource(ss._grant_capabilities_from_invite)
        assert "present_caps" not in src, "隐式继承桥（present_caps）必须已删除"
        assert 'capability="market_review"' not in src, (
            "兑换函数不得硬编码补发 market_review"
        )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v", "--tb=short"])
