"""Retire legacy Market Review tables (R1 review-retirement).

schema-reversible, data-destructive

upgrade:
  - DELETE factor_publications WHERE publication_kind = 'market_review'
    (factor_publications.data_run_id is a plain UUID with no FK, so this is safe
    and does NOT drop the factor_publications table).
  - DROP the 11 legacy market_review_* / review_scope_* tables in FK-safe order,
    strictly WITHOUT ``DROP ... CASCADE``.

  Preserved (never touched): factor_publications, scheduler_job_runs,
  job_run_events, market_dashboard_market_daily, market_dashboard_scope_daily,
  market_boards, market_board_memberships, bars_daily, instruments, StockFeature
  / Core / FirstPyramid / Auction tables, users, chip_consensus_runs,
  first_pyramid_history_runs, board_analysis_snapshots.

downgrade:
  - Recreate the exact HEAD=095 legacy schema as EMPTY tables in dependency-safe
    order (parent tables first). Does NOT restore deleted historical rows.
  - Does NOT recreate market_review factor_publication rows.
  - Does NOT invent data.

The 11 tables are ONLY referenced by each other (full inbound-FK scan across
migrations 076-095 found no non-legacy table with an inbound FK to any of them;
factor_publications.data_run_id has no FK constraint). Therefore they can be
dropped in FK-safe order without CASCADE.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "096_retire_legacy_market_review"
down_revision = "095_market_dashboard_projection"
branch_labels = None
depends_on = None

# FK-safe DROP order: referencing (child) tables first, root table last.
_UPGRADE_DROP_ORDER = [
    "market_review_tracking_evaluations",
    "market_review_trackings",
    "market_review_signal_attributions",
    "market_review_signal_instruments",
    "market_review_signals",
    "market_review_run_items",
    "market_review_scope_snapshots",
    "market_review_metric_observations",
    "review_scope_observation_facts",
    "review_scope_composition_snapshots",
    "market_review_runs",
]


def upgrade() -> None:
    # 1) Remove only the market_review publication pointers. No FK, plain column.
    op.execute(
        sa.text(
            "DELETE FROM factor_publications WHERE publication_kind = 'market_review'"
        )
    )

    # 2) Drop the 11 legacy tables, FK-safe order, no CASCADE.
    for table in _UPGRADE_DROP_ORDER:
        op.drop_table(table)


def downgrade() -> None:
    # Recreate exact HEAD=095 legacy schema as empty tables (parents first).

    # 1) market_review_runs (root)
    op.create_table(
        "market_review_runs",
        sa.Column("id", postgresql.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source_core_run_id", postgresql.UUID(), nullable=False),
        sa.Column("source_board_run_id", postgresql.UUID(), nullable=True),
        sa.Column("source_chip_run_id", postgresql.UUID(), nullable=True),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("filter_version", sa.Text(), nullable=False),
        sa.Column("baseline_window", sa.Integer(), nullable=False, server_default=sa.text("120")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'computing'")),
        sa.Column("expected_scope_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("succeeded_scope_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_scope_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("signal_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("coverage_ratio", sa.Numeric(), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("completed_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("published_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("metadata_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["source_chip_run_id"], ["chip_consensus_runs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "trade_date", "source_core_run_id", "algorithm_version", "filter_version",
            name="uq_review_runs_date_core_algo_filter",
        ),
        sa.CheckConstraint(
            "status IN ('computing','succeeded','failed','cancelled')",
            name="review_runs_status_check",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_review_runs_status", "market_review_runs", ["status"])
    op.create_index("ix_review_runs_trade_date", "market_review_runs", ["trade_date"])
    op.create_index("ix_market_review_runs_chip_run", "market_review_runs", ["source_chip_run_id"])

    # 2) market_review_signals (refs runs)
    op.create_table(
        "market_review_signals",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("review_run_id", postgresql.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("filter_family", sa.Text(), nullable=False),
        sa.Column("signal_type", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("scope_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("first_seen_date", sa.Date(), nullable=False),
        sa.Column("previous_signal_id", postgresql.UUID(), nullable=True),
        sa.Column("transformed_to_signal_id", postgresql.UUID(), nullable=True),
        sa.Column("trigger_payload", postgresql.JSONB(), nullable=True),
        sa.Column("baseline_payload", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_payload", postgresql.JSONB(), nullable=True),
        sa.Column("confirmation_rule", postgresql.JSONB(), nullable=True),
        sa.Column("invalidation_rule", postgresql.JSONB(), nullable=True),
        sa.Column("coverage_ratio", sa.Numeric(), nullable=True),
        sa.Column("rank_key", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["previous_signal_id"], ["market_review_signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["transformed_to_signal_id"], ["market_review_signals.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "review_run_id", "filter_family", "signal_type", "scope_type", "scope_key",
            name="uq_review_signals_run_family_type_scope",
        ),
        sa.CheckConstraint("filter_family IN ('A','B','C','D')", name="review_signals_filter_family_check"),
        sa.CheckConstraint(
            "status IN ('pending','active','invalidated','completed')",
            name="review_signals_status_check",
        ),
    )
    op.create_index("ix_review_signals_run_scope", "market_review_signals", ["review_run_id", "scope_type"])
    op.create_index("ix_review_signals_run_family", "market_review_signals", ["review_run_id", "filter_family"])
    op.create_index("ix_review_signals_date_status", "market_review_signals", ["trade_date", "status"])
    op.create_index("ix_review_signals_scope", "market_review_signals", ["scope_type", "scope_key"])

    # 3) market_review_run_items (refs runs)
    op.create_table(
        "market_review_run_items",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("review_run_id", postgresql.UUID(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("phase", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=True),
        sa.Column("lease_expires_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("completed_at", postgresql.TIMESTAMP(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "review_run_id", "scope_type", "scope_key", "phase",
            name="uq_review_items_run_scope_phase",
        ),
        sa.CheckConstraint(
            "phase IN ('discovery','cross_section','historical')",
            name="review_items_phase_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled')",
            name="review_items_status_check",
        ),
    )
    op.create_index("ix_review_items_run_status", "market_review_run_items", ["review_run_id", "status"])
    op.create_index("ix_review_items_scope", "market_review_run_items", ["scope_type", "scope_key"])

    # 4) market_review_scope_snapshots (refs runs + board_analysis_snapshots)
    op.create_table(
        "market_review_scope_snapshots",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("review_run_id", postgresql.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("scope_name", sa.Text(), nullable=False),
        sa.Column("parent_scope_type", sa.Text(), nullable=True),
        sa.Column("parent_scope_key", sa.Text(), nullable=True),
        sa.Column("source_board_snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("eligible_count", sa.Integer(), nullable=False),
        sa.Column("ready_count", sa.Integer(), nullable=False),
        sa.Column("coverage_ratio", sa.Numeric(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("p_payload", postgresql.JSONB(), nullable=True),
        sa.Column("q_payload", postgresql.JSONB(), nullable=True),
        sa.Column("u_payload", postgresql.JSONB(), nullable=True),
        sa.Column("c_payload", postgresql.JSONB(), nullable=True),
        sa.Column("v_payload", postgresql.JSONB(), nullable=True),
        sa.Column("data_quality_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
        sa.Column("membership_version", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_board_snapshot_id"], ["board_analysis_snapshots.id"], ondelete="SET NULL"),
        sa.UniqueConstraint(
            "review_run_id", "scope_type", "scope_key",
            name="uq_review_scope_snapshots_run_scope",
        ),
    )
    op.create_index("ix_review_scope_snapshots_run_type", "market_review_scope_snapshots", ["review_run_id", "scope_type"])
    op.create_index("ix_review_scope_snapshots_date_type", "market_review_scope_snapshots", ["trade_date", "scope_type"])

    # 5) market_review_metric_observations (refs runs + first_pyramid_history_runs)
    op.create_table(
        "market_review_metric_observations",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("review_run_id", postgresql.UUID(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("metric_code", sa.Text(), nullable=False),
        sa.Column("component_name", sa.Text(), nullable=False),
        sa.Column("observation_value", sa.Numeric(), nullable=True),
        sa.Column("observation_json", postgresql.JSONB(), nullable=True),
        sa.Column("source_kind", sa.Text(), nullable=False, server_default=sa.text("'live'")),
        sa.Column("source_history_run_id", postgresql.UUID(), nullable=True),
        sa.Column("history_contract_version", sa.Text(), nullable=True),
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_history_run_id"], ["first_pyramid_history_runs.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "source_kind IN ('live','history_replay')",
            name="ck_review_observation_dual_lineage",
        ),
    )
    op.create_index(
        "uq_review_obs_live_run_scope_component",
        "market_review_metric_observations",
        ["review_run_id", "scope_type", "scope_key", "metric_code", "component_name"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'live'"),
    )
    op.create_index(
        "uq_review_obs_replay_run_date_scope_component",
        "market_review_metric_observations",
        ["source_history_run_id", "trade_date", "scope_type", "scope_key", "metric_code", "component_name"],
        unique=True,
        postgresql_where=sa.text("source_kind = 'history_replay'"),
    )

    # 6) market_review_signal_attributions (refs signals + board_analysis_snapshots)
    op.create_table(
        "market_review_signal_attributions",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("signal_id", postgresql.UUID(), nullable=False),
        sa.Column("child_scope_type", sa.Text(), nullable=False),
        sa.Column("child_scope_key", sa.Text(), nullable=False),
        sa.Column("child_scope_name", sa.Text(), nullable=False),
        sa.Column("relation_type", sa.Text(), nullable=True),
        sa.Column("contribution_value", sa.Numeric(), nullable=True),
        sa.Column("contribution_rank", sa.Integer(), nullable=True),
        sa.Column("metrics_payload", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_payload", postgresql.JSONB(), nullable=True),
        sa.Column("coverage_ratio", sa.Numeric(), nullable=True),
        sa.Column("source_board_snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("taxonomy_compatibility_key", sa.Text(), nullable=True),
        sa.Column("membership_version", sa.Text(), nullable=True),
        sa.Column("eligible_count", sa.Integer(), nullable=True),
        sa.Column("ready_count", sa.Integer(), nullable=True),
        sa.Column("data_quality_json", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["signal_id"], ["market_review_signals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_board_snapshot_id"], ["board_analysis_snapshots.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_review_attributions_signal", "market_review_signal_attributions", ["signal_id"])
    op.create_index("ix_review_attributions_signal_rank", "market_review_signal_attributions", ["signal_id", "contribution_rank"])

    # 7) market_review_signal_instruments (refs signals + instruments)
    op.create_table(
        "market_review_signal_instruments",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("signal_id", postgresql.UUID(), nullable=False),
        sa.Column("instrument_id", postgresql.UUID(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("board_role", sa.Text(), nullable=True),
        sa.Column("relation_to_scope", sa.Text(), nullable=True),
        sa.Column("contribution_value", sa.Numeric(), nullable=True),
        sa.Column("contribution_rank", sa.Integer(), nullable=True),
        sa.Column("first_pyramid_payload", postgresql.JSONB(), nullable=True),
        sa.Column("fresh_events_payload", postgresql.JSONB(), nullable=True),
        sa.Column("source_snapshot_id", postgresql.UUID(), nullable=True),
        sa.Column("contribution_payload", postgresql.JSONB(), nullable=True),
        sa.Column("role_evidence", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["signal_id"], ["market_review_signals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"]),
        sa.CheckConstraint(
            "board_role IN ('leadership','core','satellite')",
            name="review_instruments_board_role_check",
        ),
        sa.CheckConstraint(
            "relation_to_scope IN ('in','out','neutral')",
            name="review_instruments_relation_to_scope_check",
        ),
    )
    op.create_index("ix_review_instruments_signal", "market_review_signal_instruments", ["signal_id"])
    op.create_index("ix_review_instruments_signal_rank", "market_review_signal_instruments", ["signal_id", "contribution_rank"])
    op.create_index("ix_review_instruments_instrument", "market_review_signal_instruments", ["instrument_id"])

    # 8) market_review_trackings (refs signals + users + instruments)
    op.create_table(
        "market_review_trackings",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("user_id", postgresql.UUID(), nullable=False),
        sa.Column("source_signal_id", postgresql.UUID(), nullable=True),
        sa.Column("tracking_type", sa.Text(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=True),
        sa.Column("scope_key", sa.Text(), nullable=True),
        sa.Column("instrument_id", postgresql.UUID(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confirmation_conditions", postgresql.JSONB(), nullable=True),
        sa.Column("invalidation_conditions", postgresql.JSONB(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("discovery_id", sa.Text(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("closed_at", postgresql.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_signal_id"], ["market_review_signals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "tracking_type IN ('signal','scope','instrument','discovery')",
            name="review_trackings_tracking_type_check",
        ),
        sa.CheckConstraint(
            "status IN ('active','closed','cancelled')",
            name="review_trackings_status_check",
        ),
    )
    op.create_index("ix_review_trackings_user_status", "market_review_trackings", ["user_id", "status"])
    op.create_index("ix_review_trackings_signal", "market_review_trackings", ["source_signal_id"])
    op.create_index("ix_review_trackings_instrument", "market_review_trackings", ["instrument_id"])

    # 9) market_review_tracking_evaluations (refs trackings + runs)
    op.create_table(
        "market_review_tracking_evaluations",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True),
        sa.Column("tracking_id", postgresql.UUID(), nullable=False),
        sa.Column("review_run_id", postgresql.UUID(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("previous_state", sa.Text(), nullable=True),
        sa.Column("current_state", sa.Text(), nullable=False),
        sa.Column("evaluation_payload", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tracking_id"], ["market_review_trackings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tracking_id", "trade_date", name="uq_review_evaluations_tracking_date"),
    )
    op.create_index("ix_review_evaluations_run", "market_review_tracking_evaluations", ["review_run_id"])

    # 10) review_scope_observation_facts (refs runs)
    op.create_table(
        "review_scope_observation_facts",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("scope_name", sa.Text(), nullable=True),
        sa.Column("canonical_t1", sa.Date(), nullable=True),
        sa.Column("pit_member_count", sa.Integer(), nullable=False),
        sa.Column("pit_member_count_t1", sa.Integer(), nullable=True),
        sa.Column("provided_member_count", sa.Integer(), nullable=True),
        sa.Column("t1_membership_available", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pit_status_t", sa.Text(), nullable=False),
        sa.Column("pit_status_t1", sa.Text(), nullable=True),
        sa.Column("readiness", sa.Text(), nullable=False),
        sa.Column("observation_payload", postgresql.JSONB(), nullable=False),
        sa.Column("diagnostics", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("algorithm_version", sa.Text(), nullable=True),
        sa.Column("review_run_id", postgresql.UUID(), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "review_run_id", "trade_date", "scope_type", "scope_key",
            name="uq_review_scope_observation_facts_run_day_scope",
        ),
    )
    op.create_index("ix_review_scope_observation_facts_run", "review_scope_observation_facts", ["review_run_id"])

    # 11) review_scope_composition_snapshots (refs runs)
    op.create_table(
        "review_scope_composition_snapshots",
        sa.Column("id", postgresql.UUID(), nullable=False, primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("review_run_id", postgresql.UUID(), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("composition_payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", postgresql.TIMESTAMP(), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.ForeignKeyConstraint(["review_run_id"], ["market_review_runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "review_run_id", "scope_type", "scope_key",
            name="uq_review_scope_composition_run_scope",
        ),
    )
    op.create_index("ix_review_scope_composition_run_date", "review_scope_composition_snapshots", ["review_run_id", "trade_date"])
