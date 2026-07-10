"""Capture Snapshot API 测试（Phase C Task C.11.2）。

测试 GET /api/v1/capture/stocks/{instrument_id}/snapshot 端点：
1. 有效 Capture Token + 数据正常 → 返回完整快照（instrument/bars/indicators/events）
2. instrument_id 与 token 不匹配 → 返回 403
3. 无效 token → 返回 401
4. 普通访问 token → 返回 401（Capture API 只接受 capture token）

测试策略：
- 复用 conftest 的 db_session / test_instrument fixture
- mock MarketDataAggregationService.get_bars 与 compute_all_indicators（避免依赖真实行情数据）
- 通过 ASGITransport + AsyncClient 调用真实 HTTP 端点
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_capture_token
from app.main import app
from tests.conftest import make_asgi_transport


def _capture_token_headers(
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    scope: str = "stock_detail_capture",
) -> dict[str, str]:
    """生成 capture token 的 Bearer 认证头。"""
    token = create_capture_token(
        subject=str(user_id),
        event_id=str(instrument_id),
        expires_delta=timedelta(minutes=5),
        scope=scope,
        instrument_id=str(instrument_id),
        user_id=str(user_id),
    )
    return {"Authorization": f"Bearer {token}"}


def _make_empty_bars_result() -> MagicMock:
    """构造空 BarAggregationResult mock（Capture API 行情聚合返回空数据）。"""
    mock = MagicMock()
    mock.bars = pd.DataFrame()
    mock.data_source = "db"
    mock.as_of = datetime.now(UTC)
    mock.is_partial = False
    mock.last_persisted_bar_time = None
    mock.last_live_bar_time = None
    mock.freshness_seconds = 0.0
    mock.degraded = False
    mock.degraded_reason = None
    mock.cache_hit = False
    return mock


def _make_bars_result_with_data(instrument_id: uuid.UUID) -> MagicMock:
    """构造含 1 条 bar 数据的 BarAggregationResult mock。"""
    mock = MagicMock()
    mock.bars = pd.DataFrame(
        [
            {
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.2,
                "volume": 1000000.0,
                "amount": 10200000.0,
                "adj_factor": 1.0,
            }
        ],
        index=pd.to_datetime(["2026-06-30"]),
    )
    mock.bars.index.name = "trade_date"
    mock.data_source = "db"
    mock.as_of = datetime.now(UTC)
    mock.is_partial = False
    mock.last_persisted_bar_time = pd.Timestamp("2026-06-30")
    mock.last_live_bar_time = None
    mock.freshness_seconds = 10.0
    mock.degraded = False
    mock.degraded_reason = None
    mock.cache_hit = False
    return mock


@pytest_asyncio.fixture
async def capture_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


# ============================================================
# Capture Snapshot API 测试
# ============================================================


class TestCaptureSnapshot:
    """Capture Snapshot API 测试（C.11.2）。"""

    @pytest.mark.asyncio
    async def test_capture_snapshot_success(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """有效 Capture Token + 数据正常 → 返回完整快照。"""
        client, db = capture_client

        # 创建一个临时用户用于 token
        from app.models.user import User
        user = User(
            id=uuid.uuid4(),
            email=f"capture_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        db.add(user)
        await db.flush()

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data = {
            "layers": [{"key": "watchlist_monitor", "name": "监控指标"}],
            "data": {"watchlist_monitor": {"current_price": [10.2]}},
            "errors": {},
        }

        with patch(
            "app.api.capture.MarketDataAggregationService.get_bars",
            new=AsyncMock(return_value=bars_result),
        ), patch(
            "app.api.capture.compute_all_indicators",
            new=AsyncMock(return_value=indicators_data),
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()
        # 验证返回结构
        assert "instrument" in data
        assert "bars" in data
        assert "indicators" in data
        assert "events" in data
        assert "snapshot_time" in data
        assert "capture" in data
        # instrument 字段
        assert data["instrument"]["id"] == str(test_instrument.id)
        assert data["instrument"]["symbol"] == test_instrument.symbol
        # bars 字段
        assert data["bars"]["timeframe"] == "1d"
        assert data["bars"]["adj"] == "qfq"
        assert data["bars"]["total"] == 1
        assert len(data["bars"]["items"]) == 1
        # indicators 字段
        assert "layers" in data["indicators"]
        assert "data" in data["indicators"]
        # capture 元信息
        assert data["capture"]["scope"] == "stock_detail_capture"
        assert data["capture"]["user_id"] == str(user.id)
        assert data["capture"]["event_id"] == str(test_instrument.id)

    @pytest.mark.asyncio
    async def test_capture_snapshot_instrument_id_mismatch_403(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """instrument_id 与 token 不匹配 → 403。"""
        client, db = capture_client

        from app.models.user import User
        user = User(
            id=uuid.uuid4(),
            email=f"capture_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        db.add(user)
        await db.flush()

        # token 中的 instrument_id 与 path 不同
        other_id = uuid.uuid4()
        headers = _capture_token_headers(user.id, other_id)

        resp = await client.get(
            f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
            headers=headers,
        )

        assert resp.status_code == 403
        assert "不匹配" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_capture_snapshot_invalid_token_401(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """无效 token → 401。"""
        client, _ = capture_client

        resp = await client.get(
            f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
            headers={"Authorization": "Bearer invalid-token-string"},
        )

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_capture_snapshot_access_token_rejected_401(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """普通 access token → 401（Capture API 只接受 capture token）。"""
        client, db = capture_client

        from app.models.user import User
        user = User(
            id=uuid.uuid4(),
            email=f"capture_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        db.add(user)
        await db.flush()

        access_token = create_access_token(str(user.id))
        headers = {"Authorization": f"Bearer {access_token}"}

        resp = await client.get(
            f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
            headers=headers,
        )

        assert resp.status_code == 401
        detail = resp.json()["detail"]
        assert "token 类型错误" in detail or "需要 capture token" in detail

    @pytest.mark.asyncio
    async def test_capture_snapshot_query_param_token(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """通过 query 参数 token 访问也应成功（前端 /capture/stock/:symbol?...&token=... 场景）。"""
        client, db = capture_client

        from app.models.user import User
        user = User(
            id=uuid.uuid4(),
            email=f"capture_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        db.add(user)
        await db.flush()

        # 通过 query 参数传递 token（不通过 Authorization header）
        token = create_capture_token(
            subject=str(user.id),
            event_id=str(test_instrument.id),
            expires_delta=timedelta(minutes=5),
            scope="stock_detail_capture",
            instrument_id=str(test_instrument.id),
            user_id=str(user.id),
        )
        bars_result = _make_empty_bars_result()
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        with patch(
            "app.api.capture.MarketDataAggregationService.get_bars",
            new=AsyncMock(return_value=bars_result),
        ), patch(
            "app.api.capture.compute_all_indicators",
            new=AsyncMock(return_value=indicators_data),
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot?token={token}",
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()
        assert data["instrument"]["id"] == str(test_instrument.id)
        assert data["bars"]["total"] == 0  # 空 bars

    @pytest.mark.asyncio
    async def test_capture_snapshot_instrument_not_found_404(
        self, capture_client: tuple[AsyncClient, AsyncSession],
    ) -> None:
        """标的不存在 → 404（token instrument_id 与 path 一致但 DB 无此标的）。"""
        client, db = capture_client

        from app.models.user import User
        user = User(
            id=uuid.uuid4(),
            email=f"capture_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
        )
        db.add(user)
        await db.flush()

        fake_instrument_id = uuid.uuid4()
        headers = _capture_token_headers(user.id, fake_instrument_id)

        resp = await client.get(
            f"/api/v1/capture/stocks/{fake_instrument_id}/snapshot",
            headers=headers,
        )

        assert resp.status_code == 404


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
