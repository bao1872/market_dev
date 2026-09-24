"""Board/Concept 手动同步状态读模型（[BOARD-LOCAL-OWNERSHIP-01]）。

独立于盘后 DAG：board/concept 同步已迁出 after-close，改为本地手动
`scripts/ops/panji-board-sync` + 生产 importer。本模块只读：

- 当前库计数：复用 `board_sync_service.get_current_detailed_counts()`（计数单一 owner）。
- last_success_at：`MAX(MarketBoard.updatedAt) WHERE isActive = true`
  （成功的 `sync_boards()` 会刷新有效 board 的 updated_at，故它是「最近一次成功同步」）。
- recent_attempt：`board_sync_service.get_sync_status()`（Redis 短期诊断；不可用→None）。

**无频率/SLA 语义**：不计算 is_stale / overdue / next_due_at；
最近一次尝试失败但库中仍有有效数据 → available 仍为 true。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard
from app.services.board_sync_service import (
    get_current_detailed_counts,
    get_sync_status,
)

logger = logging.getLogger(__name__)


async def get_board_sync_status(db: AsyncSession) -> dict[str, Any]:
    """返回手动板块同步状态读模型（board/concept）。

    Returns:
        dict，字段见 `app.schemas.board_sync.BoardSyncStatusResponse`。
    """
    counts = await get_current_detailed_counts(db)

    # last_success_at 仅在有有效 board 时才有意义；无 active board → null。
    if counts["board_count"] > 0:
        last_success_at = await db.scalar(
            select(func.max(MarketBoard.updatedAt)).where(
                MarketBoard.isActive.is_(True)
            )
        )
    else:
        last_success_at = None

    # Redis 最近尝试状态：不可用时为 None，**不**影响 DB 状态可用性。
    recent_attempt: dict[str, Any] | None
    try:
        recent_attempt = await get_sync_status()
    except Exception as exc:  # noqa: BLE001 - Redis 缺失不得使 DB 状态不可用
        logger.warning("[BoardSyncStatus] 读取 Redis 最近状态失败: %s", exc)
        recent_attempt = None

    return {
        "mode": "local_manual",
        "source": "wencai",
        # 有有效板块数据即视为可用；年龄/最近失败不改变该判定。
        "available": counts["board_count"] > 0,
        "last_success_at": (
            last_success_at.isoformat() if last_success_at is not None else None
        ),
        "board_count": counts["board_count"],
        "industry_count": counts["industry_count"],
        "concept_count": counts["concept_count"],
        "membership_count": counts["membership_count"],
        "stock_count": counts["stock_count"],
        "recent_attempt": recent_attempt,
    }


__all__ = ["get_board_sync_status"]
