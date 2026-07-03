"""055 feishu platform app only - 永久禁止 feishu_webhook

Revision ID: 055_feishu_platform_app_only
Revises: 054_strategy_run_items_reason_code
Create Date: 2026-07-02

变更内容：
- 前置检查：notification_channels 表若存在 adapter_type='feishu_webhook' 行，主动失败
- 重建 active 唯一索引为 platform_app only（删除 041 创建的 webhook+platform_app 联合索引）
- 新增 CHECK 约束：adapter_type != 'feishu_webhook'（永久禁止新增 webhook）

设计说明：
- Phase C 统一 Platform App only，Webhook 运行时已删除
- 不静默删除数据：若存在历史 feishu_webhook 行，需人工确认后清理
- 041 创建的旧索引同时约束 webhook+platform_app，需重建为 platform_app only
- 不修改 041（历史 migration 不动），仅在 055 中 drop+recreate
- 不打印 target_config，避免泄露敏感字段（webhook_url/sign_secret）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "055_feishu_platform_app_only"
down_revision: str | None = "054_strategy_run_items_reason_code"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [NotificationChannel] - 前置检查：禁止静默删除历史 webhook 数据
    # 若存在 feishu_webhook 行，主动失败并提示人工清理
    # 不打印 target_config，避免泄露 webhook_url/sign_secret 等敏感字段
    connection = op.get_bind()
    result = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM notification_channels "
            "WHERE adapter_type = 'feishu_webhook'"
        )
    )
    webhook_count = result.scalar()
    if webhook_count and webhook_count > 0:
        raise RuntimeError(
            f"检测到 {webhook_count} 条 feishu_webhook 渠道记录。"
            "Phase C 已永久删除 Webhook 运行时，统一为 Platform App only。"
            "请先人工确认并清理这些记录（迁移至 feishu_platform_app 或删除）"
            "后再执行本迁移。本迁移不静默删除任何数据。"
        )

    # [NotificationChannel] - 重建 active 唯一索引为 platform_app only
    # 041 创建的旧索引同时约束 feishu_webhook + feishu_platform_app
    # Webhook 已废弃，索引只需约束 platform_app
    op.drop_index(
        "uq_notification_channels_active_feishu",
        table_name="notification_channels",
    )
    op.create_index(
        "uq_notification_channels_active_feishu",
        "notification_channels",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND adapter_type = 'feishu_platform_app'"
        ),
    )

    # [NotificationChannel] - CHECK 约束：永久禁止新增 feishu_webhook
    # 数据库层兜底，防止应用层漏检或直接 SQL 插入
    op.create_check_constraint(
        "ck_notification_channels_no_feishu_webhook",
        "notification_channels",
        "adapter_type != 'feishu_webhook'",
    )


def downgrade() -> None:
    # 回滚：移除 CHECK 约束，重建 041 的旧索引（含 webhook + platform_app）
    op.drop_constraint(
        "ck_notification_channels_no_feishu_webhook",
        "notification_channels",
        type_="check",
    )
    op.drop_index(
        "uq_notification_channels_active_feishu",
        table_name="notification_channels",
    )
    op.create_index(
        "uq_notification_channels_active_feishu",
        "notification_channels",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "status = 'active' AND adapter_type IN ('feishu_webhook', 'feishu_platform_app')"
        ),
    )


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "055_feishu_platform_app_only"
    assert down_revision == "054_strategy_run_items_reason_code"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
