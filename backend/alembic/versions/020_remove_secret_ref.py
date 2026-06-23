"""020 remove secret_ref from notification_channels

Revision ID: 020_remove_secret_ref
Revises: 019_stock_memo
Create Date: 2026-06-23

移除 notification_channels.secret_ref 列：
- 敏感字段改为存储在 target_config JSONB 中，API 读取时脱敏
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "020_remove_secret_ref"
down_revision: str | None = "019_stock_memo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("notification_channels", "secret_ref")


def downgrade() -> None:
    op.add_column(
        "notification_channels",
        sa.Column("secret_ref", sa.UUID(), nullable=True),
    )
