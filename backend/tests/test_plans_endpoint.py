"""GET /plans 公开端点测试。

验证公开端点返回 Alembic 048 初始化的 active 套餐列表。
"""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_plans_returns_seeded_plans(client):
    """公开端点返回 Alembic 048 初始化的套餐列表。"""
    resp = await client.get("/plans")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 2

    codes = {p["plan_code"] for p in data}
    assert "observe_20" in codes
    assert "research_50" in codes

    for p in data:
        assert "plan_code" in p
        assert "display_name" in p
        assert "monitor_limit" in p
        assert "notification_channel_limit" in p
        assert "message_retention_days" in p
        assert "features" in p
        assert isinstance(p["features"], list)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
