"""059 user_table_view_presets - 用户表格视图配置表

Revision ID: 059_user_table_view_presets
Revises: 058_research_feature_matrix
Create Date: 2026-07-09

变更内容：
- 新增 user_table_view_presets 表（用户表格视图配置）
- 字段：id/user_id/table_id/strategy_key/name/config/is_default/created_at/updated_at
- 唯一约束：两个 partial unique index 解决 NULL strategy_key 重复问题
  * strategy_key IS NOT NULL → unique(user_id, table_id, strategy_key, name)
  * strategy_key IS NULL     → unique(user_id, table_id, name)
- 索引：(user_id, table_id, strategy_key) 用于查询和 quota 检查

设计说明：
- config 为 JSONB，仅允许 keyword/sort/filters/hiddenColumns/pageSize 五个字段
- 禁止保存 selectedKeys/page/activeRunId/rows 等业务数据（由 Pydantic schema 强制）
- 每 user+table_id+strategy_key 最多 20 个（由应用层 quota 检查）
- is_default 同维度至多 1 个 true（由应用层互斥更新）
- user_id 由认证上下文注入，不接受 body 传入
- strategy_key 可空：PostgreSQL 普通 UNIQUE 允许多个 NULL，故改用 partial unique index

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "059_user_table_view_presets"
down_revision: str | None = "058_research_feature_matrix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 user_table_view_presets 表。

    唯一约束用两个 partial unique index 实现：
    - strategy_key IS NOT NULL 时 unique(user_id, table_id, strategy_key, name)
    - strategy_key IS NULL 时 unique(user_id, table_id, name)
    这样 NULL strategy_key 场景下同名也会被拦截（普通 UNIQUE 因 NULL!=NULL 无法拦截）。
    """
    op.create_table(
        "user_table_view_presets",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            comment="主键 UUID（PostgreSQL 端 gen_random_uuid 兜底）",
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            comment="用户 ID（由认证上下文注入）",
        ),
        sa.Column(
            "table_id",
            sa.Text(),
            nullable=False,
            comment="表格标识（如 screener/watchlist，由前端约定）",
        ),
        sa.Column(
            "strategy_key",
            sa.Text(),
            nullable=True,
            comment="策略 key（可空，适用于无策略的表格）",
        ),
        sa.Column(
            "name",
            sa.Text(),
            nullable=False,
            comment="配置名称（用户自定义）",
        ),
        sa.Column(
            "config",
            JSONB,
            nullable=False,
            comment="配置内容（仅允许 keyword/sort/filters/hiddenColumns/pageSize）",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="是否默认配置（同 user+table_id+strategy_key 至多 1 个 true）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="更新时间（由 onupdate 自动维护）",
        ),
        comment="用户表格视图配置（保存筛选/排序/列设置，禁止保存业务数据）",
    )
    # [UniqueConstraint] - partial unique index：strategy_key 非空时约束 (user, table, strategy, name)
    op.create_index(
        "uq_user_table_view_preset_strategy_not_null",
        "user_table_view_presets",
        ["user_id", "table_id", "strategy_key", "name"],
        unique=True,
        postgresql_where=sa.text("strategy_key IS NOT NULL"),
    )
    # [UniqueConstraint] - partial unique index：strategy_key 为 NULL 时约束 (user, table, name)
    op.create_index(
        "uq_user_table_view_preset_strategy_null",
        "user_table_view_presets",
        ["user_id", "table_id", "name"],
        unique=True,
        postgresql_where=sa.text("strategy_key IS NULL"),
    )
    # 查询/quota 检查辅助索引
    op.create_index(
        "ix_user_table_view_presets_user_table_strategy",
        "user_table_view_presets",
        ["user_id", "table_id", "strategy_key"],
    )


def downgrade() -> None:
    """删除 user_table_view_presets 表。"""
    op.drop_index(
        "ix_user_table_view_presets_user_table_strategy",
        table_name="user_table_view_presets",
    )
    op.drop_index(
        "uq_user_table_view_preset_strategy_null",
        table_name="user_table_view_presets",
    )
    op.drop_index(
        "uq_user_table_view_preset_strategy_not_null",
        table_name="user_table_view_presets",
    )
    op.drop_table("user_table_view_presets")
