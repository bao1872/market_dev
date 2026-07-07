"""StockFeatureSnapshotRun ORM 模型 - 特征快照运行记录（publish gate）。

对应迁移 057_stock_feature_snapshot_runs 中的 stock_feature_snapshot_runs 表：
- 每次 after_close / backfill / manual 触发创建一条 run 记录，状态机 running → succeeded/failed。
- watchlist 只读取 expected_snapshot_trade_date 且存在 succeeded/published run 的 snapshot 行。
- 失败 run 对应的 snapshot 即使存在也不得被 watchlist 读取，避免半成品被显示为 SUCCEEDED。

设计说明：
- 唯一约束采用 PARTIAL UNIQUE INDEX（仅约束 status='running' 的活跃记录），
  对齐 scheduler_job_runs 的 run_key 模式，支持失败后重试创建新 run。
- 不给 metadata JSONB 加 GIN 索引（run 表很小，优先节省磁盘）。
- 仅在 (trade_date, status) 与 (trade_date, schema_version) 上建 btree 索引，
  满足 watchlist 按 trade_date + status='succeeded' 的查询需求。
- 表很小（每天 after_close 一条 + backfill 每次 N 条），不需要更多索引。

模块自测：
    python -m app.models.stock_feature_snapshot_run
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# [RunGate] - 运行类型枚举
RUN_TYPE_AFTER_CLOSE = "after_close"
RUN_TYPE_BACKFILL = "backfill"
RUN_TYPE_MANUAL = "manual"
ALL_RUN_TYPES = {RUN_TYPE_AFTER_CLOSE, RUN_TYPE_BACKFILL, RUN_TYPE_MANUAL}

# [RunGate] - 状态机枚举：running → succeeded/failed
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
ALL_STATUSES = {STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED}


class StockFeatureSnapshotRun(Base):
    """特征快照运行记录 - 单次 after_close/backfill/manual 执行的生命周期与发布门禁。

    状态流转：
        running → succeeded（成功，写 published_at，对应 snapshot 行可被 watchlist 读取）
        running → failed（失败，对应 snapshot 行不得被 watchlist 读取）

    唯一约束：仅 status='running' 时 (trade_date, schema_version,
    primary_timeframe, secondary_timeframe, adj, run_type) 唯一；
    失败后可创建新 run 重试，历史 failed 记录保留用于审计。
    """

    __tablename__ = "stock_feature_snapshot_runs"

    __table_args__ = (
        # [Idempotency] - 部分唯一索引：仅约束 running 活跃记录，允许 failed 后新建 attempt
        Index(
            "uq_snapshot_runs_active_key",
            "trade_date",
            "schema_version",
            "primary_timeframe",
            "secondary_timeframe",
            "adj",
            "run_type",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "ix_snapshot_runs_trade_date_status",
            "trade_date",
            "status",
        ),
        Index(
            "ix_snapshot_runs_trade_date_schema",
            "trade_date",
            "schema_version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="运行 ID",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日"
    )
    schema_version: Mapped[int] = mapped_column(
        Integer(), nullable=False, server_default=text("1"), comment="快照 schema 版本"
    )
    primary_timeframe: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default=text("'1d'"), comment="主时间周期"
    )
    secondary_timeframe: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default=text("'15m'"), comment="次时间周期"
    )
    adj: Mapped[str] = mapped_column(
        Text(), nullable=False, server_default=text("'qfq'"), comment="复权方式"
    )
    run_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="触发方式：after_close/backfill/manual",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'running'"),
        comment="运行状态：running/succeeded/failed",
    )
    expected_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="预期快照数（active A 股总数）"
    )
    snapshot_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="实际写入快照数（含降级）"
    )
    failed_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="失败股票数"
    )
    skipped_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="跳过股票数（停牌/无数据）"
    )
    failure_rate: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="失败率 0.0-1.0"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="发布时间（succeeded 时写入，watchlist 据此判断是否可读）",
    )
    # [RunGate] - 描述: metadata_ 加下划线后缀避免与 SQLAlchemy 保留属性 metadata 冲突
    metadata_: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata_",
        JSONB(astext_type=Text()),
        nullable=True,
        comment="额外元数据 JSONB（如 failure_threshold、rollback_reason）",
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
        comment="更新时间",
    )

    def __repr__(self) -> str:
        return (
            f"<StockFeatureSnapshotRun(trade_date={self.trade_date!r}, "
            f"run_type={self.run_type!r}, status={self.status!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(f"StockFeatureSnapshotRun.__tablename__={StockFeatureSnapshotRun.__tablename__}")
    cols = [c.name for c in StockFeatureSnapshotRun.__table__.columns]
    print(f"columns={cols}")

    # 验证必需列存在
    required_cols = [
        "id", "trade_date", "schema_version",
        "primary_timeframe", "secondary_timeframe", "adj",
        "run_type", "status",
        "expected_count", "snapshot_count", "failed_count", "skipped_count",
        "failure_rate",
        "started_at", "finished_at", "published_at",
        "metadata_", "created_at", "updated_at",
    ]
    for col in required_cols:
        assert col in cols, f"缺少列: {col}"
    print("columns ✓")

    # 验证索引（含部分唯一索引）
    idx_names = {idx.name for idx in StockFeatureSnapshotRun.__table__.indexes if idx.name}
    assert "uq_snapshot_runs_active_key" in idx_names, f"缺少部分唯一索引: {idx_names}"
    assert "ix_snapshot_runs_trade_date_status" in idx_names, f"缺少索引: {idx_names}"
    assert "ix_snapshot_runs_trade_date_schema" in idx_names, f"缺少索引: {idx_names}"
    print("indexes ✓")

    # 验证枚举常量
    assert RUN_TYPE_AFTER_CLOSE in ALL_RUN_TYPES
    assert RUN_TYPE_BACKFILL in ALL_RUN_TYPES
    assert RUN_TYPE_MANUAL in ALL_RUN_TYPES
    assert STATUS_RUNNING in ALL_STATUSES
    assert STATUS_SUCCEEDED in ALL_STATUSES
    assert STATUS_FAILED in ALL_STATUSES
    print("enums ✓")

    print("OK")
