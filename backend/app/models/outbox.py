"""Outbox ORM 模型 - 事务性发件箱。

对应迁移 012_outbox：
- outbox: 事务性发件箱（status 状态机，relay worker 轮询投递）

字段说明：
- aggregate_type: 聚合根类型（如 strategy_run, selection_plan_run 等）
- aggregate_id: 聚合根 ID
- event_type: 事件类型
- payload: 事件负载 JSONB
- headers: 事件头 JSONB（如 trace_id, tenant_id）
- status: pending/processed/failed/deferred
- retry_count: 重试次数
- next_attempt_at: 下次可投递时间（deferred 状态使用）

At-least-once 投递：relay worker 轮询 pending 记录，投递成功后标记 processed，
失败则增加 retry_count，保证至少一次投递。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_indexes
from app.models.base import Base


class Outbox(Base):
    """事务性发件箱记录。

    与业务写入同事务，保证 DB 与 Outbox 一致性。
    Relay worker 轮询 status=pending 的记录投递到 Redis 队列。
    """

    __tablename__ = "outbox"
    __table_args__ = (
        Index("ix_outbox_status_created", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    aggregate_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="聚合根类型"
    )
    aggregate_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="聚合根 ID"
    )
    event_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="事件类型"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()), nullable=False, comment="事件负载 JSONB"
    )
    headers: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="事件头 JSONB（trace_id, tenant_id 等）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="pending",
        server_default=func.text("'pending'"),
        comment="pending/processed/failed/deferred",
    )
    retry_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, server_default="0", comment="重试次数"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下次可投递时间（deferred 状态使用）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="处理完成时间"
    )

    def __repr__(self) -> str:
        return (
            f"<Outbox(event_type={self.event_type!r}, "
            f"status={self.status!r}, retry_count={self.retry_count})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"Outbox.__tablename__={Outbox.__tablename__}")
    cols = [c.name for c in Outbox.__table__.columns]
    print(f"Outbox columns={cols}")
    assert "aggregate_type" in cols
    assert "event_type" in cols
    assert "payload" in cols
    assert "status" in cols
    assert "retry_count" in cols
    assert "next_attempt_at" in cols
    idxs = [idx.name for idx in table_indexes(Outbox)]
    print(f"Outbox indexes={idxs}")
    print("OK")
