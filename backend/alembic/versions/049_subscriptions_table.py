"""049 subscriptions table - 订阅表取代 memberships 表

Revision ID: 049_subscriptions_table
Revises: 048_plans_table
Create Date: 2026-06-30

变更内容：
- 创建 subscriptions 表（取代旧 memberships 表，但本迁移不删除 memberships 表）
- 字段重命名：started_at → starts_at
- 字段新增：entitlement_snapshot（JSONB 权益快照，NOT NULL）、source（来源）、
  created_by（创建人 FK users.id）、created_at（创建时间）
- 字段移除：monitor_limit（迁移到 entitlement_snapshot 中）
- CheckConstraint：status 仅允许 active/revoked/cancelled（expired 实时计算，不持久化）；
  source 仅允许 invite/admin_grant/migration
- 数据迁移：从 memberships 表插入 subscriptions
  - plan_code 为 NULL 时 COALESCE 到 'observe_20'，再 INNER JOIN plans 表生成
    entitlement_snapshot（monitor_limit/notification_channel_limit/
    message_retention_days/features）
  - source='migration'（旧 memberships 数据迁移来源）
  - status='expired' 转为 'active'（新模型不持久化 expired，到期由 expires_at 实时计算）
- 不删除 memberships 表（由 050_drop_memberships_table 独立迁移删除，便于回滚）

业务背景：
- Phase 2 Task 2.2：subscriptions 表取代 memberships 表
- Phase 8：status 不持久化 expired；entitlement_snapshot 改为 NOT NULL 并从 plans 表快照
- 一个用户一条订阅记录（user_id 唯一约束）
- 有效订阅实时计算：status='active' AND starts_at <= now AND expires_at > now
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
            comment="active/revoked/cancelled（expired 实时计算，不持久化）",
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
            nullable=False,
            comment="权益快照（monitor_limit/notification_channel_limit/message_retention_days/features）",
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'invite'"),
            comment="来源 invite/admin_grant/migration",
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
            "status IN ('active','revoked','cancelled')",
            name="subscriptions_status_check",
        ),
        sa.CheckConstraint(
            "source IN ('invite','admin_grant','migration')",
            name="subscriptions_source_check",
        ),
    )
    # [Subscription] - 描述: user_id 唯一索引（与 unique 约束互为补充，加速按用户查询订阅）
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])

    # [Subscription] - 描述: 数据迁移 memberships -> subscriptions
    # 字段映射：started_at -> starts_at；plan_code 为 NULL 时 COALESCE 到 'observe_20'；
    # entitlement_snapshot 从 plans 表 INNER JOIN 生成（COALESCE 后 plan_code 必命中 plans）；
    # source='migration'（旧数据迁移来源）；status='expired' 转为 'active'
    # （新模型不持久化 expired，到期由 expires_at 实时计算，旧 memberships 仅有 active/expired）；
    # created_at 用旧 memberships.updated_at 回填（旧表无 created_at 字段）
    op.execute(
        """
        INSERT INTO subscriptions (id, user_id, plan_code, status, starts_at,
            expires_at, entitlement_snapshot, source, created_by, created_at, updated_at)
        SELECT
            m.id,
            m.user_id,
            COALESCE(m.plan_code, 'observe_20'),
            CASE WHEN m.status = 'expired' THEN 'active' ELSE m.status END,
            m.started_at,
            m.expires_at,
            jsonb_build_object(
                'monitor_limit', p.monitor_limit,
                'notification_channel_limit', p.notification_channel_limit,
                'message_retention_days', p.message_retention_days,
                'features', p.features
            ),
            'migration',
            NULL,
            m.updated_at,
            m.updated_at
        FROM memberships m
        INNER JOIN plans p ON p.plan_code = COALESCE(m.plan_code, 'observe_20')
        """
    )
    # 注意：本迁移不删除 memberships 表，由 050_drop_memberships_table 独立迁移删除


def downgrade() -> None:
    # [Subscription] - 描述: 回滚 049 - 仅删除 subscriptions 表（保留 memberships 不动）
    # 049 未删除 memberships，故 downgrade 无需重建 memberships，只需清理 subscriptions
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
