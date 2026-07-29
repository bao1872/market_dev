"""StockFeatureSnapshotRunItem ORM 模型 - 单股×阶段检查点。

对应迁移 073 中的 stock_feature_snapshot_run_items 表：
- 每个 (snapshot_run_id, instrument_id, phase) 组合保存一条 item
- phase=core: 核心特征快照计算
- phase=event_outbox: 事件 outbox 写入
- 支持 per-stock commit、失败隔离和断点恢复

设计原则（ref/instruction.md §三）：
- batch 只控制吞吐/内存，不是完成或发布边界
- 单股结果 commit 成功后才标记 item succeeded
- 单股失败不回滚其他已成功股票
- 重启只处理 pending、可重试 failed、lease 过期 running

模块自测：
    python -m app.models.stock_feature_snapshot_run_item
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models._table_meta import table_constraints, table_indexes
from app.models.base import Base

# 阶段枚举
PHASE_CORE = "core"
PHASE_EVENT_OUTBOX = "event_outbox"
ALL_PHASES = {PHASE_CORE, PHASE_EVENT_OUTBOX}

# 状态枚举
ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_SUCCEEDED = "succeeded"
ITEM_FAILED = "failed"
ITEM_SKIPPED = "skipped"
ALL_ITEM_STATUSES = {
    ITEM_PENDING, ITEM_RUNNING, ITEM_SUCCEEDED, ITEM_FAILED, ITEM_SKIPPED,
}
# 可 resume 的状态（重启后需要处理的）
RESUMABLE_STATUSES = {ITEM_PENDING, ITEM_FAILED}
# 已完成不需要重处理的
TERMINAL_STATUSES = {ITEM_SUCCEEDED, ITEM_SKIPPED}


class StockFeatureSnapshotRunItem(Base):
    """单股×阶段检查点 - per-stock commit 粒度。

    生命周期：
        pending → running（Worker 领取 + lease）→ succeeded/failed/skipped
        running →（lease 过期，watchdog 标记）→ pending（resume）

    唯一键：(snapshot_run_id, instrument_id, phase)
    """

    __tablename__ = "stock_feature_snapshot_run_items"

    __table_args__ = (
        UniqueConstraint(
            "snapshot_run_id", "instrument_id", "phase",
            name="uq_snapshot_run_items_run_instr_phase",
        ),
        Index(
            "ix_snapshot_run_items_run_status",
            "snapshot_run_id",
            "status",
        ),
        Index(
            "ix_snapshot_run_items_run_phase_status",
            "snapshot_run_id",
            "phase",
            "status",
        ),
        Index(
            "ix_snapshot_run_items_lease_expires",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    snapshot_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("stock_feature_snapshot_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联的 StockFeatureSnapshotRun.id（数据版本）",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="股票 ID",
    )
    phase: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="阶段：core / event_outbox",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="pending",
        comment="状态：pending/running/succeeded/failed/skipped",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0"),
        comment="尝试次数，首次 0，自动 resume 递增",
    )
    input_hash: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="输入 bars hash（用于校验重跑一致性）",
    )
    worker_instance_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Worker 实例标识 hostname:pid",
    )
    lease_epoch: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("0"),
        comment="租约代际，Worker 领取时递增，写操作校验防 fencing",
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="租约过期时间",
    )
    result_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="结果数（如 snapshot 写入数）",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="失败原因",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际开始时间",
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="心跳时间",
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
            f"<StockFeatureSnapshotRunItem("
            f"snapshot_run_id={self.snapshot_run_id!r}, "
            f"instrument_id={self.instrument_id!r}, "
            f"phase={self.phase!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    cols = StockFeatureSnapshotRunItem.__table__.columns
    expected = {
        "id", "snapshot_run_id", "instrument_id", "phase", "status",
        "attempt_count", "input_hash", "worker_instance_id", "lease_epoch",
        "lease_expires_at", "result_count", "last_error", "started_at",
        "heartbeat_at", "completed_at", "created_at", "updated_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {StockFeatureSnapshotRunItem.__tablename__} columns verified")

    constraint_names = {
        c.name for c in table_constraints(StockFeatureSnapshotRunItem)
        if hasattr(c, "name") and c.name
    }
    assert "uq_snapshot_run_items_run_instr_phase" in constraint_names, (
        f"缺少唯一约束: {constraint_names}"
    )
    print("unique constraint ✓")

    idx_names = {idx.name for idx in table_indexes(StockFeatureSnapshotRunItem) if idx.name}
    expected_idx = {
        "ix_snapshot_run_items_run_status",
        "ix_snapshot_run_items_run_phase_status",
        "ix_snapshot_run_items_lease_expires",
    }
    assert expected_idx.issubset(idx_names), f"缺少索引: {expected_idx - idx_names}"
    print("indexes ✓")
    print("OK")
