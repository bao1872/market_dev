"""UserCapability ORM 模型 - 三类独立权限授权表（PRD60 PA-01）。

对应迁移：
- 068_user_capabilities: 创建 user_capabilities 表 + 从现有有效订阅回填

表结构：
- user_capabilities: 用户能力授权表（per-capability 独立 expires_at）

设计要点（PRD60）：
- 三类独立 capability: self_selection / market_data / research_replay
- 每个 capability 独立授予/撤销/过期，admin 豁免所有检查
- self_selection 携带 watchlist_limit（管理员自定义，PA-02）
- expires_at 按自然月计算（PA-03），per-capability 独立
- 旧 Subscription/plan_code 保留兼容期（新读取优先、旧数据 fallback）
- source: invite_code（邀请码兑换）/ admin_grant（管理员直接授予）/ migration（回填）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# PRD60 PA-01: 三类独立权限固定值
CAPABILITY_SELF_SELECTION = "self_selection"
CAPABILITY_MARKET_DATA = "market_data"
CAPABILITY_RESEARCH_REPLAY = "research_replay"
ALL_CAPABILITIES: tuple[str, ...] = (
    CAPABILITY_SELF_SELECTION,
    CAPABILITY_MARKET_DATA,
    CAPABILITY_RESEARCH_REPLAY,
)


class UserCapability(Base):
    """用户能力授权表 - per-capability 独立授权与有效期（PRD60 PA-01/PA-03）。

    字段语义：
    - user_id: 用户 ID
    - capability: 权限类型（self_selection/market_data/research_replay）
    - watchlist_limit: 自选数量上限（仅 self_selection 使用，PA-02；其他 capability 为 NULL）
    - granted_at: 授予时间
    - expires_at: 过期时间（per-capability 独立自然月，PA-03）
    - source: 来源（invite_code/admin_grant/migration）
    - granted_by: 授予人 user_id（admin_grant 时记录，invite_code/migration 为 NULL）
    - created_at: 记录创建时间

    约束：
    - (user_id, capability) 唯一：一个用户每个 capability 只有一条记录
    - admin 用户豁免所有 capability 检查（由 require_capability 实现）
    """

    __tablename__ = "user_capabilities"
    __table_args__ = (
        UniqueConstraint("user_id", "capability", name="uq_user_capabilities_user_capability"),
    )

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
    capability: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        comment="权限类型 self_selection/market_data/research_replay",
    )
    watchlist_limit: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="自选数量上限（仅 self_selection 使用，PA-02）",
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="授予时间",
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="过期时间（per-capability 独立自然月，PA-03）",
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="invite_code",
        server_default="'invite_code'",
        comment="来源 invite_code/admin_grant/migration",
    )
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
        comment="授予人 user_id（admin_grant 时记录）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<UserCapability(user_id={self.user_id!r}, capability={self.capability!r}, "
            f"expires_at={self.expires_at!r}, source={self.source!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    assert UserCapability.__tablename__ == "user_capabilities"
    cols = {c.name for c in UserCapability.__table__.columns}
    expected = {
        "id", "user_id", "capability", "watchlist_limit",
        "granted_at", "expires_at", "source", "granted_by", "created_at",
    }
    assert cols == expected, f"UserCapability 列不匹配: {cols ^ expected}"
    # 唯一约束
    uqs = [c.name for c in UserCapability.__table__.constraints if c.name == "uq_user_capabilities_user_capability"]
    assert len(uqs) == 1, "缺少 (user_id, capability) 唯一约束"
    print(f"UserCapability columns={sorted(cols)}")
    print("OK: UserCapability 模型表结构验证通过")
