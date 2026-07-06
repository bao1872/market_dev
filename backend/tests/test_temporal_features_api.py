"""时序特征 API 单元测试。

验证维度：
1. 有效请求返回 200 + 正确结构（daily_context/m15_response/derived_relation/meta）
2. 无效 timeframe 返回 400
3. 无效 adj 返回 400
4. 不存在的 instrument 返回 200 + degraded_reasons（不报 404）
5. 响应包含完整 meta 结构

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_temporal_features_api.py -v
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP 客户端 fixture。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===== 1. 有效请求返回 200 + 正确结构 =====
@pytest.mark.asyncio
async def test_get_temporal_features_returns_200(client: AsyncClient) -> None:
    """有效请求返回 200 + daily_context/m15_response/derived_relation/meta。"""
    mock_result = {
        "daily_context": {
            "daily_dsa_dir": 1,
            "daily_dsa_segment_duration_percentile": 0.65,
            "daily_dsa_slope_atr_per_bar": 0.02,
            "daily_dsa_efficiency_0_1": 0.8,
            "daily_price_position_in_swing_0_1": 0.7,
            "daily_distance_to_swing_high_atr": -1.5,
            "daily_distance_to_node_above_atr": -2.0,
            "daily_sqzmom_change_since_segment_start": 0.3,
            "daily_volume_percentile_change_since_segment_start": 0.1,
        },
        "m15_response": {
            "m15_price_position_in_swing_0_1": 0.5,
            "m15_position_change_since_swing_anchor": 0.05,
            "m15_distance_to_swing_high_atr": -0.8,
            "m15_distance_to_swing_low_atr": 1.2,
            "m15_sqzmom_change_since_swing_anchor": 0.02,
            "m15_sqzmom_abs_percentile": 0.9,
            "m15_sqz_off": True,
            "m15_bb_bandwidth_change_since_swing_anchor": -0.1,
            "m15_volume_percentile_change_since_swing_anchor": 0.15,
        },
        "derived_relation": {
            "m15_position_relative_to_daily": -0.2,
            "m15_response_direction_relative_to_daily": "aligned",
            "m15_response_intensity": 0.08,
        },
        "meta": {
            "as_of": "2026-07-05",
            "primary_timeframe": "1d",
            "secondary_timeframe": "15m",
            "degraded_reasons": [],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.temporal_features.compute_temporal_features",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000001/temporal-features"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "daily_context" in data
    assert "m15_response" in data
    assert "derived_relation" in data
    assert "meta" in data
    assert "degraded_reasons" in data["meta"]
    assert "warmup_notes" in data["meta"]
    # daily_context 9 字段
    assert len(data["daily_context"]) == 9
    # m15_response 9 字段
    assert len(data["m15_response"]) == 9
    # derived_relation 3 字段
    assert len(data["derived_relation"]) == 3


# ===== 2. 无效 timeframe 返回 400 =====
@pytest.mark.asyncio
async def test_invalid_timeframe_returns_400(client: AsyncClient) -> None:
    """无效 primary_timeframe 返回 400。"""
    resp = await client.get(
        "/api/v1/instruments/00000000-0000-0000-0000-000000000001/temporal-features"
        "?primary_timeframe=2h"
    )
    assert resp.status_code == 400


# ===== 3. 无效 adj 返回 400 =====
@pytest.mark.asyncio
async def test_invalid_adj_returns_400(client: AsyncClient) -> None:
    """无效 adj 返回 400。"""
    resp = await client.get(
        "/api/v1/instruments/00000000-0000-0000-0000-000000000001/temporal-features"
        "?adj=hfq"
    )
    assert resp.status_code == 400


# ===== 4. 不存在的 instrument 返回 200 + degraded_reasons =====
@pytest.mark.asyncio
async def test_nonexistent_instrument_returns_200_degraded(
    client: AsyncClient,
) -> None:
    """不存在的 instrument 返回 200 + degraded_reasons（不报 404）。

    服务层会捕获 get_bars 失败，返回 degraded 结构。
    """
    mock_result = {
        "daily_context": dict.fromkeys([
            "daily_dsa_dir", "daily_dsa_segment_duration_percentile",
            "daily_dsa_slope_atr_per_bar", "daily_dsa_efficiency_0_1",
            "daily_price_position_in_swing_0_1", "daily_distance_to_swing_high_atr",
            "daily_distance_to_node_above_atr", "daily_sqzmom_change_since_segment_start",
            "daily_volume_percentile_change_since_segment_start",
        ]),
        "m15_response": dict.fromkeys([
            "m15_price_position_in_swing_0_1", "m15_position_change_since_swing_anchor",
            "m15_distance_to_swing_high_atr", "m15_distance_to_swing_low_atr",
            "m15_sqzmom_change_since_swing_anchor", "m15_sqzmom_abs_percentile",
            "m15_sqz_off", "m15_bb_bandwidth_change_since_swing_anchor",
            "m15_volume_percentile_change_since_swing_anchor",
        ]),
        "derived_relation": {
            "m15_position_relative_to_daily": None,
            "m15_response_direction_relative_to_daily": None,
            "m15_response_intensity": None,
        },
        "meta": {
            "as_of": "unknown",
            "primary_timeframe": "1d",
            "secondary_timeframe": "15m",
            "degraded_reasons": ["1d: bars is None or empty", "15m: bars is None or empty"],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.temporal_features.compute_temporal_features",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000002/temporal-features"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["meta"]["degraded_reasons"]) > 0


# ===== 5. 响应包含完整 meta 结构 =====
@pytest.mark.asyncio
async def test_response_meta_structure(client: AsyncClient) -> None:
    """响应 meta 包含所有必需字段。"""
    mock_result = {
        "daily_context": {},
        "m15_response": {},
        "derived_relation": {},
        "meta": {
            "as_of": "2026-07-05",
            "primary_timeframe": "1d",
            "secondary_timeframe": "15m",
            "degraded_reasons": [],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.temporal_features.compute_temporal_features",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000001/temporal-features"
        )
    meta = resp.json()["meta"]
    assert meta["as_of"] == "2026-07-05"
    assert meta["primary_timeframe"] == "1d"
    assert meta["secondary_timeframe"] == "15m"
    assert isinstance(meta["degraded_reasons"], list)
    assert isinstance(meta["warmup_notes"], list)


# ===== 6. as_of != latest 返回 400 =====
@pytest.mark.asyncio
async def test_invalid_as_of_returns_400(client: AsyncClient) -> None:
    """V1 只支持 as_of=latest，其他值返回 400。"""
    resp = await client.get(
        "/api/v1/instruments/00000000-0000-0000-0000-000000000001/temporal-features"
        "?as_of=2025-01-01"
    )
    assert resp.status_code == 400
    assert "as_of" in resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
