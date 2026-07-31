"""023 create strategy_event_recipients table

Revision ID: 023_event_recips
Revises: 022_monitor_evals
Create Date: 2026-06-23

事件接收人表 - 记录每个策略事件应通知的用户：
- id: UUID PK (server_default gen_random_uuid)
- event_id: 策略事件 FK
- user_id: 用户 FK
- watchlist_item_id: 自选股记录 FK（可空，标识用户通过哪条自选股关联）
- preference_snapshot: 通知偏好快照 JSONB（可空，记录事件时刻的用户偏好）
- created_at: 创建时间

唯一约束: (event_id, user_id) - 同一事件同一用户只接收一次
索引: (user_id) - 查询用户相关事件
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "023_event_recips"
down_revision: str | None = "022_monitor_evals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_event_recipients",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "event_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_events.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "watchlist_item_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_watchlist_items.id"),
            nullable=True,
        ),
        sa.Column(
            "preference_snapshot",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="通知偏好快照 JSONB（事件时刻的用户偏好）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "event_id",
            "user_id",
            name="uq_event_recipients_event_user",
        ),
    )
    op.create_index(
        "ix_event_recipients_user_id",
        "strategy_event_recipients",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_event_recipients_user_id",
        table_name="strategy_event_recipients",
    )
    op.drop_table("strategy_event_recipients")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "023_event_recips"
    assert down_revision == "022_monitor_evals"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
