"""review run chip dependency columns

[QM-63 review 依赖矩阵 2026-08-04] MarketReviewRun 新增 chip 来源与降级记录：
- source_chip_run_id：chip 共识 run id（None 表示 chip 不可用，降级 core-only）
- degraded_reasons：降级原因列表（chip不可用/auction失败等），空数组=无降级

Revision ID: 083_review_run_chip_dependency
Revises: 082_auction_analysis_publication
Create Date: 2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "083_review_run_chip_dependency"
down_revision: str | None = "082_auction_analysis_publication"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "market_review_runs",
        sa.Column(
            "source_chip_run_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="输入 chip 共识 run id；NULL 表示 chip 不可用，run 降级为 core-only",
        ),
    )
    op.add_column(
        "market_review_runs",
        sa.Column(
            "degraded_reasons",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="降级原因列表（chip不可用/auction失败等）；空数组=无降级",
        ),
    )


def downgrade() -> None:
    op.drop_column("market_review_runs", "degraded_reasons")
    op.drop_column("market_review_runs", "source_chip_run_id")
