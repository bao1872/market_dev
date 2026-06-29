"""047 trading_calendar semantics fix

Revision ID: 047_calendar_semantics_fix
Revises: 046_calendar_source_status
Create Date: 2026-06-29

变更内容：
- 修正 source/status 默认值：MOOTDX_HOLIDAY / UNKNOWN
- 新增 note/validation_error 字段
- 迁移历史数据：
  - source='pytdx' -> 'MOOTDX_HISTORICAL'
  - status='confirmed_trading' -> 'OPEN'
  - status='confirmed_closed' -> 'CLOSED'
  - status='unknown' -> 'UNKNOWN'
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "047_calendar_semantics_fix"
down_revision: str | None = "046_calendar_source_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Calendar] - 描述: 新增 note/validation_error 字段
    op.add_column(
        "trading_calendar",
        sa.Column(
            "note",
            sa.String(length=256),
            nullable=True,
            comment="人工备注",
        ),
    )
    op.add_column(
        "trading_calendar",
        sa.Column(
            "validation_error",
            sa.String(length=512),
            nullable=True,
            comment="校验失败说明",
        ),
    )

    # [Calendar] - 描述: 迁移旧 source/status 值到新语义
    op.execute(
        "UPDATE trading_calendar SET source = 'MOOTDX_HISTORICAL' WHERE source = 'pytdx'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'OPEN' WHERE status = 'confirmed_trading'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'CLOSED' WHERE status = 'confirmed_closed'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'UNKNOWN' WHERE status = 'unknown'"
    )

    # [Calendar] - 描述: 修正 source/status 默认值
    op.alter_column(
        "trading_calendar",
        "source",
        server_default=sa.text("'MOOTDX_HOLIDAY'"),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "trading_calendar",
        "status",
        server_default=sa.text("'UNKNOWN'"),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )


def downgrade() -> None:
    # [Calendar] - 描述: 回滚默认值
    op.alter_column(
        "trading_calendar",
        "source",
        server_default=sa.text("'pytdx'"),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )
    op.alter_column(
        "trading_calendar",
        "status",
        server_default=sa.text("'unknown'"),
        existing_type=sa.String(length=32),
        existing_nullable=False,
    )

    # [Calendar] - 描述: 回滚 source/status 数据
    op.execute(
        "UPDATE trading_calendar SET source = 'pytdx' WHERE source = 'MOOTDX_HISTORICAL'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'confirmed_trading' WHERE status = 'OPEN'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'confirmed_closed' WHERE status = 'CLOSED'"
    )
    op.execute(
        "UPDATE trading_calendar SET status = 'unknown' WHERE status = 'UNKNOWN'"
    )

    # [Calendar] - 描述: 删除 note/validation_error 字段
    op.drop_column("trading_calendar", "validation_error")
    op.drop_column("trading_calendar", "note")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "047_calendar_semantics_fix"
    assert down_revision == "046_calendar_source_status"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
