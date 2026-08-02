"""auction analysis publication pointer

Revision ID: 082_auction_analysis_publication
Revises: 081_review_metric_observations
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "082_auction_analysis_publication"
down_revision: str | None = "081_review_metric_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "auction_analysis_publications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scan_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auction_scan_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("capture_run_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auction_quote_capture_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("test_namespace", sa.Text(), nullable=False),
        sa.Column("coverage_ratio", sa.Float(), nullable=False),
        sa.Column("truth_status", sa.Text(), nullable=False),
        sa.Column("gate_evidence", postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("trade_date", "algorithm_version",
                            name="uq_auction_analysis_publication_date_version"),
        sa.UniqueConstraint("scan_run_id", name="uq_auction_analysis_publication_scan_run"),
    )
    op.create_index("ix_auction_analysis_publication_date",
                    "auction_analysis_publications", ["trade_date"])


def downgrade() -> None:
    op.drop_index("ix_auction_analysis_publication_date",
                  table_name="auction_analysis_publications")
    op.drop_table("auction_analysis_publications")
