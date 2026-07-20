"""股票截图服务单元测试。

目标：验证 capture_stock_chart 在 Playwright 场景下的关键行为：
1. page.goto 使用 wait_until="load" 而非 "networkidle"，避免前端长连接导致 30s 超时。
2. 等待 data-render-ready="true" 选择器后才截图。
3. 成功截图后返回 PNG bytes 并写入缓存。
4. [CHANGE-20260719-002 §三] PNG 内容校验失败时抛出 StockCaptureError。

注意：本测试不启动真实浏览器，全部 mock Playwright API。
PNG 校验使用真实 validate_png 函数，mock 返回有效的渐变 PNG bytes。
"""

from __future__ import annotations

import os
import struct
import tempfile
import uuid
import zlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.png_validator import _PNG_SIGNATURE
from app.services.stock_capture_service import StockCaptureError, capture_stock_chart


def _make_valid_gradient_png(width: int = 500, height: int = 500) -> bytes:
    """构造一个有效的渐变 PNG（用于 mock Playwright 截图返回值）。

    生成 RGBA 渐变 PNG，通过 validate_png 校验。
    """

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b""
    for y in range(height):
        raw += b"\x00"  # filter type None
        for x in range(width):
            r = (x * 255) // width
            g = (y * 255) // height
            b = 128
            raw += bytes([r, g, b, 255])
    idat = zlib.compress(raw)
    return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _make_solid_png(width: int = 300, height: int = 300) -> bytes:
    """构造一个纯色 PNG（用于测试 PNG 校验失败场景）。"""

    def chunk(ctype: bytes, data: bytes) -> bytes:
        length = struct.pack(">I", len(data))
        crc = struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        return length + ctype + data + crc

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    raw = b""
    for _ in range(height):
        raw += b"\x00"  # filter type None
        raw += bytes([100, 150, 200, 255]) * width
    idat = zlib.compress(raw)
    return _PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.fixture
def mock_playwright() -> MagicMock:
    """构造 playwright.async_api.async_playwright 的 mock 链。"""
    element = MagicMock()
    # [CHANGE-20260719-002 §三] 使用有效的渐变 PNG 替代 fake bytes
    element.screenshot = AsyncMock(return_value=_make_valid_gradient_png())

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
    valid_png = _make_valid_gradient_png()

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

        assert result.png_bytes == valid_png
        cache_files = [f for f in os.listdir(tmpdir) if f.endswith(".png")]
        assert len(cache_files) == 1


@pytest.mark.asyncio
async def test_capture_rejects_solid_color_png() -> None:
    """[CHANGE-20260719-002 §三] 截图为纯色时必须抛出 StockCaptureError。

    PROMPT.md §3 要求 4：非全透明 / 非纯色。
    """
    solid_png = _make_solid_png(300, 300)

    element = MagicMock()
    element.screenshot = AsyncMock(return_value=solid_png)

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

    event_id = uuid.uuid4()
    with patch(
        "app.services.stock_capture_service.async_playwright",
        return_value=playwright_cm,
    ):
        with pytest.raises(StockCaptureError, match="PNG 内容校验失败"):
            await capture_stock_chart(
                symbol="000032",
                event_id=event_id,
                token="fake-token",
                frontend_base_url="http://frontend",
                instrument_id="inst-123",
                chart_version="v1",
            )


@pytest.mark.asyncio
async def test_capture_rejects_too_small_png() -> None:
    """[CHANGE-20260719-002 §三] 截图字节数过小时必须抛出 StockCaptureError。

    PROMPT.md §3 要求 3：合理字节数。
    """
    element = MagicMock()
    # 50 字节，远小于 100 字节最小阈值
    element.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)

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

    event_id = uuid.uuid4()
    with patch(
        "app.services.stock_capture_service.async_playwright",
        return_value=playwright_cm,
    ):
        with pytest.raises(StockCaptureError, match="截图结果异常"):
            await capture_stock_chart(
                symbol="000032",
                event_id=event_id,
                token="fake-token",
                frontend_base_url="http://frontend",
                instrument_id="inst-123",
                chart_version="v1",
            )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
