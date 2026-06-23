"""健康检查路由。

GET /health: 返回应用存活状态
GET /health/ready: 返回应用就绪状态（策略资产完整性检查）
GET /version: 返回构建版本信息（git_sha / build_time / app_version / alembic_revision）
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db import AsyncSessionLocal

logger = logging.getLogger("health")

router = APIRouter(tags=["health"])

# [策略资产] - 就绪标志：启动时检查策略资产文件完整性，缺失则置 False
_strategy_assets_ready: bool = True


def check_strategy_assets() -> None:
    """检查策略资产文件完整性，更新 _strategy_assets_ready 标志。

    在应用启动时由 lifespan 调用，检查关键策略资产文件是否存在。
    """
    global _strategy_assets_ready

    base = Path(__file__).resolve().parent.parent / "strategy_assets"
    required_files = [
        base / "manifests" / "dsa_selector.yaml",
        base / "manifests" / "bb_monitor.yaml",
        base / "manifests" / "volume_node_monitor.yaml",
        base / "schemas" / "strategy_manifest.schema.json",
    ]
    required_dirs_with_py = [
        base / "algorithms" / "features",
    ]

    missing: list[str] = []
    for f in required_files:
        if not f.is_file():
            missing.append(str(f))

    for d in required_dirs_with_py:
        if not d.is_dir():
            missing.append(str(d))
        elif not any(p.suffix == ".py" and p.name != "__init__.py" for p in d.iterdir()):
            missing.append(f"{d} (no .py files)")

    if missing:
        _strategy_assets_ready = False
        for path in missing:
            logger.error("策略资产缺失: %s", path)
    else:
        _strategy_assets_ready = True
        logger.info("策略资产完整性检查通过")


@router.get("/health")
async def health() -> JSONResponse:
    """健康检查端点，返回 200 表示应用存活。"""
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "service": "trading-platform", "version": "1.1.0"},
    )


@router.get("/health/ready")
async def readiness() -> JSONResponse:
    """就绪检查端点，策略资产缺失时返回 503。"""
    if _strategy_assets_ready:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={"status": "ready"},
        )
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "not_ready", "reason": "strategy_assets_missing"},
    )


@router.get("/version")
async def version() -> JSONResponse:
    """版本信息端点，返回构建版本与数据库迁移版本（无需认证）。"""
    alembic_revision = "unknown"
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(text("SELECT version_num FROM alembic_version"))
            row = result.scalar_one_or_none()
            if row:
                alembic_revision = row
    except Exception:
        logger.exception("查询 alembic_version 失败")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "git_sha": os.environ.get("GIT_SHA", "unknown"),
            "build_time": os.environ.get("BUILD_TIME", "unknown"),
            "app_version": "1.1.0",
            "alembic_revision": alembic_revision,
        },
    )


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    check_strategy_assets()
    print(f"_strategy_assets_ready={_strategy_assets_ready}")
