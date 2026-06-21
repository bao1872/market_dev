"""005 strategy_runs, strategy_results, strategy_result_metrics

Revision ID: 005_strategy_runs
Revises: 004_strategy
Create Date: 2026-06-18

策略运行与结果：
- strategy_runs: 策略运行记录（idempotency_key 唯一）
- strategy_results: 策略结果（唯一约束 strategy_version + trade_date + instrument）
- strategy_result_metrics: 策略结果指标（复合主键 result_id + metric_key）

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005_strategy_runs"
down_revision: str | None = "004_strategy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_runs",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_type", sa.Text(), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=True),
        sa.Column("data_cutoff", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("input_overrides", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )

    op.create_table(
        "strategy_results",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["run_id"], ["strategy_runs.id"]),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_version_id", "trade_date", "instrument_id"),
    )

    op.create_table(
        "strategy_result_metrics",
        sa.Column("result_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("metric_key", sa.Text(), nullable=False),
        sa.Column("numeric_value", sa.Float(), nullable=True),
        sa.Column("text_value", sa.Text(), nullable=True),
        sa.Column("bool_value", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["result_id"], ["strategy_results.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("result_id", "metric_key"),
    )
    op.create_index(
        "ix_metric_numeric",
        "strategy_result_metrics",
        ["strategy_version_id", "trade_date", "metric_key", "numeric_value"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_numeric", table_name="strategy_result_metrics")
    op.drop_table("strategy_result_metrics")
    op.drop_table("strategy_results")
    op.drop_table("strategy_runs")
