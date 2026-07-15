"""060 stock state events - 状态变化事件表

Revision ID: 060_stock_state_events
Revises: 059_user_table_view_presets
Create Date: 2026-07-11

变更内容：
- 新增 stock_state_events 表
- 盘后快照成功发布后，比较相邻快照 code/value 生成聚合事件
- 每只股票每个 source_run_id 最多一条事件（idempotency_key 唯一约束）
- ON CONFLICT DO NOTHING 保证幂等
- 保存 changed_fields + 必要证据，不保存完整 StockState
- 90 天清理任务通过 created_at 索引支持

设计说明：
- instrument_id 关联 instruments 主键（非 symbol），保证关系完整性
- symbol 列冗余存储便于查询，但 FK 指向 instrument_id
- idempotency_key = f"{symbol}:{source_run_id}:{algorithm_version}" 稳定幂等键
- evidence 为 JSONB 但只保存触发事件的必要证据（字段名、前后值），不保存完整状态
- 表尺寸预算：每日约 4000 只 A 股 × ~10% 有变化 ≈ 400 行/天 ≈ 100K/年，单行 < 2KB，年增 < 200MB
- 90 天清理：created_at 索引支持高效删除，cleanup 函数在 state_event_service 实现

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "060_stock_state_events"
down_revision: str | None = "059_user_table_view_presets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stock_state_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件 ID",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID（关联 instruments 主键）",
        ),
        sa.Column(
            "symbol",
            sa.String(32),
            nullable=False,
            comment="股票代码（冗余存储便于查询）",
        ),
        sa.Column(
            "source_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("stock_feature_snapshot_runs.id"),
            nullable=False,
            comment="触发事件的特征快照运行 ID",
        ),
        sa.Column(
            "algorithm_version",
            sa.String(32),
            nullable=False,
            comment="算法版本（来自快照 schema_version）",
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="事件发生时间（当前快照 trade_date 15:00+08:00）",
        ),
        sa.Column(
            "previous_as_of",
            sa.Date(),
            nullable=True,
            comment="前一快照 trade_date（首次无前值时为 null）",
        ),
        sa.Column(
            "current_as_of",
            sa.Date(),
            nullable=False,
            comment="当前快照 trade_date",
        ),
        sa.Column(
            "event_type",
            sa.String(64),
            nullable=False,
            comment="稳定事件类型（如 state_transition）",
        ),
        sa.Column(
            "title",
            sa.String(256),
            nullable=False,
            comment="事件标题（用户可读）",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=False,
            comment="事件描述",
        ),
        sa.Column(
            "changed_fields",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
            comment="全部变化字段列表（稳定 code 路径）",
        ),
        sa.Column(
            "evidence",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
            comment="必要证据（字段 code/前后值），不保存完整状态",
        ),
        sa.Column(
            "idempotency_key",
            sa.String(128),
            nullable=False,
            comment="稳定幂等键: symbol:source_run_id:algorithm_version",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间（90 天清理依据）",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_state_events_idempotency_key",
        ),
    )
    # 查询索引：按 instrument + 时间倒序（详情页右栏最近事件）
    op.create_index(
        "ix_state_events_instrument_occurred",
        "stock_state_events",
        ["instrument_id", sa.text("occurred_at DESC")],
    )
    # 查询索引：按 run_id 查询（事件生成批量查询）
    op.create_index(
        "ix_state_events_source_run",
        "stock_state_events",
        ["source_run_id"],
    )
    # 清理索引：90 天清理按 created_at 删除
    op.create_index(
        "ix_state_events_created_at",
        "stock_state_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_table("stock_state_events")


if __name__ == "__main__":
    assert revision == "060_stock_state_events"
    assert down_revision == "059_user_table_view_presets"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
