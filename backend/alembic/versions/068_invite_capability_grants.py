"""068 invite capability grants and invite_codes duration_months/revoked/redeemed fields

Revision ID: 068_invite_capability_grants
Revises: 067_scheduler_job_runs_lease_epoch_attempt_no
Create Date: 2026-07-25

变更内容（PRD V2.1 §8 数据模型）：
- 新增 invite_code_capabilities 表
  - 邀请码能力配置（每个邀请码可关联多个能力键）
  - CheckConstraint: capability_key IN ('watchlist_management','market_screening','review_management')
  - CheckConstraint: 自选能力 limit_value > 0；非自选能力 limit_value IS NULL
  - UNIQUE(invite_code_id, capability_key)
- 新增 user_capability_grants 表
  - 用户能力授权（每项能力独立 grant，支持多次兑换续期）
  - 同样的 capability_key CheckConstraint
  - 同样的 limit_value CheckConstraint
  - expires_at > starts_at
  - UNIQUE(source_type, source_id, capability_key)
  - 有效状态实时推导：revoked_at IS NULL AND starts_at <= now AND expires_at > now
- invite_codes 表新增字段（不破坏现有字段）：
  - duration_months INTEGER nullable（替代 grant_months，PRD §6.3 日历月）
  - revoked_at TIMESTAMPTZ nullable（替代 status='revoked' 推导，PRD §8.1）
  - redeemed_by_user_id UUID FK->users.id nullable（替代 used_by）
  - redeemed_at TIMESTAMPTZ nullable（替代 used_at）

设计说明：
- 不删除 invite_codes 旧字段（grant_months/grant_days/used_by/used_at/status），
  保留向后兼容；新代码优先使用 duration_months/revoked_at/redeemed_by_user_id/redeemed_at
- 新增 (user_id, capability_key) 复合索引便于按用户聚合查询有效 grant
- 新增 (invite_code_id) 索引便于按邀请码查询能力配置
- source_id 使用 VARCHAR(64) 而非 UUID，因为 legacy_subscription 的 source_id 可能是字符串

配合：
- app.models.capability_grant.InviteCodeCapability / UserCapabilityGrant（ORM 模型）
- app.constants.capability_keys（能力键常量）
- app.services.access_control_service（聚合方法，阶段B）
- 邀请码创建/兑换 API（阶段C/D）

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "068_invite_capability_grants"
down_revision: str | None = "067_scheduler_job_runs_lease_epoch_attempt_no"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 invite_code_capabilities / user_capability_grants 表，invite_codes 补充字段。"""
    # 1. invite_codes 表新增字段（PRD §8.1）
    op.add_column(
        "invite_codes",
        sa.Column(
            "duration_months",
            sa.Integer(),
            nullable=True,
            comment="授权月数（替代 grant_months，按日历月计算；>0）",
        ),
    )
    op.add_column(
        "invite_codes",
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="邀请码撤销时间（NULL=未撤销）",
        ),
    )
    op.add_column(
        "invite_codes",
        sa.Column(
            "redeemed_by_user_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="兑换用户 ID（NULL=未兑换；与 used_by 并存，新代码使用此字段）",
        ),
    )
    op.add_column(
        "invite_codes",
        sa.Column(
            "redeemed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="兑换时间（NULL=未兑换；与 used_at 并存，新代码使用此字段）",
        ),
    )
    # duration_months > 0（仅对非 NULL 值校验）
    op.create_check_constraint(
        "ck_invite_codes_duration_months_positive",
        "invite_codes",
        "duration_months IS NULL OR duration_months > 0",
    )

    # 2. 创建 invite_code_capabilities 表（PRD §8.2）
    op.create_table(
        "invite_code_capabilities",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "invite_code_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("invite_codes.id", ondelete="CASCADE"),
            nullable=False,
            comment="邀请码 ID",
        ),
        sa.Column(
            "capability_key",
            sa.String(64),
            nullable=False,
            comment="能力键 watchlist_management/market_screening/review_management",
        ),
        sa.Column(
            "limit_value",
            sa.Integer(),
            nullable=True,
            comment="自选额度（仅 watchlist_management 使用，正整数；其他能力为 NULL）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "invite_code_id",
            "capability_key",
            name="uq_invite_code_capability",
        ),
        sa.CheckConstraint(
            "capability_key IN ('watchlist_management', 'market_screening', 'review_management')",
            name="ck_invite_code_capability_key",
        ),
        sa.CheckConstraint(
            "(capability_key = 'watchlist_management' AND limit_value IS NOT NULL AND limit_value > 0) "
            "OR (capability_key != 'watchlist_management' AND limit_value IS NULL)",
            name="ck_invite_code_capability_limit",
        ),
    )
    op.create_index(
        "ix_invite_code_capabilities_invite_code_id",
        "invite_code_capabilities",
        ["invite_code_id"],
        unique=False,
    )

    # 3. 创建 user_capability_grants 表（PRD §8.3）
    op.create_table(
        "user_capability_grants",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            comment="用户 ID",
        ),
        sa.Column(
            "capability_key",
            sa.String(64),
            nullable=False,
            comment="能力键 watchlist_management/market_screening/review_management",
        ),
        sa.Column(
            "limit_value",
            sa.Integer(),
            nullable=True,
            comment="自选额度（仅 watchlist_management 使用，正整数；其他能力为 NULL）",
        ),
        sa.Column(
            "source_type",
            sa.String(32),
            nullable=False,
            comment="来源 invite_code/legacy_subscription/legacy_invite",
        ),
        sa.Column(
            "source_id",
            sa.String(64),
            nullable=False,
            comment="来源记录 ID（邀请码 ID 或旧订阅 ID 的字符串形式）",
        ),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="生效时间",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="到期时间（exclusive）",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="撤销时间（NULL=未撤销）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_by",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
            comment="创建人（管理员授予时记录，邀请码兑换时为 NULL）",
        ),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            "capability_key",
            name="uq_grant_source_capability",
        ),
        sa.CheckConstraint(
            "capability_key IN ('watchlist_management', 'market_screening', 'review_management')",
            name="ck_grant_capability_key",
        ),
        sa.CheckConstraint(
            "expires_at > starts_at",
            name="ck_grant_expires_after_starts",
        ),
        sa.CheckConstraint(
            "(capability_key = 'watchlist_management' AND limit_value IS NOT NULL AND limit_value > 0) "
            "OR (capability_key != 'watchlist_management' AND limit_value IS NULL)",
            name="ck_grant_limit_value",
        ),
    )
    op.create_index(
        "ix_user_capability_grants_user_id_capability_key",
        "user_capability_grants",
        ["user_id", "capability_key"],
        unique=False,
    )
    op.create_index(
        "ix_user_capability_grants_source",
        "user_capability_grants",
        ["source_type", "source_id"],
        unique=False,
    )


def downgrade() -> None:
    """回滚：删除 user_capability_grants / invite_code_capabilities 表，移除 invite_codes 新字段。"""
    # 1. 删除 user_capability_grants 表
    op.drop_index(
        "ix_user_capability_grants_source",
        table_name="user_capability_grants",
    )
    op.drop_index(
        "ix_user_capability_grants_user_id_capability_key",
        table_name="user_capability_grants",
    )
    op.drop_table("user_capability_grants")

    # 2. 删除 invite_code_capabilities 表
    op.drop_index(
        "ix_invite_code_capabilities_invite_code_id",
        table_name="invite_code_capabilities",
    )
    op.drop_table("invite_code_capabilities")

    # 3. 移除 invite_codes 新字段
    op.drop_constraint(
        "ck_invite_codes_duration_months_positive",
        "invite_codes",
        type_="check",
    )
    op.drop_column("invite_codes", "redeemed_at")
    op.drop_column("invite_codes", "redeemed_by_user_id")
    op.drop_column("invite_codes", "revoked_at")
    op.drop_column("invite_codes", "duration_months")
