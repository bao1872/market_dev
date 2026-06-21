"""010 config_definitions

Revision ID: 010_config
Revises: 009_notification
Create Date: 2026-06-18

配置注册表（DDL 缺失，按 V1.1 06_CONFIGURATION_CENTER.md + config_definition.schema.json 补充）：
- config_definitions: 配置项定义（config_key 唯一，value_type 区分类型，sensitivity 区分敏感度）

字段说明：
- value_type: string/integer/number/boolean/enum/duration/time/json/secret/url
- allowed_scopes: system/plan/strategy/user/resource/runtime
- sensitivity: public/internal/secret
- restart_policy: immediate/worker_reload/restart/redeploy/new_strategy_version
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "010_config"
down_revision: str | None = "009_notification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "config_definitions",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("config_key", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("allowed_scopes", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("default_value", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("current_value", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("validation", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("sensitivity", sa.Text(), nullable=False, server_default="'public'"),
        sa.Column("restart_policy", sa.Text(), nullable=False, server_default="'immediate'"),
        sa.Column("ui", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("test_action", sa.Text(), nullable=True),
        sa.Column("audit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.Text(), nullable=False, server_default="'active'"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("config_key"),
        sa.CheckConstraint(
            "value_type IN ('string','integer','number','boolean','enum','duration','time','json','secret','url')",
            name="config_definitions_value_type_check",
        ),
        sa.CheckConstraint(
            "sensitivity IN ('public','internal','secret')",
            name="config_definitions_sensitivity_check",
        ),
        sa.CheckConstraint(
            "restart_policy IN ('immediate','worker_reload','restart','redeploy','new_strategy_version')",
            name="config_definitions_restart_policy_check",
        ),
    )
    op.create_index("ix_config_definitions_key", "config_definitions", ["config_key"])


def downgrade() -> None:
    op.drop_index("ix_config_definitions_key", table_name="config_definitions")
    op.drop_table("config_definitions")
