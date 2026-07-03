"""050 drop memberships table - 删除旧 memberships 表

Revision ID: 050_drop_memberships_table
Revises: 049_subscriptions_table
Create Date: 2026-06-30

变更内容：
- 删除旧 memberships 表及其索引（数据已由 049 迁移至 subscriptions）
- 与 049 拆分为独立迁移，便于单独回滚 subscriptions 表结构而不丢失 memberships 数据

业务背景：
- Phase 8：049 创建 subscriptions 表并迁移数据（保留 memberships），050 单独删除 memberships
- 拆分原因：049 修改了 subscriptions 的 CheckConstraint / NOT NULL 等结构约束，
  若 049 同时删除 memberships，则 downgrade 无法回滚数据；拆分后 049 downgrade 仅
  清理 subscriptions，memberships 数据完整保留，050 downgrade 再从 subscriptions 回迁

downgrade 行为：
- 重建 memberships 表（结构与 048 之前一致：status 仅 active/expired）
- 从 subscriptions 回迁数据：starts_at->started_at；monitor_limit 从
  entitlement_snapshot->>'monitor_limit' 提取；status 还原为旧语义
  （active 且未过期 -> 'active'，其余 revoked/cancelled/已过期 -> 'expired'）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "050_drop_memberships_table"
down_revision: str | None = "049_subscriptions_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Subscription] - 描述: 删除旧 memberships 表（数据已由 049 迁移至 subscriptions）
    op.drop_index("ix_memberships_status", table_name="memberships")
    op.drop_index("ix_memberships_user_id", table_name="memberships")
    op.drop_table("memberships")


def downgrade() -> None:
    # [Subscription] - 描述: 重建 memberships 表（结构与 048 之前一致）
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
    # 字段映射：starts_at -> started_at；monitor_limit 从 entitlement_snapshot->>'monitor_limit'
    # 提取为整数；status 还原为旧语义（旧表仅 active/expired）：
    #   active 且未过期 -> 'active'；revoked/cancelled/已过期 -> 'expired'
    op.execute(
        """
        INSERT INTO memberships (id, user_id, status, started_at, expires_at,
            plan_code, monitor_limit, updated_at)
        SELECT
            id,
            user_id,
            CASE WHEN status = 'active' AND expires_at > now() THEN 'active'
                 ELSE 'expired' END,
            starts_at,
            expires_at,
            plan_code,
            (entitlement_snapshot->>'monitor_limit')::integer,
            updated_at
        FROM subscriptions
        """
    )


if __name__ == "__main__":
    # [Subscription] - 描述: 自测入口，验证 revision 链与函数定义（不连接数据库）
    assert revision == "050_drop_memberships_table"
    assert down_revision == "049_subscriptions_table"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
