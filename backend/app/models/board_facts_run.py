"""BoardFactsRun ORM 模型 - 行业/概念事实（pywencai）领域 run。

对应迁移 084 中的 board_facts_runs / board_facts_run_items 表。

[PRD V2.1 §5 / next.md EPIC-02]
- 一次 pywencai 行业/概念事实抓取 + 规范化 + 门禁 + PIT 发布 = 一个 BoardFactsRun。
- run 状态与 SchedulerJobRun 分离，作为独立领域产品 run。
- 失败复用：status=reused_previous + reused_from_run_id + readiness=ready_reused。
- historical replay 禁止调用 pywencai，只消费已存在的 PIT publication。

模块自测：
    python -m app.models.board_facts_run
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BoardFactsRun(Base):
    """行业/概念事实抓取与发布 run。

    状态机（PRD §5.2 E02-T13）：
        queued → fetching → normalizing → validating → persisting → published
        queued → reused_previous（失败复用）
        任意 → failed / cancelled / interrupted
    """

    __tablename__ = "board_facts_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    scheduler_job_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="关联 SchedulerJobRun.id（父级任务，可为空）",
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False, comment="业务交易日")
    run_mode: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="scheduled_current / manual_current / historical_replay",
    )
    source: Mapped[str] = mapped_column(Text(), nullable=False, comment="数据源：pywencai")
    source_query: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="固定查询语句")
    query_hash: Mapped[str | None] = mapped_column(Text(), nullable=True, comment="查询 hash")
    provider_contract_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    normalization_contract_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    identity_contract_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    quality_gate_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    snapshot_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    taxonomy_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    membership_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default="queued",
        comment="queued/fetching/normalizing/validating/persisting/published/reused_previous/failed/cancelled/interrupted",
    )
    readiness: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment="ready / ready_reused / unavailable / pending",
    )
    reused_from_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("board_facts_runs.id"), nullable=True,
        comment="status=reused_previous 时指向被复用的旧 run",
    )
    staleness: Mapped[int | None] = mapped_column(
        Integer(), nullable=True,
        comment="复用快照的陈旧交易日数",
    )
    raw_rows: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    resolved_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    unresolved_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    industry_l1_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    industry_l2_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    industry_l3_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    concept_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    membership_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    coverage_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    diagnostics_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    gate_results_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_board_facts_runs_trade_date", "trade_date"),
        Index("ix_board_facts_runs_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<BoardFactsRun(trade_date={self.trade_date!r}, "
            f"mode={self.run_mode!r}, status={self.status!r}, readiness={self.readiness!r})>"
        )


class BoardFactsRunItem(Base):
    """BoardFactsRun 的逐项归属（按股票）。

    用于审计与逐行 coverage 统计；行业/概念定义本身落在 PIT 表。
    """

    __tablename__ = "board_facts_run_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("board_facts_runs.id", ondelete="CASCADE"), nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    instrument_symbol: Mapped[str | None] = mapped_column(Text(), nullable=True)
    resolved: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    industry_l1: Mapped[str | None] = mapped_column(Text(), nullable=True)
    industry_l2: Mapped[str | None] = mapped_column(Text(), nullable=True)
    industry_l3: Mapped[str | None] = mapped_column(Text(), nullable=True)
    concepts: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    reason_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "instrument_symbol",
            name="uq_board_facts_run_items_run_symbol",
        ),
    )


if __name__ == "__main__":
    cols = {c.name for c in BoardFactsRun.__table__.columns}
    required = {
        "id", "trade_date", "run_mode", "source", "status", "readiness",
        "started_at", "heartbeat_at", "finished_at", "error_code", "error_message",
    }
    assert required.issubset(cols), f"缺少字段: {required - cols}"
    print(f"OK: {BoardFactsRun.__tablename__} columns verified")
    item_cols = {c.name for c in BoardFactsRunItem.__table__.columns}
    required_items = {"id", "run_id", "trade_date", "resolved"}
    assert required_items.issubset(item_cols), f"缺少字段: {required_items - item_cols}"
    print(f"OK: {BoardFactsRunItem.__tablename__} columns verified")
