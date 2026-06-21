"""股票主数据 API 路由。

提供：
- GET /instruments: 股票列表查询（支持关键词搜索 + 分页）
- POST /instruments/batch: 按 ID 列表批量查询（最多 1000 个）
- GET /instruments/{id}: 按 ID 查询单个股票
- GET /instruments/by-symbol/{symbol}: 按 symbol 查询

设计说明：
- 关键词搜索：symbol 或 name 模糊匹配（ILIKE，大小写不敏感）
- 分页：page 从 1 开始，page_size 默认 20，最大 100
- 按 symbol 唯一约束，by-symbol 查询最多返回 1 条
- 批量查询：避免前端逐个查询或加载全量数据，单次最多 1000 个 ID
"""

from __future__ import annotations

import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db
from app.models.instrument import Instrument
from app.schemas.instrument import (
    InstrumentBatchRequest,
    InstrumentBatchResponse,
    InstrumentListResponse,
    InstrumentResponse,
)

router = APIRouter(prefix="/instruments", tags=["instruments"])


@router.get("", response_model=InstrumentListResponse)
async def list_instruments(
    keyword: str | None = Query(None, description="关键词（symbol 或 name 模糊匹配）"),
    market: str | None = Query(None, description="市场筛选：SH/SZ/BJ"),
    status: str | None = Query(None, description="状态筛选：active/delisted/suspended"),
    page: int = Query(1, ge=1, description="页码（从 1 开始）"),
    page_size: int = Query(20, ge=1, le=100, description="每页大小（最大 100）"),
    db: AsyncSession = Depends(get_db),
) -> InstrumentListResponse:
    """查询股票列表，支持关键词搜索、市场/状态筛选与分页。"""
    # 构建查询条件
    conditions = []
    if keyword:
        # ILIKE 大小写不敏感模糊匹配
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                Instrument.symbol.ilike(pattern),
                Instrument.name.ilike(pattern),
            )
        )
    if market:
        conditions.append(Instrument.market == market)
    if status:
        conditions.append(Instrument.status == status)

    # 计数查询（总数）
    count_stmt = select(func.count()).select_from(Instrument)
    for cond in conditions:
        count_stmt = count_stmt.where(cond)
    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    # 分页数据查询
    data_stmt = select(Instrument)
    for cond in conditions:
        data_stmt = data_stmt.where(cond)
    data_stmt = data_stmt.order_by(Instrument.symbol).offset((page - 1) * page_size).limit(page_size)
    data_result = await db.execute(data_stmt)
    items = data_result.scalars().all()

    pages = math.ceil(total / page_size) if total > 0 else 0

    return InstrumentListResponse(
        items=[InstrumentResponse.model_validate(item) for item in items],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.post("/batch", response_model=InstrumentBatchResponse)
async def batch_get_instruments(
    request: InstrumentBatchRequest,
    db: AsyncSession = Depends(get_db),
) -> InstrumentBatchResponse:
    """按 ID 列表批量查询股票（最多 1000 个）。

    用于前端根据 strategy_results 的 instrument_id 列表批量获取股票主数据，
    避免逐个查询或加载全量数据。
    """
    stmt = select(Instrument).where(Instrument.id.in_(request.ids))
    result = await db.execute(stmt)
    items = result.scalars().all()
    return InstrumentBatchResponse(
        items=[InstrumentResponse.model_validate(item) for item in items],
        total=len(items),
    )


@router.get("/by-symbol/{symbol}", response_model=InstrumentResponse)
async def get_instrument_by_symbol(
    symbol: str,
    db: AsyncSession = Depends(get_db),
) -> InstrumentResponse:
    """按 symbol 查询股票（symbol 唯一，最多返回 1 条）。"""
    stmt = select(Instrument).where(Instrument.symbol == symbol)
    result = await db.execute(stmt)
    instrument = result.scalar_one_or_none()
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 symbol={symbol} 的股票",
        )
    return InstrumentResponse.model_validate(instrument)


@router.get("/{instrument_id}", response_model=InstrumentResponse)
async def get_instrument(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
) -> InstrumentResponse:
    """按 ID 查询单个股票。"""
    stmt = select(Instrument).where(Instrument.id == instrument_id)
    result = await db.execute(stmt)
    instrument = result.scalar_one_or_none()
    if instrument is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 id={instrument_id} 的股票",
        )
    return InstrumentResponse.model_validate(instrument)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={[r.path for r in router.routes]}")
    print("OK")
