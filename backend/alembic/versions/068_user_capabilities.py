"""068 user_capabilities table for independent capability grants (PRD60 PA-01)

Revision ID: 068_user_capabilities
Revises: 067_scheduler_job_runs_lease_epoch_attempt_no
Create Date: 2026-07-27

变更内容（PRD60 PA-01/PA-02/PA-03）：
- 新增 user_capabilities 表（三类独立权限授权）
  - capability: self_selection / market_data / research_replay
  - watchlist_limit: 仅 self_selection 使用（PA-02 管理员自定义）
  - expires_at: per-capability 独立自然月有效期（PA-03）
  - (user_id, capability) 唯一约束
- 从现有有效订阅确定性回填（不降权/增权）：
  - observe_20 → self_selection(limit=20) + market_data
  - research_50 → self_selection(limit=50) + market_data + research_replay
  - source='migration'，granted_by=NULL
  - 仅回填 status='active' AND expires_at > NOW() 的订阅

非破坏性：
- 只新建表 + INSERT，不修改/删除现有 subscriptions/invite_codes 表
- 旧 Subscription/plan_code 保留兼容期（新读取优先、旧数据 fallback）
- ON CONFLICT DO NOTHING 保证幂等

配合：
- app.models.user_capability.UserCapability（模型字段）
- app.services.access_control_service.require_capability（权限检查）
- 前端 CapabilityRoute（路由级守卫）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "068_user_capabilities"
down_revision: str | None = "067_scheduler_job_runs_lease_epoch_attempt_no"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 user_capabilities 表 + 从有效订阅回填。"""
    # 1. 创建 user_capabilities 表
    op.create_table(
        "user_capabilities",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability", sa.String(32), nullable=False,
                  comment="self_selection/market_data/research_replay"),
        sa.Column("watchlist_limit", sa.Integer(), nullable=True,
                  comment="仅 self_selection 使用"),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False,
                  server_default=sa.text("'invite_code'")),
        sa.Column("granted_by", sa.dialects.postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.UniqueConstraint("user_id", "capability",
                            name="uq_user_capabilities_user_capability"),
    )
    op.create_index(
        "ix_user_capabilities_user_id", "user_capabilities", ["user_id"]
    )

    # 2. 从现有有效订阅确定性回填（不降权/增权）
    # observe_20 → self_selection(limit=20) + market_data
    # research_50 → self_selection(limit=50) + market_data + research_replay
    # 仅回填 status='active' AND expires_at > NOW() 的订阅
    op.execute("""
        INSERT INTO user_capabilities (id, user_id, capability, watchlist_limit, granted_at, expires_at, source, granted_by, created_at)
        SELECT
            gen_random_uuid(),
            s.user_id,
            c.capability,
            c.watchlist_limit,
            s.starts_at,
            s.expires_at,
            'migration',
            NULL,
            NOW()
        FROM subscriptions s
        CROSS JOIN LATERAL (
            SELECT 'self_selection' AS capability,
                   CASE WHEN s.plan_code = 'observe_20' THEN 20
                        WHEN s.plan_code = 'research_50' THEN 50
                        ELSE NULL END AS watchlist_limit
            UNION ALL
            SELECT 'market_data' AS capability, NULL AS watchlist_limit
            UNION ALL
            SELECT 'research_replay' AS capability, NULL AS watchlist_limit
            WHERE s.plan_code = 'research_50'
        ) c
        WHERE s.status = 'active'
          AND s.expires_at > NOW()
        ON CONFLICT (user_id, capability) DO NOTHING
    """)


def downgrade() -> None:
    """删除 user_capabilities 表（仅删表，不影响 subscriptions）。"""
    op.drop_index("ix_user_capabilities_user_id", table_name="user_capabilities")
    op.drop_table("user_capabilities")
