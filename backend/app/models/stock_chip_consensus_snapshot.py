"""StockChipConsensusSnapshot ORM 模型 - 筹码共识独立快照持久化。

对应迁移 071_chip_consensus_snapshots 中的 stock_chip_consensus_snapshots 表：
- 每个 (instrument_id, trade_date, core_run_id, algorithm_version) 组合保存一份
  point-in-time 筹码共识快照。
- chip_payload 保存 ChipConsensusResult.to_dict() 完整输出。
- core_run_id 关联 StockFeatureSnapshotRun.id（核心数据版本，不指向 SchedulerJobRun.id）。
- chip_hash 独立于 core inputHash（daily + 15m bars 的 hash）。

设计说明（[CHANGE-20260729-003] 核心与筹码解耦 - P0-10）：
- 主 run 成功后由独立 after_close_chip_consensus job 异步写入
- chip 失败/部分成功不反改主 run 状态（写 metadata.chip_status=partial）
- 失败重试只覆盖失败 instrument，不重算成功项

[CHANGE-20260729-007 ID 合同修复 2026-07-29]：
- core_run_id FK 从 scheduler_job_runs.id 修正为 stock_feature_snapshot_runs.id
- 与 after_close_orchestrator 传入的 snapshot_run_id 一致

模块自测：
    python -m app.models.stock_chip_consensus_snapshot
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Date, DateTime, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StockChipConsensusSnapshot(Base):
    """筹码共识独立快照 - 每只股票每个交易日的 point-in-time 筹码共识固化。

    [CHANGE-20260729-003] 核心与筹码解耦：chip 异步持久化，不阻塞主 run。
    """

    __tablename__ = "stock_chip_consensus_snapshots"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="chip 快照 ID",
    )
    instrument_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(),
        nullable=False,
        comment="业务交易日",
    )
    core_run_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("stock_feature_snapshot_runs.id"),
        nullable=False,
        comment="关联 StockFeatureSnapshotRun.id（核心数据版本，不指向 SchedulerJobRun.id）",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="chip 算法版本（CHIP_CONSENSUS_ALGORITHM_VERSION）",
    )
    chip_hash: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="chip 输入 hash（daily + 15m bars）",
    )
    chip_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        comment="ChipConsensusResult.to_dict() 完整输出 JSONB",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default="succeeded",
        comment="单股 chip 状态：succeeded/failed/skipped",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="失败原因（status=failed 时写入）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trade_date",
            "core_run_id",
            "algorithm_version",
            name="uq_chip_consensus_instrument_date_run_version",
        ),
        Index(
            "ix_chip_consensus_trade_date",
            "trade_date",
        ),
        Index(
            "ix_chip_consensus_core_run_id",
            "core_run_id",
        ),
        Index(
            "ix_chip_consensus_instrument_date",
            "instrument_id",
            "trade_date",
            postgresql_using="btree",
            postgresql_ops={"trade_date": "desc"},
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<StockChipConsensusSnapshot("
            f"instrument_id={self.instrument_id!r}, "
            f"trade_date={self.trade_date!r}, "
            f"core_run_id={self.core_run_id!r}, "
            f"status={self.status!r})>"
        )


if __name__ == "__main__":
    cols = StockChipConsensusSnapshot.__table__.columns
    expected = {
        "id", "instrument_id", "trade_date", "core_run_id",
        "algorithm_version", "chip_hash", "chip_payload",
        "status", "error_message", "created_at", "updated_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {StockChipConsensusSnapshot.__tablename__} columns verified")
    print(f"columns: {sorted(actual)}")
