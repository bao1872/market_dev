"""review hierarchy attribution evidence

Revision ID: 080_review_hierarchy_attribution_evidence
Revises: 079_board_hierarchy_batch_identity
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "080_review_hierarchy_attribution_evidence"
down_revision: str | None = "079_board_hierarchy_batch_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_review_scope_snapshots",
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
    )
    op.add_column(
        "market_review_scope_snapshots",
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "market_review_scope_snapshots",
        sa.Column("membership_version", sa.Text(), nullable=True),
    )

    op.add_column(
        "market_review_signal_attributions",
        sa.Column("source_board_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("membership_version", sa.Text(), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("eligible_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("ready_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "market_review_signal_attributions",
        sa.Column("data_quality_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_foreign_key(
        "fk_review_attribution_board_snapshot",
        "market_review_signal_attributions",
        "board_analysis_snapshots",
        ["source_board_snapshot_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_review_attributions_board_snapshot",
        "market_review_signal_attributions",
        ["source_board_snapshot_id"],
    )

    op.add_column(
        "market_review_signal_instruments",
        sa.Column("contribution_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "market_review_signal_instruments",
        sa.Column("role_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("market_review_signal_instruments", "role_evidence")
    op.drop_column("market_review_signal_instruments", "contribution_payload")

    op.drop_index(
        "ix_review_attributions_board_snapshot",
        table_name="market_review_signal_attributions",
    )
    op.drop_constraint(
        "fk_review_attribution_board_snapshot",
        "market_review_signal_attributions",
        type_="foreignkey",
    )
    op.drop_column("market_review_signal_attributions", "data_quality_json")
    op.drop_column("market_review_signal_attributions", "ready_count")
    op.drop_column("market_review_signal_attributions", "eligible_count")
    op.drop_column("market_review_signal_attributions", "membership_version")
    op.drop_column("market_review_signal_attributions", "taxonomy_compatibility_key")
    op.drop_column("market_review_signal_attributions", "taxonomy_version")
    op.drop_column("market_review_signal_attributions", "source_board_snapshot_id")

    op.drop_column("market_review_scope_snapshots", "membership_version")
    op.drop_column("market_review_scope_snapshots", "taxonomy_compatibility_key")
    op.drop_column("market_review_scope_snapshots", "taxonomy_version")
