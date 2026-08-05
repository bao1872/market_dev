"""chip consensus run database-level uniqueness

[Corrective-3.1 §P1 2026-08-05] `resolve_or_create_chip_run` 采用
SELECT-then-INSERT，在单 worker 下可用，但并发下无法保证幂等，可能对同一
(trade_date, source_core_run_id, algorithm_version) 创建多个领域 run。

本迁移补齐数据库级唯一约束，使重复创建在存储层被真正阻止，并让服务层可以
使用 ON CONFLICT DO NOTHING 做原子 upsert。

**处理历史重复的正确方式（经确认，非静默伪造去重）**：
- 历史重复行的 status 改成 cancelled **不会改变**唯一键的三列，重复组依然存在，
  把行置 cancelled 并不能让唯一约束创建成功。
- 因此本迁移**不修改任何历史业务记录**，只在 upgrade 开头做重复 preflight：
  - 若存在重复组，明确 RAISE 并输出重复组详情，事务整体回滚，约束不创建；
  - 无重复时才创建硬唯一约束。

若真实库已存在重复，需单独的数据对账方案（选 canonical run、核查
publication pointer / run items / SchedulerJobRun metadata、明确引用关系、
经人工确认后再合并或归档），不在迁移内自动处理。

Revision ID: 086_chip_consensus_run_uniqueness
Revises: 085_board_definition_identity_contract
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "086_chip_consensus_run_uniqueness"
down_revision: str | None = "085_board_definition_identity_contract"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "uq_chip_consensus_runs_date_core_algo"

# 与 ORM 模型 ChipConsensusRun.__table_args__ 中的 UniqueConstraint 列顺序一致
_UNIQUE_COLUMNS = ["trade_date", "source_core_run_id", "algorithm_version"]


def _duplicate_groups_exist() -> bool:
    """返回是否存在 (trade_date, source_core_run_id, algorithm_version) 重复组。

    仅做只读 SELECT，不修改任何历史行。
    """
    result = op.get_bind().execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT 1
                FROM chip_consensus_runs
                GROUP BY trade_date, source_core_run_id, algorithm_version
                HAVING COUNT(*) > 1
            ) AS d
            """
        )
    )
    count = result.scalar() or 0
    return count > 0


def _raise_duplicate_error() -> None:
    """输出重复组详情并显式报错，使 upgrade 事务整体回滚、约束不创建。"""
    dup_rows = op.get_bind().execute(
        sa.text(
            """
            SELECT trade_date, source_core_run_id, algorithm_version, COUNT(*) AS n
            FROM chip_consensus_runs
            GROUP BY trade_date, source_core_run_id, algorithm_version
            HAVING COUNT(*) > 1
            ORDER BY n DESC, trade_date
            LIMIT 50
            """
        )
    ).fetchall()
    detail_lines = [
        f"  ({row[0]}, {row[1]}, {row[2]}) x{row[3]}" for row in dup_rows
    ]
    detail = "\n".join(detail_lines)
    raise Exception(
        "migration 086: 检测到 "
        f"{len(dup_rows)} 组重复的 chip_consensus_runs，唯一约束无法创建。\n"
        "请先做数据对账（选 canonical run、核查 publication pointer / run items / "
        "SchedulerJobRun metadata），经人工确认后再重试 migration。\n"
        "重复组（前 50）:\n"
        f"{detail}"
    )


def upgrade() -> None:
    # 1) 重复 preflight：只读检查，发现重复立即报错并回滚，不修改历史业务数据
    if _duplicate_groups_exist():
        _raise_duplicate_error()

    # 2) 无重复时创建硬唯一约束
    op.create_unique_constraint(_CONSTRAINT, "chip_consensus_runs", _UNIQUE_COLUMNS)


def downgrade() -> None:
    # 只删除约束，不修改任何业务数据
    op.drop_constraint(_CONSTRAINT, "chip_consensus_runs", type_="unique")
