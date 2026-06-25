"""033 message_delivery state machine

Revision ID: 033_message_delivery_state_machine
Revises: 032_dsa_backfill
Create Date: 2026-06-25

变更内容：
- message_deliveries 新增 image_url 列（图片投递时截图服务返回的本地静态 URL）
- message_deliveries.status 增加 CHECK 约束，限定取值：
  pending/sending/success/failed/retrying/dead
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "033_message_delivery_state_machine"
down_revision: str | None = "032_dsa_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 允许的 MessageDelivery.status 枚举值
_STATUS_VALUES = {"pending", "sending", "success", "failed", "retrying", "dead"}


def upgrade() -> None:
    # 扩展 alembic_version 表 version_num 列长度，避免长 revision id 写入失败
    op.execute("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE varchar(255)")

    # 新增 image_url 列
    op.add_column(
        "message_deliveries",
        sa.Column(
            "image_url",
            sa.Text(),
            nullable=True,
            comment="图片投递时截图 URL（本地静态地址）",
        ),
    )

    # 增加 status CHECK 约束
    op.create_check_constraint(
        "ck_message_deliveries_status",
        "message_deliveries",
        sa.text(f"status IN {tuple(_STATUS_VALUES)}"),
    )


def downgrade() -> None:
    op.drop_constraint("ck_message_deliveries_status", "message_deliveries", type_="check")
    op.drop_column("message_deliveries", "image_url")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "033_message_delivery_state_machine"
    assert down_revision == "032_dsa_backfill"
    assert _STATUS_VALUES == {"pending", "sending", "success", "failed", "retrying", "dead"}
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
