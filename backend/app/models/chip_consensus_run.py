"""ChipConsensusRun ORM 模型 - 筹码共识领域 run。

对应迁移 084 中的 chip_consensus_runs / chip_consensus_run_items 表。

[PRD V2.1 §8 / next.md EPIC-05]
- chip 是独立、非破坏的异步增强，不阻断 stock_core。
- ChipConsensusRun 与 SchedulerJobRun 分离，作为独立领域 run。
- 在 stock_core commit 后立即 create/reuse，不等待 board/Review。
- 每股逐项 run item：ready/failed/skipped + reason + attempt。

模块自测：
    python -m app.models.chip_consensus_run
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChipConsensusRun(Base):
    """筹码共识领域 run。

    状态机：
        queued → running → succeeded / partial / failed / skipped
        任意 → interrupted / cancelled
    """

    __tablename__ = "chip_consensus_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    scheduler_job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="关联 SchedulerJobRun.id（父任务，可为空）",
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False, comment="业务交易日")
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="关联 stock_core run（StockFeatureSnapshotRun.id）",
    )
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    config_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="queued",
        comment="queued/running/succeeded/partial/failed/skipped/interrupted/cancelled",
    )
    readiness: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment="ready / unavailable / pending / degraded",
    )
    reuse_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chip_consensus_runs.id"), nullable=True,
        comment="复用旧 run 时指向被复用的 run",
    )
    expected_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    diagnostics_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer(), nullable=False, server_default="0")
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_chip_consensus_runs_trade_date", "trade_date"),
        Index("ix_chip_consensus_runs_core_run", "source_core_run_id"),
        Index("ix_chip_consensus_runs_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ChipConsensusRun(trade_date={self.trade_date!r}, "
            f"core_run={self.source_core_run_id!r}, status={self.status!r}, "
            f"coverage={self.coverage_ratio:.3f})>"
        )


class ChipConsensusRunItem(Base):
    """每股 chip 计算项。

    有界并发 + heartbeat + lease + retry + successful skip + stale reconcile。
    """

    __tablename__ = "chip_consensus_run_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chip_consensus_runs.id", ondelete="CASCADE"), nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="queued",
        comment="queued/running/ready/failed/skipped",
    )
    reason_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, server_default="0")
    chip_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    chip_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="关联 stock_chip_consensus_snapshots.id",
    )
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "instrument_id",
            name="uq_chip_consensus_run_items_run_instrument",
        ),
        Index("ix_chip_consensus_run_items_status", "run_id", "status"),
    )


if __name__ == "__main__":
    cols = {c.name for c in ChipConsensusRun.__table__.columns}
    required = {
        "id", "trade_date", "source_core_run_id", "algorithm_version", "status",
        "expected_count", "succeeded_count", "failed_count", "skipped_count", "coverage_ratio",
    }
    assert required.issubset(cols), f"缺少字段: {required - cols}"
    print(f"OK: {ChipConsensusRun.__tablename__} columns verified")
    item_cols = {c.name for c in ChipConsensusRunItem.__table__.columns}
    required_items = {"id", "run_id", "trade_date", "instrument_id", "status"}
    assert required_items.issubset(item_cols), f"缺少字段: {required_items - item_cols}"
    print(f"OK: {ChipConsensusRunItem.__tablename__} columns verified")
