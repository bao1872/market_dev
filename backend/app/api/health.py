"""健康检查路由。

GET /health: 返回应用存活状态
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> JSONResponse:
    """健康检查端点，返回 200 表示应用存活。"""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "service": "trading-platform", "version": "1.1.0"},
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
