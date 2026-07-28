"""070 worker_heartbeats add stopped_at column (Gate4)

Revision ID: 070_worker_heartbeat_stopped_at
Revises: 069_invite_code_capabilities
Create Date: 2026-07-28

变更内容（Gate4 Worker 心跳收口）：
- worker_heartbeats 表新增 stopped_at 列（nullable, timestamptz）
  - Worker 退出（_heartbeat_loop 收到 _shutdown）时写入 stopped_at = now
  - mark_stale_worker_heartbeats 标记僵尸心跳为 stopped 时同步写入 stopped_at = now
  - NULL 表示当前 running/idle，或历史记录无 stopped_at（向后兼容）

非破坏性：
- 只新增列（nullable），不修改/删除现有列
- 旧记录 stopped_at=NULL，UI 在 status=stopped 时回退显示 heartbeat_at
- 不删除审计数据，started_at/heartbeat_at/build_sha 全部保留

设计说明：
- stopped_at 与 heartbeat_at 分离：heartbeat_at 表示最后一次心跳时间（运行中持续更新），
  stopped_at 表示 Worker 停止时间（仅在退出时写入一次），二者语义不同
- 前端根据 status 区分展示：
  - running/idle: 显示 "距上次心跳 Xs"（基于 heartbeat_at）
  - stopped: 显示 "已停止于 YYYY-MM-DD HH:MM:SS"（基于 stopped_at，回退 heartbeat_at）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "070_worker_heartbeat_stopped_at"
down_revision: str | None = "069_invite_code_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给 worker_heartbeats 表添加 stopped_at 列。"""
    op.add_column(
        "worker_heartbeats",
        sa.Column(
            "stopped_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Worker 停止时间（Gate4）；NULL 表示运行中或历史记录无此字段",
        ),
    )


def downgrade() -> None:
    """删除 worker_heartbeats.stopped_at 列。"""
    op.drop_column("worker_heartbeats", "stopped_at")
