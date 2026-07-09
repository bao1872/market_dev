"""StrategyEventRecipient ORM 模型 - 事件接收人。

对应迁移 023_event_recipients：
- strategy_event_recipients: 记录每个策略事件应通知的用户

字段说明：
- id: UUID 主键（数据库 gen_random_uuid() 生成）
- event_id: 策略事件 ID（FK strategy_events.id）
- user_id: 用户 ID（FK users.id）
- watchlist_item_id: 自选股记录 ID（FK user_watchlist_items.id，可空）
- preference_snapshot: 通知偏好快照 JSONB（可空，记录事件时刻的用户偏好）
- created_at: 创建时间

设计要点：
- (event_id, user_id) 唯一约束：同一事件同一用户只接收一次
- watchlist_item_id 记录用户通过哪条自选股关联到该事件
- preference_snapshot 冻结事件时刻的用户通知偏好，便于后续审计
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base


class StrategyEventRecipient(Base):
    """事件接收人 - 记录每个策略事件应通知的用户。

    (event_id, user_id) 唯一约束保证同一事件同一用户只接收一次。
    watchlist_item_id 标识用户通过哪条自选股关联到该事件。
    """

    __tablename__ = "strategy_event_recipients"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="主键 UUID",
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_events.id"),
        nullable=False,
        comment="策略事件 ID",
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=False,
        comment="用户 ID",
    )
    watchlist_item_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user_watchlist_items.id"),
        nullable=True,
        comment="自选股记录 ID（标识关联来源）",
    )
    preference_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="通知偏好快照 JSONB（事件时刻的用户偏好）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    __table_args__ = (
        UniqueConstraint("event_id", "user_id", name="uq_event_recipients_event_user"),
        Index("ix_event_recipients_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyEventRecipient(event_id={self.event_id!r}, "
            f"user_id={self.user_id!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StrategyEventRecipient.__tablename__={StrategyEventRecipient.__tablename__}")
    cols = [c.name for c in StrategyEventRecipient.__table__.columns]
    print(f"columns={cols}")
    for required in [
        "id", "event_id", "user_id", "watchlist_item_id",
        "preference_snapshot", "created_at",
    ]:
        assert required in cols, f"缺少列: {required}"
    # 验证唯一约束
    uq_names = [c.name for c in table_constraints(StrategyEventRecipient)
                if hasattr(c, "name") and isinstance(c, UniqueConstraint)]
    assert "uq_event_recipients_event_user" in uq_names, "缺少唯一约束 uq_event_recipients_event_user"
    # 验证索引
    idx_names = [idx.name for idx in table_indexes(StrategyEventRecipient)]
    assert "ix_event_recipients_user_id" in idx_names, "缺少索引 ix_event_recipients_user_id"
    print("OK")
