"""091 observation run lineage - ReviewRun FK + Composition snapshot (REVIEW-BACKEND-FINAL-CLOSURE)

Revision ID: 091_observation_run_lineage
Revises: 090_scope_observation_facts
Create Date: 2026-08-19

变更内容（P0 run lineage + Composition persistence）：

1. review_scope_observation_facts 增加 review_run_id 列（FK → market_review_runs.id
   ondelete CASCADE）；唯一约束从 (trade_date, scope_type, scope_key) 改为
   (review_run_id, trade_date, scope_type, scope_key)。
   - 目的：避免同日双 run（Run A published，Run B 同 trade_date 后跑）覆盖 Observation
     导致 Composition A 被 B 污染的 lineage contamination。
   - review_run_id 设为 nullable=True 兼容历史无 binding 行；PostgreSQL 中 NULL 不
     参与唯一约束，旧行互不冲突。运行时新写入均带 review_run_id。

2. 新增 review_scope_composition_snapshots：Canonical Scope Composition 薄快照，
   一 scope / run 一行（grain = review_run_id + scope_type + scope_key），保存完整
   Composition（Dynamics / Internal Structure / Leadership / Member Attribution /
   readiness）。payload JSONB + algorithm_version，不写死结构，未来换版本即可。

非破坏性：
- 纯加列 + 重建唯一约束（drop 旧 / create 新）+ 新增表，不修改任何历史行数据语义。
- 历史 ORM 表（MarketReviewScopeSnapshot 等 P/Q/U/C/V）不动、不 DROP。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "091_observation_run_lineage"
down_revision: str | None = "090_scope_observation_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. review_scope_observation_facts: +review_run_id + 重建唯一约束 ---
    op.add_column(
        "review_scope_observation_facts",
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=True,
            comment="生成该 fact 的 ReviewRun（lineage）；历史行可为 NULL",
        ),
    )
    op.create_index(
        "ix_review_scope_observation_facts_run",
        "review_scope_observation_facts",
        ["review_run_id"],
    )
    # 重建唯一约束（drop 旧 day-scope 组合 → create 新 run-day-scope 组合）
    op.drop_constraint(
        "uq_review_scope_observation_facts_day_scope",
        "review_scope_observation_facts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_review_scope_observation_facts_run_day_scope",
        "review_scope_observation_facts",
        ["review_run_id", "trade_date", "scope_type", "scope_key"],
    )

    # --- 2. review_scope_composition_snapshots: 新增薄表 ---
    op.create_table(
        "review_scope_composition_snapshots",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column(
            "review_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("composition_payload", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_unique_constraint(
        "uq_review_scope_composition_run_scope",
        "review_scope_composition_snapshots",
        ["review_run_id", "scope_type", "scope_key"],
    )
    op.create_index(
        "ix_review_scope_composition_run_date",
        "review_scope_composition_snapshots",
        ["review_run_id", "trade_date"],
    )


def downgrade() -> None:
    # --- 2. 删 composition 表 ---
    op.drop_index("ix_review_scope_composition_run_date", table_name="review_scope_composition_snapshots")
    op.drop_constraint("uq_review_scope_composition_run_scope", "review_scope_composition_snapshots", type_="unique")
    op.drop_table("review_scope_composition_snapshots")

    # --- 1. 还原 observation facts 唯一约束 + 删 review_run_id ---
    op.drop_constraint(
        "uq_review_scope_observation_facts_run_day_scope",
        "review_scope_observation_facts",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_review_scope_observation_facts_day_scope",
        "review_scope_observation_facts",
        ["trade_date", "scope_type", "scope_key"],
    )
    op.drop_index("ix_review_scope_observation_facts_run", table_name="review_scope_observation_facts")
    op.drop_column("review_scope_observation_facts", "review_run_id")
