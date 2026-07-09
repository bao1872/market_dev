"""JobRun ORM 模型 - 任务运行记录。

对应迁移 011_job_runs：
- job_runs: Job 运行记录（job_type 区分任务类型，status 状态机）

字段说明：
- job_type: 任务类型（如 strategy_run, selection_plan_run, data_sync 等）
- status: pending/running/succeeded/failed/cancelled
- payload: 输入参数 JSONB
- result: 输出结果 JSONB
- error: 失败原因
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_indexes
from app.models.base import Base


class JobRun(Base):
    """任务运行记录。

    状态机：pending -> running -> succeeded/failed/cancelled
    """

    __tablename__ = "job_runs"
    __table_args__ = (
        Index("ix_job_runs_type_status", "job_type", "status"),
        Index("ix_job_runs_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    job_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="任务类型"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="pending",
        comment="pending/running/succeeded/failed/cancelled",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="输入参数 JSONB",
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="输出结果 JSONB"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始执行时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    error: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="失败原因"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<JobRun(job_type={self.job_type!r}, "
            f"status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"JobRun.__tablename__={JobRun.__tablename__}")
    cols = [c.name for c in JobRun.__table__.columns]
    print(f"JobRun columns={cols}")
    assert "job_type" in cols
    assert "status" in cols
    assert "payload" in cols
    assert "result" in cols
    assert "error" in cols
    idxs = [idx.name for idx in table_indexes(JobRun)]
    print(f"JobRun indexes={idxs}")
    print("OK")
