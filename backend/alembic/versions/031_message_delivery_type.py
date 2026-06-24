"""031 add delivery_type to message_deliveries

Revision ID: 031_message_delivery_type
Revises: 030_strategy_run_attempt_no
Create Date: 2026-06-24

变更内容：
- message_deliveries 新增 delivery_type 列，默认 card
- 业务含义：区分卡片消息 (card) 与图片消息 (image) 投递
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "031_message_delivery_type"
down_revision: str | None = "030_strategy_run_attempt_no"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "message_deliveries",
        sa.Column(
            "delivery_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'card'"),
            comment="card/image",
        ),
    )


def downgrade() -> None:
    op.drop_column("message_deliveries", "delivery_type")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "031_message_delivery_type"
    assert down_revision == "030_strategy_run_attempt_no"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
