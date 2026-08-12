"""090 scope observation facts - Canonical Observation Fact persistence (PRD §7.9)

Revision ID: 090_scope_observation_facts
Revises: 089_review_discovery_tracking
Create Date: 2026-08-12

变更内容（Round 1C — Canonical Observation Fact Persistence）：
- 新增 review_scope_observation_facts：每日客观事实快照（PRD §7.9）
- 业务 grain：trade_date + scope_type + scope_key → one daily fact snapshot
- 唯一约束 uq_review_scope_observation_facts_day_scope (trade_date, scope_type, scope_key)
- 只保存 Core output objective facts；不保存 score / 机会 / 风险 / 强弱 / 推荐 /
  ranking / Filter / Discovery 判断
- 无 review_run FK / publication FK / revision / version / pointer / revision chain

设计要点：
- UUID 主键（server_default=gen_random_uuid()）
- observation_payload 为 JSONB，保存 Canonical Observation Core output 原样
- diagnostics JSONB（server_default=[]）
- t1_membership_available BOOLEAN server_default=false

非破坏性：
- 纯新增表，不修改任何历史迁移（down_revision=089）
- 部署后表为空，由 shadow persistence 路径异步填充
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "090_scope_observation_facts"
down_revision: str | None = "089_review_discovery_tracking"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_scope_observation_facts",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("trade_date", sa.Date(), nullable=False, comment="业务交易日"),
        sa.Column(
            "scope_type",
            sa.Text(),
            nullable=False,
            comment="范围类型（industry_l1/l2/l3/concept）",
        ),
        sa.Column("scope_key", sa.Text(), nullable=False, comment="范围标识"),
        sa.Column("scope_name", sa.Text(), nullable=True, comment="范围名称（冗余展示元数据）"),
        sa.Column("canonical_t1", sa.Date(), nullable=True, comment="实际使用的 T-1 交易日"),
        sa.Column(
            "pit_member_count",
            sa.Integer(),
            nullable=False,
            comment="PIT(T) 成员数（denominator）",
        ),
        sa.Column("pit_member_count_t1", sa.Integer(), nullable=True, comment="PIT(T-1) 成员数"),
        sa.Column(
            "provided_member_count",
            sa.Integer(),
            nullable=True,
            comment="实际提供的成员观察数",
        ),
        sa.Column(
            "t1_membership_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
            comment="T-1 PIT membership 是否真实可用",
        ),
        sa.Column(
            "pit_status_t",
            sa.Text(),
            nullable=False,
            comment="PIT(T) 状态：historical_pit/ready/unavailable 等",
        ),
        sa.Column("pit_status_t1", sa.Text(), nullable=True, comment="PIT(T-1) 状态"),
        sa.Column(
            "readiness",
            sa.Text(),
            nullable=False,
            comment="snapshot-level readiness（现有明确状态导出，无阈值）",
        ),
        sa.Column(
            "observation_payload",
            JSONB(),
            nullable=False,
            comment="Canonical Observation Core output 原样（PRD §7.9.3）",
        ),
        sa.Column(
            "diagnostics",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="准备/计算诊断信息",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=True,
            comment="算法版本（仅 metadata/lineage，不作为唯一键）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "trade_date",
            "scope_type",
            "scope_key",
            name="uq_review_scope_observation_facts_day_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("review_scope_observation_facts")
