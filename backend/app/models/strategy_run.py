"""StrategyRun/Result/Metric/RunItem ORM 模型 - 策略运行与结果存储。

对应迁移：
- 005_strategy_runs: strategy_runs/strategy_results/strategy_result_metrics
- 015_strategy_batch: strategy_run_items + strategy_runs 扩展字段 + 索引
- 016_run_published_at: strategy_runs.published_at 列
- 026_strategy_run_lease_fields: strategy_runs 租约与恢复字段
- 030_strategy_run_attempt_no: strategy_runs.attempt_no 业务重试序号

字段说明：
- strategy_runs.run_type: 触发方式（manual/scheduled/replay）
- strategy_runs.status: 运行状态（queued/running/completed/partial_failed/published/failed）
- strategy_runs.input_overrides: 输入参数覆盖（JSONB，仅保留原始输入参数）
- strategy_runs.effective_config: 运行时实际使用的配置快照（JSONB，不可变）
- strategy_runs.effective_config_hash: effective_config 的 SHA256 哈希
- strategy_runs.total/succeeded/failed/skipped_count: 批量统计
- strategy_runs.published_at: 发布时间（非空表示已发布，用户可查询）
- strategy_runs.attempt_no: 业务重试序号（同一 version/date/run_type 内第几次尝试）
- strategy_runs.attempt_count: 租约恢复计数（Worker 宕机恢复时累加）
- strategy_results.payload: 完整结果 JSON（含所有指标，便于详情查询）
- strategy_result_metrics: 拆分为 numeric_value/text_value/bool_value 三列，
  支持按指标高效筛选排序（ix_metric_numeric 索引）
- strategy_run_items: per-stock 执行状态跟踪（status/attempt_count/error/result_id）

注意：
- ORM 严格对齐迁移 DDL，未在迁移中声明的字段/FK 不在 ORM 中映射
- 错误信息分两级存储：run 级存 strategy_runs.error_message/error_code/failure_stage，
  per-stock 级存 strategy_run_items.error_message（不再用 input_overrides）
- strategy_result_metrics 的 strategy_version_id/instrument_id 在迁移中无 FK（冗余字段，用于索引）
- matched 不持久化到 payload（命中由用户筛选条件动态决定）
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


# [StrategyRun] - 失败阶段枚举：DSA 运行各阶段异常标记，写入 failure_stage 字段
FAILURE_STAGE_DATA_READINESS = "DATA_READINESS"
FAILURE_STAGE_LOAD_VERSION = "LOAD_VERSION"
FAILURE_STAGE_LOAD_RUNTIME = "LOAD_RUNTIME"
FAILURE_STAGE_LOAD_INSTRUMENTS = "LOAD_INSTRUMENTS"
FAILURE_STAGE_CALCULATE_INSTRUMENTS = "CALCULATE_INSTRUMENTS"
FAILURE_STAGE_WRITE_RESULTS = "WRITE_RESULTS"
FAILURE_STAGE_QUALITY_GATE = "QUALITY_GATE"
FAILURE_STAGE_PUBLISH = "PUBLISH"
FAILURE_STAGE_WORKER_INTERRUPTED = "WORKER_INTERRUPTED"

# 全部失败阶段集合（用于校验 failure_stage 取值合法性）
ALL_FAILURE_STAGES = {
    FAILURE_STAGE_DATA_READINESS,
    FAILURE_STAGE_LOAD_VERSION,
    FAILURE_STAGE_LOAD_RUNTIME,
    FAILURE_STAGE_LOAD_INSTRUMENTS,
    FAILURE_STAGE_CALCULATE_INSTRUMENTS,
    FAILURE_STAGE_WRITE_RESULTS,
    FAILURE_STAGE_QUALITY_GATE,
    FAILURE_STAGE_PUBLISH,
    FAILURE_STAGE_WORKER_INTERRUPTED,
}


class StrategyRun(Base):
    """策略运行记录 - 一次策略执行的生命周期。

    对应迁移 005 strategy_runs 表 + 015 扩展字段 + 030 attempt_no + 035 错误字段。
    idempotency_key 唯一约束防止重复运行。

    字段映射（任务描述 → 迁移 DDL）：
    - trigger_kind → run_type（manual/scheduled/replay）
    - status: queued/running/completed/partial_failed/published/failed
    - error → run 级存 error_code/failure_stage/error_message（迁移 035）；
      per-stock 级存 strategy_run_items.error_message
    - effective_config: 运行时从 manifest 读取的参数快照（迁移 015 新增）
    - attempt_no: 业务重试序号（迁移 030 新增）
    - attempt_count: 租约恢复计数（迁移 026 新增，与 attempt_no 语义不同）
    """

    __tablename__ = "strategy_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=False,
        comment="策略版本 ID",
    )
    run_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="触发方式：manual/scheduled/replay"
    )
    trade_date: Mapped[date | None] = mapped_column(
        Date(), nullable=True, comment="交易日（selector 策略的选股日期）"
    )
    data_cutoff: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="数据截止时间"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="运行状态：queued/running/completed/partial_failed/published/failed",
    )
    input_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=func.text("'{}'"),
        comment="输入参数覆盖（仅保留原始输入参数，如 trade_date、strategy_key）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    idempotency_key: Mapped[str] = mapped_column(
        Text(), nullable=False, unique=True, comment="幂等键（防重复运行）"
    )
    # 迁移 015 新增字段
    effective_config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=True,
        comment="运行时实际使用的配置快照（从 manifest 读取，不可变）",
    )
    effective_config_hash: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="effective_config 的 SHA256 哈希"
    )
    total_instruments: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="标的总数"
    )
    succeeded_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="成功数"
    )
    failed_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="失败数"
    )
    skipped_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="跳过数（停牌等）"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="发布时间（非空表示已发布，用户可查询）",
    )
    # [StrategyRun] - 租约与恢复字段
    queued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="入队时间，创建时赋值"
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="Worker 心跳时间"
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="租约过期时间"
    )
    worker_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="执行 Worker 标识"
    )
    # [StrategyRun] - attempt_count 为租约恢复计数（Worker 宕机恢复时累加）
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="0", comment="租约恢复计数（与 attempt_no 区分）"
    )
    # [StrategyRun] - attempt_no 为业务重试序号（同一 version/date/run_type 内第几次尝试）
    attempt_no: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default="1", comment="业务重试序号"
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="下次重试时间"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="错误码"
    )
    # [StrategyRun] - 运行级错误信息: 存储 DSA 运行失败的详细原因
    error_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="运行级错误详情（per-stock 错误见 strategy_run_items.error_message）"
    )
    # [StrategyRun] - 失败阶段: DATA_READINESS/LOAD_VERSION/.../WORKER_INTERRUPTED
    failure_stage: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="失败阶段枚举（见 ALL_FAILURE_STAGES）"
    )
    # TODO: 添加 bars_snapshot_id 字段，记录 DSA 运行时使用的行情数据版本
    # 需 DB 迁移：ALTER TABLE strategy_runs ADD COLUMN bars_snapshot_id UUID;
    # 用于追踪 DSA 运行与 bars 刷新的对应关系，确保数据可溯源

    def __repr__(self) -> str:
        return (
            f"<StrategyRun(run_type={self.run_type!r}, "
            f"status={self.status!r}, trade_date={self.trade_date}, "
            f"attempt_no={self.attempt_no})>"
        )


class StrategyResult(Base):
    """策略结果 - 单只标的在一个交易日的计算输出。

    对应迁移 005 strategy_results 表。
    唯一约束 (run_id, instrument_id) 确保同一 run 内同一标的结果唯一，不同 run 的结果互不覆盖。

    payload 存储完整结果 JSON（含所有指标），便于详情查询。
    指标同时拆分到 strategy_result_metrics 表以支持高效筛选排序。
    """

    __tablename__ = "strategy_results"
    __table_args__ = (
        # 同一 run 内同一 instrument 结果唯一
        UniqueConstraint(
            "run_id",
            "instrument_id",
            name="uq_strategy_results_run_instrument",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_runs.id"),
        nullable=False,
        comment="所属运行 ID",
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_versions.id"),
        nullable=False,
        comment="策略版本 ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="标的 ID",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="交易日"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        comment="完整结果 JSON（含所有指标）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    # [选股] - 关联标的主数据，用于查询时 eager load symbol/name/market
    instrument: Mapped["Instrument"] = relationship(
        "Instrument", lazy="raise", foreign_keys=[instrument_id],
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyResult(instrument_id={self.instrument_id!r}, "
            f"trade_date={self.trade_date})>"
        )


class StrategyResultMetric(Base):
    """策略结果指标 - 拆分存储以支持高效筛选排序。

    对应迁移 005 strategy_result_metrics 表。
    复合主键 (result_id, metric_key) 确保每个结果的每个指标唯一。

    指标值按类型存储：
    - numeric_value: 数值型指标（支持范围筛选和排序）
    - text_value: 文本型指标
    - bool_value: 布尔型指标

    索引 ix_metric_numeric (strategy_version_id, trade_date, metric_key, numeric_value)
    支持按指标高效筛选排序。

    注意：strategy_version_id/instrument_id 在迁移中无 FK（冗余字段，用于索引）。
    """

    __tablename__ = "strategy_result_metrics"
    __table_args__ = (
        PrimaryKeyConstraint("result_id", "metric_key"),
        Index(
            "ix_metric_numeric",
            "strategy_version_id",
            "trade_date",
            "metric_key",
            "numeric_value",
        ),
    )

    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_results.id", ondelete="CASCADE"),
        nullable=False,
        comment="结果 ID",
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="策略版本 ID（冗余，用于索引，迁移中无 FK）",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="交易日（冗余，用于索引）"
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="标的 ID（冗余，用于索引，迁移中无 FK）",
    )
    metric_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="指标名"
    )
    numeric_value: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="数值型指标值"
    )
    text_value: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="文本型指标值"
    )
    bool_value: Mapped[bool | None] = mapped_column(
        Boolean(), nullable=True, comment="布尔型指标值"
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyResultMetric(result_id={self.result_id!r}, "
            f"metric_key={self.metric_key!r})>"
        )


class StrategyRunItem(Base):
    """策略运行子项 - 单只标的在一次运行中的执行状态。

    对应迁移 015 strategy_run_items 表。
    唯一约束 (run_id, instrument_id) 确保每次运行中每只标的只有一条记录。

    状态流转：pending → running → succeeded/failed/skipped
    - succeeded: 计算成功，result_id 指向写入的 strategy_results 记录
    - failed: 计算失败，error_message 记录原因
    - skipped: 跳过（停牌、数据不足等），不视为失败

    索引：
    - ix_run_items_run_status: (run_id, status) 按批次查询某状态股票
    - ix_run_items_instrument: (instrument_id) 按股票查询历史
    """

    __tablename__ = "strategy_run_items"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "instrument_id",
            name="uq_strategy_run_items_run_instrument",
        ),
        Index("ix_run_items_run_status", "run_id", "status"),
        Index("ix_run_items_instrument", "instrument_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属运行 ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="标的 ID",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default="pending",
        comment="pending/running/succeeded/failed/skipped",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        server_default="0",
        comment="尝试次数",
    )
    error_message: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="失败原因"
    )
    result_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("strategy_results.id"),
        nullable=True,
        comment="关联结果 ID（成功时填充）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyRunItem(run_id={self.run_id!r}, "
            f"instrument_id={self.instrument_id!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StrategyRun.__tablename__={StrategyRun.__tablename__}")
    run_cols = [c.name for c in StrategyRun.__table__.columns]
    print(f"StrategyRun columns={run_cols}")
    assert "strategy_version_id" in run_cols
    assert "run_type" in run_cols
    assert "status" in run_cols
    assert "idempotency_key" in run_cols
    assert "input_overrides" in run_cols
    # 迁移 015 新增字段
    assert "effective_config" in run_cols
    assert "effective_config_hash" in run_cols
    assert "total_instruments" in run_cols
    assert "succeeded_count" in run_cols
    assert "failed_count" in run_cols
    assert "skipped_count" in run_cols
    assert "published_at" in run_cols
    # 迁移 026 新增字段
    assert "queued_at" in run_cols
    assert "heartbeat_at" in run_cols
    assert "lease_expires_at" in run_cols
    assert "worker_id" in run_cols
    assert "attempt_count" in run_cols
    assert "next_retry_at" in run_cols
    assert "error_code" in run_cols
    # 迁移 035 新增字段
    assert "error_message" in run_cols
    assert "failure_stage" in run_cols
    # 迁移 030 新增字段
    assert "attempt_no" in run_cols

    # 验证失败阶段枚举（迁移 035）
    assert len(ALL_FAILURE_STAGES) == 9, f"ALL_FAILURE_STAGES 应包含 9 种值，实际 {len(ALL_FAILURE_STAGES)}"
    assert FAILURE_STAGE_WORKER_INTERRUPTED in ALL_FAILURE_STAGES
    assert FAILURE_STAGE_LOAD_VERSION in ALL_FAILURE_STAGES

    print(f"StrategyResult.__tablename__={StrategyResult.__tablename__}")
    res_cols = [c.name for c in StrategyResult.__table__.columns]
    print(f"StrategyResult columns={res_cols}")
    assert "run_id" in res_cols
    assert "instrument_id" in res_cols
    assert "trade_date" in res_cols
    assert "payload" in res_cols

    print(f"StrategyResultMetric.__tablename__={StrategyResultMetric.__tablename__}")
    metric_cols = [c.name for c in StrategyResultMetric.__table__.columns]
    print(f"StrategyResultMetric columns={metric_cols}")
    assert "result_id" in metric_cols
    assert "metric_key" in metric_cols
    assert "numeric_value" in metric_cols
    assert "text_value" in metric_cols
    assert "bool_value" in metric_cols

    # 验证索引
    metric_indexes = [idx.name for idx in StrategyResultMetric.__table__.indexes]
    print(f"StrategyResultMetric indexes={metric_indexes}")
    assert "ix_metric_numeric" in metric_indexes

    # 验证主键
    metric_pk = [c.name for c in StrategyResultMetric.__table__.primary_key.columns]
    print(f"StrategyResultMetric PK={metric_pk}")
    assert metric_pk == ["result_id", "metric_key"]

    # 验证 StrategyRunItem（迁移 015 新增）
    print(f"StrategyRunItem.__tablename__={StrategyRunItem.__tablename__}")
    item_cols = [c.name for c in StrategyRunItem.__table__.columns]
    print(f"StrategyRunItem columns={item_cols}")
    assert "run_id" in item_cols
    assert "instrument_id" in item_cols
    assert "status" in item_cols
    assert "attempt_count" in item_cols
    assert "error_message" in item_cols
    assert "result_id" in item_cols
    assert "started_at" in item_cols
    assert "finished_at" in item_cols

    # 验证 StrategyRunItem 索引
    item_indexes = [idx.name for idx in StrategyRunItem.__table__.indexes]
    print(f"StrategyRunItem indexes={item_indexes}")
    assert "ix_run_items_run_status" in item_indexes
    assert "ix_run_items_instrument" in item_indexes

    # 验证 StrategyRunItem 唯一约束
    item_uqs = [
        c.name
        for c in StrategyRunItem.__table__.constraints
        if hasattr(c, "name") and c.name and "uq" in c.name.lower()
    ]
    print(f"StrategyRunItem unique constraints={item_uqs}")
    assert "uq_strategy_run_items_run_instrument" in item_uqs

    print("OK")
