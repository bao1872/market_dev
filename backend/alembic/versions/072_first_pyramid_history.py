"""072 first_pyramid_history - 第一金字塔历史回补持久化表

Revision ID: 072_first_pyramid_history
Revises: 071_chip_consensus_snapshots
Create Date: 2026-07-29

变更内容（[CHANGE-20260729-003] 核心与筹码解耦 - P0-11 非筹码历史回补）：
- 新增 first_pyramid_history_daily_state 表：保存每只标的每个交易日的
  point-in-time daily state（最近 250 日），由 compute_first_pyramid_history
  一次计算多日产出
- 新增 first_pyramid_history_events 表：保存不可变事件流
  （BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL/SQZ_RELEASE/
  ZERO_CROSS_*），事件一旦写入不可修改

设计说明：
- 按"个股为外层，一次调用 history SSOT"模式回补：
  禁止逐日调用 snapshot，禁止回补 chip（chip 由独立 after_close_chip_consensus
  job 异步处理）
- 唯一键支持幂等重跑：相同 (instrument_id, trade_date, algorithm_version)
  重复 upsert 只更新内容，不产生重复行
- events 表以 (instrument_id, algorithm_version, event_id) 唯一，event_id
  来源于事件 payload 中的稳定标识（bar_index + type 或 anchor_time）

非破坏性：
- 纯新增表，不修改/删除现有列
- 不影响 stock_feature_snapshots 与 stock_chip_consensus_snapshots 表
- 部署后表为空，由 backfill_first_pyramid_history_batch 异步填充

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "072_first_pyramid_history"
down_revision: str | None = "071_chip_consensus_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 first_pyramid_history_daily_state 与 first_pyramid_history_events 表。"""
    # 1. daily_state 表
    op.create_table(
        "first_pyramid_history_daily_state",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="daily state 行 ID",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID",
        ),
        sa.Column(
            "trade_date",
            sa.Date(),
            nullable=False,
            comment="业务交易日（对应 daily_state.time）",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="core 算法版本（FIRST_PYRAMID_CORE_ALGORITHM_VERSION）",
        ),
        sa.Column(
            "input_hash",
            sa.Text(),
            nullable=False,
            comment="输入 bars hash（用于校验重跑一致性）",
        ),
        sa.Column(
            "state_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="daily_state 单行完整字段 JSONB（trend/structure/momentum/volume 等）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="更新时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "trade_date",
            "algorithm_version",
            name="uq_first_pyramid_history_daily_state_instr_date_ver",
        ),
        sa.Index(
            "ix_first_pyramid_history_daily_state_trade_date",
            "trade_date",
        ),
        sa.Index(
            "ix_first_pyramid_history_daily_state_instr_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
    )

    # 2. events 表（不可变事件流）
    op.create_table(
        "first_pyramid_history_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="事件行 ID",
        ),
        sa.Column(
            "instrument_id",
            UUID(as_uuid=True),
            sa.ForeignKey("instruments.id"),
            nullable=False,
            comment="股票 ID",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="core 算法版本",
        ),
        sa.Column(
            "event_type",
            sa.Text(),
            nullable=False,
            comment="事件类型：BOS/CHoCH/OB_CREATED/OB_ENTERED/OB_MITIGATED/EQH/EQL/SQZ_RELEASE/ZERO_CROSS_*",
        ),
        sa.Column(
            "event_id",
            sa.Text(),
            nullable=False,
            comment="事件稳定标识（bar_index+type 或 anchor_time+type），用于幂等去重",
        ),
        sa.Column(
            "event_time",
            sa.Text(),
            nullable=True,
            comment="事件发生时间（ISO 字符串，对应 bar time）",
        ),
        sa.Column(
            "event_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="事件完整字段 JSONB（不可变）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="创建时间",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "instrument_id",
            "algorithm_version",
            "event_id",
            name="uq_first_pyramid_history_events_instr_ver_evid",
        ),
        sa.Index(
            "ix_first_pyramid_history_events_instr_type",
            "instrument_id",
            "event_type",
        ),
        sa.Index(
            "ix_first_pyramid_history_events_type",
            "event_type",
        ),
    )


def downgrade() -> None:
    op.drop_table("first_pyramid_history_events")
    op.drop_table("first_pyramid_history_daily_state")
