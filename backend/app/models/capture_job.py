"""CaptureJob 模型 - 持久化截图任务，支持失败重试。

advice.md: 监控事务先提交事件和 card Outbox，再异步截图；
截图失败建失败任务 + 自动重试 + 达上限标记 dead。

表字段：event_id/instrument_id/user_id/message_group_id/status/attempt_count/
        image_url/error_code/error_message/created_at/finished_at
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# 截图任务状态枚举
CAPTURE_STATUS_PENDING = "pending"
CAPTURE_STATUS_RUNNING = "running"
CAPTURE_STATUS_SUCCEEDED = "succeeded"
CAPTURE_STATUS_FAILED = "failed"
CAPTURE_STATUS_DEAD = "dead"

# 最大重试次数（advice.md: 达上限后标记 dead）
CAPTURE_MAX_ATTEMPTS = 3


class CaptureJob(Base):
    """持久化截图任务，记录截图请求 + 状态 + 重试次数。"""

    __tablename__ = "capture_jobs"
    __table_args__ = (
        Index("ix_capture_jobs_status_created", "status", "created_at"),
        Index("ix_capture_jobs_message_group_id", "message_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="触发的监控事件 ID"
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="标的 ID"
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="用户 ID（生成 capture token 用）"
    )
    message_group_id: Mapped[str] = mapped_column(
        Text, nullable=False, comment="消息组 ID（关联 card/image Outbox）"
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=CAPTURE_STATUS_PENDING,
        comment="pending/running/succeeded/failed/dead",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="尝试次数"
    )
    image_url: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="截图成功后的图片 URL"
    )
    error_code: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败错误码"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败错误信息"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="成功/失败/dead 时间"
    )

    def __repr__(self) -> str:
        return (
            f"<CaptureJob(id={self.id!s}, status={self.status!r}, "
            f"attempt_count={self.attempt_count})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"CaptureJob.__tablename__={CaptureJob.__tablename__}")
    cols = {c.name for c in CaptureJob.__table__.columns}
    expected = {
        "id", "event_id", "instrument_id", "user_id", "message_group_id",
        "status", "attempt_count", "image_url", "error_code", "error_message",
        "created_at", "finished_at",
    }
    assert expected.issubset(cols), f"缺失字段: {expected - cols}"
    print(f"columns OK: {sorted(cols)}")
