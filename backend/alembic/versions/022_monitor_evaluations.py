"""022 create monitor_evaluations table

Revision ID: 022_monitor_evals
Revises: 021_drop_vdi_uq
Create Date: 2026-06-23

监控评估结果表：
- id: UUID PK (server_default gen_random_uuid)
- strategy_version_id: 策略版本 FK
- instrument_id: 股票 FK
- source_bar_time: 1m bar 时间戳
- status: SUCCEEDED/SKIPPED/FAILED
- metrics: 完整多指标输出 (JSONB)
- suppressed_events: 被冷却抑制的事件 (JSONB)
- calculated_at: 计算时间
- error_code: 错误码

唯一约束: (strategy_version_id, instrument_id, source_bar_time)
索引: (instrument_id, source_bar_time) 用于按股票+时间查询
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022_monitor_evals"
down_revision: str | None = "021_drop_vdi_uq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "monitor_evaluations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "strategy_version_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("strategy_versions.id"),
            nullable=False,
        ),
        sa.Column(
            "instrument_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
        ),
        sa.Column(
            "source_bar_time",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="1m bar 时间戳",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            comment="SUCCEEDED/SKIPPED/FAILED",
        ),
        sa.Column(
            "metrics",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="完整多指标输出",
        ),
        sa.Column(
            "suppressed_events",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="被冷却抑制的事件",
        ),
        sa.Column(
            "calculated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="计算时间",
        ),
        sa.Column(
            "error_code",
            sa.Text(),
            nullable=True,
            comment="错误码",
        ),
        sa.UniqueConstraint(
            "strategy_version_id",
            "instrument_id",
            "source_bar_time",
            name="uq_monitor_evaluations_version_instrument_bar",
        ),
    )
    op.create_index(
        "ix_monitor_evaluations_instrument_bar_time",
        "monitor_evaluations",
        ["instrument_id", "source_bar_time"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monitor_evaluations_instrument_bar_time",
        table_name="monitor_evaluations",
    )
    op.drop_table("monitor_evaluations")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "022_monitor_evals"
    assert down_revision == "021_drop_vdi_uq"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
