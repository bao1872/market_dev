"""Review Discovery tracking (additive)

Revision ID: 089_review_discovery_tracking
Revises: 088_review_historical_lineage
Create Date: 2026-08-11

[REVIEW-V2-B3] Discovery tracking additive correction：
让用户追踪能区分「追踪某个 Discovery」与「追踪某个 Scope」，
不把 Discovery target 退化成 scope target。

CURRENT SCHEMA
- market_review_trackings.tracking_type 仅允许 signal/scope/instrument
  （CheckConstraint review_trackings_tracking_type_check）
- 无任何列保存 Discovery logical identity

WHY DISCOVERY IDENTITY IS LOST
- Discovery 是 review run 的派生 read model，discovery_id =
  sha256(run_id:scope_type:scope_key)[:12]，不持久化。
- 现有 scope 追踪只存 scope_type/scope_key，无法无歧义表达
  「追踪的是该 run 的某个具体 Discovery」这一语义。

MINIMUM ADDITIVE SHAPE
- 只扩展 tracking_type 允许值：+ 'discovery'
- 新增单列 discovery_id TEXT NULL：保存 Discovery logical identity
- scope_type/scope_key 仍可填写作 evaluation context，但 Discovery target
  以 discovery_id 为唯一身份（不退化成 scope target）
- 不新增 tracking 表 / scheduler / event system / 不重写状态机

本迁移只做 additive，不对现有行做任何改写。
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "089_review_discovery_tracking"
down_revision = "088_review_historical_lineage"
branch_labels = None
depends_on = None

TABLE = "market_review_trackings"
CONSTRAINT = "review_trackings_tracking_type_check"


def upgrade() -> None:
    op.add_column(
        TABLE,
        sa.Column(
            "discovery_id",
            sa.Text(),
            nullable=True,
            comment="Discovery logical identity（追踪 discovery 时填充）",
        ),
    )
    # 扩展 tracking_type 允许值
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "tracking_type IN ('signal','scope','instrument','discovery')",
    )


def downgrade() -> None:
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.create_check_constraint(
        CONSTRAINT,
        TABLE,
        "tracking_type IN ('signal','scope','instrument')",
    )
    op.drop_column(TABLE, "discovery_id")
