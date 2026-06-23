"""StockMemo ORM 模型 - 个股备忘录。

对应迁移 019_stock_memo：
- stock_memos: 用户对个股的备忘录（每用户每股票一条）

字段说明：
- id: UUID 主键（数据库生成 gen_random_uuid()）
- user_id: 用户 ID（FK users.id），由认证上下文注入
- instrument_id: 股票 ID（FK instruments.id）
- content: 备忘录文本内容
- notify_feishu: 是否在盘中监控时推送飞书
- created_at: 创建时间
- updated_at: 更新时间

设计要点：
- (user_id, instrument_id) 唯一约束：同一用户同一股票只有一条备忘录
- notify_feishu 默认 false
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockMemo(Base):
    """个股备忘录 - 用户对单只股票的备忘记录（含飞书推送开关）。

    (user_id, instrument_id) 唯一约束保证同一用户同一股票只有一条备忘录。
    """

    __tablename__ = "stock_memos"

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
    content: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="备忘录文本内容",
    )
    notify_feishu: Mapped[bool] = mapped_column(
        Boolean(),
        nullable=False,
        default=False,
        server_default=func.false(),
        comment="是否在盘中监控时推送飞书",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
        comment="更新时间",
    )

    __table_args__ = (
        UniqueConstraint("user_id", "instrument_id", name="uq_stock_memo_user_instrument"),
        Index("ix_stock_memo_user_notify", "user_id", "notify_feishu"),
    )

    def __repr__(self) -> str:
        return (
            f"<StockMemo(user_id={self.user_id!r}, "
            f"instrument_id={self.instrument_id!r}, "
            f"notify_feishu={self.notify_feishu!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StockMemo.__tablename__={StockMemo.__tablename__}")
    cols = [c.name for c in StockMemo.__table__.columns]
    print(f"columns={cols}")
    assert "id" in cols
    assert "user_id" in cols
    assert "instrument_id" in cols
    assert "content" in cols
    assert "notify_feishu" in cols
    assert "created_at" in cols
    assert "updated_at" in cols
    # 验证唯一约束 (user_id, instrument_id)
    uq_constraints = [
        c for c in StockMemo.__table__.constraints
        if getattr(c, "name", None) and "user_id" in [col.name for col in c.columns]
    ]
    print(f"unique_constraints_count={len(uq_constraints)}")
    indexes = [idx.name for idx in StockMemo.__table__.indexes]
    print(f"indexes={indexes}")
    assert "ix_stock_memo_user_notify" in indexes
    print("OK")
