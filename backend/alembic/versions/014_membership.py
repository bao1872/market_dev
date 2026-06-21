"""014 memberships, invite_codes, invite_redemptions

Revision ID: 014_membership
Revises: 013_user_watchlist
Create Date: 2026-06-20

会员与邀请码系统（V1.6 新增）：
- memberships: 会员状态表（user_id 唯一，status active/expired，started_at/expires_at）
- invite_codes: 邀请码表（code_hash 唯一，status unused/used/revoked，grant_days 固定 30）
- invite_redemptions: 邀请码兑换记录（invite_code_id + user_id，记录 old/new expires_at）

依赖：001_users（users 表）
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014_membership"
down_revision: str | None = "013_user_watchlist"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 会员状态表
    op.create_table(
        "memberships",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint("status IN ('active','expired')", name="memberships_status_check"),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_status", "memberships", ["status"])

    # 邀请码表
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="'unused'"),
        sa.Column("grant_days", sa.Integer(), nullable=False, server_default=sa.text("30")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("used_by", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_type", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["used_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
        sa.CheckConstraint("status IN ('unused','used','revoked')", name="invite_codes_status_check"),
        sa.CheckConstraint("usage_type IN ('registration','renewal')", name="invite_codes_usage_type_check"),
    )
    op.create_index("ix_invite_codes_status", "invite_codes", ["status"])
    op.create_index("ix_invite_codes_created_by", "invite_codes", ["created_by"])

    # 邀请码兑换记录表
    op.create_table(
        "invite_redemptions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("invite_code_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("usage_type", sa.Text(), nullable=False),
        sa.Column("old_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("new_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["invite_code_id"], ["invite_codes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("usage_type IN ('registration','renewal')", name="invite_redemptions_usage_type_check"),
    )
    op.create_index("ix_invite_redemptions_user_id", "invite_redemptions", ["user_id"])
    op.create_index("ix_invite_redemptions_invite_code_id", "invite_redemptions", ["invite_code_id"])


def downgrade() -> None:
    op.drop_index("ix_invite_redemptions_invite_code_id", table_name="invite_redemptions")
    op.drop_index("ix_invite_redemptions_user_id", table_name="invite_redemptions")
    op.drop_table("invite_redemptions")
    op.drop_index("ix_invite_codes_created_by", table_name="invite_codes")
    op.drop_index("ix_invite_codes_status", table_name="invite_codes")
    op.drop_table("invite_codes")
    op.drop_index("ix_memberships_status", table_name="memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")
