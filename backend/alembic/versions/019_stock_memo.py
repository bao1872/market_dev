"""019 stock_memos

Revision ID: 019_stock_memo
Revises: 018_multi_period_bars
Create Date: 2026-06-22

个股备忘录：
- stock_memos: 用户对个股的备忘录（user_id + instrument_id 唯一，每用户每股票一条）

字段说明：
- content: 备忘录文本内容
- notify_feishu: 是否在盘中监控时推送飞书
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "019_stock_memo"
down_revision: str | None = "018_multi_period_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_memos",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("notify_feishu", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "instrument_id", name="uq_stock_memo_user_instrument"),
    )
    op.create_index("ix_stock_memo_user_notify", "stock_memos", ["user_id", "notify_feishu"])


def downgrade() -> None:
    op.drop_index("ix_stock_memo_user_notify", table_name="stock_memos")
    op.drop_table("stock_memos")
