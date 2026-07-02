"""054 strategy_run_items reason_code

Revision ID: 054_strategy_run_items_reason_code
Revises: 053_message_delivery_image_upload_status
Create Date: 2026-07-02

变更内容：
- strategy_run_items 新增 reason_code 列（Text，nullable）
  用于记录 skipped/failed 原因的标准编码，支撑发布门禁对 skipped 原因的 allowlist 校验。

业务背景：
- Phase 5 Task 5.4：严格发布门禁要求每个 skipped 项都有 reason_code，且 reason_code
  必须在允许列表内；single-stock 超时记 failed 时同样填充 reason_code。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "054_strategy_run_items_reason_code"
down_revision: str | None = "053_message_delivery_image_upload_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [StrategyRunItem] - 描述: skipped/failed 原因标准编码（allowlist 校验）
    op.add_column(
        "strategy_run_items",
        sa.Column(
            "reason_code",
            sa.Text(),
            nullable=True,
            comment="skipped/failed 原因标准编码（如 insufficient_data, suspended, timeout）",
        ),
    )


def downgrade() -> None:
    op.drop_column("strategy_run_items", "reason_code")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "054_strategy_run_items_reason_code"
    assert down_revision == "053_message_delivery_image_upload_status"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
