"""截图 Worker HTTP 服务 - 独立进程运行 Playwright 截图。

用法：
    python -m app.capture_main

环境变量：
    CAPTURE_HOST: 监听地址（默认 0.0.0.0）
    CAPTURE_PORT: 监听端口（默认 8001）
    CAPTURE_STATIC_DIR: 截图保存目录（默认 /app/static/captures）
    CAPTURE_STATIC_URL_PREFIX: 静态文件 URL 前缀（默认 /static/captures）

端点：
    POST /capture
    {
        "symbol": "600519",
        "event_id": "...",
        "token": "<short_lived_jwt>",
        "frontend_base_url": "http://frontend",
        "output_filename": "optional-prefix",  // 可选，默认使用 uuid
        "instrument_id": "<uuid>",            // 可选，启用截图缓存（任务 6.1）
        "chart_version": "v1",                 // 可选，默认 v1
        "timeframe": "15m",                    // 可选，透传 Capture 页面周期
        "source_bar_time": "2026-07-10T14:30:00", // 可选，实时 bar 时间（防旧图）
        "capture_run_id": "run-1",             // 可选，截图运行 ID（防旧图）
        "disable_cache": true,                 // 可选，默认 false（跳过读缓存仍写新缓存）
        "viewport_width": 1920,                // 可选，覆盖 env 默认
        "viewport_height": 1200,               // 可选，覆盖 env 默认
        "device_scale_factor": 2               // 可选，默认 2（严禁 4）
    }
    -> {
        "symbol": "600519",
        "event_id": "...",
        "image_url": "/static/captures/xxx.png",
        "size": 12345,
        "width": 3840,
        "height": 2400,
        "device_scale_factor": 2,
        "cache_hit": false
    }

错误响应（advice.md 第十一节遗留清理：技术错误返回三字段）：
    - 截图失败: {"error_code": "CAPTURE_FAILED", "error_message": "...", "failed_step": "capture"}
    - 保存失败: {"error_code": "SAVE_FAILED", "error_message": "...", "failed_step": "save"}
    - 截图超时: {"error_code": "CAPTURE_TIMEOUT", "error_message": "...", "failed_step": "capture"}

设计：
- 复用 app.services.stock_capture_service.capture_stock_chart
- 等待 data-render-ready="true" 后截取 data-testid="stock-detail-capture"
- 图片保存到本地静态目录，返回本地静态 URL
- 不长期存 base64 到 Outbox
- 截图缓存（任务 6.1）：传入 instrument_id 时，capture_stock_chart 内部按
  event_id+instrument_id+chart_version 缓存 PNG（TTL 600s），缓存命中不启动浏览器
"""

from __future__ import annotations

import os
import uuid
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.core.route_utils import get_route_paths
from app.services.stock_capture_service import (
    StockCaptureError,
    capture_stock_chart,
)

app = FastAPI(title="Capture Worker")

CAPTURE_HOST = os.getenv("CAPTURE_HOST", "0.0.0.0")
CAPTURE_PORT = int(os.getenv("CAPTURE_PORT", "8001"))
CAPTURE_STATIC_DIR = os.getenv("CAPTURE_STATIC_DIR", "/app/static/captures")
CAPTURE_STATIC_URL_PREFIX = os.getenv("CAPTURE_STATIC_URL_PREFIX", "/static/captures")


def _error_detail(
    error_code: str,
    error_message: str,
    failed_step: str,
) -> dict[str, str]:
    """构造技术错误三字段响应体。

    [capture-worker] - 描述: HTTP 异常返回 {error_code, error_message, failed_step} 结构
    （advice.md 第十一节遗留清理：技术错误必须返回三字段）
    """
    return {
        "error_code": error_code,
        "error_message": error_message,
        "failed_step": failed_step,
    }


class CaptureRequest(BaseModel):
    """截图请求。"""

    symbol: str = Field(..., description="股票代码")
    event_id: UUID | str = Field(..., description="事件 ID")
    token: str = Field(..., description="短期 JWT")
    frontend_base_url: str = Field(..., description="前端 base URL")
    output_filename: str | None = Field(None, description="输出文件名前缀（可选）")
    instrument_id: str | None = Field(
        None, description="标的 ID（可选）。提供时启用截图缓存（任务 6.1）"
    )
    chart_version: str = Field("v1", description="图表版本号，默认 v1")
    # [capture-realtime] - 扩展字段：透传周期/实时来源/运行ID/缓存旁路/高清参数
    timeframe: str | None = Field(
        None, description="K线周期（透传 Capture 页面；默认由页面决定）"
    )
    source_bar_time: str | None = Field(
        None, description="实时 bar 时间（扩展缓存 key，防旧图/旧指标）"
    )
    capture_run_id: str | None = Field(
        None, description="本次截图运行 ID（扩展缓存 key，防旧图）"
    )
    disable_cache: bool = Field(
        False, description="True 时跳过读缓存但允许写新缓存（飞书实时截图默认 True）"
    )
    viewport_width: int | None = Field(
        None, description="高清视口宽（覆盖 env CAPTURE_VIEWPORT_WIDTH，默认 1920）"
    )
    viewport_height: int | None = Field(
        None, description="高清视口高（覆盖 env CAPTURE_VIEWPORT_HEIGHT，默认 1200）"
    )
    device_scale_factor: int | None = Field(
        None, description="设备像素比（覆盖 env CAPTURE_DEVICE_SCALE_FACTOR，默认 2，严禁 4）"
    )


