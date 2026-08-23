"""092 review core only identity - remove Board run from Review identity

Revision ID: 092_review_core_only_identity
Revises: 091_observation_run_lineage
Create Date: 2026-08-23

Slice 3: Review 启动与身份不再依赖 Board Analysis run / market_aggregation
publication。Review identity 由：

    trade_date + source_core_run_id + source_board_run_id
    + algorithm_version + filter_version

改为：

    trade_date + source_core_run_id + algorithm_version + filter_version

source_board_run_id 列**物理保留**（历史 lineage 不丢），新 run 写入 NULL。
Board Analysis 物理 stage 与 BoardAnalysisRun / BoardAnalysisSnapshot 表均不删。

upgrade 顺序（fail-closed）：
1. 先查 core-only 身份重复；若有重复，raise 阻止 migration（不自动 dedupe）。
2. DROP 旧 unique uq_review_runs_date_core_board_algo_filter。
3. source_board_run_id DROP NOT NULL（允许新 run 为 NULL）。
4. CREATE 新 unique uq_review_runs_date_core_algo_filter。

downgrade（fail-safe）：
- 若已存在 source_board_run_id IS NULL 的 canonical run，则 FAIL（拒绝把 NULL
  填假 UUID 或从 latest Board run 猜历史 lineage）。
- 仅当全部非 NULL 才允许：drop new unique → NOT NULL → recreate old unique。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "092_review_core_only_identity"
down_revision: str | None = "091_observation_run_lineage"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_UQ = "uq_review_runs_date_core_board_algo_filter"
NEW_UQ = "uq_review_runs_date_core_algo_filter"
TABLE = "market_review_runs"


def _raise_if_core_identity_duplicates() -> None:
    """Fail-closed: 不允许 core-only 身份已存在重复行时执行迁移。"""
    conn = op.get_bind()
    dup = conn.execute(
        sa.text(
            f"""
            SELECT COUNT(*) FROM (
                SELECT trade_date, source_core_run_id,
                       algorithm_version, filter_version
                FROM {TABLE}
                GROUP BY trade_date, source_core_run_id,
                         algorithm_version, filter_version
                HAVING COUNT(*) > 1
            ) d
            """
        )
    ).scalar()
    if dup and dup > 0:
        raise RuntimeError(
            f"cannot apply {NEW_UQ}: found {dup} duplicate rows on "
            f"(trade_date, source_core_run_id, algorithm_version, filter_version). "
            f"Manual dedupe / investigation required before migration."
        )


def upgrade() -> None:
    _raise_if_core_identity_duplicates()

    # 1. drop old unique (board-coupled)
    op.drop_constraint(OLD_UQ, TABLE, type_="unique")

    # 2. source_board_run_id 允许 NULL（历史 run 保留旧值；新 run 写 NULL）
    op.alter_column(
        TABLE,
        "source_board_run_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )

    # 3. create new core-only unique
    op.create_unique_constraint(
        NEW_UQ,
        TABLE,
        ["trade_date", "source_core_run_id", "algorithm_version", "filter_version"],
    )


def downgrade() -> None:
    # fail-safe: 拒绝在存在 NULL board lineage 的 canonical run 时回退
    conn = op.get_bind()
    null_board = conn.execute(
        sa.text(
            f"SELECT COUNT(*) FROM {TABLE} WHERE source_board_run_id IS NULL"
        )
    ).scalar()
    if null_board and null_board > 0:
        raise RuntimeError(
            "cannot restore legacy non-null board identity while canonical "
            "Review runs with NULL board lineage exist "
            f"({null_board} rows). Keep 092 applied."
        )

    # 1. drop new unique
    op.drop_constraint(NEW_UQ, TABLE, type_="unique")

    # 2. restore NOT NULL
    op.alter_column(
        TABLE,
        "source_board_run_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )

    # 3. recreate old board-coupled unique
    op.create_unique_constraint(
        OLD_UQ,
        TABLE,
        [
            "trade_date",
            "source_core_run_id",
            "source_board_run_id",
            "algorithm_version",
            "filter_version",
        ],
    )
