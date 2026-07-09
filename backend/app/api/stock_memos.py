"""个股备忘录 API 路由。

端点：
- GET /instruments/{instrument_id}/memo: 获取当前用户对该股票的备忘录
- PUT /instruments/{instrument_id}/memo: 创建/更新备忘录（upsert）
- DELETE /instruments/{instrument_id}/memo: 删除备忘录（硬删除）
- PATCH /instruments/{instrument_id}/memo/notify: 切换飞书推送开关

设计说明：
- user_id 由 get_current_active_user 注入，不接受请求体传入（V1.1 安全约束）
- (user_id, instrument_id) 唯一约束：同一用户同一股票只有一条备忘录
- upsert 语义：已有则更新，没有则创建
- 删除采用硬删除
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.core.route_utils import get_route_paths
from app.db import get_db
from app.models.instrument import Instrument
from app.models.stock_memo import StockMemo
from app.models.user import User
from app.schemas.stock_memo import (
    StockMemoNotifyToggleRequest,
    StockMemoResponse,
    StockMemoUpsertRequest,
)

router = APIRouter(prefix="/instruments/{instrument_id}/memo", tags=["stock-memo"])


@router.get("", response_model=StockMemoResponse)
async def get_memo(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StockMemoResponse:
    """获取当前用户对该股票的备忘录。

    user_id 由认证上下文注入，不接受查询参数传入。
    不存在返回 404。
    """
    stmt = select(StockMemo).where(
        StockMemo.user_id == current_user.id,
        StockMemo.instrument_id == instrument_id,
    )
    result = await db.execute(stmt)
    memo = result.scalar_one_or_none()
    if memo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备忘录不存在",
        )
    return StockMemoResponse.model_validate(memo)


@router.put("", response_model=StockMemoResponse)
async def upsert_memo(
    instrument_id: UUID,
    payload: StockMemoUpsertRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StockMemoResponse:
    """创建/更新备忘录（upsert）。

    user_id 由认证上下文注入（不接受 body 中的 user_id）。
    先校验 instrument_id 存在，再执行 upsert。
    """
    # 校验股票存在
    inst_stmt = select(Instrument).where(Instrument.id == instrument_id)
    inst_result = await db.execute(inst_stmt)
    if inst_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"未找到 instrument_id={instrument_id} 的股票",
        )

    # 查询已有备忘录
    stmt = select(StockMemo).where(
        StockMemo.user_id == current_user.id,
        StockMemo.instrument_id == instrument_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        # 更新
        existing.content = payload.content
        existing.notify_feishu = payload.notify_feishu
        await db.commit()
        await db.refresh(existing)
        return StockMemoResponse.model_validate(existing)

    # 创建
    memo = StockMemo(
        user_id=current_user.id,
        instrument_id=instrument_id,
        content=payload.content,
        notify_feishu=payload.notify_feishu,
    )
    db.add(memo)
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        # 唯一约束冲突兜底（并发场景）
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"创建备忘录失败（可能已存在）：{e}",
        ) from e
    await db.refresh(memo)
    return StockMemoResponse.model_validate(memo)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memo(
    instrument_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    """删除备忘录（硬删除）。

    user_id 由认证上下文注入。
    不存在返回 404。
    """
    stmt = select(StockMemo).where(
        StockMemo.user_id == current_user.id,
        StockMemo.instrument_id == instrument_id,
    )
    result = await db.execute(stmt)
    memo = result.scalar_one_or_none()
    if memo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备忘录不存在",
        )
    await db.delete(memo)
    await db.commit()


@router.patch("/notify", response_model=StockMemoResponse)
async def toggle_notify(
    instrument_id: UUID,
    payload: StockMemoNotifyToggleRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StockMemoResponse:
    """切换飞书推送开关。

    user_id 由认证上下文注入。
    不存在返回 404。
    """
    stmt = select(StockMemo).where(
        StockMemo.user_id == current_user.id,
        StockMemo.instrument_id == instrument_id,
    )
    result = await db.execute(stmt)
    memo = result.scalar_one_or_none()
    if memo is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="备忘录不存在",
        )
    memo.notify_feishu = payload.notify_feishu
    await db.commit()
    await db.refresh(memo)
    return StockMemoResponse.model_validate(memo)


if __name__ == "__main__":
    # 自测入口：验证路由注册
    print(f"router.routes={get_route_paths(router.routes)}")
    print("OK")
