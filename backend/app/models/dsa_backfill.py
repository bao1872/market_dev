"""DSA 历史回补任务 ORM 模型。

对应迁移 032_dsa_backfill：
- dsa_backfill_jobs: 父级回补任务记录
- dsa_backfill_instrument_progress: 单只股票回补进度（断点续跑）

字段说明：
- DSABackfillJob 记录整个区间回补，以股票为粒度跟踪进度。
- BackfillInstrumentProgress 记录每只股票的处理状态，支持 Worker 重启后断点恢复。
- target_trade_dates 以 date[] 数组存储目标交易日。
- 父任务进度以股票为单位；每个日期仍对应独立的 StrategyRun（run_type=backfill）。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class DSABackfillJob(Base):
    """DSA 历史回补父任务。

    记录整个区间回补的元数据、进度和状态。
    每个目标交易日对应一个独立的 StrategyRun（run_type=backfill）。
    """

    __tablename__ = "dsa_backfill_jobs"
    __table_args__ = (
        Index("ix_dsa_backfill_jobs_status", "status"),
        Index("ix_dsa_backfill_jobs_dates", "start_date", "end_date"),
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=False,
        comment="策略版本 ID",
    )
    start_date: Mapped[date] = mapped_column(Date(), nullable=False, comment="回补起始日期")
    end_date: Mapped[date] = mapped_column(Date(), nullable=False, comment="回补结束日期")
    target_trade_dates: Mapped[list[date]] = mapped_column(
        ARRAY(Date()), nullable=False, comment="目标交易日数组"
    )
    total_stocks: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="目标股票总数"
    )
    processed_stocks: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="已处理股票数"
    )
    succeeded_stocks: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="成功股票数"
    )
    failed_stocks: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="失败股票数"
    )
    selected_result_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="累计选股结果数"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="queued",
        comment="状态：queued/running/completed/partial_failed/failed/cancelled/published",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="Worker 心跳时间"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="租约过期时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    current_instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="当前正在处理的股票 ID"
    )
    error_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()), nullable=True, comment="错误汇总（按 error_code 聚合）"
    )
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, comment="请求人用户 ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<DSABackfillJob(status={self.status!r}, "
            f"processed={self.processed_stocks}/{self.total_stocks})>"
        )


class BackfillInstrumentProgress(Base):
    """DSA 历史回补单只股票进度。

    用于断点续跑：Worker 重启后跳过 SUCCEEDED，重试 FAILED 或租约过期的 RUNNING。
    """

    __tablename__ = "dsa_backfill_instrument_progress"
    __table_args__ = (
        UniqueConstraint(
            "backfill_job_id",
            "instrument_id",
            name="uq_backfill_progress_job_instrument",
        ),
        Index("ix_backfill_progress_job_status", "backfill_job_id", "status"),
        Index("ix_backfill_progress_instrument", "instrument_id"),
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    backfill_job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("dsa_backfill_jobs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属回补任务 ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        default="pending",
        comment="状态：pending/running/succeeded/failed/skipped",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="尝试次数"
    )
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="错误码")
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="错误信息")
    result_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="生成结果数"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<BackfillInstrumentProgress(job_id={self.backfill_job_id!r}, "
            f"instrument_id={self.instrument_id!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"DSABackfillJob.__tablename__={DSABackfillJob.__tablename__}")
    job_cols = [c.name for c in DSABackfillJob.__table__.columns]
    print(f"DSABackfillJob columns={job_cols}")
    assert "strategy_version_id" in job_cols
    assert "start_date" in job_cols
    assert "end_date" in job_cols
    assert "target_trade_dates" in job_cols
    assert "total_stocks" in job_cols
    assert "processed_stocks" in job_cols
    assert "succeeded_stocks" in job_cols
    assert "failed_stocks" in job_cols
    assert "selected_result_count" in job_cols
    assert "status" in job_cols
    assert "current_instrument_id" in job_cols
    assert "error_summary" in job_cols
    assert "requested_by" in job_cols

    print(f"BackfillInstrumentProgress.__tablename__={BackfillInstrumentProgress.__tablename__}")
    prog_cols = [c.name for c in BackfillInstrumentProgress.__table__.columns]
    print(f"BackfillInstrumentProgress columns={prog_cols}")
    assert "backfill_job_id" in prog_cols
    assert "instrument_id" in prog_cols
    assert "status" in prog_cols
    assert "attempt_count" in prog_cols
    assert "error_code" in prog_cols
    assert "error_message" in prog_cols
    assert "result_count" in prog_cols

    # 验证唯一约束
    prog_uqs = {
        c.name
        for c in BackfillInstrumentProgress.__table__.constraints  # type: ignore[attr-defined]
        if hasattr(c, "name") and c.name and "uq" in c.name.lower()
    }
    print(f"BackfillInstrumentProgress unique constraints={sorted(prog_uqs)}")
    assert "uq_backfill_progress_job_instrument" in prog_uqs

    print("OK")
