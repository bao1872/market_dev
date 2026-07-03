"""邀请码 ORM 模型 - V1.6 邀请码系统 + plans 表套餐权限。

对应迁移：
- 014_membership: 基础表结构（invite_codes/invite_redemptions，memberships 已由 049 删除）
- 044_plan_contract_fields: 套餐字段（plan_code/monitor_limit/grant_months）
- 048_plans_table: 套餐定义表（plans，套餐契约唯一真源）
- 049_subscriptions_table: subscriptions 表取代 memberships 表（Membership 模型已删除）

表结构：
- invite_codes: 邀请码表（code_hash 唯一，status unused/used/revoked，
  plan_code/monitor_limit 快照，grant_months 自然月，grant_days 兼容旧逻辑）
- invite_redemptions: 邀请码兑换记录（invite_code_id + user_id，记录 old/new expires_at）

设计要点：
- 邀请码存储 SHA256 哈希（code_hash），明文仅在生成时返回一次
- 邀请码为一次性兑换码，status 状态机：unused → used / revoked
- 兑换记录保留 old_expires_at 和 new_expires_at，支持审计追踪
- 管理员停用账户（users.status=disabled）与订阅到期（subscriptions.status=expired）是两个独立状态
- plan_code/monitor_limit 为套餐快照，从 plans 表查询（app.services.plan_service.get_plan）
- grant_months 优先用于自然月计算（dateutil.relativedelta），grant_days 保留兼容性
- Phase 2 Task 2.2：Membership 模型已删除，订阅数据迁移到 subscriptions 表 + Subscription 模型
  （见 app/models/subscription.py）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class InviteCode(Base):
    """邀请码表 - 一次性兑换码，可用于注册或续期。

    status 状态机：
    - unused: 未使用
    - used: 已使用（used_by/used_at/usage_type 已填充）
    - revoked: 已作废（管理员手动作废，不可再使用）

    邀请码明文不存储，仅存储 SHA256 哈希（code_hash）。
    生成时返回明文，后续无法再次获取。

    套餐字段（044_plan_contract_fields 迁移新增）：
    - plan_code: 套餐代码（observe_20/research_50），生成时从 plans 表选定
    - monitor_limit: 监控上限快照（从 plans 表读取，写入邀请码作为不可变快照）
    - grant_months: 兑换后增加的自然月数（替代 grant_days 的 30 天近似）
    - grant_days: 保留兼容性（旧邀请码=30，新代码优先使用 grant_months）
    """

    __tablename__ = "invite_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    code_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="邀请码 SHA256 哈希（明文不存储）"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="unused",
        comment="unused/used/revoked",
    )
    grant_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, comment="兑换后增加的天数（旧字段，保留兼容性）"
    )
    plan_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="套餐代码 observe_20/research_50（生成时从 plans 表选定）",
    )
    monitor_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="监控数量上限快照（从 plans 表读取，写入后不可变）",
    )
    grant_months: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="兑换后增加的自然月数（优先于 grant_days，用 relativedelta 计算）",
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="批次备注"
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="创建者（管理员 user_id）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="使用者 user_id（未使用时为 NULL）",
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="使用时间（未使用时为 NULL）"
    )
    usage_type: Mapped[str | None] = mapped_column(
        String(16), nullable=True, comment="兑换用途：registration/renewal（未使用时为 NULL）"
    )

    def __repr__(self) -> str:
        return (
            f"<InviteCode(id={self.id!r}, status={self.status!r}, "
            f"plan_code={self.plan_code!r}, grant_months={self.grant_months!r}, "
            f"grant_days={self.grant_days!r})>"
        )


class InviteRedemption(Base):
    """邀请码兑换记录 - 每次兑换生成一条记录，保留 old/new expires_at 用于审计。

    记录邀请码被兑换的完整上下文：
    - 哪个邀请码（invite_code_id）
    - 谁兑换的（user_id）
    - 兑换用途（usage_type: registration/renewal）
    - 兑换前的到期时间（old_expires_at，注册时为 NULL）
    - 兑换后的到期时间（new_expires_at）
    - 兑换时间（redeemed_at）
    """

    __tablename__ = "invite_redemptions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    invite_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invite_codes.id", ondelete="CASCADE"),
        nullable=False,
        comment="邀请码 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="兑换者 user_id",
    )
    usage_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="registration/renewal"
    )
    old_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="兑换前到期时间（注册时为 NULL）"
    )
    new_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="兑换后到期时间"
    )
    redeemed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<InviteRedemption(invite_code_id={self.invite_code_id!r}, "
            f"user_id={self.user_id!r}, usage_type={self.usage_type!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (InviteCode, InviteRedemption):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")
    # 验证关键约束
    assert InviteCode.__table__.c.code_hash.unique is True
    assert InviteCode.__table__.c.grant_days.default.arg == 30
    # 验证 plans 表套餐字段已添加
    assert "plan_code" in [c.name for c in InviteCode.__table__.columns]
    assert "monitor_limit" in [c.name for c in InviteCode.__table__.columns]
    assert "grant_months" in [c.name for c in InviteCode.__table__.columns]
    print("OK")
