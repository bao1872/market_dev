"""062 market boards - qstock 概念/行业板块同步表

Revision ID: 062_market_boards
Revises: 061_snapshot_source_run_id
Create Date: 2026-07-11

变更内容：
- 新增 market_boards 表：板块目录（行业/概念），只存最新态
- 新增 market_board_memberships 表：板块成分股关系，只存最新态
- 不增加历史日期维度，不存板块行情/资金流

设计说明（PRD §7.5）：
- qstock 只存在于独立采集适配器，不成为用户请求链的运行时依赖
- 每日收盘后执行一次：拉目录→标准化→拉成分→暂存→校验→事务原子切换
- 失败保持上一成功版本，不删除旧关系
- market_boards.type: 'industry' | 'concept'
- (external_code, type) 唯一约束：同类型同外部代码唯一
- (board_id, instrument_id) 唯一约束：避免重复关系

用法：
    cd backend && alembic upgrade head
    cd backend && alembic downgrade -1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "062_market_boards"
down_revision: str | None = "061_snapshot_source_run_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. market_boards: 板块目录
    op.create_table(
        "market_boards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("external_code", sa.String(32), nullable=False, comment="外部代码（qstock 原始代码）"),
        sa.Column("name", sa.String(128), nullable=False, comment="板块名称"),
        sa.Column("type", sa.String(16), nullable=False, comment="板块类型：industry | concept"),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="板块目录（只存最新态，不存历史）",
    )
    op.create_unique_constraint(
        "uq_market_boards_code_type",
        "market_boards",
        ["external_code", "type"],
    )
    op.create_index(
        "ix_market_boards_type",
        "market_boards",
        ["type"],
    )

    # 2. market_board_memberships: 板块成分股关系
    op.create_table(
        "market_board_memberships",
        sa.Column("board_id", UUID(as_uuid=True), sa.ForeignKey("market_boards.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("instrument_id", UUID(as_uuid=True), sa.ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        comment="板块成分股关系（只存最新态）",
    )
    op.create_index(
        "ix_market_board_memberships_instrument",
        "market_board_memberships",
        ["instrument_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_board_memberships_instrument", table_name="market_board_memberships")
    op.drop_table("market_board_memberships")
    op.drop_index("ix_market_boards_type", table_name="market_boards")
    op.drop_constraint("uq_market_boards_code_type", "market_boards", type_="unique")
    op.drop_table("market_boards")


if __name__ == "__main__":
    assert revision == "062_market_boards"
    assert down_revision == "061_snapshot_source_run_id"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
