"""045 beta_applications table

Revision ID: 045_beta_applications
Revises: 044_plan_contract_fields
Create Date: 2026-06-28

变更内容：
- 新建 beta_applications 表（内测申请闭环，spec 第三节）
- 字段：id/wechat/phone/watch_stock_count/reason_code/reason_other/status/
  source/admin_note/handled_by/handled_at/submitted_at/updated_at/ip_hash/
  feishu_delivery_status/feishu_delivered_at/feishu_last_error
- 索引：status/submitted_at/ip_hash/phone/wechat
- CHECK 约束：status 枚举（new/contacted/approved/rejected/converted）
- CHECK 约束：reason_code 枚举（busy/too_many/forget/quant/other）

业务背景：
- advice.md v8 第三节要求新增内测申请后端闭环
- 公开端点 POST /public/beta-applications 无需登录即可提交
- ip_hash 存储 IP 的 SHA256 哈希（不存原始 IP）
- 飞书通知通过 Outbox 异步投递（feishu_delivery_status 跟踪状态）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "045_beta_applications"
down_revision: str | None = "044_plan_contract_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [beta_application] - 描述: 内测申请表（公开端点提交，无需登录）
    op.create_table(
        "beta_applications",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wechat", sa.String(length=64), nullable=True, comment="微信号（与 phone 至少填一个）"),
        sa.Column("phone", sa.String(length=32), nullable=True, comment="手机号（与 wechat 至少填一个）"),
        sa.Column("watch_stock_count", sa.Integer(), nullable=False, comment="盯盘股票数量（正整数）"),
        sa.Column(
            "reason_code",
            sa.String(length=32),
            nullable=False,
            comment="使用理由代码 busy/too_many/forget/quant/other",
        ),
        sa.Column(
            "reason_other",
            sa.Text(),
            nullable=True,
            comment="补充说明（reason_code='other' 时必填）",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'new'"),
            comment="new/contacted/approved/rejected/converted",
        ),
        sa.Column(
            "source",
            sa.String(length=64),
            nullable=True,
            comment="提交来源（如 landing_page/pricing_section）",
        ),
        sa.Column("admin_note", sa.Text(), nullable=True, comment="管理员备注"),
        sa.Column(
            "handled_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=True,
            comment="处理人 user_id（管理员）",
        ),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True, comment="处理时间"),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="提交时间",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="更新时间",
        ),
        sa.Column(
            "ip_hash",
            sa.String(length=64),
            nullable=False,
            comment="客户端 IP 的 SHA256 哈希",
        ),
        sa.Column(
            "feishu_delivery_status",
            sa.String(length=16),
            nullable=True,
            comment="飞书投递状态 pending/success/failed",
        ),
        sa.Column(
            "feishu_delivered_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="飞书投递成功时间",
        ),
        sa.Column(
            "feishu_last_error",
            sa.Text(),
            nullable=True,
            comment="飞书投递最近错误",
        ),
        sa.ForeignKeyConstraint(["handled_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('new','contacted','approved','rejected','converted')",
            name="beta_applications_status_check",
        ),
        sa.CheckConstraint(
            "reason_code IN ('busy','too_many','forget','quant','other')",
            name="beta_applications_reason_code_check",
        ),
    )
    # 索引：支持管理后台筛选、限流查询、重复检测
    op.create_index(
        "ix_beta_applications_status", "beta_applications", ["status"]
    )
    op.create_index(
        "ix_beta_applications_submitted_at", "beta_applications", ["submitted_at"]
    )
    op.create_index(
        "ix_beta_applications_ip_hash", "beta_applications", ["ip_hash"]
    )
    op.create_index(
        "ix_beta_applications_phone", "beta_applications", ["phone"]
    )
    op.create_index(
        "ix_beta_applications_wechat", "beta_applications", ["wechat"]
    )


def downgrade() -> None:
    # 回滚：删除索引和表
    op.drop_index("ix_beta_applications_wechat", table_name="beta_applications")
    op.drop_index("ix_beta_applications_phone", table_name="beta_applications")
    op.drop_index("ix_beta_applications_ip_hash", table_name="beta_applications")
    op.drop_index("ix_beta_applications_submitted_at", table_name="beta_applications")
    op.drop_index("ix_beta_applications_status", table_name="beta_applications")
    op.drop_table("beta_applications")


if __name__ == "__main__":
    # 自测入口：验证 revision 链与函数定义（不连接数据库）
    assert revision == "045_beta_applications"
    assert down_revision == "044_plan_contract_fields"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
