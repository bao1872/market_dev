"""078 review_filter_family_d - 复盘信号新增 D 族（第二金字塔维度偏差）

Revision ID: 078_review_filter_family_d
Revises: 077_auction_analysis
Create Date: 2026-07-30

变更内容（[P0-7] PRD §24 第二金字塔维度偏差筛选器）：
- 放宽 market_review_signals.filter_family CheckConstraint：A/B/C → A/B/C/D
- D 族筛选器对应第二金字塔 6 维度（状态迁移/事件新鲜度/宽度/集中度/相对强度）
- 仅修改约束，不新增表、不新增列、不修改数据

非破坏性：
- 约束放宽（新增允许值 'D'），不影响存量 A/B/C 数据
- 回滚恢复原约束（A/B/C），需先清除 filter_family='D' 的信号
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "078_review_filter_family_d"
down_revision: str | None = "077_auction_analysis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 放宽 filter_family 约束：新增 'D'（第二金字塔维度偏差，PRD §24）
    op.execute(
        "ALTER TABLE market_review_signals "
        "DROP CONSTRAINT IF EXISTS review_signals_filter_family_check"
    )
    op.execute(
        "ALTER TABLE market_review_signals "
        "ADD CONSTRAINT review_signals_filter_family_check "
        "CHECK (filter_family IN ('A','B','C','D'))"
    )


def downgrade() -> None:
    # 回滚前需清除 filter_family='D' 的信号
    op.execute(
        "DELETE FROM market_review_signals WHERE filter_family = 'D'"
    )
    op.execute(
        "ALTER TABLE market_review_signals "
        "DROP CONSTRAINT IF EXISTS review_signals_filter_family_check"
    )
    op.execute(
        "ALTER TABLE market_review_signals "
        "ADD CONSTRAINT review_signals_filter_family_check "
        "CHECK (filter_family IN ('A','B','C'))"
    )
