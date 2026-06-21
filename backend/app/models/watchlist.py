"""UserWatchlistItem ORM 模型 - 用户自选股。

对应迁移 013_user_watchlist：
- user_watchlist_items: 用户自选股（id 主键，user_id+instrument_id 唯一，source 标识来源）

字段说明：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- user_id: 用户 ID（FK users.id），由认证上下文注入，不接受客户端传入
- instrument_id: 股票 ID（FK instruments.id）
- source: 加入来源（manual / selection_plan / monitor 等）
- active: 是否活跃（软删除标记，false 表示已移除）
- created_at: 加入时间
- removed_at: 移除时间（软删除，active=false 时填充）

设计要点：
- (user_id, instrument_id) 唯一约束：同一用户同一股票只能加入一次
- 软删除（active + removed_at）：保留历史，支持重新加入
- 加入自选即参与当前启用的监控方案（由 universe_service 聚合 active=true 的记录）
- user_id 由认证上下文注入（V1.1 安全约束：私有资源 user_id 不接受 body 传入）
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserWatchlistItem(Base):
    """用户自选股 - 用户与股票的多对多关系（含来源与软删除）。

    (user_id, instrument_id) 唯一约束保证同一用户同一股票只有一条活跃记录。
    active=false 表示已移除（软删除），universe 聚合时仅取 active=true。
    """

    __tablename__ = "user_watchlist_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
        comment="主键 UUID（客户端默认 uuid4，PostgreSQL 端 gen_random_uuid 兜底）",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID（由认证上下文注入）",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    source: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="加入来源（manual/selection_plan/monitor）"
    )
    active: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=True,
        server_default=func.true(),
        comment="是否活跃（软删除标记）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="加入时间",
    )
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="移除时间（软删除）"
    )

    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", name="uq_user_watchlist_user_instrument"),
        Index("ix_user_watchlist_user_active", "user_id", "active"),
    )

    def __repr__(self) -> str:
        return (
            f"<UserWatchlistItem(user_id={self.user_id!r}, "
            f"instrument_id={self.instrument_id!r}, source={self.source!r}, "
            f"active={self.active!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"UserWatchlistItem.__tablename__={UserWatchlistItem.__tablename__}")
    cols = [c.name for c in UserWatchlistItem.__table__.columns]
    print(f"columns={cols}")
    assert "id" in cols
    assert "user_id" in cols
    assert "instrument_id" in cols
    assert "source" in cols
    assert "active" in cols
    assert "created_at" in cols
    assert "removed_at" in cols
    # 验证唯一约束 (user_id, instrument_id)
    uq_constraints = [
        c for c in UserWatchlistItem.__table__.constraints
        if getattr(c, "name", None) and "user_id" in [col.name for col in c.columns]
    ]
    print(f"unique_constraints_count={len(uq_constraints)}")
    indexes = [idx.name for idx in UserWatchlistItem.__table__.indexes]
    print(f"indexes={indexes}")
    assert "ix_user_watchlist_user_active" in indexes
    print("OK")
