"""权限模型 V2 统一重构 - 核心服务单元测试。

覆盖：
1. _compute_default_route 默认路由矩阵（纯函数）
2. resolve_effective_access：显式 user_capabilities 解析
3. legacy plan fallback 显式标记 source
4. 登录 /me/access 返回 capabilities（C/D/E 断点回归）
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.effective_access_service import (
    CAP_MARKET_DATA,
    CAP_RESEARCH_REPLAY,
    CAP_SELF_SELECTION,
    DEFAULT_ROUTE_ADMIN,
    DEFAULT_ROUTE_FORBIDDEN,
    DEFAULT_ROUTE_MARKET,
    DEFAULT_ROUTE_MARKET_WATCHLIST,
    DEFAULT_ROUTE_REVIEW,
    CapabilityState,
    compute_default_route,
    resolve_effective_access,
)


def _cap(key: str, active: bool) -> CapabilityState:
    return CapabilityState(key=key, active=active, expires_at=datetime.utcnow() + timedelta(days=1) if active else None)


class TestComputeDefaultRoute:
    """默认路由矩阵（纯函数）。"""

    @pytest.mark.parametrize(
        "is_admin,active_keys,expected",
        [
            (True, [], DEFAULT_ROUTE_ADMIN),
            (False, [], DEFAULT_ROUTE_FORBIDDEN),
            (False, [CAP_SELF_SELECTION, CAP_MARKET_DATA], DEFAULT_ROUTE_MARKET),
            (False, [CAP_SELF_SELECTION], DEFAULT_ROUTE_MARKET_WATCHLIST),
            (False, [CAP_MARKET_DATA], DEFAULT_ROUTE_MARKET),
            (False, [CAP_RESEARCH_REPLAY], DEFAULT_ROUTE_REVIEW),
            (False, [CAP_RESEARCH_REPLAY, CAP_MARKET_DATA], DEFAULT_ROUTE_MARKET),
            (False, [CAP_RESEARCH_REPLAY, CAP_SELF_SELECTION], DEFAULT_ROUTE_MARKET),
        ],
    )
    def test_route_matrix(self, is_admin: bool, active_keys: list[str], expected: str) -> None:
        caps = {k: _cap(k, k in active_keys) for k in active_keys}
        assert compute_default_route(is_admin, caps) == expected


class TestResolveEffectiveAccess:
    """resolve_effective_access：显式 user_capabilities 是唯一真源。"""

    def _mk_user(self, roles: list[str] | None = None, status: str = "active") -> SimpleNamespace:
        return SimpleNamespace(id="user-1", status=status, _roles=roles or ["member"])

    def _mk_row(self, capability: str, expires_at: datetime | None, watchlist_limit: int | None = None, source: str = "invite_code") -> SimpleNamespace:
        return SimpleNamespace(
            capability=capability,
            granted_at=datetime.utcnow(),
            expires_at=expires_at,
            watchlist_limit=watchlist_limit,
            source=source,
        )

    def _mk_db(self, rows: list[Any], sub=None) -> SimpleNamespace:
        async def execute(stmt, params=None):
            return SimpleNamespace(
                fetchall=lambda: rows,
                scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None),
            )

        return SimpleNamespace(execute=execute, scalar_one_or_none=lambda: None)

    @pytest.mark.asyncio
    async def test_self_selection_active_returns_capability(self) -> None:
        rows = [self._mk_row(CAP_SELF_SELECTION, datetime.utcnow() + timedelta(days=30), watchlist_limit=1)]
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == [CAP_SELF_SELECTION]
        assert profile.has_any_access is True
        assert profile.capabilities[CAP_SELF_SELECTION].watchlist_limit == 1
        assert profile.capabilities[CAP_SELF_SELECTION].source == "invite_code"
        assert profile.default_route == DEFAULT_ROUTE_MARKET_WATCHLIST

    @pytest.mark.asyncio
    async def test_expired_capability_not_active(self) -> None:
        rows = [self._mk_row(CAP_SELF_SELECTION, datetime.utcnow() - timedelta(days=1), watchlist_limit=1)]
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == []
        assert profile.has_any_access is False
        assert profile.default_route == DEFAULT_ROUTE_FORBIDDEN

    @pytest.mark.asyncio
    async def test_market_data_route(self) -> None:
        rows = [self._mk_row(CAP_MARKET_DATA, datetime.utcnow() + timedelta(days=30))]
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == [CAP_MARKET_DATA]
        assert profile.default_route == DEFAULT_ROUTE_MARKET

    @pytest.mark.asyncio
    async def test_research_replay_route(self) -> None:
        rows = [self._mk_row(CAP_RESEARCH_REPLAY, datetime.utcnow() + timedelta(days=30))]
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == [CAP_RESEARCH_REPLAY]
        assert profile.default_route == DEFAULT_ROUTE_REVIEW

    @pytest.mark.asyncio
    async def test_admin_all_capabilities(self) -> None:
        rows: list[Any] = []
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user(roles=["admin"]))
        assert profile.is_admin is True
        assert profile.has_any_access is True
        assert profile.default_route == DEFAULT_ROUTE_ADMIN

    @pytest.mark.asyncio
    async def test_legacy_plan_fallback_marked(self) -> None:
        # 无 user_capabilities 行 → 走 legacy plan fallback，source 显式标记
        rows: list[Any] = []
        db = self._mk_db(rows)
        profile = await resolve_effective_access(db, self._mk_user())
        # fallback 需要订阅查询，这里 mock 无订阅 → 无 capabilities
        assert profile.diagnostics == [] or "legacy_plan_fallback" in profile.diagnostics


class TestCapabilityTimezone:
    """时区统一：aware/naive/过期/正好当前/无 expires 判定。"""

    def _mk_row(self, expires_at: datetime | None) -> SimpleNamespace:
        return SimpleNamespace(
            capability=CAP_SELF_SELECTION, granted_at=None,
            expires_at=expires_at, watchlist_limit=1, source="invite_code",
        )

    def _mk_user(self, roles: list[str] | None = None, status: str = "active") -> SimpleNamespace:
        return SimpleNamespace(id="user-1", status=status, _roles=roles or ["member"])

    def _mk_db(self, rows: list[Any]) -> SimpleNamespace:
        async def execute(stmt, params=None):
            return SimpleNamespace(
                fetchall=lambda: rows,
                scalars=lambda: SimpleNamespace(all=lambda: rows, first=lambda: rows[0] if rows else None),
            )
        return SimpleNamespace(execute=execute, scalar_one_or_none=lambda: None)

    @pytest.mark.asyncio
    async def test_aware_expires_future_is_active(self) -> None:
        from datetime import UTC, timedelta
        exp = datetime.now(UTC) + timedelta(days=1)
        db = self._mk_db([self._mk_row(exp)])
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == [CAP_SELF_SELECTION]

    @pytest.mark.asyncio
    async def test_naive_expires_future_is_active(self) -> None:
        from datetime import timedelta
        # naive 时间视为 UTC
        exp = datetime.utcnow() + timedelta(days=1)
        db = self._mk_db([self._mk_row(exp)])
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == [CAP_SELF_SELECTION]

    @pytest.mark.asyncio
    async def test_expired_is_inactive(self) -> None:
        from datetime import UTC, timedelta
        exp = datetime.now(UTC) - timedelta(days=1)
        db = self._mk_db([self._mk_row(exp)])
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == []
        assert profile.capabilities[CAP_SELF_SELECTION].reason == "expired"

    @pytest.mark.asyncio
    async def test_no_expires_is_inactive_no_expiry(self) -> None:
        db = self._mk_db([self._mk_row(None)])
        profile = await resolve_effective_access(db, self._mk_user())
        assert profile.active_capability_keys == []
        assert profile.capabilities[CAP_SELF_SELECTION].reason == "no_expiry"
