"""[CHANGE-20260730-011] 板块分析 V1 API 路由。

端点：
- GET /api/v1/boards/analysis: 板块分析列表（分页，按 type/trade_date/sort 过滤）
- GET /api/v1/boards/{board_id}/analysis: 单板块分析详情
- POST /api/v1/admin/boards/{board_id}/analysis/compute: 管理员触发计算（canary 用）
- POST /api/v1/admin/boards/analysis/compute-all: 管理员触发批量计算（行业+概念）

权限：
- GET 接口：require_authenticated（任何登录用户可读）
- POST 接口：require_roles("admin")

设计：
- 读接口从 board_analysis_snapshots 表查询，不触发计算
- 写接口（admin）触发计算并发布
- 失败返回 4xx/5xx，不静默返回空数据
"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.board_analysis import (
    BoardAnalysisDetailResponse,
    BoardAnalysisListResponse,
    BoardAnalysisSnapshotDTO,
)
from app.services.access_control_service import AccessContext, require_authenticated
from app.services.board_analysis_service import (
    BOARD_ANALYSIS_ALGORITHM_VERSION,
    BOARD_ANALYSIS_MIN_COVERAGE,
    check_is_published,
    compute_all_boards,
    compute_board_analysis,
    compute_is_stale,
    get_board_analysis_detail,
    list_board_analyses,
    publish_board_analysis,
)

logger = logging.getLogger("api.board_analysis")

# 用户侧路由：/api/v1/boards
board_router = APIRouter(prefix="/api/v1/boards", tags=["board-analysis"])

# 管理员路由：/api/v1/admin/boards
admin_router = APIRouter(prefix="/api/v1/admin/boards", tags=["admin-board-analysis"])


def _to_dto(
    snapshot: Any,
    is_stale: bool,
    is_published: bool,
) -> BoardAnalysisSnapshotDTO:
    """将 ORM 对象转为 DTO（注入 is_stale / is_published）。"""
    return BoardAnalysisSnapshotDTO(
        id=str(snapshot.id),
        trade_date=snapshot.trade_date.isoformat(),
        board_id=str(snapshot.board_id),
        board_type=snapshot.board_type,
        board_name=snapshot.board_name,
        source_core_run_id=str(snapshot.source_core_run_id),
        algorithm_version=snapshot.algorithm_version,
        parameter_hash=snapshot.parameter_hash,
        eligible_count=snapshot.eligible_count,
        ready_count=snapshot.ready_count,
        coverage_ratio=snapshot.coverage_ratio,
        missing_count=snapshot.missing_count,
        missing_reasons=snapshot.missing_reasons or {},
        status=snapshot.status,
        payload=snapshot.payload or {},
        error_message=snapshot.error_message,
        started_at=snapshot.started_at.isoformat() if snapshot.started_at else None,
        finished_at=snapshot.finished_at.isoformat() if snapshot.finished_at else None,
        created_at=snapshot.created_at.isoformat() if snapshot.created_at else "",
        updated_at=snapshot.updated_at.isoformat() if snapshot.updated_at else "",
        is_stale=is_stale,
        is_published=is_published,
    )


@board_router.get("/analysis", response_model=BoardAnalysisListResponse)
async def list_board_analysis(
    type: str | None = Query(
        None, description="板块类型过滤：industry | concept",
    ),
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新）",
    ),
    sort: str = Query(
        "coverage_desc",
        description="排序：coverage_desc | coverage_asc | name_asc | ready_desc",
    ),
    page: int = Query(1, ge=1, description="页码（1-based）"),
    page_size: int = Query(20, ge=1, le=100, description="每页大小"),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> BoardAnalysisListResponse:
    """板块分析列表（只读，需登录）。

    返回所有已计算的板块分析快照，按 coverage 降序排序（默认）。
    每个 item 含 is_stale（trade_date < 最新行情日）和 is_published（已发布指针）。
    """
    _ = ctx
    if type is not None and type not in ("industry", "concept"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid type: {type}; must be one of: industry, concept",
        )
    if sort not in ("coverage_desc", "coverage_asc", "name_asc", "ready_desc"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid sort: {sort}",
        )

    result = await list_board_analyses(
        db,
        board_type=type,
        trade_date=trade_date,
        sort=sort,
        page=page,
        page_size=page_size,
    )

    items: list[BoardAnalysisSnapshotDTO] = []
    for snap in result["items"]:
        is_stale = await compute_is_stale(db, snap.trade_date)
        is_pub = await check_is_published(db, snap.board_id, snap.trade_date)
        items.append(_to_dto(snap, is_stale, is_pub))

    return BoardAnalysisListResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        page_size=result["page_size"],
        has_more=result["has_more"],
    )


@board_router.get(
    "/{board_id}/analysis",
    response_model=BoardAnalysisDetailResponse,
)
async def get_board_analysis(
    board_id: uuid.UUID,
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新）",
    ),
    db: AsyncSession = Depends(get_db),
    ctx: AccessContext = Depends(require_authenticated),
) -> BoardAnalysisDetailResponse:
    """单板块分析详情（只读，需登录）。

    返回指定板块的最新分析快照（含完整 payload）。
    """
    _ = ctx
    snapshot = await get_board_analysis_detail(db, board_id, trade_date)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"板块分析不存在: board_id={board_id}",
        )
    is_stale = await compute_is_stale(db, snapshot.trade_date)
    is_pub = await check_is_published(db, board_id, snapshot.trade_date)
    dto = _to_dto(snapshot, is_stale, is_pub)
    return BoardAnalysisDetailResponse(snapshot=dto)


# =============================================================================
# 管理员接口
# =============================================================================


@admin_router.post(
    "/{board_id}/analysis/compute",
    status_code=status.HTTP_200_OK,
)
async def trigger_compute_board(
    board_id: uuid.UUID,
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布 stock_core 日期）",
    ),
    publish: bool = Query(True, description="计算后是否发布"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """[Admin] 触发单个板块分析计算。

    若 trade_date 未指定，自动从最新 stock_core publication pointer 获取。
    """
    _ = current_user

    # 解析 trade_date
    if trade_date is None:
        # 从最新 stock_core pointer 获取 trade_date
        from sqlalchemy import select

        from app.models.factor_publication import FactorPublication

        pub_stmt = (
            select(FactorPublication)
            .where(FactorPublication.publication_kind == "stock_core")
            .order_by(FactorPublication.published_at.desc())
            .limit(1)
        )
        pub = (await db.execute(pub_stmt)).scalar_one_or_none()
        if pub is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="无已发布 stock_core pointer，无法触发板块分析",
            )
        trade_date = pub.trade_date

    try:
        snapshot = await compute_board_analysis(
            db,
            board_id,
            trade_date,
            algorithm_version=BOARD_ANALYSIS_ALGORITHM_VERSION,
        )
        pub_result = None
        if publish and snapshot.coverage_ratio >= BOARD_ANALYSIS_MIN_COVERAGE:
            pub_result = await publish_board_analysis(db, snapshot)
        await db.commit()
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        await db.rollback()
        logger.exception("[Admin] 板块分析计算失败 board_id=%s", board_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"板块分析计算失败: {exc}",
        ) from exc

    return {
        "board_id": str(board_id),
        "trade_date": trade_date.isoformat(),
        "status": snapshot.status,
        "coverage_ratio": snapshot.coverage_ratio,
        "eligible_count": snapshot.eligible_count,
        "ready_count": snapshot.ready_count,
        "published": pub_result is not None,
        "snapshot_id": str(snapshot.id),
    }


@admin_router.post(
    "/analysis/compute-all",
    status_code=status.HTTP_200_OK,
)
async def trigger_compute_all_boards(
    trade_date: date | None = Query(
        None, description="业务交易日（不传取最新已发布 stock_core 日期）",
    ),
    board_type: str | None = Query(
        None, description="限定类型：industry | concept（不传两个都计算）",
    ),
    limit: int | None = Query(
        None, ge=1, le=1000, description="限定每个类型的板块数（canary 用）",
    ),
    publish: bool = Query(True, description="计算后是否发布 coverage>=0.95 的结果"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> dict[str, Any]:
    """[Admin] 触发批量板块分析计算。

    可用于 canary（limit=5）或全量（不传 limit）。
    """
    _ = current_user

    # 解析 trade_date
    if trade_date is None:
        latest_run_id = None
        try:
            # 从最新 stock_core pointer 推断 trade_date
            from sqlalchemy import select

            from app.models.factor_publication import FactorPublication

            pub_stmt = (
                select(FactorPublication)
                .where(FactorPublication.publication_kind == "stock_core")
                .order_by(FactorPublication.published_at.desc())
                .limit(1)
            )
            pub = (await db.execute(pub_stmt)).scalar_one_or_none()
            if pub is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="无已发布 stock_core pointer，无法触发板块分析",
                )
            trade_date = pub.trade_date
            latest_run_id = pub.data_run_id
        except HTTPException:
            raise
        _ = latest_run_id

    if board_type is not None and board_type not in ("industry", "concept"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid board_type: {board_type}",
        )

    try:
        result = await compute_all_boards(
            db,
            trade_date,
            board_type=board_type,
            limit=limit,
            publish=publish,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.exception("[Admin] 批量板块分析失败")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"批量板块分析失败: {exc}",
        ) from exc

    return result
