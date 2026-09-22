"""权限模型 V2 - 显式 capability 生命周期（纯单元可验证部分）。

覆盖 access-profile 状态序列化（active/expired/revoked）与 default_route 必填合同。
revoke/regrant/ensure 的完整 DB 交互验证需远程验证数据库。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.effective_access_service import (
    CapabilityState,
    capabilities_to_serializable,
)


def _now() -> datetime:
    return datetime.now(UTC)


class TestAccessProfileSerialization:
    """access-profile 状态序列化（active/expired/revoked）。"""

    def test_state_serialization(self) -> None:
        now = _now()
        caps = {
            "self_selection": CapabilityState(
                key="self_selection", active=True, expires_at=now + timedelta(days=10),
                watchlist_limit=5, source="admin_grant", reason="active",
            ),
            "market_data": CapabilityState(
                key="market_data", active=False, expires_at=now - timedelta(days=1),
                source="admin_revoke", reason="explicitly_revoked",
            ),
            "research_replay": CapabilityState(
                key="research_replay", active=False, expires_at=now - timedelta(days=2),
                source="legacy_materialized", reason="expired",
            ),
        }
        serialized = capabilities_to_serializable(caps)
        assert serialized["self_selection"]["active"] is True
        assert serialized["market_data"]["reason"] == "explicitly_revoked"
        assert serialized["market_data"]["active"] is False
        assert serialized["research_replay"]["source"] == "legacy_materialized"

    def test_default_route_all_paths(self) -> None:
        from app.models.user_capability import (
            CAPABILITY_MARKET_DATA,
            CAPABILITY_MARKET_REVIEW,
            CAPABILITY_RESEARCH_REPLAY,
            CAPABILITY_SELF_SELECTION,
        )
        from app.services.effective_access_service import (
            DEFAULT_ROUTE_ADMIN,
            DEFAULT_ROUTE_AUCTION,
            DEFAULT_ROUTE_FORBIDDEN,
            DEFAULT_ROUTE_MARKET,
            DEFAULT_ROUTE_MARKET_WATCHLIST,
            DEFAULT_ROUTE_REVIEW,
            compute_default_route,
        )

        def cap(k: str, active: bool) -> CapabilityState:
            return CapabilityState(
                key=k, active=active,
                expires_at=_now() + timedelta(days=1) if active else _now() - timedelta(days=1),
            )

        # admin
        assert compute_default_route(True, {}) == DEFAULT_ROUTE_ADMIN
        # 无权限
        assert compute_default_route(False, {}) == DEFAULT_ROUTE_FORBIDDEN
        # 仅 self_selection
        assert compute_default_route(False, {CAPABILITY_SELF_SELECTION: cap(CAPABILITY_SELF_SELECTION, True)}) == DEFAULT_ROUTE_MARKET_WATCHLIST
        # 仅 market_data
        assert compute_default_route(False, {CAPABILITY_MARKET_DATA: cap(CAPABILITY_MARKET_DATA, True)}) == DEFAULT_ROUTE_MARKET
        # 仅 research_replay（竞价分析）
        assert compute_default_route(False, {CAPABILITY_RESEARCH_REPLAY: cap(CAPABILITY_RESEARCH_REPLAY, True)}) == DEFAULT_ROUTE_AUCTION
        # 仅 market_review（复盘分析，独立 capability）→ /review
        assert compute_default_route(False, {CAPABILITY_MARKET_REVIEW: cap(CAPABILITY_MARKET_REVIEW, True)}) == DEFAULT_ROUTE_REVIEW
        # self_selection + market_data
        assert compute_default_route(
            False,
            {CAPABILITY_SELF_SELECTION: cap(CAPABILITY_SELF_SELECTION, True), CAPABILITY_MARKET_DATA: cap(CAPABILITY_MARKET_DATA, True)},
        ) == DEFAULT_ROUTE_MARKET
