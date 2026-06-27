"""041 notification_channels active Feishu partial unique index

Revision ID: 041_notification_channel_active_unique
Revises: 040_capture_jobs
Create Date: 2026-06-27

变更内容：
- 新增部分唯一索引 uq_notification_channels_active_feishu
  仅约束 status='active' 且 adapter_type 为飞书系（feishu_webhook/feishu_platform_app）的记录。
- 创建索引前，先检查是否已存在重复 active 飞书渠道；若存在则主动失败并给出清晰提示，
  禁止静默修改用户数据。

设计说明：
- 业务规则：同一用户下最多只允许一条 active 飞书记录，不区分 webhook / platform_app。
- 使用部分唯一索引而非全局唯一约束，保留用户可拥有多条 inactive/invalid 历史记录的灵活性，
  同时允许 mock/email 等非飞书渠道不受限制。
- 数据修复必须由人工确认后执行，迁移脚本不再自动去重，避免误伤线上数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "041_notification_channel_active_unique"
down_revision: str | None = "040_capture_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [NotificationChannel] - 前置检查：禁止迁移脚本静默修改用户数据
    # 若已存在重复 active 飞书渠道，主动失败并提示人工处理
    connection = op.get_bind()
    result = connection.execute(
        sa.text(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id
                FROM notification_channels
                WHERE status = 'active'
                  AND adapter_type IN ('feishu_webhook', 'feishu_platform_app')
                GROUP BY user_id
                HAVING COUNT(*) > 1
            ) AS duplicates
            """
        )
    )
    duplicate_count = result.scalar()
    if duplicate_count and duplicate_count > 0:
        raise RuntimeError(
            "检测到重复的 active 飞书渠道（每个 user_id 最多一条 active 飞书记录，"
            "不区分 webhook / platform_app）。请先人工确认并清理重复数据后再执行迁移。"
        )

    # [NotificationChannel] - 部分唯一索引：同一用户仅允许一条 active 飞书记录
    op.create_index(
        "uq_notification_channels_active_feishu",
        "notification_channels",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND adapter_type IN ('feishu_webhook', 'feishu_platform_app')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_notification_channels_active_feishu",
        table_name="notification_channels",
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "041_notification_channel_active_unique"
    assert down_revision == "040_capture_jobs"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
