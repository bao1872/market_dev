"""053 message_delivery image upload status

Revision ID: 053_message_delivery_image_upload_status
Revises: 052_access_audit_logs
Create Date: 2026-07-02

变更内容：
- message_deliveries 新增 image_upload_status 列（Text，nullable）
  用于单独记录图片上传阶段状态：pending/success/failed
- 新增 image_upload_error_code 列（Text，nullable）记录上传阶段错误码
- 新增 image_upload_provider_response 列（JSONB，nullable）记录上传阶段渠道返回
- 新增 image_key 列（Text，nullable）记录飞书图片上传成功后的 image_key

业务背景：
- Phase 3 Task 3.5：图片上传与图片投递必须分别记录状态
- 仅 delivery_type='image' 时填充；其他类型保持 NULL
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "053_message_delivery_image_upload_status"
down_revision: str | None = "052_access_audit_logs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [MessageDelivery] - 描述: 图片上传阶段状态（与最终投递状态分离）
    op.add_column(
        "message_deliveries",
        sa.Column(
            "image_upload_status",
            sa.Text(),
            nullable=True,
            comment="图片上传状态 pending/success/failed（仅 delivery_type=image）",
        ),
    )
    # [MessageDelivery] - 描述: 图片上传阶段错误码
    op.add_column(
        "message_deliveries",
        sa.Column(
            "image_upload_error_code",
            sa.Text(),
            nullable=True,
            comment="图片上传错误码",
        ),
    )
    # [MessageDelivery] - 描述: 图片上传阶段渠道返回（JSONB）
    op.add_column(
        "message_deliveries",
        sa.Column(
            "image_upload_provider_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="图片上传渠道返回",
        ),
    )
    # [MessageDelivery] - 描述: 飞书图片 image_key（上传成功后）
    op.add_column(
        "message_deliveries",
        sa.Column(
            "image_key",
            sa.Text(),
            nullable=True,
            comment="飞书图片 image_key",
        ),
    )


def downgrade() -> None:
    op.drop_column("message_deliveries", "image_key")
    op.drop_column("message_deliveries", "image_upload_provider_response")
    op.drop_column("message_deliveries", "image_upload_error_code")
    op.drop_column("message_deliveries", "image_upload_status")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "053_message_delivery_image_upload_status"
    assert down_revision == "052_access_audit_logs"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
