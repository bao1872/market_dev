"""管理员 API 路由 - 增量发布状态查询。

端点：
- GET /admin/incremental-publish/status: 综合状态（core/aggregation/history/chip + pointer）
- GET /admin/incremental-publish/core/runs: 列出 stock_core snapshot runs
- GET /admin/incremental-publish/core/runs/{snapshot_run_id}/progress: 单 run 进度
- GET /admin/incremental-publish/history/runs: 列出 history runs
- GET /admin/incremental-publish/history/runs/{history_run_id}/progress: 单 history run 进度
- GET /admin/incremental-publish/pointers: 当前所有 publication pointer
- GET /admin/incremental-publish/chip/runs: chip 共识 job 列表（简化版）

权限：所有端点需要 admin 角色。

设计：
- 只读查询，不创建/修改/删除任何资源
- 聚合各服务的查询接口，提供统一状态视图
- 失败清单按 status=failed 筛选 run items
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.models.factor_publication import FactorPublication
from app.models.first_pyramid_history_run import FirstPyramidHistoryRun
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem
from app.services.factor_publication_service import (
    PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
    compute_coverage,
    get_publication,
)
from app.services.first_pyramid_history_service import get_history_run_progress

logger = logging.getLogger("admin_incremental_publish")

router = APIRouter(
    prefix="/v1/admin/incremental-publish",
    tags=["admin-incremental-publish"],
)


@router.get("/status")
async def get_incremental_publish_status(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """获取增量发布综合状态。

    返回：
    - core: { latest_run, pointer, coverage }
    - aggregation: { pointer }
    - history: { latest_run, pointer }
    - chip: 占位（待 chip job 表完善）
    - pointers: 所有 publication 列表
    """
    # 1. core: 最新 snapshot run + pointer
    core_latest_stmt = (
        select(StockFeatureSnapshotRun)
        .order_by(StockFeatureSnapshotRun.created_at.desc())
        .limit(1)
    )
    core_latest = (await db.execute(core_latest_stmt)).scalar_one_or_none()

    core_pointer = await get_publication(
        db,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=None,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )

    core_coverage: dict[str, Any] | None = None
    if core_latest is not None:
        try:
            core_coverage = await compute_coverage(db, core_latest.id)
        except Exception as exc:
            logger.warning("compute_coverage 失败 snapshot_run_id=%s: %s",
                           core_latest.id, exc)
            core_coverage = {"error": str(exc)}

    # 2. aggregation pointer
    agg_pointer = await get_publication(
        db,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=None,
        publication_kind=PUBLICATION_KIND_MARKET_AGGREGATION,
    )

    # 3. history: 最新 history run + pointer
    history_latest_stmt = (
        select(FirstPyramidHistoryRun)
        .order_by(FirstPyramidHistoryRun.created_at.desc())
        .limit(1)
    )
    history_latest = (await db.execute(history_latest_stmt)).scalar_one_or_none()

    history_pointer = await get_publication(
        db,
        scope_type=SCOPE_TYPE_MARKET,
        scope_key="market",
        trade_date=None,
        publication_kind=PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    )

    # 4. 所有 pointer 列表
    pointers_stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_MARKET,
            FactorPublication.scope_key == "market",
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(20)
    )
    pointers = (await db.execute(pointers_stmt)).scalars().all()

    return {
        "core": {
            "latest_run": _serialize_snapshot_run(core_latest),
            "pointer": _serialize_publication(core_pointer),
            "coverage": core_coverage,
        },
        "aggregation": {
            "pointer": _serialize_publication(agg_pointer),
        },
        "history": {
            "latest_run": _serialize_history_run(history_latest),
            "pointer": _serialize_publication(history_pointer),
        },
        "pointers": [_serialize_publication(p) for p in pointers],
    }


@router.get("/core/runs")
async def list_core_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """列出 stock_core snapshot runs。"""
    stmt = (
        select(StockFeatureSnapshotRun)
        .order_by(StockFeatureSnapshotRun.created_at.desc())
        .limit(limit)
    )
    runs = (await db.execute(stmt)).scalars().all()
    return {
        "runs": [_serialize_snapshot_run(r) for r in runs],
        "count": len(runs),
    }


@router.get("/core/runs/{snapshot_run_id}/progress")
async def get_core_run_progress(
    snapshot_run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """获取单 snapshot run 进度 + 失败清单。"""
    progress = await compute_coverage(db, snapshot_run_id)

    # 失败清单
    failed_stmt = (
        select(StockFeatureSnapshotRunItem)
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.status == "failed",
        )
        .order_by(StockFeatureSnapshotRunItem.completed_at.desc())
        .limit(50)
    )
    failed_items = (await db.execute(failed_stmt)).scalars().all()

    return {
        "snapshot_run_id": str(snapshot_run_id),
        "progress": progress,
        "failed_items": [
            {
                "id": str(it.id),
                "instrument_id": str(it.instrument_id),
                "attempt_count": it.attempt_count,
                "last_error": it.last_error,
                "completed_at": it.completed_at.isoformat() if it.completed_at else None,
            }
            for it in failed_items
        ],
    }


@router.get("/history/runs")
async def list_history_runs(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """列出 history runs。"""
    stmt = (
        select(FirstPyramidHistoryRun)
        .order_by(FirstPyramidHistoryRun.created_at.desc())
        .limit(limit)
    )
    runs = (await db.execute(stmt)).scalars().all()
    return {
        "runs": [_serialize_history_run(r) for r in runs],
        "count": len(runs),
    }


@router.get("/history/runs/{history_run_id}/progress")
async def get_history_run_progress_endpoint(
    history_run_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """获取单 history run 进度 + 失败清单。"""
    progress = await get_history_run_progress(db, history_run_id)

    failed_stmt = (
        select(FirstPyramidHistoryRunItem)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.status == "failed",
        )
        .order_by(FirstPyramidHistoryRunItem.completed_at.desc())
        .limit(50)
    )
    failed_items = (await db.execute(failed_stmt)).scalars().all()

    return {
        "history_run_id": str(history_run_id),
        "progress": progress,
        "failed_items": [
            {
                "id": str(it.id),
                "instrument_id": str(it.instrument_id),
                "attempt_count": it.attempt_count,
                "last_error": it.last_error,
                "completed_at": it.completed_at.isoformat() if it.completed_at else None,
            }
            for it in failed_items
        ],
    }


@router.get("/pointers")
async def list_pointers(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """列出所有 publication pointer。"""
    stmt = (
        select(FactorPublication)
        .where(
            FactorPublication.scope_type == SCOPE_TYPE_MARKET,
            FactorPublication.scope_key == "market",
        )
        .order_by(FactorPublication.published_at.desc())
        .limit(limit)
    )
    pointers = (await db.execute(stmt)).scalars().all()
    return {
        "pointers": [_serialize_publication(p) for p in pointers],
        "count": len(pointers),
    }


# =============================================================================
# 序列化 helpers
# =============================================================================


def _serialize_snapshot_run(run: StockFeatureSnapshotRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "trade_date": run.trade_date.isoformat() if run.trade_date else None,
        "scope": getattr(run, "scope", None),
        "status": run.status,
        "algorithm_version": run.algorithm_version,
        "expected_count": getattr(run, "expected_count", None),
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "published_at": run.published_at.isoformat() if run.published_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _serialize_history_run(run: FirstPyramidHistoryRun | None) -> dict[str, Any] | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "algorithm_version": run.algorithm_version,
        "parameter_hash": run.parameter_hash,
        "output_bars": run.output_bars,
        "scope": run.scope,
        "expected_count": run.expected_count,
        "succeeded_count": run.succeeded_count,
        "failed_count": run.failed_count,
        "skipped_count": run.skipped_count,
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


def _serialize_publication(pub: FactorPublication | None) -> dict[str, Any] | None:
    if pub is None:
        return None
    return {
        "id": str(pub.id),
        "scope_type": pub.scope_type,
        "scope_key": pub.scope_key,
        "trade_date": pub.trade_date.isoformat() if pub.trade_date else None,
        "publication_kind": pub.publication_kind,
        "algorithm_version": pub.algorithm_version,
        "data_run_id": str(pub.data_run_id),
        "coverage_ratio": pub.coverage_ratio,
        "published_at": pub.published_at.isoformat() if pub.published_at else None,
    }
