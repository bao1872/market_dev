"""034 message_delivery group_id and text type

Revision ID: 034_message_delivery_group_id
Revises: 033_message_delivery_state_machine
Create Date: 2026-06-25

变更内容：
- message_deliveries 新增 message_group_id 列（Text，nullable，带索引）
  用于关联同一监控事件的 text + image 两条投递记录
- message_deliveries.delivery_type 默认值从 'card' 改为 'text'
  可选值扩展为 text / image / card（card 仅兼容管理后台预览）
- 新增 delivery_type CHECK 约束，限定取值为 text/image/card
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "034_message_delivery_group_id"
down_revision: str | None = "033_message_delivery_state_machine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 允许的 MessageDelivery.delivery_type 枚举值
_DELIVERY_TYPE_VALUES = ("text", "image", "card")


def upgrade() -> None:
    # 新增 message_group_id 列（带索引，用于按组查询关联的 text+image 投递）
    op.add_column(
        "message_deliveries",
        sa.Column(
            "message_group_id",
            sa.Text(),
            nullable=True,
            comment="消息组 ID（关联同一事件的 text+image 两条投递记录）",
        ),
    )
    op.create_index(
        "ix_message_deliveries_message_group_id",
        "message_deliveries",
        ["message_group_id"],
    )

    # 修改 delivery_type 默认值为 'text'，更新注释
    op.alter_column(
        "message_deliveries",
        "delivery_type",
        server_default=sa.text("'text'"),
        comment="text/image/card（card 仅兼容管理后台预览）",
    )

    # 新增 delivery_type CHECK 约束
    op.create_check_constraint(
        "ck_message_deliveries_delivery_type",
        "message_deliveries",
        sa.text(f"delivery_type IN {_DELIVERY_TYPE_VALUES}"),
    )


def downgrade() -> None:
    # 删除 delivery_type CHECK 约束
    op.drop_constraint(
        "ck_message_deliveries_delivery_type", "message_deliveries", type_="check"
    )

    # 恢复 delivery_type 默认值为 'card'
    op.alter_column(
        "message_deliveries",
        "delivery_type",
        server_default=sa.text("'card'"),
        comment="card/image",
    )

    # 删除 message_group_id 索引与列
    op.drop_index(
        "ix_message_deliveries_message_group_id", table_name="message_deliveries"
    )
    op.drop_column("message_deliveries", "message_group_id")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "034_message_delivery_group_id"
    assert down_revision == "033_message_delivery_state_machine"
    assert _DELIVERY_TYPE_VALUES == ("text", "image", "card")
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print(f"delivery_type_values={_DELIVERY_TYPE_VALUES}")
    print("OK: 迁移文件验证通过")
