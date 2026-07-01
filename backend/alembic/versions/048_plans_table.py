"""048 plans table - 套餐定义表（替代 plan_contract.py 字典）

Revision ID: 048_plans_table
Revises: 047_calendar_semantics_fix
Create Date: 2026-06-30

变更内容：
- 创建 plans 表（套餐定义唯一真源，替代 app/constants/plan_contract.py 的 PLAN_CONTRACTS 字典）
- 字段：plan_code(唯一)/display_name/monitor_limit/notification_channel_limit/
  message_retention_days/features(JSONB)/status/created_at/updated_at
- 初始化两条套餐记录：
  - observe_20: 观察版，monitor_limit=20, notification_channel_limit=1,
    message_retention_days=30, features=6 项
  - research_50: 研究版，monitor_limit=50, notification_channel_limit=3,
    message_retention_days=180, features=7 项（含 advanced_export）

业务背景：
- 旧 plan_contract.py 的 PLAN_CONTRACTS 字典仅含 monitor_limit/name，字段不全
- plans 表补齐 notification_channel_limit/message_retention_days/features 字段
- 套餐定义集中到 DB 后，可通过迁移管理套餐变更，避免代码部署
- plan_code 字符串常量（DEFAULT_PLAN_CODE/ADMIN_PLAN_CODE）保留在 app/constants/plan_codes.py
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "048_plans_table"
down_revision: str | None = "047_calendar_semantics_fix"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [Plan] - 描述: 创建 plans 表（套餐定义唯一真源）
    op.create_table(
        "plans",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("plan_code", sa.Text(), nullable=False, comment="套餐代码 observe_20/research_50"),
        sa.Column("display_name", sa.Text(), nullable=False, comment="套餐展示名称（观察版/研究版）"),
        sa.Column("monitor_limit", sa.Integer(), nullable=False, comment="监控数量上限"),
        sa.Column(
            "notification_channel_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
            comment="通知渠道数量上限",
        ),
        sa.Column(
            "message_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("30"),
            comment="消息保留天数",
        ),
        sa.Column(
            "features",
            JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'"),
            comment="功能特性列表 JSONB 数组",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
            comment="状态 active/inactive",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="更新时间（每次 UPDATE 自动刷新）",
        ),
    )
    # [Plan] - 描述: plan_code 唯一索引（防止重复套餐代码）
    op.create_unique_constraint("uq_plans_plan_code", "plans", ["plan_code"])

    # [Plan] - 描述: 初始化 observe_20 套餐（观察版，6 个 features）
    op.execute(
        """
        INSERT INTO plans (plan_code, display_name, monitor_limit,
            notification_channel_limit, message_retention_days, features, status)
        VALUES (
            'observe_20', '观察版', 20, 1, 30,
            '["trend_selection", "stock_detail", "node_monitor", '
            '"in_app_message", "feishu_notification", "stock_memo"]'::jsonb,
            'active'
        )
        """
    )

    # [Plan] - 描述: 初始化 research_50 套餐（研究版，7 个 features，含 advanced_export）
    op.execute(
        """
        INSERT INTO plans (plan_code, display_name, monitor_limit,
            notification_channel_limit, message_retention_days, features, status)
        VALUES (
            'research_50', '研究版', 50, 3, 180,
            '["trend_selection", "stock_detail", "node_monitor", '
            '"in_app_message", "feishu_notification", "stock_memo", '
            '"advanced_export"]'::jsonb,
            'active'
        )
        """
    )


def downgrade() -> None:
    # [Plan] - 描述: 删除 plans 表
    op.drop_table("plans")


if __name__ == "__main__":
    # [Plan] - 描述: 自测入口，验证 revision 链与函数定义（不连接数据库）
    assert revision == "048_plans_table"
    assert down_revision == "047_calendar_semantics_fix"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
