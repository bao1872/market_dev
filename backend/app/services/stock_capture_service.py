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
- 访问 /capture/stock/{symbol}?source=watchlist&strategy=watchlist_monitor&event_id=...&token=...
  （专用 Capture 路由，不经过 ProtectedLayout/AppShell，只使用 captureClient）
- 等待 data-render-ready="true"（禁止固定 sleep）
- 截取 data-testid="stock-detail-capture" 区域
- 返回 PNG bytes
- 失败时抛出 StockCaptureError，不吞没异常

截图缓存（任务 6.1 + 盘中实时升级）：
- 缓存 key：event_id + instrument_id + chart_version + tf{timeframe} + sbt{source_bar_time}
  + run{capture_run_id} + dsf{device_scale_factor}
- 缓存 TTL：600 秒（_CACHE_TTL_SECONDS）
- 缓存存储：本地文件系统（CAPTURE_CACHE_DIR，默认 /app/static/captures/cache）
- 缓存命中且未过期：直接读取文件返回 bytes，不启动浏览器
- 缓存未命中或已过期：重新截图并写入缓存文件
- disable_cache=True：跳过读缓存，但仍写新缓存（飞书实时截图默认 True，杜绝复用旧图）
- 仅当提供 instrument_id 时启用缓存（向后兼容）

高清渲染（飞书清晰度升级）：
- viewport 默认 1920x1200（CAPTURE_VIEWPORT_WIDTH/HEIGHT），device_scale_factor 默认 2
- device_scale_factor 严禁 4（避免超大图/OOM）；截图 PNG 不落库、不存 base64

[PROMPT.md §5.3.2 V2 移动舞台规格]：
- viewport 固定 1440×2560（9:16 移动竖屏），device_scale_factor=1
- 旧版 1920×1200 DPR=2 已废弃（PC 横屏布局不匹配移动舞台）
- PNG width/height 从真实 IHDR 头读取（validate_png 返回），禁止 viewport×DPR 推算
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.services.png_validator import validate_png

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

# [capture-hd] - 高清截图渲染参数（提升飞书图片清晰度，不落库/base64）
# [PROMPT.md §5.3.2 V2] 移动舞台固定 1440×2560 9:16 竖屏 + DPR=1
#   - 旧版 1920×1200 DPR=2 已废弃（PC 横屏不匹配移动舞台，且 DPR=2 产生 3840×2400 超大图）
#   - 1440×2560 DPR=1 直接产出 1:1 PNG，与 MobileIndicatorStage CSS 尺寸一致
#   - env 变量保留以支持运维紧急覆盖（如 A/B 测试不同规格），但默认值已改为移动规格
_CAPTURE_VIEWPORT_WIDTH = int(os.getenv("CAPTURE_VIEWPORT_WIDTH", "1440"))
_CAPTURE_VIEWPORT_HEIGHT = int(os.getenv("CAPTURE_VIEWPORT_HEIGHT", "2560"))
_CAPTURE_DEVICE_SCALE_FACTOR = int(os.getenv("CAPTURE_DEVICE_SCALE_FACTOR", "1"))


@dataclass
class CaptureResult:
    """截图结果（含渲染元数据，供 CaptureResponse 透传）。

    [PROMPT.md §5.3.2 V2] width/height 从真实 PNG IHDR 头读取，
    不再用 viewport × DPR 推算（避免 Playwright 元素截图实际尺寸与 viewport 不一致时失真）。

    Attributes:
        png_bytes: PNG 图片字节
        width: 真实 PNG 像素宽（从 IHDR 读取）
        height: 真实 PNG 像素高（从 IHDR 读取）
        device_scale_factor: 设备像素比
        cache_hit: 是否命中文件缓存（仅读缓存命中为 True）
        source_bar_time: 透传的实时 bar 时间（可选，用于日志）
        snapshot_time: 截图完成时间（UTC ISO，供前端转 Asia/Shanghai 显示发送时间）
    """

    png_bytes: bytes
    width: int
    height: int
    device_scale_factor: int
    cache_hit: bool
    source_bar_time: str | None = None
    snapshot_time: str | None = None


