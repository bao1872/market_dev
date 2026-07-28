"""[Gate2 PRD60 PA-20] Capability 管理 schema 与权限矩阵测试（不依赖 DB）。

测试范围：
- GrantCapabilityRequest / CapabilityGrant / RevokeCapabilityRequest 校验逻辑
- require_any_capability / require_watchlist_limit / require_capability 权限矩阵
- InviteCodeCreate capability 组合模式

测试策略：
- 纯单元测试，不连接数据库（无 db_session fixture）
- 直接调用 Pydantic schema model_validator 与 access_control_service 工厂函数
- 覆盖 PRD60 PA-01/PA-02/PA-03/PA-20/PA-10~13 权限矩阵

权限矩阵（PRD60）：
- 3 单项：self_selection only / market_data only / research_replay only
- 任意组合：self_selection+market_data / self_selection+research_replay / 全部
- 无权限：空 dict
- 过期：active=False
- admin：所有 capability active=True（豁免）
- 邀请码：capabilities 列表优先于 plan_code
- limit 边界：watchlist_limit 1/500/边界外
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app.schemas.invitation import CapabilityGrant, InviteCodeCreate
from app.schemas.subscription import (
    GrantCapabilityRequest,
    RevokeCapabilityRequest,
)
from app.services.access_control_service import (
    AccessContext,
    require_any_capability,
    require_watchlist_limit,
)

# ============================================================
# Schema 校验测试
# ============================================================


class TestCapabilityGrantSchema:
    """CapabilityGrant schema 校验（PRD60 PA-02/PA-20）。"""

    def test_self_selection_requires_watchlist_limit(self):
        """PA-02：self_selection 必须指定 watchlist_limit。"""
        with pytest.raises(ValueError, match="self_selection capability 必须指定 watchlist_limit"):
            CapabilityGrant(capability="self_selection", months=1, watchlist_limit=None)

    def test_self_selection_with_watchlist_limit_ok(self):
        """PA-02：self_selection + watchlist_limit 合法。"""
        cap = CapabilityGrant(capability="self_selection", months=1, watchlist_limit=20)
        assert cap.capability == "self_selection"
        assert cap.watchlist_limit == 20

    def test_market_data_rejects_watchlist_limit(self):
        """非 self_selection 不支持 watchlist_limit。"""
        with pytest.raises(ValueError, match="market_data 不支持 watchlist_limit"):
            CapabilityGrant(capability="market_data", months=1, watchlist_limit=20)

    def test_research_replay_rejects_watchlist_limit(self):
        """非 self_selection 不支持 watchlist_limit。"""
        with pytest.raises(ValueError, match="research_replay 不支持 watchlist_limit"):
            CapabilityGrant(capability="research_replay", months=1, watchlist_limit=10)

    def test_invalid_capability_rejected(self):
        """非法 capability 名称被拒绝。"""
        with pytest.raises(ValueError, match="无效 capability"):
            CapabilityGrant(capability="invalid_cap", months=1)

    def test_months_bounds(self):
        """months 边界：1-36 合法，0/37 非法。"""
        cap = CapabilityGrant(capability="market_data", months=1)
        assert cap.months == 1
        cap = CapabilityGrant(capability="market_data", months=36)
        assert cap.months == 36
        with pytest.raises(ValueError):
            CapabilityGrant(capability="market_data", months=0)
        with pytest.raises(ValueError):
            CapabilityGrant(capability="market_data", months=37)

    def test_watchlist_limit_bounds(self):
        """watchlist_limit 边界：1-500 合法。"""
        cap = CapabilityGrant(capability="self_selection", months=1, watchlist_limit=1)
        assert cap.watchlist_limit == 1
        cap = CapabilityGrant(capability="self_selection", months=1, watchlist_limit=500)
        assert cap.watchlist_limit == 500
        with pytest.raises(ValueError):
            CapabilityGrant(capability="self_selection", months=1, watchlist_limit=0)
        with pytest.raises(ValueError):
            CapabilityGrant(capability="self_selection", months=1, watchlist_limit=501)


class TestGrantCapabilityRequestSchema:
    """GrantCapabilityRequest schema 校验（管理员直接授予）。"""

    def test_self_selection_requires_watchlist_limit(self):
        with pytest.raises(ValueError, match="self_selection capability 必须指定 watchlist_limit"):
            GrantCapabilityRequest(capability="self_selection", months=1, watchlist_limit=None)

    def test_market_data_ok_without_watchlist_limit(self):
        req = GrantCapabilityRequest(capability="market_data", months=3)
        assert req.capability == "market_data"
        assert req.months == 3
        assert req.watchlist_limit is None

    def test_invalid_capability_rejected(self):
        with pytest.raises(ValueError, match="无效 capability"):
            GrantCapabilityRequest(capability="admin", months=1)


class TestRevokeCapabilityRequestSchema:
    """RevokeCapabilityRequest schema 校验。"""

    def test_valid_capability(self):
        for cap in ("self_selection", "market_data", "research_replay"):
            req = RevokeCapabilityRequest(capability=cap)
            assert req.capability == cap

    def test_invalid_capability_rejected(self):
        with pytest.raises(ValueError, match="无效 capability"):
            RevokeCapabilityRequest(capability="invalid")


class TestInviteCodeCreateSchema:
    """InviteCodeCreate capability 组合模式（PA-20）。"""

    def test_capabilities_mode_takes_precedence(self):
        """capabilities 优先于 plan_code。"""
        payload = InviteCodeCreate(
            count=1,
            capabilities=[
                CapabilityGrant(capability="self_selection", months=1, watchlist_limit=20),
                CapabilityGrant(capability="market_data", months=1),
            ],
        )
        assert payload.capabilities is not None
        assert len(payload.capabilities) == 2

    def test_legacy_plan_code_mode_still_works(self):
        """旧模式：无 capabilities，使用 plan_code + grant_months。"""
        payload = InviteCodeCreate(count=1, plan_code="observe_20", grant_months=1)
        assert payload.capabilities is None
        assert payload.plan_code == "observe_20"

    def test_empty_capabilities_list_rejected_by_business(self):
        """空 capabilities 列表应由后端业务层拒绝（schema 允许 None 但非空列表）。

        注：schema 不强制非空，由 service 层 validate_capabilities 检查。
        """
        payload = InviteCodeCreate(capabilities=[])
        # schema 允许空列表（语义上等于"无权限"），由后端业务层拒绝
        assert payload.capabilities == []


# ============================================================
# 权限矩阵测试（require_any_capability / require_watchlist_limit）
# ============================================================


def _make_ctx(
    is_admin: bool = False,
    capabilities: dict | None = None,
    limits: dict | None = None,
) -> AccessContext:
    """构造 AccessContext 测试对象（不查 DB）。"""
    return AccessContext(
        user_id=str(uuid.uuid4()),
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        capabilities=capabilities or {},
        limits=limits or {},
    )


class TestRequireAnyCapability:
    """require_any_capability 权限矩阵测试（PRD60 PA-01）。"""

    @pytest.mark.asyncio
    async def test_admin_exempt(self):
        """admin 豁免：所有 capability 通过。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(is_admin=True)
        result = await dep(ctx=ctx)
        assert result.is_admin is True

    @pytest.mark.asyncio
    async def test_self_selection_only_passes_market_route(self):
        """仅 self_selection 用户可进入 /market（任一即可）。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": True, "expires_at": None, "watchlist_limit": 20},
            }
        )
        result = await dep(ctx=ctx)
        assert result.user_id == ctx.user_id

    @pytest.mark.asyncio
    async def test_market_data_only_passes_market_route(self):
        """仅 market_data 用户可进入 /market（任一即可）。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "market_data": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        result = await dep(ctx=ctx)
        assert result.user_id == ctx.user_id

    @pytest.mark.asyncio
    async def test_research_replay_only_fails_market_route(self):
        """仅 research_replay 用户不能进入 /market。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "research_replay": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_capabilities_fails(self):
        """无任何 capability 的用户被拒绝。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(capabilities={})
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_expired_capability_fails(self):
        """过期 capability 不算 active（active=False）。"""
        dep = require_any_capability("self_selection", "market_data")
        expired = datetime.now(UTC) - timedelta(days=1)
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": False, "expires_at": expired, "watchlist_limit": 20},
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_all_three_capabilities_passes(self):
        """全部 capability 用户通过。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": True, "expires_at": None, "watchlist_limit": 50},
                "market_data": {"active": True, "expires_at": None, "watchlist_limit": None},
                "research_replay": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        result = await dep(ctx=ctx)
        assert result.user_id == ctx.user_id


class TestRequireWatchlistLimit:
    """require_watchlist_limit 权限矩阵测试（PRD60 PA-02）。"""

    @pytest.mark.asyncio
    async def test_admin_returns_none(self):
        """admin 返回 None 表示无限制。"""
        dep = require_watchlist_limit()
        ctx = _make_ctx(is_admin=True)
        result = await dep(ctx=ctx)
        assert result is None

    @pytest.mark.asyncio
    async def test_capability_watchlist_limit_preferred(self):
        """优先从 self_selection capability 取 watchlist_limit（PA-02）。"""
        dep = require_watchlist_limit()
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": True, "expires_at": None, "watchlist_limit": 30},
            },
            limits={"monitor_limit": 20},  # legacy limit 应被忽略
        )
        result = await dep(ctx=ctx)
        assert result == 30  # 来自 capability，不是 legacy limits

    @pytest.mark.asyncio
    async def test_fallback_to_legacy_limits(self):
        """无 capability 行时 fallback 到 plan limits（兼容期）。"""
        dep = require_watchlist_limit()
        ctx = _make_ctx(
            capabilities={},
            limits={"monitor_limit": 20},
        )
        result = await dep(ctx=ctx)
        assert result == 20

    @pytest.mark.asyncio
    async def test_no_capability_no_limits_returns_403(self):
        """无 capability 且无 plan limits：拒绝。"""
        dep = require_watchlist_limit()
        ctx = _make_ctx(capabilities={}, limits={})
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_expired_self_selection_falls_back_to_limits(self):
        """过期 capability 不算 active，fallback 到 legacy limits。

        注：require_watchlist_limit 检查 active=True，过期时 fallback。
        但 require_capability("self_selection") 会先拒绝（403），
        所以这个分支只在直接调用 require_watchlist_limit 时触发。
        """
        dep = require_watchlist_limit()
        expired = datetime.now(UTC) - timedelta(days=1)
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": False, "expires_at": expired, "watchlist_limit": 30},
            },
            limits={"monitor_limit": 20},
        )
        result = await dep(ctx=ctx)
        assert result == 20  # fallback 到 legacy


# ============================================================
# 权限矩阵覆盖矩阵（PRD60 PA-10/11/12/13）
# ============================================================


class TestPermissionMatrix:
    """PRD60 权限矩阵覆盖测试。

    矩阵：
    - 仅 self_selection：行情列表+自选+盘中可见，详情按钮禁用，/stock/:symbol API 403
    - 仅 market_data：行情+详情可见，自选/盘中入口隐藏
    - 仅 research_replay：仅复盘入口
    - 任意组合：按各自 capability 累加
    - 全部：完整访问
    - 无：所有业务路由 403
    """

    @pytest.mark.asyncio
    async def test_self_selection_only_can_access_market(self):
        """PA-10：仅 self_selection 可访问 /market。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": True, "expires_at": None, "watchlist_limit": 20},
            }
        )
        result = await dep(ctx=ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_self_selection_only_cannot_access_stock_detail(self):
        """PA-11：仅 self_selection 不能访问 /stock/:symbol（需 market_data）。"""
        from app.services.access_control_service import require_capability

        dep = require_capability("market_data")
        ctx = _make_ctx(
            capabilities={
                "self_selection": {"active": True, "expires_at": None, "watchlist_limit": 20},
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_market_data_only_can_access_stock_detail(self):
        """PA-13：仅 market_data 可访问 /stock/:symbol。"""
        from app.services.access_control_service import require_capability

        dep = require_capability("market_data")
        ctx = _make_ctx(
            capabilities={
                "market_data": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        result = await dep(ctx=ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_market_data_only_can_access_market(self):
        """PA-11：仅 market_data 也可访问 /market（任一即可）。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "market_data": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        result = await dep(ctx=ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_research_replay_only_can_access_replay(self):
        """PA-12：仅 research_replay 可访问 /replay。"""
        from app.services.access_control_service import require_capability

        dep = require_capability("research_replay")
        ctx = _make_ctx(
            capabilities={
                "research_replay": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        result = await dep(ctx=ctx)
        assert result is not None

    @pytest.mark.asyncio
    async def test_research_replay_only_cannot_access_market(self):
        """仅 research_replay 不能访问 /market。"""
        dep = require_any_capability("self_selection", "market_data")
        ctx = _make_ctx(
            capabilities={
                "research_replay": {"active": True, "expires_at": None, "watchlist_limit": None},
            }
        )
        with pytest.raises(HTTPException) as exc_info:
            await dep(ctx=ctx)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_capabilities_cannot_access_anything(self):
        """无任何 capability：所有业务路由 403。"""
        ctx = _make_ctx(capabilities={})

        for dep_factory in [
            require_any_capability("self_selection", "market_data"),
            require_watchlist_limit(),
        ]:
            with pytest.raises(HTTPException) as exc_info:
                await dep_factory(ctx=ctx)
            assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_passes_all_routes(self):
        """admin 豁免所有路由检查。"""
        ctx = _make_ctx(is_admin=True)

        market_dep = require_any_capability("self_selection", "market_data")
        result = await market_dep(ctx=ctx)
        assert result.is_admin is True

        watchlist_dep = require_watchlist_limit()
        limit = await watchlist_dep(ctx=ctx)
        assert limit is None  # admin 无限制


# ============================================================
# DB 集成测试：服务层 grant/revoke/get 行为（Gate2 真实补验）
# ============================================================


class TestCapabilityServiceIntegration:
    """[Gate2 真实补验] 服务层 grant/revoke/get 行为测试（依赖 DB，事务回滚隔离）。

    覆盖：
    - grant_capability_to_user：新建 capability 行
    - grant_capability_to_user：已有 capability 取较晚 expires_at（不降权）
    - revoke_capability_from_user：硬删除行
    - get_user_capabilities：返回三类 capability 状态
    - 邀请码三权限组合：InviteCode.capabilities JSONB 写入与读取
    - 过期：expires_at < now → active=False
    - admin：grant_by 记录管理员 ID
    - 仅行情/仅自选/仅复盘：三类独立授予
    """

    @pytest.mark.asyncio
    async def test_grant_new_self_selection_capability(
        self, db_session, user_factory,
    ) -> None:
        """授予新 self_selection capability：创建行，source=admin_grant。"""
        from app.services.subscription_service import grant_capability_to_user

        user = await user_factory()
        admin = await user_factory(roles=["admin"])

        result = await grant_capability_to_user(
            db=db_session,
            user_id=user.id,
            capability="self_selection",
            months=1,
            watchlist_limit=20,
            granted_by=admin.id,
        )

        assert result["active"] is True
        assert result["watchlist_limit"] == 20
        assert result["expires_at"] is not None
        # 验证 DB 行存在
        from sqlalchemy import select

        from app.models.user_capability import UserCapability
        stmt = select(UserCapability).where(
            UserCapability.user_id == user.id,
            UserCapability.capability == "self_selection",
        )
        row = (await db_session.execute(stmt)).scalar_one_or_none()
        assert row is not None
        assert row.source == "admin_grant"
        assert row.granted_by == admin.id
        assert row.watchlist_limit == 20

    @pytest.mark.asyncio
    async def test_grant_existing_takes_later_expires(
        self, db_session, user_factory,
    ) -> None:
        """已有 capability：取较晚 expires_at（不降权）。"""
        from app.models.user_capability import UserCapability
        from app.services.subscription_service import grant_capability_to_user

        user = await user_factory()
        admin = await user_factory(roles=["admin"])

        # 第一次授予 1 个月
        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="market_data",
            months=1, watchlist_limit=None, granted_by=admin.id,
        )
        # 查询第一次的 expires_at
        from sqlalchemy import select
        stmt = select(UserCapability).where(
            UserCapability.user_id == user.id,
            UserCapability.capability == "market_data",
        )
        first_row = (await db_session.execute(stmt)).scalar_one()
        first_expires = first_row.expires_at

        # 第二次授予 3 个月（应取较晚的 expires_at）
        result = await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="market_data",
            months=3, watchlist_limit=None, granted_by=admin.id,
        )
        # 较晚的 expires_at（3 个月后 > 1 个月后）
        assert result["expires_at"] > first_expires
        # 仍只有一行
        rows = (await db_session.execute(stmt)).scalars().all()
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_revoke_capability_removes_row(
        self, db_session, user_factory,
    ) -> None:
        """revoke 硬删除 user_capabilities 行。"""
        from sqlalchemy import select

        from app.models.user_capability import UserCapability
        from app.services.subscription_service import (
            grant_capability_to_user,
            revoke_capability_from_user,
        )

        user = await user_factory()
        admin = await user_factory(roles=["admin"])

        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="research_replay",
            months=1, watchlist_limit=None, granted_by=admin.id,
        )
        # 确认行存在
        stmt = select(UserCapability).where(
            UserCapability.user_id == user.id,
            UserCapability.capability == "research_replay",
        )
        assert (await db_session.execute(stmt)).scalar_one_or_none() is not None

        # 撤销
        removed = await revoke_capability_from_user(
            db=db_session, user_id=user.id, capability="research_replay",
        )
        assert removed is True
        # 行已删除
        assert (await db_session.execute(stmt)).scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_get_user_capabilities_returns_all_three(
        self, db_session, user_factory,
    ) -> None:
        """get_user_capabilities 返回已授予的 capability 状态（无行的不返回）。"""
        from app.services.subscription_service import (
            get_user_capabilities,
            grant_capability_to_user,
        )

        user = await user_factory()
        admin = await user_factory(roles=["admin"])

        # 仅授予 self_selection
        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="self_selection",
            months=1, watchlist_limit=30, granted_by=admin.id,
        )

        caps = await get_user_capabilities(db_session, user.id)
        # 仅返回已授予的（无行的不返回，由调用方 fallback 到 plan_code 推断）
        assert "self_selection" in caps
        assert "market_data" not in caps
        assert "research_replay" not in caps
        # self_selection active
        assert caps["self_selection"]["active"] is True
        assert caps["self_selection"]["watchlist_limit"] == 30

    @pytest.mark.asyncio
    async def test_expired_capability_not_active(
        self, db_session, user_factory,
    ) -> None:
        """过期 capability active=False（expires_at < now）。"""
        from app.models.user_capability import UserCapability
        from app.services.subscription_service import get_user_capabilities

        user = await user_factory()

        # 直接构造过期行（绕过 grant 服务层的 now + months 计算）
        expired_at = datetime.now(UTC) - timedelta(days=1)
        db_row = UserCapability(
            user_id=user.id,
            capability="market_data",
            watchlist_limit=None,
            granted_at=datetime.now(UTC) - timedelta(days=30),
            expires_at=expired_at,
            source="admin_grant",
        )
        db_session.add(db_row)
        await db_session.flush()

        caps = await get_user_capabilities(db_session, user.id)
        assert caps["market_data"]["active"] is False
        assert caps["market_data"]["expires_at"] == expired_at

    @pytest.mark.asyncio
    async def test_grant_three_capabilities_independently(
        self, db_session, user_factory,
    ) -> None:
        """三类 capability 独立授予（仅行情/仅自选/仅复盘 各自独立）。"""
        from app.services.subscription_service import (
            get_user_capabilities,
            grant_capability_to_user,
        )

        user = await user_factory()
        admin = await user_factory(roles=["admin"])

        # 分别授予三类
        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="self_selection",
            months=1, watchlist_limit=20, granted_by=admin.id,
        )
        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="market_data",
            months=2, watchlist_limit=None, granted_by=admin.id,
        )
        await grant_capability_to_user(
            db=db_session, user_id=user.id, capability="research_replay",
            months=3, watchlist_limit=None, granted_by=admin.id,
        )

        caps = await get_user_capabilities(db_session, user.id)
        assert caps["self_selection"]["active"] is True
        assert caps["market_data"]["active"] is True
        assert caps["research_replay"]["active"] is True
        # watchlist_limit 仅 self_selection 有值
        assert caps["self_selection"]["watchlist_limit"] == 20
        assert caps["market_data"]["watchlist_limit"] is None
        assert caps["research_replay"]["watchlist_limit"] is None
        # expires_at 各自独立
        assert caps["self_selection"]["expires_at"] != caps["market_data"]["expires_at"]

    @pytest.mark.asyncio
    async def test_invite_code_capabilities_jsonb_roundtrip(
        self, db_session, user_factory,
    ) -> None:
        """邀请码 capabilities JSONB 写入与读取（PA-20 三权限组合）。"""
        import hashlib

        from sqlalchemy import select

        from app.models.invitation import InviteCode
        from app.schemas.invitation import CapabilityGrant

        admin = await user_factory(roles=["admin"])

        # 构造三权限组合邀请码（明文不存储，仅 code_hash）
        plain_code = f"TEST-{uuid.uuid4().hex[:8]}"
        code_hash = hashlib.sha256(plain_code.encode()).hexdigest()

        caps = [
            CapabilityGrant(capability="self_selection", months=1, watchlist_limit=20),
            CapabilityGrant(capability="market_data", months=1),
            CapabilityGrant(capability="research_replay", months=1),
        ]
        invite = InviteCode(
            code_hash=code_hash,
            status="unused",
            grant_months=1,
            capabilities=[c.model_dump() for c in caps],
            created_by=admin.id,
        )
        db_session.add(invite)
        await db_session.flush()

        # 读取回来
        stmt = select(InviteCode).where(InviteCode.code_hash == code_hash)
        loaded = (await db_session.execute(stmt)).scalar_one()
        assert loaded.capabilities is not None
        assert len(loaded.capabilities) == 3
        cap_types = {c["capability"] for c in loaded.capabilities}
        assert cap_types == {"self_selection", "market_data", "research_replay"}


if __name__ == "__main__":
    # 自测入口：直接运行 pytest
    pytest.main([__file__, "-v", "--tb=short"])
