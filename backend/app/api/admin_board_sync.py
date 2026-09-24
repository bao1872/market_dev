"""管理员 API 路由 - 板块/概念手动同步状态（只读）。

端点：
- GET /v1/admin/board-sync/status: 板块/概念手动数据状态（board/concept）

权限：admin 角色（RBAC）。

[BOARD-LOCAL-OWNERSHIP-01] 语义边界：
- 板块/概念同步已迁出盘后 DAG，改为本地手动 `scripts/ops/panji-board-sync`
  （Mac 本地 cookie 访问问财 → SSH stdin → 生产 importer → sync_boards）。
- 本端点**只读**：**无**「立即同步」/服务端抓取问财端点。
- **无**频率/SLA 判定（is_stale / overdue / next_due_at 等一律不提供）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_roles
from app.schemas.board_sync import BoardSyncStatusResponse
from app.services.board_sync_status_service import get_board_sync_status

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin-board-sync"],
)


@router.get(
    "/board-sync/status",
    response_model=BoardSyncStatusResponse,
)
async def get_board_sync_status_endpoint(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_roles("admin")),
) -> BoardSyncStatusResponse:
    """返回板块/概念手动同步状态读模型（admin only，只读）。"""
    data = await get_board_sync_status(db)
    return BoardSyncStatusResponse(**data)
