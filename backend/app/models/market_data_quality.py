"""MarketDataQualityRun / MarketDataQualityItem ORM 模型 - 全市场行情扫描与修复。

对应迁移 075 中的两张表：
- market_data_quality_runs：run 级元数据，run_key 幂等键
- market_data_quality_items：per-instrument 检查明细

设计说明：
- run_key 格式："mdq:{timeframe}:{start}:{end}:{algorithm_version}"，唯一约束保证幂等
- algorithm_version 常量 "mdq-v1.0.0"
- 支持 1d / 15m 两个 timeframe，分别扫描 bars_daily / bars_15min
- 修复模式仅对 classification=DB_MISSING 的 item 执行 upsert（raw OHLCV）
- factor 重算委托 adjustment_factor_calculator.calculate_adjustment_factor_series
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MarketDataQualityRun(Base):
    """全市场行情质量扫描 run 级记录。

    使用方式：
    1. create_run：根据 (timeframe, start, end, algorithm_version) 生成 run_key
    2. 已存在 succeeded run 直接复用（幂等）；否则新建 status=created
    3. execute_scan：批次拉取 pending items → scan_instrument → 更新 item + run 计数
    4. execute_repair：仅对 classification=DB_MISSING 的 item 执行 upsert 修复
    5. summarize_run：按 issue_type / classification / severity 聚合
    """

    __tablename__ = "market_data_quality_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    run_key: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="幂等键，格式 mdq:{timeframe}:{start}:{end}:{algorithm_version}",
    )
    timeframe: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="周期：1d | 15m",
    )
    start_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="扫描起始日期（含）",
    )
    end_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="扫描结束日期（含）",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="扫描算法版本（常量 mdq-v1.0.0）",
    )
    parameter_hash: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="参数 hash（含算法版本与固定参数，便于跨入口一致性校验）",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="状态：created/running/succeeded/partial/failed/cancelled",
    )
    total_instruments: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0,
        comment="待扫描标的总数（活跃 A 股）",
    )
    succeeded_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="扫描成功数",
    )
    failed_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="扫描失败数",
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="扫描跳过数",
    )
    coverage_ratio: Mapped[float] = mapped_column(
        Float(), nullable=False, default=0.0,
        comment="覆盖率 = succeeded / total",
    )
    issue_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict,
        comment="issue 分布 JSON：{issue_type: count}",
    )
    repair_mode: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False,
        comment="是否启用修复模式（仅 DB_MISSING 触发修复）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="扫描开始时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="扫描完成时间",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="run 级失败原因（status=failed 时填充）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("run_key", name="uq_mdq_runs_run_key"),
        Index("ix_mdq_runs_status", "status"),
        Index("ix_mdq_runs_timeframe_date", "timeframe", "start_date", "end_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketDataQualityRun("
            f"run_key={self.run_key!r}, timeframe={self.timeframe!r}, "
            f"status={self.status!r}, total={self.total_instruments!r}, "
            f"succeeded={self.succeeded_count!r})>"
        )


class MarketDataQualityItem(Base):
    """全市场行情质量扫描 per-instrument 检查明细。

    状态机：
    - pending → running → succeeded（成功）/ failed（异常）/ skipped（不扫描）
    - classification 决定修复路径：
      NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING → 不可修复
      DB_MISSING → 可修复（从 upstream pytdx 拉取并 upsert）
      FACTOR_MISSING → 可修复（重算 adj_factor）
      OK → 无需修复
    """

    __tablename__ = "market_data_quality_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_data_quality_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_data_quality_runs.id",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="关联 instruments.id",
    )
    symbol: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="股票代码（冗余存储，便于查询展示）",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="状态：pending/running/succeeded/failed/skipped",
    )
    issue_type: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment=(
            "问题类型：NO_ISSUE/INTERNAL_GAP/TAIL_GAP/DUPLICATE/TIME_REVERSED/"
            "OHLC_INVALID/VOLUME_ANOMALY/AMOUNT_ANOMALY/FACTOR_MISSING/"
            "FACTOR_ANOMALY/BAR_COUNT_INSUFFICIENT"
        ),
    )
    issue_reason: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment="问题说明（自由文本，如 missing dates: 2026-02-15, 2026-02-16）",
    )
    severity: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="严重程度：info/warning/error",
    )
    missing_dates: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, comment="缺失日期列表（ISO 字符串数组）",
    )
    duplicate_dates: Mapped[list[str] | None] = mapped_column(
        JSONB, nullable=True, comment="重复日期列表（ISO 字符串数组）",
    )
    first_bar_date: Mapped[date | None] = mapped_column(
        Date(), nullable=True, comment="首条 bar 日期",
    )
    last_bar_date: Mapped[date | None] = mapped_column(
        Date(), nullable=True, comment="末条 bar 日期",
    )
    bar_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="实际 bar 数",
    )
    expected_bar_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="期望 bar 数（交易日历覆盖数）",
    )
    factor_min: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True,
        comment="adj_factor 最小值",
    )
    factor_max: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=20, scale=8), nullable=True,
        comment="adj_factor 最大值",
    )
    factor_anomaly_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="adj_factor 异常跳变次数",
    )
    classification: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment=(
            "分类：NOT_LISTED/SUSPENDED/DELISTED/SOURCE_MISSING/"
            "DB_MISSING/FACTOR_MISSING/OK"
        ),
    )
    repair_attempted: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False,
        comment="是否尝试过修复",
    )
    repair_status: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="修复状态：pending/succeeded/failed/skipped",
    )
    repair_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="修复说明（自由文本）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="单标的扫描开始时间",
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="单标的扫描完成时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "run_id", "instrument_id", name="uq_mdq_items_run_instrument",
        ),
        Index("ix_mdq_items_run_status", "run_id", "status"),
        Index("ix_mdq_items_issue_type", "issue_type"),
        Index("ix_mdq_items_classification", "classification"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketDataQualityItem("
            f"run_id={self.run_id!r}, symbol={self.symbol!r}, "
            f"status={self.status!r}, classification={self.classification!r}, "
            f"issue_type={self.issue_type!r})>"
        )


if __name__ == "__main__":
    # 自测：验证两表字段与迁移定义一致
    run_cols = MarketDataQualityRun.__table__.columns
    run_expected = {
        "id", "run_key", "timeframe", "start_date", "end_date",
        "algorithm_version", "parameter_hash", "status",
        "total_instruments", "succeeded_count", "failed_count", "skipped_count",
        "coverage_ratio", "issue_summary", "repair_mode",
        "started_at", "finished_at", "error_message",
        "created_at", "updated_at",
    }
    run_actual = {c.name for c in run_cols}
    assert run_expected == run_actual, (
        f"MarketDataQualityRun 字段不匹配: {run_expected ^ run_actual}"
    )
    print(f"OK: {MarketDataQualityRun.__tablename__} columns verified")

    item_cols = MarketDataQualityItem.__table__.columns
    item_expected = {
        "id", "run_id", "instrument_id", "symbol", "status",
        "issue_type", "issue_reason", "severity",
        "missing_dates", "duplicate_dates",
        "first_bar_date", "last_bar_date", "bar_count", "expected_bar_count",
        "factor_min", "factor_max", "factor_anomaly_count",
        "classification", "repair_attempted", "repair_status", "repair_message",
        "started_at", "finished_at", "created_at", "updated_at",
    }
    item_actual = {c.name for c in item_cols}
    assert item_expected == item_actual, (
        f"MarketDataQualityItem 字段不匹配: {item_expected ^ item_actual}"
    )
    print(f"OK: {MarketDataQualityItem.__tablename__} columns verified")

    # 验证 unique 约束名
    run_uq = {c.name for c in MarketDataQualityRun.__table__.constraints if c.name}
    assert "uq_mdq_runs_run_key" in run_uq, f"缺少 uq_mdq_runs_run_key: {run_uq}"
    item_uq = {c.name for c in MarketDataQualityItem.__table__.constraints if c.name}
    assert "uq_mdq_items_run_instrument" in item_uq, (
        f"缺少 uq_mdq_items_run_instrument: {item_uq}"
    )
    print("OK: unique constraints verified")
