"""[CHANGE-20260802-002] research_replay = 复盘与竞价 权限合同测试（纯单元，不连接 DB）。

背景：
竞价（/v1/auction/*）此前仅 require_authenticated，任何登录用户可读；
本轮收口为与复盘同一权益 require_capability("research_replay")，
不新增独立 auction capability，管理端 POST 仍要求 admin。

测试范围（后端 1~11 项）：
1. 未登录访问 5 个竞价 GET → 401（依赖链 require_authenticated 前置）
2. 已登录但无 research_replay → 403
3. 有 research_replay → 通过权限层
4. admin 访问不回归（豁免）
5. 两个 admin POST 仍要求 admin
6. 创建邀请码响应包含 capabilities
7. 列表响应包含 capabilities
8. 审计日志包含 capabilities
9. research_50 fallback 含 research_replay
10. observe_20 fallback 不含 research_replay
11. 显式 capabilities 不被 plan_code 错误覆盖

测试策略：
- 纯单元测试（PURE_UNIT_TEST=1），不连接数据库
- 权限层直接调用 access_control_service 依赖工厂，断言 HTTPException 状态码
- 路由守卫通过 FastAPI 依赖树静态断言（不发起真实请求）
- 邀请码回显通过 Pydantic schema 构造 + API 层源码依赖断言
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import auction as auction_api
from app.schemas.invitation import (
    CapabilityGrant,
    InviteCodeCreate,
    InviteCodeListItem,
    InviteCodeResponse,
)
from app.services.access_control_service import (
    AccessContext,
    _infer_capabilities_from_plan,
    require_admin,
    require_capability,
)

# ============================================================
# 测试夹具：构造不同权限的 AccessContext
# ============================================================

_EXPIRES = datetime.now(UTC) + timedelta(days=30)


def _ctx(
    *,
    is_admin: bool = False,
    capabilities: dict | None = None,
) -> AccessContext:
    """构造最小 AccessContext（仅填充权限判定所需字段）。"""
    return AccessContext(
        user_id=str(uuid.uuid4()),
        email="tester@example.com",
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        subscription_expires_at=_EXPIRES,
        plan_code=None,
        plan_display_name=None,
        features=[],
        limits={},
        capabilities=capabilities or {},
    )


def _active_cap() -> dict:
    return {"active": True, "expires_at": _EXPIRES, "watchlist_limit": None}


# 竞价 5 个只读 GET 端点函数（与 app/api/auction.py 一一对应）
AUCTION_GET_ENDPOINTS = [
    auction_api.get_market_page,
    auction_api.get_board_page,
    auction_api.get_stock_page,
    auction_api.get_anchor_status,
    auction_api.get_auction_backflow,
]

# 竞价 2 个管理端 POST 端点函数
AUCTION_ADMIN_POST_ENDPOINTS = [
    auction_api.trigger_scan,
    auction_api.trigger_anchors,
]


def _ctx_dependency_call(endpoint) -> object:
    """取出端点 ctx 参数的 Depends(...) 依赖可调用对象。"""
    sig = inspect.signature(endpoint)
    param = sig.parameters["ctx"]
    return param.default.dependency


# ============================================================
# 1~3. 竞价 GET 权限矩阵
# ============================================================


class TestAuctionGetRequiresReplayCapability:
    """竞价 5 个只读 GET 使用 research_replay capability 守卫。"""

    def test_capability_machine_value_is_research_replay(self):
        """机器值必须是 research_replay，不得新增 auction capability。"""
        assert auction_api.AUCTION_CAPABILITY == "research_replay"

    @pytest.mark.parametrize("endpoint", AUCTION_GET_ENDPOINTS)
    def test_get_endpoint_uses_capability_guard(self, endpoint):
        """(1) 依赖为 require_capability 产物，而非 require_authenticated。

        require_capability 内部先解析 AccessContext（未登录由 get_current_user 抛 401），
        因此未认证请求在依赖链前置阶段即返回 401，不会进入 capability 判定。
        """
        dep = _ctx_dependency_call(endpoint)
        # require_capability 返回的闭包命名为 _dep，且捕获 capability 变量
        assert dep.__closure__ is not None
        captured = [c.cell_contents for c in dep.__closure__]
        assert "research_replay" in captured, f"{endpoint.__name__} 未绑定 research_replay"

    @pytest.mark.parametrize("endpoint", AUCTION_GET_ENDPOINTS)
    def test_no_endpoint_still_uses_require_authenticated(self, endpoint):
        """5 个 GET 均不得残留 require_authenticated。"""
        dep = _ctx_dependency_call(endpoint)
        assert getattr(dep, "__name__", "") != "require_authenticated"

    def test_source_has_no_require_authenticated_import(self):
        """auction.py 不再导入 require_authenticated（避免误用回退）。"""
        source = Path(auction_api.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "access_control" in node.module:
                imported.update(alias.name for alias in node.names)
        assert "require_authenticated" not in imported
        assert "require_capability" in imported

    @pytest.mark.asyncio
    async def test_authenticated_without_capability_is_403(self):
        """(2) 已登录但无 research_replay → 403，与 /v1/review/* 合同一致。"""
        dep = require_capability("research_replay")
        ctx = _ctx(capabilities={"market_data": _active_cap()})
        with pytest.raises(HTTPException) as exc:
            await dep(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_expired_capability_is_403(self):
        """capability 存在但 active=False（已过期）同样 403。"""
        dep = require_capability("research_replay")
        expired = {"active": False, "expires_at": _EXPIRES, "watchlist_limit": None}
        ctx = _ctx(capabilities={"research_replay": expired})
        with pytest.raises(HTTPException) as exc:
            await dep(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_with_capability_passes(self):
        """(3) 有 research_replay → 通过权限层并原样返回 ctx。"""
        dep = require_capability("research_replay")
        ctx = _ctx(capabilities={"research_replay": _active_cap()})
        assert await dep(ctx=ctx) is ctx

    @pytest.mark.asyncio
    async def test_admin_without_capability_passes(self):
        """(4) admin 访问不回归：无 capability 行也豁免通过。"""
        dep = require_capability("research_replay")
        ctx = _ctx(is_admin=True, capabilities={})
        assert await dep(ctx=ctx) is ctx


# ============================================================
# 5. 管理端 POST 仍要求 admin
# ============================================================


class TestAuctionAdminPostUnchanged:
    """两个 admin POST 接口继续要求 admin 权限。"""

    @pytest.mark.parametrize("endpoint", AUCTION_ADMIN_POST_ENDPOINTS)
    def test_admin_post_uses_require_admin(self, endpoint):
        dep = _ctx_dependency_call(endpoint)
        assert dep is require_admin

    @pytest.mark.asyncio
    async def test_require_admin_rejects_replay_user(self):
        """仅有 research_replay 的普通用户不得触发竞价扫描/锚点发布。"""
        ctx = _ctx(capabilities={"research_replay": _active_cap()})
        with pytest.raises(HTTPException) as exc:
            await require_admin(ctx=ctx)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_require_admin_allows_admin(self):
        ctx = _ctx(is_admin=True)
        assert await require_admin(ctx=ctx) is ctx


# ============================================================
# 6~8. 邀请码 capabilities 回显与审计
# ============================================================

_SAMPLE_CAPS = [
    {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
    {"capability": "research_replay", "months": 1, "watchlist_limit": None},
]


def _api_source() -> str:
    from app.api import admin_subscription

    return Path(admin_subscription.__file__).read_text(encoding="utf-8")


class TestInviteCodeCapabilityEcho:
    """邀请码创建/列表响应与审计日志回显 capabilities。"""

    def test_create_response_carries_capabilities(self):
        """(6) InviteCodeResponse 能承载 capability 组合。"""
        resp = InviteCodeResponse(
            id=uuid.uuid4(),
            code="ABCD1234",
            grant_days=30,
            plan_code="research_50",
            monitor_limit=50,
            grant_months=1,
            capabilities=_SAMPLE_CAPS,
            note=None,
            created_at=datetime.now(UTC),
        )
        assert resp.capabilities is not None
        assert {c["capability"] for c in resp.capabilities} == {
            "self_selection",
            "research_replay",
        }

    def test_list_item_carries_capabilities(self):
        """(7) InviteCodeListItem 能承载 capability 组合。"""
        item = InviteCodeListItem(
            id=uuid.uuid4(),
            status="unused",
            grant_days=30,
            plan_code="research_50",
            monitor_limit=50,
            grant_months=1,
            capabilities=_SAMPLE_CAPS,
            note=None,
            created_by=uuid.uuid4(),
            created_at=datetime.now(UTC),
            used_by=None,
            used_at=None,
            usage_type=None,
        )
        assert item.capabilities == _SAMPLE_CAPS

    def test_old_mode_capabilities_is_none(self):
        """旧模式邀请码 capabilities=None，字段仍显式存在（不是漏传）。"""
        item = InviteCodeListItem(
            id=uuid.uuid4(),
            status="unused",
            grant_days=30,
            plan_code="observe_20",
            monitor_limit=20,
            grant_months=1,
            capabilities=None,
            note=None,
            created_by=uuid.uuid4(),
            created_at=datetime.now(UTC),
            used_by=None,
            used_at=None,
            usage_type=None,
        )
        assert "capabilities" in item.model_dump()
        assert item.capabilities is None

    def test_create_endpoint_passes_capabilities_to_response(self):
        """(6) API 层构造响应时显式传入 capabilities=invite.capabilities。"""
        source = _api_source()
        assert source.count("capabilities=invite.capabilities") >= 3, (
            "创建响应/列表响应/撤销响应均需回显 capabilities"
        )

    def test_audit_log_records_capabilities(self):
        """(8) 创建与撤销审计日志均记录 capabilities。"""
        source = _api_source()
        assert '"capabilities": invite.capabilities' in source
        # 撤销审计的 before_data 需保留原本授予的权限，便于还原
        assert '"status": "unused", "capabilities": invite.capabilities' in source


# ============================================================
# 9~10. plan_code fallback 权限矩阵
# ============================================================


class TestPlanFallbackCapabilities:
    """plan_code → capabilities 兼容期 fallback（旧用户无 user_capabilities 行）。"""

    def test_research_50_includes_research_replay(self):
        """(9) research_50 fallback 含 research_replay（即含竞价权限）。"""
        caps = _infer_capabilities_from_plan(
            plan_code="research_50",
            plan_monitor_limit=50,
            expires_at=_EXPIRES,
            subscription_active=True,
        )
        assert "research_replay" in caps
        assert caps["research_replay"]["active"] is True

    def test_observe_20_excludes_research_replay(self):
        """(10) observe_20 fallback 不含 research_replay（也就无竞价权限）。"""
        caps = _infer_capabilities_from_plan(
            plan_code="observe_20",
            plan_monitor_limit=20,
            expires_at=_EXPIRES,
            subscription_active=True,
        )
        assert "research_replay" not in caps
        assert set(caps) == {"self_selection", "market_data"}

    @pytest.mark.asyncio
    async def test_observe_20_user_cannot_access_auction(self):
        """observe_20 用户经 fallback 后访问竞价被 403 拦截。"""
        caps = _infer_capabilities_from_plan(
            plan_code="observe_20",
            plan_monitor_limit=20,
            expires_at=_EXPIRES,
            subscription_active=True,
        )
        dep = require_capability(auction_api.AUCTION_CAPABILITY)
        with pytest.raises(HTTPException) as exc:
            await dep(ctx=_ctx(capabilities=caps))
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_research_50_user_can_access_auction(self):
        """research_50 用户经 fallback 后可访问竞价。"""
        caps = _infer_capabilities_from_plan(
            plan_code="research_50",
            plan_monitor_limit=50,
            expires_at=_EXPIRES,
            subscription_active=True,
        )
        dep = require_capability(auction_api.AUCTION_CAPABILITY)
        ctx = _ctx(capabilities=caps)
        assert await dep(ctx=ctx) is ctx


# ============================================================
# 11. 显式 capabilities 不被 plan_code 覆盖
# ============================================================


class TestExplicitCapabilitiesPrecedence:
    """创建邀请码时显式 capabilities 优先于 plan_code。"""

    def test_explicit_capabilities_survive_default_plan_code(self):
        """(11) 显式 capabilities 与默认 observe_20 共存时不被覆盖。"""
        payload = InviteCodeCreate(
            count=1,
            capabilities=[
                CapabilityGrant(capability="research_replay", months=1),
            ],
        )
        # plan_code 保持默认值，但 capabilities 独立保留
        assert payload.plan_code == "observe_20"
        assert payload.capabilities is not None
        assert [c.capability for c in payload.capabilities] == ["research_replay"]

    def test_explicit_capabilities_not_derived_from_plan(self):
        """显式指定 observe_20 + research_replay 时不得被裁剪为套餐默认组合。"""
        payload = InviteCodeCreate(
            count=1,
            plan_code="observe_20",
            capabilities=[
                CapabilityGrant(capability="self_selection", months=1, watchlist_limit=20),
                CapabilityGrant(capability="research_replay", months=1),
            ],
        )
        assert payload.capabilities is not None
        assert {c.capability for c in payload.capabilities} == {
            "self_selection",
            "research_replay",
        }

    def test_no_auction_capability_accepted(self):
        """不存在独立 auction capability：显式传入应被拒绝。"""
        with pytest.raises(ValueError):
            InviteCodeCreate(
                count=1,
                capabilities=[CapabilityGrant(capability="auction", months=1)],
            )