def get_capture_render_config() -> dict[str, int]:
    """返回当前高清渲染配置（viewport 宽高 + device_scale_factor）。

    供 capture_main 在 CaptureResponse 中回传实际渲染尺寸。
    """
    return {
        "viewport_width": _CAPTURE_VIEWPORT_WIDTH,
        "viewport_height": _CAPTURE_VIEWPORT_HEIGHT,
        "device_scale_factor": _CAPTURE_DEVICE_SCALE_FACTOR,
    }


class StockCaptureError(RuntimeError):
    """截图失败异常。"""


def _build_cache_key(
    event_id: UUID | str,
    instrument_id: str,
    chart_version: str,
    *,
    timeframe: str | None = None,
    source_bar_time: str | None = None,
    capture_run_id: str | None = None,
    device_scale_factor: int | None = None,
    indicator_view: str | None = None,
) -> str:
    """构建截图缓存 key。

    [capture-realtime] - 扩展缓存维度，使不同时间点的盘中截图天然区分，
    避免复用旧图/旧指标：
        event_id + instrument_id + chart_version
        + tf={timeframe} + sbt={source_bar_time}
        + run={capture_run_id} + dsf={device_scale_factor}
    [CHANGE-20260720-003 §三] 新增 indicator_view 维度：
        + iv={indicator_view}
    不同指标视图（node_cluster|bollinger|smc）天然区分缓存，禁止不同指标复用旧图。
    source_bar_time/capture_run_id/indicator_view 变化即视为新图，禁止跨时间点/跨视图复用。
    """
    parts = [str(event_id), str(instrument_id), str(chart_version)]
    if timeframe is not None:
        parts.append(f"tf={timeframe}")
    if source_bar_time is not None:
        parts.append(f"sbt={source_bar_time}")
    if capture_run_id is not None:
        parts.append(f"run={capture_run_id}")
    parts.append(f"dsf={device_scale_factor}")
    if indicator_view is not None:
        parts.append(f"iv={indicator_view}")
    return "_".join(parts)


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
    *,
    timeframe: str | None = None,
    source_bar_time: str | None = None,
    capture_run_id: str | None = None,
    disable_cache: bool = False,
    viewport_width: int | None = None,
    viewport_height: int | None = None,
    device_scale_factor: int | None = None,
    indicator_view: str | None = None,
) -> CaptureResult:
    """截取个股详情页指定区域，返回 PNG bytes 与渲染元数据。

    Args:
        symbol: 股票代码
        event_id: 事件 ID
        token: 短期 JWT（URL 参数传递）
        frontend_base_url: 前端 base URL（如 http://localhost:5173）
        render_timeout_ms: 等待 data-render-ready="true" 的超时（毫秒）
        screenshot_timeout_ms: 截图操作超时（毫秒）
        instrument_id: 标的 ID（可选）。提供时启用截图缓存
        chart_version: 图表版本号（默认 v1）。版本变更时强制刷新缓存
        timeframe: 截图 K线周期（透传到 Capture 页面，默认由页面决定）
        source_bar_time: 实时 bar 时间（扩展缓存 key，防旧图）
        capture_run_id: 本次截图运行 ID（扩展缓存 key，防旧图）
        disable_cache: True 时跳过读缓存但允许写新缓存（飞书实时截图默认 True）
        viewport_width/height/device_scale_factor: 高清渲染参数（默认 env 1920x1200 dsf=2）
        indicator_view: 指标视图 node_cluster|bollinger|smc（扩展缓存 key + URL 参数，
            [CHANGE-20260720-003 §三] 一张图只渲染一个指标视图，禁止混合指标叠图）

    Returns:
        CaptureResult（png_bytes + 渲染元数据）

    Raises:
        StockCaptureError: 截图失败（页面不可达、渲染超时、元素不存在等）
    """
    # [capture-hd] - 解析渲染参数（请求覆盖优先，否则 env 默认）
    vw = viewport_width or _CAPTURE_VIEWPORT_WIDTH
    vh = viewport_height or _CAPTURE_VIEWPORT_HEIGHT
    dsf = device_scale_factor or _CAPTURE_DEVICE_SCALE_FACTOR

    # [screenshot-cache] - 缓存命中检查（扩展 key 维度）
    # 仅当提供 instrument_id 时启用缓存，向后兼容无 instrument_id 的调用
    cache_path: str | None = None
    cache_hit = False
    if instrument_id:
        cache_key = _build_cache_key(
            event_id, instrument_id, chart_version,
            timeframe=timeframe,
            source_bar_time=source_bar_time,
            capture_run_id=capture_run_id,
            device_scale_factor=dsf,
            indicator_view=indicator_view,
        )
        cache_path = os.path.join(_CACHE_DIR, f"{cache_key}.png")
        if not disable_cache:
            cached = _read_cache(cache_path)
            if cached is not None:
                cache_hit = True
                # [PROMPT.md §5.3.2 V2] 从真实 PNG IHDR 读取宽高（禁止 viewport×DPR 推算）
                cached_png_result = validate_png(cached)
                if not cached_png_result.valid:
                    logger.warning(
                        "缓存 PNG 校验失败，删除并重截: symbol=%s error=%s",
                        symbol, cached_png_result.error_code,
                    )
                    try:
                        os.remove(cache_path)
                    except OSError:
                        pass
                else:
                    logger.info(
                        "截图命中缓存: symbol=%s event_id=%s cache_hit=true "
                        "size=%d png=%dx%d viewport=%dx%d dsf=%d",
                        symbol, event_id, len(cached),
                        cached_png_result.width, cached_png_result.height,
                        vw, vh, dsf,
                    )
                    return CaptureResult(
                        png_bytes=cached,
                        width=cached_png_result.width,
                        height=cached_png_result.height,
                        device_scale_factor=dsf,
                        cache_hit=cache_hit,
                        source_bar_time=source_bar_time,
                        snapshot_time=datetime.now(UTC).isoformat(),
                    )
        else:
            logger.info(
                "disable_cache=true 跳过读缓存: symbol=%s event_id=%s "
                "viewport=%dx%d dsf=%d",
                symbol, event_id, vw, vh, dsf,
            )

    # [capture-route] - 描述: 使用专用 /capture/stock/{symbol} 路由（不经过 ProtectedLayout/AppShell）
    # 整个路由即为 capture 专用；token 由页面写入 CAPTURE_TOKEN_KEY
    # instrument_id 必须传入，前端从 URL 读取后调用 Snapshot API；timeframe 透传周期
    url = (
        f"{frontend_base_url.rstrip('/')}/capture/stock/{symbol}?"
        f"source=watchlist&strategy=watchlist_monitor&event_id={event_id}&"
        f"token={token}&instrument_id={instrument_id}"
    )
    if timeframe is not None:
        url += f"&timeframe={timeframe}"
    if source_bar_time is not None:
        url += f"&source_bar_time={source_bar_time}"
    if capture_run_id is not None:
        url += f"&capture_run_id={capture_run_id}"
    if indicator_view is not None:
        # [CHANGE-20260720-003 §三] 前端按 indicator_view 切换图层组合
        url += f"&indicator_view={indicator_view}"
    if disable_cache:
        url += "&disable_cache=true"
    # [capture-realtime] - 截图页面始终强制实时指标/行情（等价 force_refresh）
    url += "&force_refresh=1&capture=1"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            # [capture-hd] - 高清视口 + device_scale_factor（提升清晰度，不落库）
            context = await browser.new_context(
                viewport={"width": vw, "height": vh},
                device_scale_factor=dsf,
            )
            page = await context.new_page()

            try:
                logger.info(
                    "截图服务访问页面: symbol=%s event_id=%s viewport=%dx%d dsf=%d",
                    symbol, event_id, vw, vh, dsf,
                )
                # [capture-worker] - 描述: page.goto 使用 wait_until="load"
                # 历史根因：wait_until="networkidle" 在前端存在长连接/持续轮询时永远不会触发，
                # 导致 30s 超时返回 502。后续通过 wait_for_selector 等待 data-render-ready
                # 确保业务数据加载完成即可，不依赖网络完全空闲。
                await page.goto(url, wait_until="load", timeout=render_timeout_ms)

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

                # [CHANGE-20260719-002 §三] PNG 内容校验（PROMPT.md §3 要求 1-4）
                # 校验：PNG 存在、合理尺寸、合理字节数、非全透明/非纯色
                png_result = validate_png(png_bytes)
                if not png_result.valid:
                    raise StockCaptureError(
                        f"PNG 内容校验失败: symbol={symbol}, "
                        f"error_code={png_result.error_code}, "
                        f"error_message={png_result.error_message}, "
                        f"size={len(png_bytes)}"
                    )

                logger.info(
                    "截图成功: symbol=%s event_id=%s cache_hit=false "
                    "size=%d png=%dx%d viewport=%dx%d dsf=%d",
                    symbol, event_id, len(png_bytes),
                    png_result.width, png_result.height,
                    vw, vh, dsf,
                )

                # [screenshot-cache] - 截图成功后写入缓存（disable_cache 仍允许写新缓存）
                if cache_path is not None:
                    _write_cache(cache_path, png_bytes)

                # [PROMPT.md §5.3.2 V2] width/height 从真实 PNG IHDR 读取
                #   validate_png 已解析 IHDR，直接使用其结果（png_result.width/height）
                #   禁止用 viewport × DPR 推算（元素截图实际尺寸可能因 CSS 布局微调与 viewport 不同）
                return CaptureResult(
                    png_bytes=png_bytes,
                    width=png_result.width,
                    height=png_result.height,
                    device_scale_factor=dsf,
                    cache_hit=False,
                    source_bar_time=source_bar_time,
                    snapshot_time=datetime.now(UTC).isoformat(),
                )
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

    # 测试 _build_cache_key（扩展 key 维度）
    key = _build_cache_key("evt-123", "inst-456", "v1", device_scale_factor=2)
    assert key == "evt-123_inst-456_v1_dsf=2", f"cache key 异常: {key}"
    print(f"cache_key={key}")

    # 扩展维度：timeframe/source_bar_time/capture_run_id 变化应产生不同 key
    key_full = _build_cache_key(
        "evt-123", "inst-456", "v1",
        timeframe="15m", source_bar_time="2026-07-10T14:30:00",
        capture_run_id="run-1", device_scale_factor=2,
    )
    assert "tf=15m" in key_full and "sbt=2026-07-10T14:30:00" in key_full \
        and "run=run-1" in key_full, f"扩展 key 维度缺失: {key_full}"
    assert key_full != key, "不同维度应产生不同 key"
    print(f"cache_key_ext={key_full}")

    # [CHANGE-20260720-003 §三] indicator_view 维度：不同视图应产生不同 key
    key_nc = _build_cache_key(
        "evt-123", "inst-456", "v1",
        device_scale_factor=2, indicator_view="node_cluster",
    )
    key_bb = _build_cache_key(
        "evt-123", "inst-456", "v1",
        device_scale_factor=2, indicator_view="bollinger",
    )
    key_smc = _build_cache_key(
        "evt-123", "inst-456", "v1",
        device_scale_factor=2, indicator_view="smc",
    )
    assert "iv=node_cluster" in key_nc, f"indicator_view 维度缺失: {key_nc}"
    assert "iv=bollinger" in key_bb, f"indicator_view 维度缺失: {key_bb}"
    assert "iv=smc" in key_smc, f"indicator_view 维度缺失: {key_smc}"
    assert key_nc != key_bb != key_smc != key_nc, \
        f"不同 indicator_view 应产生不同 key: {key_nc} / {key_bb} / {key_smc}"
    print(f"cache_key_iv: nc={key_nc} bb={key_bb} smc={key_smc}")

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
