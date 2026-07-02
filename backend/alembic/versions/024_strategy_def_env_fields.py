"""024 add environment/is_user_visible/is_scheduled to strategy_definitions

Revision ID: 024_strat_def_env
Revises: 023_event_recips
Create Date: 2026-06-24
"""

from alembic import op
import sqlalchemy as sa

revision = "024_strat_def_env"
down_revision = "023_event_recips"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_definitions",
        sa.Column("environment", sa.Text(), nullable=False, server_default="production"),
    )
    op.add_column(
        "strategy_definitions",
        sa.Column("is_user_visible", sa.Boolean(), nullable=False, server_default="true"),
    )
    op.add_column(
        "strategy_definitions",
        sa.Column("is_scheduled", sa.Boolean(), nullable=False, server_default="true"),
    )


def downgrade() -> None:
    op.drop_column("strategy_definitions", "is_scheduled")
    op.drop_column("strategy_definitions", "is_user_visible")
    op.drop_column("strategy_definitions", "environment")
