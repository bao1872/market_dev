"""能力授权 ORM 模型 - V2.1 邀请码模块化授权。

对应迁移：
- 068_invite_capability_grants: 创建 invite_code_capabilities 和 user_capability_grants 表

表结构：
- invite_code_capabilities: 邀请码能力配置（每个邀请码可关联多个能力键）
- user_capability_grants: 用户能力授权（每项能力独立 grant，支持多次兑换续期）

设计要点（PRD §8）：
- 能力键 CheckConstraint 限定三个固定值
- 自选能力 limit_value > 0；非自选能力 limit_value IS NULL
- expires_at > starts_at
- UNIQUE(source_type, source_id, capability_key) 防止同来源重复 grant
- 有效状态实时推导：revoked_at IS NULL AND starts_at <= now AND expires_at > now
- 不依赖每日任务更新状态
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.capability_keys import ALL_CAPABILITY_KEYS
from app.models.base import Base


class InviteCodeCapability(Base):
    """邀请码能力配置 - 每个邀请码可关联多个能力键。

    PRD §8.2：
    - capability_key: 三个固定能力键之一
    - limit_value: 仅自选能力使用（正整数），非自选能力为 NULL
    - UNIQUE(invite_code_id, capability_key): 同一邀请码同一能力不重复
    """

    __tablename__ = "invite_code_capabilities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    invite_code_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invite_codes.id", ondelete="CASCADE"),
        nullable=False,
        comment="邀请码 ID",
    )
    capability_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="能力键 watchlist_management/market_screening/review_management",
    )
    limit_value: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="自选额度（仅 watchlist_management 使用，正整数；其他能力为 NULL）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("invite_code_id", "capability_key", name="uq_invite_code_capability"),
        CheckConstraint(
            f"capability_key IN {tuple(ALL_CAPABILITY_KEYS)}",
            name="ck_invite_code_capability_key",
        ),
        CheckConstraint(
            "(capability_key = 'watchlist_management' AND limit_value IS NOT NULL AND limit_value > 0) "
            "OR (capability_key != 'watchlist_management' AND limit_value IS NULL)",
            name="ck_invite_code_capability_limit",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<InviteCodeCapability(invite_code_id={self.invite_code_id!r}, "
            f"capability_key={self.capability_key!r}, limit_value={self.limit_value!r})>"
        )


class UserCapabilityGrant(Base):
    """用户能力授权 - 每项能力独立 grant，支持多次兑换续期。

    PRD §8.3：
    - source_type: invite_code / legacy_subscription / legacy_invite
    - source_id: 来源记录 ID（邀请码 ID 或旧订阅 ID）
    - starts_at/expires_at: 有效区间 [starts_at, expires_at)
    - revoked_at: 撤销时间（NULL 表示未撤销）
    - 有效状态实时推导：revoked_at IS NULL AND starts_at <= now AND expires_at > now
    - UNIQUE(source_type, source_id, capability_key): 同来源同能力不重复
    """

    __tablename__ = "user_capability_grants"

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
        comment="用户 ID",
    )
    capability_key: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="能力键 watchlist_management/market_screening/review_management",
    )
    limit_value: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="自选额度（仅 watchlist_management 使用，正整数；其他能力为 NULL）",
    )
    source_type: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="来源 invite_code/legacy_subscription/legacy_invite",
    )
    source_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="来源记录 ID（邀请码 ID 或旧订阅 ID 的字符串形式）",
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="生效时间"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="到期时间（exclusive）"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="撤销时间（NULL=未撤销）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="创建人（管理员授予时记录，邀请码兑换时为 NULL）",
    )

    __table_args__ = (
        UniqueConstraint("source_type", "source_id", "capability_key", name="uq_grant_source_capability"),
        CheckConstraint(
            f"capability_key IN {tuple(ALL_CAPABILITY_KEYS)}",
            name="ck_grant_capability_key",
        ),
        CheckConstraint(
            "expires_at > starts_at",
            name="ck_grant_expires_after_starts",
        ),
        CheckConstraint(
            "(capability_key = 'watchlist_management' AND limit_value IS NOT NULL AND limit_value > 0) "
            "OR (capability_key != 'watchlist_management' AND limit_value IS NULL)",
            name="ck_grant_limit_value",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<UserCapabilityGrant(user_id={self.user_id!r}, "
            f"capability_key={self.capability_key!r}, source_type={self.source_type!r}, "
            f"expires_at={self.expires_at!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    for cls in (InviteCodeCapability, UserCapabilityGrant):
        cols = [c.name for c in cls.__table__.columns]
        print(f"{cls.__name__} table={cls.__tablename__} columns={cols}")
    # 验证约束
    icc_constraints = [c.name for c in InviteCodeCapability.__table__.constraints]
    assert "uq_invite_code_capability" in icc_constraints
    assert "ck_invite_code_capability_key" in icc_constraints
    assert "ck_invite_code_capability_limit" in icc_constraints
    ucg_constraints = [c.name for c in UserCapabilityGrant.__table__.constraints]
    assert "uq_grant_source_capability" in ucg_constraints
    assert "ck_grant_capability_key" in ucg_constraints
    assert "ck_grant_expires_after_starts" in ucg_constraints
    assert "ck_grant_limit_value" in ucg_constraints
    print("OK")
