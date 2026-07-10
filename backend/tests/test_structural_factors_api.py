"""结构状态因子 API 单元测试。

验证维度：
1. 有效请求返回 200 + 正确结构
2. 无效 timeframe 返回 400
3. 无效 adj 返回 400
4. 不存在的 instrument 返回 200 + degraded_reasons（不报 404）
5. 响应包含 primary/secondary/relation/meta

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+asyncpg://... \
        pytest tests/test_structural_factors_api.py -v
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

# Import the app after env setup
from app.main import app
from tests.conftest import make_asgi_transport


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    """HTTP 客户端 fixture。"""
    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===== 1. 有效请求返回 200 + 正确结构 =====
@pytest.mark.asyncio
async def test_get_structural_factors_returns_200(client: AsyncClient) -> None:
    """有效请求返回 200 + primary/secondary/relation/meta。"""
    # Mock service to return synthetic data
    mock_result = {
        "primary": {"1d": {"dsa_segment": None, "swing_position": None}},
        "secondary": {"15m": {"dsa_segment": None, "swing_position": None}},
        "relation": {"trend_alignment": None, "momentum_alignment": None, "notes": []},
        "meta": {
            "as_of": "2026-07-04",
            "primary_lookback_bars": 250,
            "secondary_lookback_bars": 500,
            "degraded_reasons": [],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.structural_factors.compute_structural_factors",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000001/structural-factors"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "primary" in data
    assert "secondary" in data
    assert "relation" in data
    assert "meta" in data
    assert "degraded_reasons" in data["meta"]


# ===== 2. 无效 timeframe 返回 400 =====
@pytest.mark.asyncio
async def test_invalid_timeframe_returns_400(client: AsyncClient) -> None:
    """无效 primary_timeframe 返回 400。"""
    resp = await client.get(
        "/api/v1/instruments/00000000-0000-0000-0000-000000000001/structural-factors"
        "?primary_timeframe=2h"
    )
    assert resp.status_code == 400


# ===== 3. 无效 adj 返回 400 =====
@pytest.mark.asyncio
async def test_invalid_adj_returns_400(client: AsyncClient) -> None:
    """无效 adj 返回 400。"""
    resp = await client.get(
        "/api/v1/instruments/00000000-0000-0000-0000-000000000001/structural-factors"
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
        "primary": {"1d": {}},
        "secondary": {"15m": {}},
        "relation": {},
        "meta": {
            "as_of": "unknown",
            "primary_lookback_bars": 250,
            "secondary_lookback_bars": 500,
            "degraded_reasons": ["1d: get_bars failed: instrument not found"],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.structural_factors.compute_structural_factors",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000002/structural-factors"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["meta"]["degraded_reasons"]) > 0


# ===== 5. 响应包含完整 meta 结构 =====
@pytest.mark.asyncio
async def test_response_meta_structure(client: AsyncClient) -> None:
    """响应 meta 包含所有必需字段。"""
    mock_result = {
        "primary": {},
        "secondary": {},
        "relation": {},
        "meta": {
            "as_of": "2026-07-04",
            "primary_lookback_bars": 250,
            "secondary_lookback_bars": 500,
            "degraded_reasons": [],
            "warmup_notes": [],
        },
    }
    with patch(
        "app.api.structural_factors.compute_structural_factors",
        new_callable=AsyncMock,
        return_value=mock_result,
    ):
        resp = await client.get(
            "/api/v1/instruments/00000000-0000-0000-0000-000000000001/structural-factors"
        )
    meta = resp.json()["meta"]
    assert meta["as_of"] == "2026-07-04"
    assert meta["primary_lookback_bars"] == 250
    assert meta["secondary_lookback_bars"] == 500
    assert isinstance(meta["degraded_reasons"], list)
    assert isinstance(meta["warmup_notes"], list)


# ===== 模块自测入口 =====
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
