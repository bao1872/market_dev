"""会员与邀请码 ORM 模型 - V1.6 会员系统 + plan_contract 套餐权限。

对应迁移：
- 014_membership: 基础表结构（memberships/invite_codes/invite_redemptions）
- 044_plan_contract_fields: 套餐字段（plan_code/monitor_limit/grant_months）

表结构：
- memberships: 会员状态表（user_id 唯一，status active/expired，started_at/expires_at，
  plan_code/monitor_limit 记录当前套餐与监控上限）
- invite_codes: 邀请码表（code_hash 唯一，status unused/used/revoked，
  plan_code/monitor_limit 快照，grant_months 自然月，grant_days 兼容旧逻辑）
- invite_redemptions: 邀请码兑换记录（invite_code_id + user_id，记录 old/new expires_at）

设计要点：
- 一个用户只有一条 membership 记录（user_id 唯一约束）
- 邀请码存储 SHA256 哈希（code_hash），明文仅在生成时返回一次
- 邀请码为一次性兑换码，status 状态机：unused → used / revoked
- 兑换记录保留 old_expires_at 和 new_expires_at，支持审计追踪
- 管理员停用账户（users.status=disabled）与会员到期（memberships.status=expired）是两个独立状态
- plan_code/monitor_limit 为套餐快照，从 app.constants.plan_contract.PLAN_CONTRACTS 读取
- grant_months 优先用于自然月计算（dateutil.relativedelta），grant_days 保留兼容性
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Membership(Base):
    """会员状态表 - 一个用户一条记录，记录会员有效期与当前套餐。

    status 状态机：
    - active: 会员有效（expires_at > 当前时间）
    - expired: 会员已到期（expires_at <= 当前时间）

    套餐字段（044_plan_contract_fields 迁移新增）：
    - plan_code: 当前套餐代码（observe_20/research_50），续期时随邀请码更新
    - monitor_limit: 当前套餐监控数量上限，watchlist POST 时校验

    注意：status 由业务逻辑维护，不依赖数据库触发器。
    续期时更新 expires_at 并将 status 重置为 active，同时更新 plan_code/monitor_limit。
    """

    __tablename__ = "memberships"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=func.gen_random_uuid()
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        comment="用户 ID（唯一，一个用户一条会员记录）",
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="active",
        comment="active/expired",
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="会员开始时间"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="会员到期时间"
    )
    plan_code: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="当前套餐代码 observe_20/research_50",
    )
    monitor_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="当前套餐监控数量上限",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Membership(user_id={self.user_id!r}, status={self.status!r}, "
            f"plan_code={self.plan_code!r}, monitor_limit={self.monitor_limit!r}, "
            f"expires_at={self.expires_at!r})>"
        )


class InviteCode(Base):
    """邀请码表 - 一次性兑换码，可用于注册或续期。

    status 状态机：
    - unused: 未使用
    - used: 已使用（used_by/used_at/usage_type 已填充）
    - revoked: 已作废（管理员手动作废，不可再使用）

    邀请码明文不存储，仅存储 SHA256 哈希（code_hash）。
    生成时返回明文，后续无法再次获取。

    套餐字段（044_plan_contract_fields 迁移新增）：
    - plan_code: 套餐代码（observe_20/research_50），生成时从 PLAN_CONTRACTS 选定
    - monitor_limit: 监控上限快照（从 PLAN_CONTRACTS 读取，写入邀请码作为不可变快照）
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
        comment="套餐代码 observe_20/research_50（生成时从 PLAN_CONTRACTS 选定）",
    )
    monitor_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="监控数量上限快照（从 PLAN_CONTRACTS 读取，写入后不可变）",
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
    for cls in (Membership, InviteCode, InviteRedemption):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")
    # 验证关键约束
    assert Membership.__table__.c.user_id.unique is True
    assert InviteCode.__table__.c.code_hash.unique is True
    assert InviteCode.__table__.c.grant_days.default.arg == 30
    # 验证 plan_contract 套餐字段已添加
    assert "plan_code" in [c.name for c in Membership.__table__.columns]
    assert "monitor_limit" in [c.name for c in Membership.__table__.columns]
    assert "plan_code" in [c.name for c in InviteCode.__table__.columns]
    assert "monitor_limit" in [c.name for c in InviteCode.__table__.columns]
    assert "grant_months" in [c.name for c in InviteCode.__table__.columns]
    print("OK")
