"""049 subscriptions table - 订阅表取代 memberships 表

Revision ID: 049_subscriptions_table
Revises: 048_plans_table
Create Date: 2026-06-30

变更内容：
- 创建 subscriptions 表（取代旧 memberships 表）
- 字段重命名：started_at → starts_at
- 字段新增：entitlement_snapshot（JSONB 权益快照）、source（来源）、
  created_by（创建人 FK users.id）、created_at（创建时间）
- 字段移除：monitor_limit（迁移到 entitlement_snapshot 中）
- 数据迁移：从 memberships 表插入 subscriptions，plan_code 为 NULL 时默认 'observe_20'，
  source 默认 'invite'，entitlement_snapshot/created_by 为 NULL
- 删除旧 memberships 表

业务背景：
- Phase 2 Task 2.2：subscriptions 表取代 memberships 表
- 一个用户一条订阅记录（user_id 唯一约束）
- 有效订阅实时计算：status='active' AND starts_at <= now AND expires_at > now
- entitlement_snapshot 从 plans 表快照 monitor_limit/notification_channel_limit/
  message_retention_days/features（迁移时为 NULL，由后续业务逻辑按需快照）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "049_subscriptions_table"
down_revision: str | None = "048_plans_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Subscription] - 描述: 创建 subscriptions 表（取代 memberships）
    op.create_table(
        "subscriptions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="用户 ID（唯一，一个用户一条订阅记录）",
        ),
        sa.Column(
            "plan_code",
            sa.String(length=32),
            nullable=False,
            comment="套餐代码 observe_20/research_50",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="active/expired",
        ),
        sa.Column(
            "starts_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="订阅生效时间",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="订阅到期时间",
        ),
        sa.Column(
            "entitlement_snapshot",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="权益快照（monitor_limit/notification_channel_limit/message_retention_days/features）",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'invite'"),
            comment="来源 invite/admin_grant",
        ),
        sa.Column(
            "created_by",
            UUID(as_uuid=True),
            nullable=True,
            comment="创建人 user_id（管理员授予时记录，邀请码兑换时为 NULL）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint(
            "status IN ('active','expired')", name="subscriptions_status_check"
        ),
        sa.CheckConstraint(
            "source IN ('invite','admin_grant')", name="subscriptions_source_check"
        ),
    )
    # [Subscription] - 描述: user_id 唯一索引（与 unique 约束互为补充，加速按用户查询订阅）
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    # [Subscription] - 描述: 数据迁移 memberships -> subscriptions
    # 字段映射：started_at -> starts_at；plan_code 为 NULL 时默认 'observe_20'；
    # source 默认 'invite'；entitlement_snapshot/created_by 为 NULL；
    # created_at 用旧 memberships.updated_at 回填（旧表无 created_at 字段）
    op.execute(
        """
        INSERT INTO subscriptions (id, user_id, plan_code, status, starts_at,
            expires_at, entitlement_snapshot, source, created_by, created_at, updated_at)
        SELECT
            id,
            user_id,
            COALESCE(plan_code, 'observe_20'),
            status,
            started_at,
            expires_at,
            NULL,
            'invite',
            NULL,
            updated_at,
            updated_at
        FROM memberships
        """
    )

    # [Subscription] - 描述: 删除旧 memberships 表
    op.drop_index("ix_memberships_status", table_name="memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")


def downgrade() -> None:
    # [Subscription] - 描述: 重建 memberships 表（回滚 subscriptions 改动）
    op.create_table(
        "memberships",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'active'")
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "plan_code", sa.String(length=32), nullable=True,
            comment="当前套餐代码 observe_20/research_50",
        ),
        sa.Column(
            "monitor_limit", sa.Integer(), nullable=True,
            comment="当前套餐监控数量上限",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
        sa.CheckConstraint(
            "status IN ('active','expired')", name="memberships_status_check"
        ),
    )
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_index("ix_memberships_status", "memberships", ["status"])

    # [Subscription] - 描述: 数据回迁 subscriptions -> memberships
    # 字段映射：starts_at -> started_at；monitor_limit 回填 NULL（旧表允许 NULL）
    op.execute(
        """
        INSERT INTO memberships (id, user_id, status, started_at, expires_at,
            plan_code, monitor_limit, updated_at)
        SELECT
            id,
            user_id,
            status,
            starts_at,
            expires_at,
            plan_code,
            NULL,
            updated_at
        FROM subscriptions
        """
    )

    # [Subscription] - 描述: 删除 subscriptions 表
    op.drop_index("ix_subscriptions_status", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")


if __name__ == "__main__":
    # [Subscription] - 描述: 自测入口，验证 revision 链与函数定义（不连接数据库）
    assert revision == "049_subscriptions_table"
    assert down_revision == "048_plans_table"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
