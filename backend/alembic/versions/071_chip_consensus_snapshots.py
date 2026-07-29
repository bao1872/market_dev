"""071 stock_chip_consensus_snapshots - 筹码共识独立持久化表

Revision ID: 071_chip_consensus_snapshots
Revises: 070_worker_heartbeat_stopped_at
Create Date: 2026-07-29

变更内容（[CHANGE-20260729-003] 核心与筹码解耦 - P0-10）：
- 新增 stock_chip_consensus_snapshots 表
- 保存每只标的每个交易日的 point-in-time 筹码共识快照
- 支持 upsert 幂等写入与失败重试
- 唯一键：instrument_id + trade_date + core_run_id + algorithm_version

设计说明：
- chip_payload 保存 ChipConsensusResult.to_dict() 完整输出
- chip_hash 独立于 core inputHash（daily+15m bars 的 hash）
- core_run_id 关联主 after_close 主 run（不反改主 run）
- 失败重试只覆盖失败 instrument，不重算成功项

非破坏性：
- 纯新增表，不修改/删除现有列
- 现有 stock_feature_snapshots 表的 summary_payload.first_pyramid.chipConsensus
  仍可读（review core 路径下 chipConsensus=None）
- 部署后 chip 表为空，主 run 成功后异步填充

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "071_chip_consensus_snapshots"
down_revision: str | None = "070_worker_heartbeat_stopped_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 stock_chip_consensus_snapshots 表。"""
    op.create_table(
        "stock_chip_consensus_snapshots",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
            comment="chip 快照 ID",
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
            comment="业务交易日",
        ),
        sa.Column(
            "core_run_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scheduler_job_runs.id"),
            nullable=False,
            comment="关联主 after_close run id（不反改主 run）",
        ),
        sa.Column(
            "algorithm_version",
            sa.Text(),
            nullable=False,
            comment="chip 算法版本（CHIP_CONSENSUS_ALGORITHM_VERSION）",
        ),
        sa.Column(
            "chip_hash",
            sa.Text(),
            nullable=False,
            comment="chip 输入 hash（daily + 15m bars）",
        ),
        sa.Column(
            "chip_payload",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="ChipConsensusResult.to_dict() 完整输出 JSONB",
        ),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'succeeded'"),
            comment="单股 chip 状态：succeeded/failed/skipped",
        ),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=True,
            comment="失败原因（status=failed 时写入）",
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
            "core_run_id",
            "algorithm_version",
            name="uq_chip_consensus_instrument_date_run_version",
        ),
        sa.Index(
            "ix_chip_consensus_trade_date",
            "trade_date",
        ),
        sa.Index(
            "ix_chip_consensus_core_run_id",
            "core_run_id",
        ),
        sa.Index(
            "ix_chip_consensus_instrument_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
    )


def downgrade() -> None:
    op.drop_table("stock_chip_consensus_snapshots")