class CaptureResponse(BaseModel):
    """截图响应。"""

    symbol: str = Field(..., description="股票代码")
    event_id: str = Field(..., description="事件 ID")
    image_url: str = Field(..., description="图片本地静态 URL")
    size: int = Field(..., description="图片字节数")
    width: int | None = Field(None, description="截图像素宽（viewport_width * device_scale_factor）")
    height: int | None = Field(None, description="截图像素高（viewport_height * device_scale_factor）")
    device_scale_factor: int | None = Field(None, description="设备像素比")
    cache_hit: bool = Field(False, description="是否命中文件缓存（仅读缓存命中为 True）")


# [capture-worker] - 确保静态目录存在，并挂载静态文件服务
os.makedirs(CAPTURE_STATIC_DIR, exist_ok=True)
app.mount(CAPTURE_STATIC_URL_PREFIX, StaticFiles(directory=CAPTURE_STATIC_DIR), name="captures")


@app.post("/capture", response_model=CaptureResponse)
async def capture(request: CaptureRequest) -> CaptureResponse:
    """截取个股详情页并返回本地静态 URL。"""
    filename_prefix = request.output_filename or str(uuid.uuid4())
    filename = f"{filename_prefix}.png"
    local_path = os.path.join(CAPTURE_STATIC_DIR, filename)
    image_url = f"{CAPTURE_STATIC_URL_PREFIX}/{filename}"

    try:
        result = await capture_stock_chart(
            symbol=request.symbol,
            event_id=request.event_id,
            token=request.token,
            frontend_base_url=request.frontend_base_url,
            instrument_id=request.instrument_id,
            chart_version=request.chart_version,
            timeframe=request.timeframe,
            source_bar_time=request.source_bar_time,
            capture_run_id=request.capture_run_id,
            disable_cache=request.disable_cache,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
            device_scale_factor=request.device_scale_factor,
        )
    except StockCaptureError as e:
        # [capture-worker] - 区分超时与失败：错误消息含"超时"归为 CAPTURE_TIMEOUT
        err_msg = str(e)
        err_code = "CAPTURE_TIMEOUT" if "超时" in err_msg else "CAPTURE_FAILED"
        raise HTTPException(
            status_code=502,
            detail=_error_detail(err_code, err_msg, "capture"),
        ) from e
    except Exception as e:
        # [capture-worker] - 其他未预期异常归为 CAPTURE_FAILED
        raise HTTPException(
            status_code=500,
            detail=_error_detail("CAPTURE_FAILED", f"截图异常: {e}", "capture"),
        ) from e

    png_bytes = result.png_bytes
    try:
        with open(local_path, "wb") as f:
            f.write(png_bytes)
    except OSError as e:
        # [capture-worker] - 文件保存失败归为 SAVE_FAILED
        raise HTTPException(
            status_code=500,
            detail=_error_detail("SAVE_FAILED", f"保存截图失败: {e}", "save"),
        ) from e

    return CaptureResponse(
        symbol=request.symbol,
        event_id=str(request.event_id),
        image_url=image_url,
        size=len(png_bytes),
        width=result.width,
        height=result.height,
        device_scale_factor=result.device_scale_factor,
        cache_hit=result.cache_hit,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查。"""
    return {"status": "ok"}


if __name__ == "__main__":
    # 自测入口：验证模块可导入（不实际启动浏览器/服务）
    import uvicorn

    print(f"capture_main loaded: host={CAPTURE_HOST}, port={CAPTURE_PORT}")
    print(f"static_dir={CAPTURE_STATIC_DIR}, prefix={CAPTURE_STATIC_URL_PREFIX}")
    print(f"routes={get_route_paths(app.routes)}")
    uvicorn.run(app, host=CAPTURE_HOST, port=CAPTURE_PORT)
