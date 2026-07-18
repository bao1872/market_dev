"""065 instruments add factor reconciliation version columns

Revision ID: 065_instruments_factor_reconciliation
Revises: 064_feature_snapshot_market_data_meta
Create Date: 2026-07-18

变更内容（CHANGE-20260718-005 全市场一致性修复）：
- instruments 新增 3 列用于跟踪复权因子对账状态：
  - factor_algorithm_version VARCHAR(8) — 上次成功对账时使用的算法版本（如 'fq-v1'）
  - factor_reconciliation_version INTEGER — 上次成功对账的对账版本（如 1）
  - factor_reconciled_at TIMESTAMPTZ — 上次成功对账时间

设计说明：
- 全部可空，兼容历史 instruments（NULL 表示未对账）
- 版本 < 当前常量版本时标记 needs_reaudit（即使因子值看起来正确）
- 弥补 xdxr fingerprint 无法发现"fingerprint 未变但历史序列已错误"的缺口
- 不加索引（对账按批次全表扫描，不按版本列查询）

配合：
- app.constants.factor_contract.FACTOR_ALGORITHM_VERSION = 'fq-v1'
- app.constants.factor_contract.FACTOR_RECONCILIATION_VERSION = 1
- app.services.factor_consistency_audit.FactorConsistencyAuditor
- app.services.factor_reconciliation.FactorReconciliationTask

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "065_instruments_factor_reconciliation"
down_revision: str | None = "064_feature_snapshot_market_data_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [CHANGE-20260718-005] - factor_algorithm_version: 上次对账时使用的复权算法版本
    op.add_column(
        "instruments",
        sa.Column("factor_algorithm_version", sa.String(length=8), nullable=True),
    )
    # [CHANGE-20260718-005] - factor_reconciliation_version: 上次对账的对账逻辑版本
    op.add_column(
        "instruments",
        sa.Column("factor_reconciliation_version", sa.Integer(), nullable=True),
    )
    # [CHANGE-20260718-005] - factor_reconciled_at: 上次成功对账时间
    op.add_column(
        "instruments",
        sa.Column("factor_reconciled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("instruments", "factor_reconciled_at")
    op.drop_column("instruments", "factor_reconciliation_version")
    op.drop_column("instruments", "factor_algorithm_version")
