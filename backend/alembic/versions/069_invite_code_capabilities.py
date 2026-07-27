"""069 invite_codes add capabilities JSONB column (PRD60 PA-20)

Revision ID: 069_invite_code_capabilities
Revises: 068_user_capabilities
Create Date: 2026-07-27

变更内容（PRD60 PA-20）：
- invite_codes 表新增 capabilities JSONB 列（nullable）
  - 格式: [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}, ...]
  - NULL 时回退到 plan_code/monitor_limit/grant_months 旧逻辑（兼容期）

非破坏性：
- 只新增列（nullable），不修改/删除现有列
- 旧邀请码 capabilities=NULL，兑换时 fallback 到 plan_code
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "069_invite_code_capabilities"
down_revision: str | None = "068_user_capabilities"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """给 invite_codes 表添加 capabilities JSONB 列。"""
    op.add_column(
        "invite_codes",
        sa.Column(
            "capabilities",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text(), none_as_null=True),
            nullable=True,
            comment="capability 组合（PA-20）；NULL 时 fallback 到 plan_code",
        ),
    )


def downgrade() -> None:
    """删除 invite_codes.capabilities 列。"""
    op.drop_column("invite_codes", "capabilities")
