"""versioned review metric observations

Revision ID: 081_review_metric_observations
Revises: 080_review_hierarchy_attribution_evidence
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "081_review_metric_observations"
down_revision: str | None = "080_review_hierarchy_attribution_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "market_review_metric_observations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "review_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("market_review_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("metric_code", sa.Text(), nullable=False),
        sa.Column("component_name", sa.Text(), nullable=False),
        sa.Column("raw_value", sa.Numeric(), nullable=True),
        sa.Column("denominator", sa.Integer(), nullable=True),
        sa.Column("field_source_json", postgresql.JSONB(), nullable=False),
        sa.Column("weight_mode", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("input_hash", sa.Text(), nullable=False),
        sa.Column("membership_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            "metric_code",
            "component_name",
            name="uq_review_metric_observation_run_scope_component",
        ),
    )
    op.create_index(
        "ix_review_metric_observation_history",
        "market_review_metric_observations",
        [
            "scope_type",
            "scope_key",
            "algorithm_version",
            "metric_code",
            "component_name",
            "trade_date",
        ],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_review_metric_observation_history",
        table_name="market_review_metric_observations",
    )
    op.drop_table("market_review_metric_observations")
