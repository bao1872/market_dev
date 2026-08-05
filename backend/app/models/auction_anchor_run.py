"""AuctionAnchorRun ORM 模型 - 竞价锚点领域 run。

对应迁移 084 中的 auction_anchor_runs 表。

[PRD V2.1 §8.3 / next.md EPIC-05]
- auction 支持 structure_only / hybrid / composite 三种模式。
- AuctionAnchorRun 是竞价锚点的领域 run，与 AuctionAnchorSnapshot 并存（后者为快照）。
- structure_only：stock_core 发布后即可发布。
- hybrid：部分 chip 可用时，每股 mode + 批次 hybrid + coverage，无伪 composite。
- composite：只有全部可发布 anchor 为 composite 且无 failed/stale 才成立。
- 晚到升级：chip 完成 → 新 run + atomic pointer，不改旧 publication。

模块自测：
    python -m app.models.auction_anchor_run
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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuctionAnchorRun(Base):
    """竞价锚点领域 run。

    状态机：
        queued → running → succeeded / partial / failed / skipped
        任意 → interrupted / cancelled
    """

    __tablename__ = "auction_anchor_runs"

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
        comment="关联 stock_core run",
    )
    source_chip_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chip_consensus_runs.id"), nullable=True,
        comment="关联 chip run（hybrid/composite 时使用）",
    )
    mode: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="structure_only / hybrid / composite",
    )
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    config_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="queued",
        comment="queued/running/succeeded/partial/failed/skipped/interrupted/cancelled",
    )
    readiness: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment="ready / unavailable / pending / degraded",
    )
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    structure_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    chip_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    composite_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    diagnostics_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    superseded_by_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auction_anchor_runs.id"), nullable=True,
        comment="晚到升级：被新 run 取代时指向新 run",
    )
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
        Index("ix_auction_anchor_runs_trade_date", "trade_date"),
        Index("ix_auction_anchor_runs_core_run", "source_core_run_id"),
        Index("ix_auction_anchor_runs_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<AuctionAnchorRun(trade_date={self.trade_date!r}, mode={self.mode!r}, "
            f"status={self.status!r}, coverage={self.coverage_ratio:.3f})>"
        )


class AuctionAnchorRunItem(Base):
    """每股竞价锚点项。

    记录每股 mode（structure / chip / composite）与来源 run。
    """

    __tablename__ = "auction_anchor_run_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("auction_anchor_runs.id", ondelete="CASCADE"), nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mode: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="structure / chip / composite",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="queued",
        comment="queued/running/ready/failed/skipped",
    )
    source_chip_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    anchor_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="关联 auction_anchor_snapshots.id",
    )
    reason_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


if __name__ == "__main__":
    cols = {c.name for c in AuctionAnchorRun.__table__.columns}
    required = {
        "id", "trade_date", "source_core_run_id", "mode", "algorithm_version", "status",
        "coverage_ratio", "structure_anchor_count", "chip_anchor_count", "composite_anchor_count",
    }
    assert required.issubset(cols), f"缺少字段: {required - cols}"
    print(f"OK: {AuctionAnchorRun.__tablename__} columns verified")
    item_cols = {c.name for c in AuctionAnchorRunItem.__table__.columns}
    required_items = {"id", "run_id", "trade_date", "instrument_id", "mode", "status"}
    assert required_items.issubset(item_cols), f"缺少字段: {required_items - item_cols}"
    print(f"OK: {AuctionAnchorRunItem.__tablename__} columns verified")
