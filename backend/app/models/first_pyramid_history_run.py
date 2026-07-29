"""FirstPyramidHistoryRun ORM 模型 - 历史回补 run 级追踪。

对应迁移 073 中的 first_pyramid_history_runs 表：
- 每次历史回补创建一条 run 记录，记录算法版本、参数 hash、范围和进度
- status: running/partial/succeeded/failed
- scheduler_job_run_id 仅用于任务追踪（metadata），不代表数据版本

模块自测：
    python -m app.models.first_pyramid_history_run
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_indexes
from app.models.base import Base

# 状态枚举
HISTORY_RUN_RUNNING = "running"
HISTORY_RUN_PARTIAL = "partial"
HISTORY_RUN_SUCCEEDED = "succeeded"
HISTORY_RUN_FAILED = "failed"
ALL_HISTORY_RUN_STATUSES = {
    HISTORY_RUN_RUNNING, HISTORY_RUN_PARTIAL, HISTORY_RUN_SUCCEEDED, HISTORY_RUN_FAILED,
}

# 范围枚举
SCOPE_ALL_A_SHARE = "all_a_share"
SCOPE_CANARY = "canary"
SCOPE_SYMBOLS = "symbols"


class FirstPyramidHistoryRun(Base):
    """历史回补 run - 单次历史回补的生命周期与进度。

    状态流转：
        running → succeeded（全部成功）
        running → partial（部分成功，部分失败/跳过）
        running → failed（全部失败或无法继续）
    """

    __tablename__ = "first_pyramid_history_runs"

    __table_args__ = (
        Index("ix_first_pyramid_history_runs_algo_ver", "algorithm_version"),
        Index("ix_first_pyramid_history_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    scheduler_job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scheduler_job_runs.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联的 SchedulerJobRun.id（任务追踪，纯 metadata）",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="core 算法版本（FIRST_PYRAMID_CORE_ALGORITHM_VERSION）",
    )
    parameter_hash: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="参数 hash（output_bars + include_chip 等）",
    )
    output_bars: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=250,
        comment="输出最近 N 个有效日",
    )
    scope: Mapped[str] = mapped_column(
        Text(), nullable=False, default="all_a_share",
        comment="范围：all_a_share / canary / symbols",
    )
    expected_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="预期 instrument 数量",
    )
    succeeded_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="成功 instrument 数量",
    )
    failed_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="失败 instrument 数量",
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="跳过 instrument 数量",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, default="running",
        comment="状态：running/partial/succeeded/failed",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间",
    )
    metadata_json: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="额外元数据 JSON",
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
            f"<FirstPyramidHistoryRun("
            f"status={self.status!r}, scope={self.scope!r}, "
            f"algorithm_version={self.algorithm_version!r})>"
        )


if __name__ == "__main__":
    cols = FirstPyramidHistoryRun.__table__.columns
    expected = {
        "id", "scheduler_job_run_id", "algorithm_version", "parameter_hash",
        "output_bars", "scope", "expected_count", "succeeded_count",
        "failed_count", "skipped_count", "status", "started_at",
        "completed_at", "metadata_json", "created_at", "updated_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {FirstPyramidHistoryRun.__tablename__} columns verified")

    idx_names = {idx.name for idx in table_indexes(FirstPyramidHistoryRun) if idx.name}
    assert "ix_first_pyramid_history_runs_algo_ver" in idx_names
    assert "ix_first_pyramid_history_runs_status" in idx_names
    print("indexes ✓")
    print("OK")
