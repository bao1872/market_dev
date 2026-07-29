"""FirstPyramidHistoryRunItem ORM 模型 - 历史回补单股 item。

对应迁移 073 中的 first_pyramid_history_run_items 表：
- 每个 (history_run_id, instrument_id) 组合保存一条 item
- 支持 per-stock commit、失败隔离和断点恢复
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FirstPyramidHistoryRunItem(Base):
    """历史回补单股 item - per-stock commit 粒度。

    生命周期：
        pending → running → succeeded/failed/skipped
    """

    __tablename__ = "first_pyramid_history_run_items"

    __table_args__ = (
        UniqueConstraint(
            "history_run_id", "instrument_id",
            name="uq_history_run_items_run_instr",
        ),
        Index("ix_history_run_items_run_status", "history_run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    history_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("first_pyramid_history_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联的 FirstPyramidHistoryRun.id",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False,
        comment="股票 ID",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="pending",
        comment="状态：pending/running/succeeded/failed/skipped",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0"),
        comment="尝试次数",
    )
    input_hash: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="输入 bars hash",
    )
    daily_state_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="写入的 daily_state 行数",
    )
    event_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="写入的 event 行数",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="失败原因",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<FirstPyramidHistoryRunItem("
            f"history_run_id={self.history_run_id!r}, "
            f"instrument_id={self.instrument_id!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    cols = FirstPyramidHistoryRunItem.__table__.columns
    expected = {
        "id", "history_run_id", "instrument_id", "status", "attempt_count",
        "input_hash", "daily_state_count", "event_count", "last_error",
        "completed_at", "created_at", "updated_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {FirstPyramidHistoryRunItem.__tablename__} columns verified")
    print("OK")
