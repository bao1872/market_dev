"""股票截图服务 - 使用 Playwright 截取前端个股详情页。

用法：
    from app.services.stock_capture_service import capture_stock_chart
    png_bytes = await capture_stock_chart(
        symbol="600519",
        event_id="...",
        token="<short_lived_jwt>",
        frontend_base_url="http://localhost:5173",
    )

设计要点：
- 访问 /stock/{symbol}?source=watchlist&strategy=watchlist_monitor&event_id=...&capture=feishu&token=...
- 等待 data-render-ready="true"（禁止固定 sleep）
- 截取 data-testid="stock-detail-capture" 区域
- 返回 PNG bytes
- 失败时抛出 StockCaptureError，不吞没异常
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("stock_capture_service")

# 默认渲染超时（秒）
_DEFAULT_RENDER_TIMEOUT = 30_000  # 30s
# 默认截图超时（秒）
_DEFAULT_SCREENSHOT_TIMEOUT = 10_000  # 10s


class StockCaptureError(RuntimeError):
    """截图失败异常。"""


async def capture_stock_chart(
    symbol: str,
    event_id: UUID | str,
    token: str,
    frontend_base_url: str,
    render_timeout_ms: int = _DEFAULT_RENDER_TIMEOUT,
    screenshot_timeout_ms: int = _DEFAULT_SCREENSHOT_TIMEOUT,
) -> bytes:
    """截取个股详情页指定区域，返回 PNG bytes。

    Args:
        symbol: 股票代码
        event_id: 事件 ID
        token: 短期 JWT（URL 参数传递）
        frontend_base_url: 前端 base URL（如 http://localhost:5173）
        render_timeout_ms: 等待 data-render-ready="true" 的超时（毫秒）
        screenshot_timeout_ms: 截图操作超时（毫秒）

    Returns:
        PNG 图片 bytes

    Raises:
        StockCaptureError: 截图失败（页面不可达、渲染超时、元素不存在等）
    """
    url = (
        f"{frontend_base_url.rstrip('/')}/stock/{symbol}?"
        f"source=watchlist&strategy=watchlist_monitor&event_id={event_id}&"
        f"capture=feishu&token={token}"
    )

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 900})
            page = await context.new_page()

            try:
                logger.info("截图服务访问页面: symbol=%s event_id=%s", symbol, event_id)
                await page.goto(url, wait_until="networkidle", timeout=render_timeout_ms)

                # 等待截图区域渲染完成（data-render-ready="true"）
                try:
                    await page.wait_for_selector(
                        '[data-testid="stock-detail-capture"][data-render-ready="true"]',
                        timeout=render_timeout_ms,
                        state="attached",
                    )
                except PlaywrightTimeoutError as e:
                    raise StockCaptureError(
                        f"截图区域未在 {render_timeout_ms}ms 内渲染完成: symbol={symbol}, url={url}"
                    ) from e

                # 截取指定元素
                element = page.locator('[data-testid="stock-detail-capture"]')
                try:
                    png_bytes = await element.screenshot(
                        type="png",
                        timeout=screenshot_timeout_ms,
                    )
                except PlaywrightTimeoutError as e:
                    raise StockCaptureError(
                        f"截图操作超时: symbol={symbol}, timeout={screenshot_timeout_ms}ms"
                    ) from e

                if not png_bytes or len(png_bytes) < 100:
                    raise StockCaptureError(
                        f"截图结果异常（空或过小）: symbol={symbol}, size={len(png_bytes) if png_bytes else 0}"
                    )

                logger.info(
                    "截图成功: symbol=%s event_id=%s size=%d bytes",
                    symbol, event_id, len(png_bytes),
                )
                return png_bytes
            finally:
                await context.close()
                await browser.close()
    except StockCaptureError:
        raise
    except Exception as e:
        raise StockCaptureError(
            f"截图过程异常: symbol={symbol}, event_id={event_id}: {e}"
        ) from e


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不实际启动浏览器）
    import inspect

    print(f"capture_stock_chart={capture_stock_chart}")
    assert inspect.iscoroutinefunction(capture_stock_chart)
    print("OK")
