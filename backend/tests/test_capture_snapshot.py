"""Capture Snapshot API 测试（Phase C Task C.11.2）。

测试 GET /api/v1/capture/stocks/{instrument_id}/snapshot 端点：
1. 有效 Capture Token + 数据正常 → 返回完整快照（instrument/bars/indicators/events）
2. instrument_id 与 token 不匹配 → 返回 403
3. 无效 token → 返回 401
4. 普通访问 token → 返回 401（Capture API 只接受 capture token）

测试策略：
- 复用 conftest 的 db_session / test_instrument fixture
- [CP-V3-B] mock ChartSnapshotService.compute_bars_and_indicators（避免依赖真实行情数据）
  Phase B 重构后，capture 端点不再直接调用 MarketDataAggregationService/compute_all_indicators，
  而是通过 ChartSnapshotService 统一入口。
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
from app.services.chart_snapshot_service import ChartSnapshotResult
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


def _make_snapshot_result(
    bars_result: MagicMock | None = None,
    indicators: dict[str, Any] | None = None,
) -> ChartSnapshotResult:
    """[CP-V3-B] 构造 ChartSnapshotResult mock（Phase B 重构后 Capture/ChartSnapshot API 共用）。

    Phase B 后 capture/chart_snapshot 端点不再直接调 MarketDataAggregationService/
    compute_all_indicators，而是通过 ChartSnapshotService.compute_bars_and_indicators 统一入口。
    本 helper 封装 ChartSnapshotResult 构造，供测试 patch 使用。
    """
    if bars_result is None:
        bars_result = _make_empty_bars_result()
    df = bars_result.bars
    return ChartSnapshotResult(
        bars_result=bars_result,
        page_df=df,
        bars_display_frame={
            "display_hash": "mock_bars_hash",
            "actual_count": len(df),
            "first_time": None,
            "last_time": None,
            "adjustment_as_of": None,
        },
        indicators=indicators if indicators is not None else {"layers": [], "data": {}, "errors": {}},
        render_frame={
            "matched": True,
            "bars_hash": "mock_bars_hash",
            "indicators_hash": "mock_indicators_hash",
            "bars_count": len(df),
            "indicators_count": len(df),
        },
        spec=MagicMock(),  # spec 不被 capture/chart_snapshot 端点直接使用
        is_empty=df.empty,
        completed_through_iso=None,
    )


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
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=AsyncMock(return_value=_make_snapshot_result(bars_result, indicators_data)),
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
    async def test_capture_snapshot_realtime_timeframe_passthrough(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """请求 timeframe=15m 时，get_bars/include_realtime/_df_to_responses/compute_all_indicators 全部透传 15m。

        阻断验收：截图链路不得回退 _CAPTURE_TIMEFRAME（1d），必须保持盘中实时多周期一致。
        """
        from app.api.bars import _df_to_responses as real_df_to_responses
        from app.constants.indicator_contract import INDICATOR_BARS
        from app.models.user import User

        client, db = capture_client
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

        spy_snapshot = AsyncMock(
            return_value=_make_snapshot_result(bars_result, indicators_data)
        )
        spy_df = MagicMock(side_effect=real_df_to_responses)

        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=spy_snapshot,
        ), patch(
            "app.api.capture._df_to_responses",
            new=spy_df,
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot"
                "?timeframe=15m&force_refresh=1&capture=1",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        # [CP-V3-B] ChartSnapshotService 必须透传 timeframe=15m 且 include_realtime=True
        # 且 bars=INDICATOR_BARS["15m"]（Phase B 后由 Service 统一接收所有参数）
        assert spy_snapshot.await_count == 1
        snap_kwargs = spy_snapshot.call_args.kwargs
        assert snap_kwargs.get("timeframe") == "15m"
        assert snap_kwargs.get("include_realtime") is True
        assert snap_kwargs.get("bars") == INDICATOR_BARS["15m"]

        # _df_to_responses 必须按 15m 格式化（位置参数第 3 个为 timeframe）
        assert spy_df.call_count == 1
        df_args, _ = spy_df.call_args
        assert df_args[2] == "15m"

        # 响应 bars.timeframe 必须与请求一致（禁止回退 1d）
        assert data["bars"]["timeframe"] == "15m"

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
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=AsyncMock(return_value=_make_snapshot_result(bars_result, indicators_data)),
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

    # ============================================================
    # [CHANGE-20260720-Phase4 §四] indicator_view 参数测试
    # ============================================================

    @pytest.mark.asyncio
    async def test_capture_snapshot_indicator_view_smc_triggers_include_smc(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """indicator_view=smc → compute_all_indicators 透传 include_smc=True。

        阻断验收：smc 视图必须触发 SMC 算法计算（BOS/CHoCH/OB/EQH/EQL/trailing），
        否则前端 SMC 图层无数据可渲染。
        """
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

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        spy_snapshot = AsyncMock(
            return_value=_make_snapshot_result(bars_result, indicators_data)
        )
        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=spy_snapshot,
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot"
                "?indicator_view=smc",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        # [CP-V3-B] ChartSnapshotService 必须以 include_smc=True 调用
        assert spy_snapshot.await_count == 1
        snap_kwargs = spy_snapshot.call_args.kwargs
        assert snap_kwargs.get("include_smc") is True, \
            "indicator_view=smc 必须透传 include_smc=True"

        # 响应必须包含 indicator_view 与 include_smc 字段
        assert data["indicator_view"] == "smc"
        assert data["include_smc"] is True

    @pytest.mark.asyncio
    async def test_capture_snapshot_indicator_view_node_cluster_skips_smc(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """indicator_view=node_cluster → include_smc=False（不消耗 SMC CPU）。"""
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

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        spy_snapshot = AsyncMock(
            return_value=_make_snapshot_result(bars_result, indicators_data)
        )
        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=spy_snapshot,
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot"
                "?indicator_view=node_cluster",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        # [CP-V3-B] node_cluster 视图不应触发 SMC 计算（按需计算约束）
        assert spy_snapshot.await_count == 1
        snap_kwargs = spy_snapshot.call_args.kwargs
        assert snap_kwargs.get("include_smc") is False, \
            "indicator_view=node_cluster 必须透传 include_smc=False（按需计算）"

        assert data["indicator_view"] == "node_cluster"
        assert data["include_smc"] is False

    @pytest.mark.asyncio
    async def test_capture_snapshot_indicator_view_bollinger_skips_smc(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """indicator_view=bollinger → include_smc=False。"""
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

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        spy_snapshot = AsyncMock(
            return_value=_make_snapshot_result(bars_result, indicators_data)
        )
        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=spy_snapshot,
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot"
                "?indicator_view=bollinger",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        assert spy_snapshot.await_count == 1
        snap_kwargs = spy_snapshot.call_args.kwargs
        assert snap_kwargs.get("include_smc") is False

        assert data["indicator_view"] == "bollinger"
        assert data["include_smc"] is False

    @pytest.mark.asyncio
    async def test_capture_snapshot_indicator_view_invalid_falls_back_to_default(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """indicator_view=invalid → 回退到 DEFAULT_INDICATOR_VIEW（node_cluster），不抛 400。

        阻断验收：截图链路必须鲁棒，非法值不阻塞截图（advice.md 第六节）。
        """
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

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=AsyncMock(return_value=_make_snapshot_result(bars_result, indicators_data)),
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot"
                "?indicator_view=invalid_view",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        # 非法值回退到 DEFAULT_INDICATOR_VIEW（node_cluster）
        assert data["indicator_view"] == "node_cluster"
        assert data["include_smc"] is False

    @pytest.mark.asyncio
    async def test_capture_snapshot_indicator_view_missing_falls_back_to_default(
        self, capture_client: tuple[AsyncClient, AsyncSession], test_instrument,
    ) -> None:
        """indicator_view 缺失 → 回退到 DEFAULT_INDICATOR_VIEW（node_cluster）。

        向后兼容：旧 capture URL 不携带 indicator_view 时，按默认视图渲染。
        """
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

        headers = _capture_token_headers(user.id, test_instrument.id)
        bars_result = _make_bars_result_with_data(test_instrument.id)
        indicators_data: dict[str, Any] = {"layers": [], "data": {}, "errors": {}}

        with patch(
            "app.api.capture.ChartSnapshotService.compute_bars_and_indicators",
            new=AsyncMock(return_value=_make_snapshot_result(bars_result, indicators_data)),
        ):
            resp = await client.get(
                f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
                headers=headers,
            )

        assert resp.status_code == 200, f"响应体: {resp.text}"
        data = resp.json()

        # 缺失 indicator_view 回退到 node_cluster（DEFAULT_INDICATOR_VIEW）
        assert data["indicator_view"] == "node_cluster"
        assert data["include_smc"] is False


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
