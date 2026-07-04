"""指标 API 参数契约测试。

验证 /api/v1/instruments/{instrument_id}/indicators 的 bars 参数上限
与 Node Cluster 契约对齐（最大 4000）。
"""

from __future__ import annotations

import uuid

import pytest

TEST_INSTRUMENT_ID = uuid.UUID("12345678-1234-1234-1234-123456789012")


@pytest.mark.asyncio
async def test_indicators_bars_4000_ok(client) -> None:
    """测试 indicators bars=4000 通过参数校验（DB 无数据时返回 500 或 200）。"""
    response = await client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/indicators",
        params={"timeframe": "1d", "bars": 4000},
    )
    # 修复 le=500 后，4000 应通过 FastAPI 参数校验；后续可能因无数据返回 500
    assert response.status_code != 422


@pytest.mark.asyncio
async def test_indicators_bars_over_4000_rejected(client) -> None:
    """测试 indicators bars>4000 被 422 拒绝。"""
    response = await client.get(
        f"/api/v1/instruments/{TEST_INSTRUMENT_ID}/indicators",
        params={"timeframe": "1d", "bars": 4001},
    )
    assert response.status_code == 422


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
