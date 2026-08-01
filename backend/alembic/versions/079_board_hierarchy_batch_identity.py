"""079 board hierarchy, PIT membership, universes, and batch identity.

Revision ID: 079_board_hierarchy_batch_identity
Revises: 078_review_filter_family_d
Create Date: 2026-08-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "079_board_hierarchy_batch_identity"
down_revision: str | None = "078_review_filter_family_d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("market_boards", sa.Column("taxonomy", sa.Text(), nullable=False, server_default="qstock"))
    op.add_column("market_boards", sa.Column("source", sa.Text(), nullable=False, server_default="qstock"))
    op.add_column("market_boards", sa.Column("taxonomy_version", sa.Text(), nullable=False, server_default="legacy-v1"))
    op.add_column("market_boards", sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=False, server_default="qstock-board-v1"))
    op.add_column("market_boards", sa.Column("hierarchy_level", sa.Text(), nullable=False, server_default="L1"))
    op.add_column("market_boards", sa.Column("parent_board_id", UUID(as_uuid=True), nullable=True))
    op.add_column("market_boards", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("market_boards", sa.Column("membership_version", sa.Text(), nullable=False, server_default="legacy-projection-20260801"))
    op.create_foreign_key(
        "fk_market_boards_parent", "market_boards", "market_boards",
        ["parent_board_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_market_boards_hierarchy", "market_boards", ["type", "hierarchy_level", "parent_board_id"])

    op.create_table(
        "board_definition_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("board_id", UUID(as_uuid=True), sa.ForeignKey("market_boards.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("taxonomy", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=False),
        sa.Column("board_type", sa.Text(), nullable=False),
        sa.Column("hierarchy_level", sa.Text(), nullable=False),
        sa.Column("parent_board_id", UUID(as_uuid=True), sa.ForeignKey("market_boards.id", ondelete="SET NULL"), nullable=True),
        sa.Column("membership_version", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("definition_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("board_id", "effective_from", "definition_hash", "membership_version", name="uq_board_definition_versions_identity"),
    )
    op.create_index("ix_board_definition_versions_pit", "board_definition_versions", ["board_id", "effective_from", "effective_to"])

    op.create_table(
        "board_membership_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("board_definition_version_id", UUID(as_uuid=True), sa.ForeignKey("board_definition_versions.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("instrument_id", UUID(as_uuid=True), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("membership_version", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("board_definition_version_id", "instrument_id", "effective_from", name="uq_board_membership_history_identity"),
    )
    op.create_index("ix_board_membership_history_pit", "board_membership_history", ["board_definition_version_id", "effective_from", "effective_to"])

    op.create_table(
        "universe_definitions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("universe_key", sa.Text(), nullable=False),
        sa.Column("universe_type", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column("compatibility_key", sa.Text(), nullable=False),
        sa.Column("membership_version", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("population_status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("universe_key", "version", "membership_version", "effective_from", name="uq_universe_definitions_identity"),
    )
    op.create_index("ix_universe_definitions_pit", "universe_definitions", ["universe_key", "effective_from", "effective_to"])

    op.create_table(
        "universe_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("universe_definition_id", UUID(as_uuid=True), sa.ForeignKey("universe_definitions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instrument_id", UUID(as_uuid=True), sa.ForeignKey("instruments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("weight", sa.Float(), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("universe_definition_id", "instrument_id", "effective_from", name="uq_universe_memberships_identity"),
    )
    op.create_index("ix_universe_memberships_pit", "universe_memberships", ["universe_definition_id", "effective_from", "effective_to"])

    op.create_table(
        "board_analysis_runs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source_core_run_id", UUID(as_uuid=True), nullable=False),
        sa.Column("taxonomy_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=False),
        sa.Column("membership_version", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("expected_count", sa.Integer(), nullable=False),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("blockers", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "trade_date",
            "source_core_run_id",
            "taxonomy_version",
            "taxonomy_compatibility_key",
            "algorithm_version",
            "membership_version",
            name="uq_board_analysis_runs_identity",
        ),
    )
    op.create_index("ix_board_analysis_runs_date_status", "board_analysis_runs", ["trade_date", "status"])

    op.add_column("board_analysis_snapshots", sa.Column("board_analysis_run_id", UUID(as_uuid=True), nullable=True))
    op.add_column("board_analysis_snapshots", sa.Column("taxonomy_version", sa.Text(), nullable=False, server_default="legacy-v1"))
    op.add_column("board_analysis_snapshots", sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=False, server_default="qstock-board-v1"))
    op.add_column("board_analysis_snapshots", sa.Column("membership_version", sa.Text(), nullable=False, server_default="legacy-projection-20260801"))
    op.create_foreign_key("fk_board_analysis_snapshots_batch_run", "board_analysis_snapshots", "board_analysis_runs", ["board_analysis_run_id"], ["id"], ondelete="RESTRICT")
    op.drop_constraint(
        "uq_board_analysis_snapshots_date_board_ver",
        "board_analysis_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_board_analysis_snapshots_run_board",
        "board_analysis_snapshots",
        ["board_analysis_run_id", "board_id"],
    )
    op.create_index(
        "ix_board_analysis_snapshots_batch",
        "board_analysis_snapshots",
        ["board_analysis_run_id", "board_id"],
    )

    # The migration date is the earliest truthful validity date for the legacy latest-state projection.
    op.execute("""
        INSERT INTO board_definition_versions (
            board_id, taxonomy, source, taxonomy_version, taxonomy_compatibility_key,
            board_type, hierarchy_level, parent_board_id, membership_version,
            effective_from, definition_hash
        )
        SELECT id, taxonomy, source, taxonomy_version, taxonomy_compatibility_key,
               type, hierarchy_level, parent_board_id, membership_version,
               DATE '2026-08-01', md5(id::text || ':' || taxonomy_version || ':' || membership_version)
        FROM market_boards
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO board_membership_history (
            board_definition_version_id, instrument_id, membership_version, effective_from
        )
        SELECT d.id, m.instrument_id, d.membership_version, DATE '2026-08-01'
        FROM board_definition_versions d
        JOIN market_board_memberships m ON m.board_id = d.board_id
        WHERE d.effective_from = DATE '2026-08-01'
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        INSERT INTO universe_definitions (
            id, universe_key, universe_type, name, source, version, compatibility_key,
            membership_version, effective_from, population_status
        ) VALUES
          ('00000000-0000-0000-0000-000000000301', 'csi300', 'major_index', '沪深300', 'authoritative-provider-required', 'v1', 'csi300-v1', 'unpopulated-v1', DATE '2026-08-01', 'blocked_external_population'),
          ('00000000-0000-0000-0000-000000000302', 'csi500', 'major_index', '中证500', 'authoritative-provider-required', 'v1', 'csi500-v1', 'unpopulated-v1', DATE '2026-08-01', 'blocked_external_population'),
          ('00000000-0000-0000-0000-000000000401', 'large_cap_style', 'style', '大盘风格', 'authoritative-provider-required', 'v1', 'large-cap-style-v1', 'unpopulated-v1', DATE '2026-08-01', 'blocked_external_population'),
          ('00000000-0000-0000-0000-000000000402', 'small_cap_style', 'style', '小盘风格', 'authoritative-provider-required', 'v1', 'small-cap-style-v1', 'unpopulated-v1', DATE '2026-08-01', 'blocked_external_population')
        ON CONFLICT DO NOTHING
    """)

    op.execute("""
        INSERT INTO board_analysis_runs (
            trade_date, source_core_run_id, taxonomy_version,
            taxonomy_compatibility_key, membership_version, algorithm_version,
            expected_count, succeeded_count, failed_count, coverage_ratio, status, blockers
        )
        SELECT trade_date, source_core_run_id, 'legacy-v1', 'qstock-board-v1',
               'legacy-projection-20260801', algorithm_version,
               count(*), count(*) FILTER (WHERE status IN ('succeeded','partial')),
               count(*) FILTER (WHERE status = 'failed'), avg(coverage_ratio),
               CASE WHEN count(*) FILTER (WHERE status = 'failed') > 0 THEN 'partial' ELSE 'succeeded' END,
               '[]'::jsonb
        FROM board_analysis_snapshots
        GROUP BY trade_date, source_core_run_id, algorithm_version
        ON CONFLICT DO NOTHING
    """)
    op.execute("""
        UPDATE board_analysis_snapshots s
        SET board_analysis_run_id = r.id
        FROM board_analysis_runs r
        WHERE r.trade_date = s.trade_date
          AND r.source_core_run_id = s.source_core_run_id
          AND r.algorithm_version = s.algorithm_version
          AND r.membership_version = 'legacy-projection-20260801'
    """)
    op.execute("""
        UPDATE factor_publications p
        SET data_run_id = s.board_analysis_run_id,
            metadata_json = (
                COALESCE(p.metadata_json, '{}')::jsonb || jsonb_build_object(
                    'legacy_board_snapshot_id', s.id::text,
                    'board_analysis_run_id', s.board_analysis_run_id::text,
                    'pointer_migrated_by', '079_board_hierarchy_batch_identity'
                )
            )::text
        FROM board_analysis_snapshots s
        WHERE p.publication_kind = 'market_aggregation'
          AND p.data_run_id = s.id
          AND s.board_analysis_run_id IS NOT NULL
    """)
    op.alter_column(
        "board_analysis_snapshots",
        "board_analysis_run_id",
        existing_type=UUID(as_uuid=True),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "board_analysis_snapshots",
        "board_analysis_run_id",
        existing_type=UUID(as_uuid=True),
        nullable=True,
    )
    op.drop_index("ix_board_analysis_snapshots_batch", table_name="board_analysis_snapshots")
    op.drop_constraint(
        "uq_board_analysis_snapshots_run_board",
        "board_analysis_snapshots",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_board_analysis_snapshots_date_board_ver",
        "board_analysis_snapshots",
        ["trade_date", "board_id", "algorithm_version"],
    )
    op.drop_constraint("fk_board_analysis_snapshots_batch_run", "board_analysis_snapshots", type_="foreignkey")
    op.drop_column("board_analysis_snapshots", "membership_version")
    op.drop_column("board_analysis_snapshots", "taxonomy_compatibility_key")
    op.drop_column("board_analysis_snapshots", "taxonomy_version")
    op.drop_column("board_analysis_snapshots", "board_analysis_run_id")
    op.drop_table("board_analysis_runs")
    op.drop_table("universe_memberships")
    op.drop_table("universe_definitions")
    op.drop_table("board_membership_history")
    op.drop_table("board_definition_versions")
    op.drop_index("ix_market_boards_hierarchy", table_name="market_boards")
    op.drop_constraint("fk_market_boards_parent", "market_boards", type_="foreignkey")
    op.drop_column("market_boards", "membership_version")
    op.drop_column("market_boards", "is_active")
    op.drop_column("market_boards", "parent_board_id")
    op.drop_column("market_boards", "hierarchy_level")
    op.drop_column("market_boards", "taxonomy_compatibility_key")
    op.drop_column("market_boards", "taxonomy_version")
    op.drop_column("market_boards", "source")
    op.drop_column("market_boards", "taxonomy")
