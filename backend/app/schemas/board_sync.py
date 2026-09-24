"""Board/Concept 手动同步状态读模型 schema（[BOARD-LOCAL-OWNERSHIP-01]）。

语义边界（严格）：
- 这是**独立的手动数据状态**，**不是**盘后 DAG 步骤，也**不**参与 daily freshness/SLA。
- **无** is_stale / stale_after_days / next_due_at / overdue / required_frequency：
  年龄仅作信息展示；旧的最后成功时间**不**导致 available=false。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BoardSyncStatusResponse(BaseModel):
    """`GET /v1/admin/board-sync/status` 响应。"""

    mode: str = Field(default="local_manual", description="更新方式：本地手动同步")
    source: str = Field(default="wencai", description="数据源：问财")
    available: bool = Field(
        description="当前库中是否存在可用板块/成分数据（board_count > 0）"
    )
    last_success_at: str | None = Field(
        default=None, description="最近成功同步时间（DB 最新有效 board 的 updated_at）"
    )
    board_count: int = 0
    industry_count: int = 0
    concept_count: int = 0
    membership_count: int = 0
    stock_count: int = 0
    recent_attempt: dict[str, Any] | None = Field(
        default=None, description="最近一次同步尝试（Redis 短期诊断；不可用时为 null）"
    )


__all__ = ["BoardSyncStatusResponse"]
