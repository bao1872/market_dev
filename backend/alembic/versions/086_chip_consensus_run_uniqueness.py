"""chip consensus run database-level uniqueness

[Corrective-3.1 §P1 2026-08-05] `resolve_or_create_chip_run` 采用
SELECT-then-INSERT，在单 worker 下可用，但并发下无法保证幂等，可能对同一
(trade_date, source_core_run_id, algorithm_version) 创建多个领域 run。

本迁移补齐数据库级唯一约束，使重复创建在存储层被真正阻止，并让服务层可以
使用 ON CONFLICT DO NOTHING 做原子 upsert。

历史数据可能已存在重复行，因此 upgrade 先做去重：保留每组中 created_at 最早
的一行（其 id 通常已被 publication / run_items 引用），把其余重复行标记为
cancelled 并置 error_code，而**不删除**，避免破坏既有外键引用与审计链。
若去重后仍存在冲突则创建约束失败，需人工介入，不做静默跳过。

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


def upgrade() -> None:
    # 1) 历史重复行降级为 cancelled（保留行本身，不破坏外键与审计链）
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        PARTITION BY trade_date, source_core_run_id, algorithm_version
                        ORDER BY created_at ASC, id ASC
                    ) AS rn
                FROM chip_consensus_runs
            )
            UPDATE chip_consensus_runs AS c
            SET status = 'cancelled',
                error_code = COALESCE(c.error_code, 'DUPLICATE_DOMAIN_RUN'),
                error_message = COALESCE(
                    c.error_message,
                    'migration 086: 同 (trade_date, source_core_run_id, '
                    'algorithm_version) 的重复领域 run，已保留最早一条'
                ),
                updated_at = now()
            FROM ranked AS r
            WHERE c.id = r.id AND r.rn > 1
            """
        )
    )

    # 2) 去重后仍冲突则直接报错（不静默跳过），交由人工处理
    op.execute(
        sa.text(
            """
            DO $$
            DECLARE dup_count integer;
            BEGIN
                SELECT COUNT(*) INTO dup_count FROM (
                    SELECT 1
                    FROM chip_consensus_runs
                    GROUP BY trade_date, source_core_run_id, algorithm_version
                    HAVING COUNT(*) > 1
                ) AS d;
                IF dup_count > 0 THEN
                    RAISE EXCEPTION
                        'migration 086: 仍存在 % 组重复 chip_consensus_runs，'
                        '请人工核对后重试', dup_count;
                END IF;
            END $$;
            """
        )
    )

    op.create_unique_constraint(
        _CONSTRAINT,
        "chip_consensus_runs",
        ["trade_date", "source_core_run_id", "algorithm_version"],
    )


def downgrade() -> None:
    # 只回退约束；被标记 cancelled 的历史行不还原（无法可靠区分原始状态）
    op.drop_constraint(_CONSTRAINT, "chip_consensus_runs", type_="unique")
