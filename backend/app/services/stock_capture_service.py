"""股票截图服务 - 使用 Playwright 截取前端个股详情页。

用法：
    from app.services.stock_capture_service import capture_stock_chart
    png_bytes = await capture_stock_chart(
        symbol="600519",
        event_id="...",
        token="<short_lived_jwt>",
        frontend_base_url="http://localhost:5173",
        instrument_id="<uuid>",   # 可选，用于缓存 key
        chart_version="v1",       # 可选，图表版本号，默认 v1
    )

设计要点：
- 访问 /stock/{symbol}?source=watchlist&strategy=watchlist_monitor&event_id=...&capture=feishu&token=...
- 等待 data-render-ready="true"（禁止固定 sleep）
- 截取 data-testid="stock-detail-capture" 区域
- 返回 PNG bytes
- 失败时抛出 StockCaptureError，不吞没异常

截图缓存（任务 6.1）：
- 缓存 key：event_id + instrument_id + chart_version
- 缓存 TTL：600 秒（_CACHE_TTL_SECONDS）
- 缓存存储：本地文件系统（CAPTURE_CACHE_DIR，默认 /app/static/captures/cache）
- 缓存命中且未过期：直接读取文件返回 bytes，不启动浏览器
- 缓存未命中或已过期：重新截图并写入缓存文件
- 仅当提供 instrument_id 时启用缓存（向后兼容）
"""

from __future__ import annotations

import logging
import os
import time
from uuid import UUID

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

logger = logging.getLogger("stock_capture_service")

# 默认渲染超时（秒）
_DEFAULT_RENDER_TIMEOUT = 30_000  # 30s
# 默认截图超时（秒）
_DEFAULT_SCREENSHOT_TIMEOUT = 10_000  # 10s

# [screenshot-cache] - 截图缓存配置（任务 6.1）
# 缓存目录：env CAPTURE_CACHE_DIR，默认位于 captures 目录下的 cache 子目录
_CACHE_DIR = os.getenv(
    "CAPTURE_CACHE_DIR",
    os.path.join(os.getenv("CAPTURE_STATIC_DIR", "/app/static/captures"), "cache"),
)
# 缓存 TTL（秒）：10 分钟
_CACHE_TTL_SECONDS = 600


class StockCaptureError(RuntimeError):
    """截图失败异常。"""


def _build_cache_key(event_id: UUID | str, instrument_id: str, chart_version: str) -> str:
    """构建截图缓存 key。

    key = {event_id}_{instrument_id}_{chart_version}
    使用下划线分隔，避免与文件系统路径冲突。
    """
    return f"{event_id}_{instrument_id}_{chart_version}"


def _read_cache(cache_path: str) -> bytes | None:
    """读取缓存文件，若不存在或已过期返回 None。

    过期判定：文件 mtime + TTL < 当前时间。
    """
    try:
        if not os.path.exists(cache_path):
            return None
        mtime = os.path.getmtime(cache_path)
        if time.time() - mtime > _CACHE_TTL_SECONDS:
            logger.debug("缓存已过期: %s", cache_path)
            return None
        with open(cache_path, "rb") as f:
            data = f.read()
        if not data or len(data) < 100:
            logger.debug("缓存文件异常（空或过小）: %s", cache_path)
            return None
        logger.info("缓存命中: %s size=%d", cache_path, len(data))
        return data
    except OSError as exc:
        logger.debug("读取缓存失败: %s: %s", cache_path, exc)
        return None


def _write_cache(cache_path: str, png_bytes: bytes) -> None:
    """写入缓存文件。失败仅记录日志，不阻塞主流程。"""
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(png_bytes)
        logger.debug("缓存已写入: %s", cache_path)
    except OSError as exc:
        logger.warning("写入缓存失败: %s: %s", cache_path, exc)


async def capture_stock_chart(
    symbol: str,
    event_id: UUID | str,
    token: str,
    frontend_base_url: str,
    render_timeout_ms: int = _DEFAULT_RENDER_TIMEOUT,
    screenshot_timeout_ms: int = _DEFAULT_SCREENSHOT_TIMEOUT,
    instrument_id: str | None = None,
    chart_version: str = "v1",
) -> bytes:
    """截取个股详情页指定区域，返回 PNG bytes。

    Args:
        symbol: 股票代码
        event_id: 事件 ID
        token: 短期 JWT（URL 参数传递）
        frontend_base_url: 前端 base URL（如 http://localhost:5173）
        render_timeout_ms: 等待 data-render-ready="true" 的超时（毫秒）
        screenshot_timeout_ms: 截图操作超时（毫秒）
        instrument_id: 标的 ID（可选）。提供时启用截图缓存（任务 6.1）
        chart_version: 图表版本号（默认 v1）。版本变更时强制刷新缓存

    Returns:
        PNG 图片 bytes

    Raises:
        StockCaptureError: 截图失败（页面不可达、渲染超时、元素不存在等）
    """
    # [screenshot-cache] - 缓存命中检查（任务 6.1）
    # 仅当提供 instrument_id 时启用缓存，向后兼容无 instrument_id 的调用
    cache_path: str | None = None
    if instrument_id:
        cache_key = _build_cache_key(event_id, instrument_id, chart_version)
        cache_path = os.path.join(_CACHE_DIR, f"{cache_key}.png")
        cached = _read_cache(cache_path)
        if cached is not None:
            return cached

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

                # [screenshot-cache] - 截图成功后写入缓存（任务 6.1）
                if cache_path is not None:
                    _write_cache(cache_path, png_bytes)

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
    # 自测入口：验证函数可导入 + 缓存 key 构建逻辑（不实际启动浏览器）
    import inspect
    import tempfile

    print(f"capture_stock_chart={capture_stock_chart}")
    assert inspect.iscoroutinefunction(capture_stock_chart)

    # 测试 _build_cache_key
    key = _build_cache_key("evt-123", "inst-456", "v1")
    assert key == "evt-123_inst-456_v1", f"cache key 异常: {key}"
    print(f"cache_key={key}")

    # 测试 _write_cache / _read_cache（TTL 内命中）
    with tempfile.TemporaryDirectory() as tmpdir:
        test_path = os.path.join(tmpdir, "test.png")
        _write_cache(test_path, b"x" * 200)
        cached = _read_cache(test_path)
        assert cached == b"x" * 200, "缓存读取不一致"
        print(f"cache read OK, size={len(cached)}")

        # 测试过期缓存（mtime 设为 1 小时前）
        old_time = time.time() - 3600
        os.utime(test_path, (old_time, old_time))
        expired = _read_cache(test_path)
        assert expired is None, "过期缓存应返回 None"
        print("cache expired OK")

    print("OK")
