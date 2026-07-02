"""公开套餐 API 路由。

提供无需登录的 GET /plans 端点，返回所有 active 套餐定义。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.plan import PlanResponse
from app.services.plan_service import list_all_plans

router = APIRouter(tags=["plans"])


@router.get("/plans", response_model=list[PlanResponse])
async def get_plans(db: AsyncSession = Depends(get_db)) -> list[PlanResponse]:
    """返回所有 active 套餐列表（公开端点，无需登录）。

    Args:
        db: 异步数据库会话

    Returns:
        PlanResponse 列表，按 plan_code 排序
    """
    plans = await list_all_plans(db)
    return [
        PlanResponse(
            plan_code=p.plan_code,
            display_name=p.display_name,
            monitor_limit=int(p.monitor_limit),
            notification_channel_limit=int(p.notification_channel_limit),
            message_retention_days=int(p.message_retention_days),
            features=list(p.features) if p.features else [],
        )
        for p in plans
    ]


if __name__ == "__main__":
    # [Plans] - 描述: 自测入口，验证路由注册
    paths = [getattr(r, "path", None) for r in router.routes]
    paths = [p for p in paths if p is not None]
    print(f"router.routes={paths}")
    assert "/plans" in paths
    print("OK")
