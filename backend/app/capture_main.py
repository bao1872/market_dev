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
        "chart_version": "v1"                  // 可选，默认 v1
    }
    -> {
        "symbol": "600519",
        "event_id": "...",
        "image_url": "/static/captures/xxx.png",
        "size": 12345
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

from app.services.stock_capture_service import StockCaptureError, capture_stock_chart

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


class CaptureResponse(BaseModel):
    """截图响应。"""

    symbol: str = Field(..., description="股票代码")
    event_id: str = Field(..., description="事件 ID")
    image_url: str = Field(..., description="图片本地静态 URL")
    size: int = Field(..., description="图片字节数")


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
        png_bytes = await capture_stock_chart(
            symbol=request.symbol,
            event_id=request.event_id,
            token=request.token,
            frontend_base_url=request.frontend_base_url,
            instrument_id=request.instrument_id,
            chart_version=request.chart_version,
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
    print(f"routes={[r.path for r in app.routes]}")
    uvicorn.run(app, host=CAPTURE_HOST, port=CAPTURE_PORT)
