"""006 monitor_states, strategy_events

Revision ID: 006_monitor_states
Revises: 005_strategy_runs
Create Date: 2026-06-18

监控状态与策略事件：
- monitor_states: 监控状态（复合主键 strategy_version_id + instrument_id）
- strategy_events: 策略事件（event_key 唯一，含 snapshot 快照）

来源：core_schema.sql
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006_monitor_states"
down_revision: str | None = "005_strategy_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitor_states",
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bar_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calculation_id", sa.Text(), nullable=False),
        sa.Column("state_schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("strategy_version_id", "instrument_id"),
    )

    op.create_table(
        "strategy_events",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("event_key", sa.Text(), nullable=False),
        sa.Column("strategy_version_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("instrument_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("logical_entity_id", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("snapshot", sa.dialects.postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["strategy_version_id"], ["strategy_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_key"),
    )
    op.create_index(
        "ix_strategy_event_symbol_time",
        "strategy_events",
        ["instrument_id", sa.text("event_time DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_strategy_event_symbol_time", table_name="strategy_events")
    op.drop_table("strategy_events")
    op.drop_table("monitor_states")
