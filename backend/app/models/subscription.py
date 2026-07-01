"""Subscription ORM 模型 - 订阅表（subscriptions）。

对应迁移：
- 049_subscriptions_table: 创建 subscriptions 表 + 从 memberships 迁移数据（保留 memberships 表）
- 050_drop_memberships_table: 删除旧 memberships 表（独立迁移，便于回滚）

表结构：
- subscriptions: 用户订阅表（user_id 唯一，记录当前套餐与权益快照）

设计要点：
- 一个用户只有一条 subscription 记录（user_id 唯一约束）
- 取代旧 memberships 表（Phase 2 Task 2.2 重命名）
- 字段重命名：started_at → starts_at
- 字段新增：entitlement_snapshot（JSONB 权益快照）、source（来源）、created_by（创建人）、
  created_at（创建时间）
- 字段移除：monitor_limit（迁移到 entitlement_snapshot 中）
- 有效订阅实时计算：status='active' AND starts_at <= now AND expires_at > now
- status 不持久化 'expired'：到期由业务逻辑实时计算，DB CheckConstraint 仅允许
  active/revoked/cancelled
- entitlement_snapshot 非空：订阅创建时即从 plans 表快照，保证权益可追溯
- 不缓存到登录态，避免 status 漂移
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Subscription(Base):
    """订阅表 - 一个用户一条记录，记录订阅有效期、当前套餐与权益快照。

    字段语义：
    - user_id: 用户 ID（唯一，一个用户一条订阅）
    - plan_code: 套餐代码（observe_20/research_50），从 plans 表选定
    - status: 订阅状态（active/revoked/cancelled）。'expired' 不持久化，由业务逻辑
      根据 expires_at 实时计算（DB CheckConstraint 拒绝 'expired'）
    - starts_at: 生效时间（原 Membership.started_at，重命名）
    - expires_at: 过期时间，业务逻辑据此判断有效订阅
    - entitlement_snapshot: 权益快照（JSONB，非空），从 plans 表快照
      monitor_limit/notification_channel_limit/message_retention_days/features
    - source: 来源（invite=邀请码兑换 / admin_grant=管理员授予 / migration=旧 memberships 迁移）
    - created_by: 创建人 user_id（管理员授予时记录，邀请码兑换时为 NULL）
    - created_at / updated_at: 时间戳（自动维护）

    有效订阅实时计算（不缓存到登录态）：
        status = 'active' AND starts_at <= now AND expires_at > now

    注意：status 不持久化 'expired'，到期判断由 get_effective_subscription_status 实时计算。
    续期时更新 expires_at 并将 status 重置为 active，同时刷新 plan_code/entitlement_snapshot。
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="用户 ID（唯一，一个用户一条订阅记录）",
    )
    plan_code: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="套餐代码 observe_20/research_50",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        server_default=sa_text("'active'"),
        comment="active/revoked/cancelled（expired 实时计算，不持久化）",
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="订阅生效时间"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="订阅到期时间"
    )
    entitlement_snapshot: Mapped[dict[str, Any]] = mapped_column(
        # none_as_null=True: Python None 映射为 SQL NULL（而非 JSON 'null'），
        # 使 NOT NULL 约束对 None 真正生效（默认 none_as_null=False 会存 JSON null 绕过约束）
        JSONB(astext_type=String(), none_as_null=True),
        nullable=False,
        comment="权益快照（monitor_limit/notification_channel_limit/message_retention_days/features）",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="invite",
        server_default=sa_text("'invite'"),
        comment="来源 invite/admin_grant/migration",
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="创建人 user_id（管理员授予时记录，邀请码兑换时为 NULL）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription(user_id={self.user_id!r}, status={self.status!r}, "
            f"plan_code={self.plan_code!r}, source={self.source!r}, "
            f"expires_at={self.expires_at!r})>"
        )


if __name__ == "__main__":
    # [Subscription] - 描述: 自测入口，验证 ORM 模型映射（无副作用，不连接数据库）
    assert Subscription.__tablename__ == "subscriptions"
    columns = {c.name for c in Subscription.__table__.columns}
    expected = {
        "id",
        "user_id",
        "plan_code",
        "status",
        "starts_at",
        "expires_at",
        "entitlement_snapshot",
        "source",
        "created_by",
        "created_at",
        "updated_at",
    }
    assert columns == expected, f"Subscription 列不匹配: {columns ^ expected}"
    # user_id 必须唯一
    assert Subscription.__table__.c.user_id.unique is True
    # plan_code/status/starts_at/expires_at/entitlement_snapshot/source 不可空
    assert Subscription.__table__.c.plan_code.nullable is False
    assert Subscription.__table__.c.status.nullable is False
    assert Subscription.__table__.c.starts_at.nullable is False
    assert Subscription.__table__.c.expires_at.nullable is False
    assert Subscription.__table__.c.entitlement_snapshot.nullable is False
    assert Subscription.__table__.c.source.nullable is False
    # created_by 可空
    assert Subscription.__table__.c.created_by.nullable is True
    print(f"Subscription columns={sorted(columns)}")
    print("OK: Subscription 模型表结构验证通过")
