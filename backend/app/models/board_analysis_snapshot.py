"""BoardAnalysisSnapshot ORM 模型 - 板块分析 V1 快照。

对应迁移 074 中的 board_analysis_snapshots 表：
- 单表设计：每条记录既是 run 又是 snapshot
- 唯一键 (trade_date, board_id, algorithm_version) 保证幂等
- coverage_ratio >= 0.95 才可正式发布（写入 factor_publications）
- 复用 factor_publications 表发布指针：
  publication_kind=market_aggregation, scope_type=board, scope_key=board_id::text
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


class BoardAnalysisRun(Base):
    """Immutable identity and quality summary for one Board batch."""

    __tablename__ = "board_analysis_runs"
    __table_args__ = (
        UniqueConstraint(
            "trade_date",
            "source_core_run_id",
            "taxonomy_version",
            "taxonomy_compatibility_key",
            "algorithm_version",
            "membership_version",
            name="uq_board_analysis_runs_identity",
        ),
        Index("ix_board_analysis_runs_date_status", "trade_date", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(Text(), nullable=False)
    taxonomy_compatibility_key: Mapped[str] = mapped_column(Text(), nullable=False)
    membership_version: Mapped[str] = mapped_column(Text(), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    expected_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    succeeded_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    blockers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]",
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now(),
    )


class BoardAnalysisSnapshot(Base):
    """板块分析 V1 快照（单表设计，含 run 级状态字段）。

    使用方式：
    1. 计算开始：upsert 一条 status=running 记录
    2. 计算完成：更新 status=succeeded, payload, coverage_ratio 等
    3. coverage_ratio >= 0.95 时，写入 factor_publications 发布指针
    4. 读请求：先查 factor_publications 获取 data_run_id，再查本表
    5. coverage_ratio < 0.95：保存 partial 结果但不发布指针
    """

    __tablename__ = "board_analysis_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "board_analysis_run_id",
            "board_id",
            name="uq_board_analysis_snapshots_run_board",
        ),
        Index(
            "ix_board_analysis_snapshots_date_type",
            "trade_date",
            "board_type",
        ),
        Index(
            "ix_board_analysis_snapshots_board_date",
            "board_id",
            "trade_date",
        ),
        Index(
            "ix_board_analysis_snapshots_batch",
            "board_analysis_run_id",
            "board_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日",
    )
    board_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_boards.id", ondelete="CASCADE"),
        nullable=False,
        comment="板块 ID（关联 market_boards.id）",
    )
    board_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="板块类型：industry | concept",
    )
    board_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="板块名称（冗余存储，便于查询展示）",
    )
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="输入 stock_core snapshot_run_id（factor_publications.data_run_id）",
    )
    board_analysis_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("board_analysis_runs.id", ondelete="RESTRICT"),
        nullable=False,
        comment="真实 Board batch run；legacy 数据由前向迁移补齐",
    )
    taxonomy_version: Mapped[str] = mapped_column(
        Text(), nullable=False, default="legacy-v1", server_default="legacy-v1",
    )
    taxonomy_compatibility_key: Mapped[str] = mapped_column(
        Text(), nullable=False, default="qstock-board-v1", server_default="qstock-board-v1",
    )
    membership_version: Mapped[str] = mapped_column(
        Text(), nullable=False, default="legacy-projection-20260801",
        server_default="legacy-projection-20260801",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="板块分析算法版本",
    )
    parameter_hash: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="参数 hash（含算法版本与固定参数）",
    )
    eligible_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="板块成员总数（含数据不足股票）",
    )
    ready_count: Mapped[int] = mapped_column(
        Integer(), nullable=False,
        comment="有效股票数（core_factor_ready=true 且同 source_core_run_id）",
    )
    coverage_ratio: Mapped[float] = mapped_column(
        Float(), nullable=False,
        comment="覆盖率 = ready_count / eligible_count；>=0.95 才可正式发布",
    )
    missing_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="缺失股票数（eligible - ready）",
    )
    missing_reasons: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}",
        comment="缺失原因分布 JSON：{reason_code: count}",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="状态：pending/running/succeeded/failed/partial",
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False,
        comment="板块分析指标 payload JSON",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="计算失败原因（status=failed 时填充）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计算开始时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计算完成时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<BoardAnalysisSnapshot("
            f"trade_date={self.trade_date!r}, board_id={self.board_id!r}, "
            f"board_type={self.board_type!r}, status={self.status!r}, "
            f"coverage_ratio={self.coverage_ratio!r})>"
        )


if __name__ == "__main__":
    run_cols = {c.name for c in BoardAnalysisRun.__table__.columns}
    assert {"id", "trade_date", "source_core_run_id", "status", "published_at"} <= run_cols

    cols = BoardAnalysisSnapshot.__table__.columns
    expected = {
        "id", "trade_date", "board_id", "board_type", "board_name",
        "source_core_run_id", "board_analysis_run_id", "taxonomy_version",
        "taxonomy_compatibility_key", "membership_version",
        "algorithm_version", "parameter_hash",
        "eligible_count", "ready_count", "coverage_ratio", "missing_count",
        "missing_reasons", "status", "payload", "error_message",
        "started_at", "finished_at", "created_at", "updated_at",
    }
    actual = {c.name for c in cols}
    assert expected == actual, f"字段不匹配: {expected ^ actual}"
    print(f"OK: {BoardAnalysisSnapshot.__tablename__} columns verified")
