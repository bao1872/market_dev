"""052 access audit logs - 访问审计日志表，记录 admin 关键操作。

Revision ID: 052_access_audit_logs
Revises: 051_remove_strategy_author_role
Create Date: 2026-07-01

变更内容：
- 创建 access_audit_logs 表，记录 admin 关键操作（邀请码生成/作废、用户禁用等）
- 字段设计：
  * actor_user_id: 操作者 user_id（FK users.id），索引
  * action: 操作类型（如 invite_code.create / invite_code.revoke / user.disable）
  * target_type / target_id: 目标对象类型与 ID（target_id 字符串兼容 UUID/其他）
  * before_data / after_data: 操作前后状态快照（JSONB，便于审计追溯）
  * request_id: 请求追踪 ID
  * ip_hash: IP 哈希（不存明文 IP，符合 docs/安全规范.md 隐私要求）
  * created_at: 操作时间（带时区）
- 索引：
  * idx_access_audit_logs_actor_created: (actor_user_id, created_at) 按操作者查询
  * idx_access_audit_logs_target: (target_type, target_id, created_at) 按目标对象查询

业务背景：
- Phase 4.5：审计日志基础设施，对接 docs/安全规范.md 8.1 节关键操作日志要求
- admin 端点（如 admin_subscription.py 的邀请码生成/作废）接入审计日志
- 服务层 app.services.access_audit_service 提供统一写入/查询接口

downgrade 行为：
- 删除索引与表（审计日志为新建表，无数据回滚需求）
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "052_access_audit_logs"
down_revision: str | None = "051_remove_strategy_author_role"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # [AuditLog] - 描述: 创建 access_audit_logs 表（admin 操作审计日志）
    op.create_table(
        "access_audit_logs",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_user_id",
            UUID(as_uuid=True),
            nullable=False,
            comment="操作者 user_id（admin）",
        ),
        sa.Column(
            "action",
            sa.String(length=100),
            nullable=False,
            comment="操作类型 invite_code.create/invite_code.revoke/user.disable 等",
        ),
        sa.Column(
            "target_type",
            sa.String(length=50),
            nullable=False,
            comment="目标对象类型 invite_code/user/subscription 等",
        ),
        sa.Column(
            "target_id",
            sa.String(length=100),
            nullable=True,
            comment="目标对象 ID（字符串，兼容 UUID/其他）",
        ),
        sa.Column(
            "before_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="操作前状态快照",
        ),
        sa.Column(
            "after_data",
            JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="操作后状态快照",
        ),
        sa.Column(
            "request_id",
            sa.String(length=100),
            nullable=True,
            comment="请求追踪 ID",
        ),
        sa.Column(
            "ip_hash",
            sa.String(length=64),
            nullable=True,
            comment="IP 哈希（不存明文 IP）",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
            comment="操作时间",
        ),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
    )
    # [AuditLog] - 描述: 按操作者+时间查询索引（admin 行为追溯）
    op.create_index(
        "idx_access_audit_logs_actor_created",
        "access_audit_logs",
        ["actor_user_id", "created_at"],
    )
    # [AuditLog] - 描述: 按目标对象+时间查询索引（目标对象操作历史）
    op.create_index(
        "idx_access_audit_logs_target",
        "access_audit_logs",
        ["target_type", "target_id", "created_at"],
    )


def downgrade() -> None:
    # [AuditLog] - 描述: 删除索引与表（审计日志为新建表，无数据回滚需求）
    op.drop_index("idx_access_audit_logs_target", table_name="access_audit_logs")
    op.drop_index("idx_access_audit_logs_actor_created", table_name="access_audit_logs")
    op.drop_table("access_audit_logs")


if __name__ == "__main__":
    # [AuditLog] - 描述: 自测入口，验证 revision 链与函数定义（不连接数据库）
    assert revision == "052_access_audit_logs"
    assert down_revision == "051_remove_strategy_author_role"
    assert callable(upgrade)
    assert callable(downgrade)
    print(f"revision={revision}")
    print(f"down_revision={down_revision}")
    print("OK: 迁移文件验证通过")
