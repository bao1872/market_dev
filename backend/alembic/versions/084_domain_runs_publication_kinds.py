"""domain runs and publication kinds

[V2.1 EPIC-01] 新增领域 run 表与 publication 发布口径：
- board_facts_runs / board_facts_run_items
- chip_consensus_runs / chip_consensus_run_items
- auction_anchor_runs / auction_anchor_run_items
（publication_kind 为 Text 列，扩展 board_facts / chip_consensus / auction_anchor 无需 DDL）

Revision ID: 084_domain_runs_publication_kinds
Revises: 083_review_run_chip_dependency
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "084_domain_runs_publication_kinds"
down_revision: str | None = "083_review_run_chip_dependency"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _uuid() -> sa.Column:
    return postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    # ===== board_facts_runs =====
    op.create_table(
        "board_facts_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("scheduler_job_run_id", _uuid(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("run_mode", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("source_query", sa.Text(), nullable=True),
        sa.Column("query_hash", sa.Text(), nullable=True),
        sa.Column("provider_contract_version", sa.Text(), nullable=True),
        sa.Column("normalization_contract_version", sa.Text(), nullable=True),
        sa.Column("identity_contract_version", sa.Text(), nullable=True),
        sa.Column("quality_gate_version", sa.Text(), nullable=True),
        sa.Column("snapshot_hash", sa.Text(), nullable=True),
        sa.Column("taxonomy_version", sa.Text(), nullable=True),
        sa.Column("membership_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("readiness", sa.Text(), nullable=True),
        sa.Column("reused_from_run_id", _uuid(), nullable=True),
        sa.Column("staleness", sa.Integer(), nullable=True),
        sa.Column("raw_rows", sa.Integer(), nullable=True),
        sa.Column("resolved_count", sa.Integer(), nullable=True),
        sa.Column("unresolved_count", sa.Integer(), nullable=True),
        sa.Column("industry_l1_count", sa.Integer(), nullable=True),
        sa.Column("industry_l2_count", sa.Integer(), nullable=True),
        sa.Column("industry_l3_count", sa.Integer(), nullable=True),
        sa.Column("concept_count", sa.Integer(), nullable=True),
        sa.Column("membership_count", sa.Integer(), nullable=True),
        sa.Column("coverage_json", postgresql.JSONB(), nullable=True),
        sa.Column("diagnostics_json", postgresql.JSONB(), nullable=True),
        sa.Column("gate_results_json", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("ix_board_facts_runs_trade_date", "trade_date"),
        sa.Index("ix_board_facts_runs_status", "status"),
    )

    op.create_table(
        "board_facts_run_items",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("run_id", _uuid(), sa.ForeignKey("board_facts_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", _uuid(), nullable=True),
        sa.Column("instrument_symbol", sa.Text(), nullable=True),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("industry_l1", sa.Text(), nullable=True),
        sa.Column("industry_l2", sa.Text(), nullable=True),
        sa.Column("industry_l3", sa.Text(), nullable=True),
        sa.Column("concepts", postgresql.JSONB(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "instrument_symbol", name="uq_board_facts_run_items_run_symbol"),
    )

    # ===== chip_consensus_runs =====
    op.create_table(
        "chip_consensus_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("scheduler_job_run_id", _uuid(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source_core_run_id", _uuid(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("readiness", sa.Text(), nullable=True),
        sa.Column("reuse_of_run_id", _uuid(), nullable=True),
        sa.Column("expected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("succeeded_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("coverage_json", postgresql.JSONB(), nullable=True),
        sa.Column("diagnostics_json", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("ix_chip_consensus_runs_trade_date", "trade_date"),
        sa.Index("ix_chip_consensus_runs_core_run", "source_core_run_id"),
        sa.Index("ix_chip_consensus_runs_status", "status"),
    )

    op.create_table(
        "chip_consensus_run_items",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("run_id", _uuid(), sa.ForeignKey("chip_consensus_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", _uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chip_hash", sa.Text(), nullable=True),
        sa.Column("chip_snapshot_id", _uuid(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("run_id", "instrument_id", name="uq_chip_consensus_run_items_run_instrument"),
        sa.Index("ix_chip_consensus_run_items_status", "run_id", "status"),
    )

    # ===== auction_anchor_runs =====
    op.create_table(
        "auction_anchor_runs",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("scheduler_job_run_id", _uuid(), nullable=True),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("source_core_run_id", _uuid(), nullable=False),
        sa.Column("source_chip_run_id", _uuid(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("algorithm_version", sa.Text(), nullable=False),
        sa.Column("config_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("readiness", sa.Text(), nullable=True),
        sa.Column("coverage_json", postgresql.JSONB(), nullable=True),
        sa.Column("coverage_ratio", sa.Float(), nullable=False, server_default="0"),
        sa.Column("structure_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chip_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("composite_anchor_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diagnostics_json", postgresql.JSONB(), nullable=True),
        sa.Column("superseded_by_run_id", _uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Index("ix_auction_anchor_runs_trade_date", "trade_date"),
        sa.Index("ix_auction_anchor_runs_core_run", "source_core_run_id"),
        sa.Index("ix_auction_anchor_runs_status", "status"),
    )

    op.create_table(
        "auction_anchor_run_items",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("run_id", _uuid(), sa.ForeignKey("auction_anchor_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", _uuid(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("source_chip_run_id", _uuid(), nullable=True),
        sa.Column("anchor_snapshot_id", _uuid(), nullable=True),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("auction_anchor_run_items")
    op.drop_table("auction_anchor_runs")
    op.drop_table("chip_consensus_run_items")
    op.drop_table("chip_consensus_runs")
    op.drop_table("board_facts_run_items")
    op.drop_table("board_facts_runs")
