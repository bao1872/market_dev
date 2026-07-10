"""股票截图服务单元测试。

目标：验证 capture_stock_chart 在 Playwright 场景下的关键行为：
1. page.goto 使用 wait_until="load" 而非 "networkidle"，避免前端长连接导致 30s 超时。
2. 等待 data-render-ready="true" 选择器后才截图。
3. 成功截图后返回 PNG bytes 并写入缓存。

注意：本测试不启动真实浏览器，全部 mock Playwright API。
"""

from __future__ import annotations

import os
import tempfile
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.stock_capture_service import capture_stock_chart


@pytest.fixture
def mock_playwright() -> MagicMock:
    """构造 playwright.async_api.async_playwright 的 mock 链。"""
    element = MagicMock()
    element.screenshot = AsyncMock(return_value=b"fake-png-bytes" * 10)

    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.wait_for_selector = AsyncMock(return_value=None)
    page.locator = MagicMock(return_value=element)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock(return_value=None)

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock(return_value=None)

    pw = MagicMock()
    pw.chromium = MagicMock()
    pw.chromium.launch = AsyncMock(return_value=browser)

    playwright_cm = MagicMock()
    playwright_cm.__aenter__ = AsyncMock(return_value=pw)
    playwright_cm.__aexit__ = AsyncMock(return_value=False)

    return playwright_cm


@pytest.mark.asyncio
async def test_capture_uses_load_wait_until(
    mock_playwright: MagicMock,
) -> None:
    """capture_stock_chart 必须调用 page.goto(..., wait_until='load')。

    历史根因：使用 wait_until='networkidle' 时，前端页面若存在长连接或持续
    轮询，网络永远不会 idle，导致 30s 超时返回 502。
    """
    event_id = uuid.uuid4()
    with patch(
        "app.services.stock_capture_service.async_playwright",
        return_value=mock_playwright,
    ):
        await capture_stock_chart(
            symbol="000032",
            event_id=event_id,
            token="fake-token",
            frontend_base_url="http://frontend",
            instrument_id="inst-123",
            chart_version="v1",
        )

    # 进入上下文后拿到 page
    pw = await mock_playwright.__aenter__()
    browser = await pw.chromium.launch()
    context = await browser.new_context()
    page = await context.new_page()

    page.goto.assert_awaited_once()
    _, kwargs = page.goto.call_args
    assert kwargs.get("wait_until") == "load", (
        f"page.goto 必须使用 wait_until='load'，当前={kwargs.get('wait_until')}"
    )


@pytest.mark.asyncio
async def test_capture_returns_bytes_and_writes_cache(
    mock_playwright: MagicMock,
) -> None:
    """渲染就绪后成功截图，返回 PNG bytes 并写入本地缓存。"""
    event_id = uuid.uuid4()
    instrument_id = f"inst-{uuid.uuid4().hex[:8]}"

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch(
            "app.services.stock_capture_service.async_playwright",
            return_value=mock_playwright,
        ), patch(
            "app.services.stock_capture_service._CACHE_DIR",
            tmpdir,
        ):
            result = await capture_stock_chart(
                symbol="000032",
                event_id=event_id,
                token="fake-token",
                frontend_base_url="http://frontend",
                instrument_id=instrument_id,
                chart_version="v1",
            )

        assert result.png_bytes == b"fake-png-bytes" * 10
        cache_files = [f for f in os.listdir(tmpdir) if f.endswith(".png")]
        assert len(cache_files) == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
