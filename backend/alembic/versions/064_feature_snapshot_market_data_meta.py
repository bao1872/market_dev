"""064 stock_feature_snapshot_runs add market data meta columns

Revision ID: 064_feature_snapshot_market_data_meta
Revises: 063_instruments_share_capital
Create Date: 2026-07-18

变更内容（CHANGE-20260717-002 SSOT）：
- stock_feature_snapshot_runs 新增 5 列用于记录行情 SSOT 诊断信息：
  - source_bar_hash VARCHAR(16) — MDAS source_bar_hash（OHLCV SHA256 前16字符）
  - adj_factor_hash VARCHAR(16) — MDAS adj_factor_hash（因子序列 SHA256 前16字符）
  - market_data_contract_version VARCHAR(8) — MDAS 契约版本（如 v2）
  - completed_through TIMESTAMPTZ — MDAS completed_through（最新已完成 bar 时间）
  - adjustment_as_of DATE — 复权锚点业务日（point-in-time 复权）

设计说明：
- 全部可空，兼容历史 run（schema_version=1 的 run 这些列为 NULL）
- 仅 succeeded run 在 finish_snapshot_run 时写入有效值
- 不加索引（run 表小，按 trade_date 查询已有 ix_snapshot_runs_trade_date_status 索引）
- 配合 feature_snapshot_service._SCHEMA_VERSION 1→2 语义升级（bars 来源改为 MDAS point-in-time）

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "064_feature_snapshot_market_data_meta"
down_revision: str | None = "063_instruments_share_capital"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [CHANGE-20260717-002 SSOT] - source_bar_hash: MDAS 返回的 OHLCV SHA256 前16字符
    op.add_column(
        "stock_feature_snapshot_runs",
        sa.Column("source_bar_hash", sa.String(length=16), nullable=True),
    )
    # [CHANGE-20260717-002 SSOT] - adj_factor_hash: MDAS 返回的因子序列 SHA256 前16字符
    op.add_column(
        "stock_feature_snapshot_runs",
        sa.Column("adj_factor_hash", sa.String(length=16), nullable=True),
    )
    # [CHANGE-20260717-002 SSOT] - market_data_contract_version: MDAS 契约版本（如 v2）
    op.add_column(
        "stock_feature_snapshot_runs",
        sa.Column("market_data_contract_version", sa.String(length=8), nullable=True),
    )
    # [CHANGE-20260717-002 SSOT] - completed_through: MDAS 返回的最新已完成 bar 时间
    op.add_column(
        "stock_feature_snapshot_runs",
        sa.Column("completed_through", sa.DateTime(timezone=True), nullable=True),
    )
    # [CHANGE-20260717-002 SSOT] - adjustment_as_of: 复权锚点业务日（point-in-time 复权）
    op.add_column(
        "stock_feature_snapshot_runs",
        sa.Column("adjustment_as_of", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("stock_feature_snapshot_runs", "adjustment_as_of")
    op.drop_column("stock_feature_snapshot_runs", "completed_through")
    op.drop_column("stock_feature_snapshot_runs", "market_data_contract_version")
    op.drop_column("stock_feature_snapshot_runs", "adj_factor_hash")
    op.drop_column("stock_feature_snapshot_runs", "source_bar_hash")
