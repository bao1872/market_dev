"""ResearchFeatureMatrixRun / ResearchFeatureMatrixRow ORM 模型 - 研究特征矩阵轻量宽表。

对应迁移 058_research_feature_matrix 中的两张表：
- research_feature_matrix_runs: 按月分批的 run 级元数据（状态机 + 统计摘要）
- research_feature_matrix_rows: 扁平宽表，33 个 feature 列直接平铺，不存 JSON payload

设计说明：
- 与生产 stock_feature_snapshots 严格分离：研究矩阵不接入 watchlist_ready，
  不修改生产 snapshot，不触发 watchlist 通知。
- 宽表设计（非 EAV），33 个 feature 列与 feature_causality_registry.db_column() 1:1 对应。
- registry 仍保留 dotted key（causal.atr），写 DB 时映射成下划线列名（causal_atr）。
- 索引精简：unique(instrument_id, trade_date) + index(trade_date, status, run_id)，
  不给单个 feature 列建索引（后续按查询需求再加）。
- metadata_json 只放小摘要（scope/notes/thresholds），不存完整 payload，不建 GIN 索引。
- 不引入 JSON payload 列，避免 EAV 与 GIN 索引膨胀。

模块自测：
    python -m app.models.research_feature_matrix
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# [RunStatus] - 状态机枚举：running → succeeded/failed
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
ALL_STATUSES = {STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED}


class ResearchFeatureMatrixRun(Base):
    """研究特征矩阵运行记录 - 单月回补执行的生命周期与统计摘要。

    状态流转：
        running → succeeded（成功，rows_count 写入实际行数）
        running → failed（失败，failed_count 写入失败数，metadata_json 记录原因）

    唯一约束：run_key 全局唯一（如 2026-01_full / 2026-01_sample_100），
    支持 --resume 通过 run_key 查找已有 run 并续跑或幂等 upsert。
    """

    __tablename__ = "research_feature_matrix_runs"

    __table_args__ = (
        UniqueConstraint(
            "run_key", name="uq_research_matrix_runs_run_key"
        ),
        Index("ix_research_matrix_runs_month", "month"),
        Index("ix_research_matrix_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="运行 ID",
    )
    run_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="运行唯一键（如 2026-01_full）"
    )
    month: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="月份 YYYY-MM"
    )
    start_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="起始日期"
    )
    end_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="结束日期"
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        server_default=text("'running'"),
        comment="运行状态：running/succeeded/failed",
    )
    instruments_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="股票数"
    )
    trade_dates_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="交易日数"
    )
    rows_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="写入行数"
    )
    failed_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="失败行数"
    )
    duration_seconds: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="耗时秒"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    # [ResearchMatrix] - 描述: metadata_json 只放小摘要（scope/notes/thresholds），不存完整 payload
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata_json",
        JSONB(astext_type=Text()),
        nullable=True,
        comment="小摘要 JSONB（scope/notes/thresholds）",
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
            f"<ResearchFeatureMatrixRun(run_key={self.run_key!r}, "
            f"month={self.month!r}, status={self.status!r})>"
        )


class ResearchFeatureMatrixRow(Base):
    """研究特征矩阵单行 - 一只股票一个交易日的 33 个 feature 值。

    扁平宽表设计（非 EAV）：
    - 33 个 feature 列直接平铺，与 feature_causality_registry.db_column() 1:1 对应
    - 不存完整 JSON payload，不建 GIN 索引
    - 所有 feature 列允许 NULL（warmup 期、未来 label 未到计算期等）

    唯一约束：(instrument_id, trade_date) 全局唯一，跨 run 幂等 upsert。
    """

    __tablename__ = "research_feature_matrix_rows"

    __table_args__ = (
        UniqueConstraint(
            "instrument_id",
            "trade_date",
            name="uq_research_matrix_rows_inst_date",
        ),
        Index("ix_research_matrix_rows_trade_date", "trade_date"),
        Index("ix_research_matrix_rows_instrument_id", "instrument_id"),
        Index("ix_research_matrix_rows_run_id", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),
        comment="行 ID",
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("research_feature_matrix_runs.id", name="fk_research_matrix_rows_run_id"),
        nullable=False,
        comment="所属 run ID",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, comment="股票 ID"
    )
    symbol: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="股票代码"
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="交易日"
    )

    # ===== [FeatureColumns] 33 个 feature 列（与 registry.db_column() 1:1 对应）=====

    # --- causal: 当时可知的滚动特征（16 列）---
    causal_atr: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="ATR 波动率"
    )
    causal_bb_percent_b: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="BB %B"
    )
    causal_bb_bandwidth_pct: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="BB 带宽百分比"
    )
    causal_sqzmom_val: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="SQZMOM 动量值"
    )
    causal_sqzmom_delta_1: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="SQZMOM 一阶差分"
    )
    causal_volume_ratio_20: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="20 日成交量比率"
    )
    causal_volume_percentile_120: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="120 日成交量百分位"
    )
    causal_active_swing_high: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="active swing 高点"
    )
    causal_active_swing_low: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="active swing 低点"
    )
    causal_developing_swing_high: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="developing swing 高点"
    )
    causal_developing_swing_low: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="developing swing 低点"
    )
    causal_active_swing_dir: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="active swing 方向"
    )
    causal_developing_swing_dir: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="developing swing 方向"
    )
    causal_dsa_confirmed_segment: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="DSA 段编号（当时已确认）"
    )
    causal_dsa_confirmed_direction: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="DSA 方向（当时已确认）"
    )
    causal_dsa_confirmed_age_bars: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="DSA 段已持续 bar 数"
    )

    # --- confirmed_delay: 仅在确认 bar 生效的字段（4 列）---
    confirmed_delay_confirmed_swing_high: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="已确认 swing 高点"
    )
    confirmed_delay_confirmed_swing_low: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="已确认 swing 低点"
    )
    confirmed_delay_bars_since_confirmed_swing_high: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="距确认 swing 高点 bar 数"
    )
    confirmed_delay_bars_since_confirmed_swing_low: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="距确认 swing 低点 bar 数"
    )

    # --- hindsight: 允许未来信息的结构标注（6 列）---
    hindsight_dsa_finalized_segment: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="DSA 段编号（未来确认后）"
    )
    hindsight_dsa_finalized_direction: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="DSA 方向（未来确认后）"
    )
    hindsight_dsa_finalized_age_bars: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="DSA 段最终持续 bar 数"
    )
    hindsight_node_cluster_label: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="Node Cluster 结构标注"
    )
    hindsight_node_cluster_support: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="Node Cluster 支撑"
    )
    hindsight_node_cluster_resistance: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="Node Cluster 阻力"
    )

    # --- label: 未来收益/胜负标签（7 列）---
    label_future_return_5d: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="未来 5 日收益率"
    )
    label_future_return_10d: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="未来 10 日收益率"
    )
    label_future_return_20d: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="未来 20 日收益率"
    )
    label_future_max_drawdown_10d: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="未来 10 日最大回撤"
    )
    label_future_max_drawdown_20d: Mapped[float | None] = mapped_column(
        Float(), nullable=True, comment="未来 20 日最大回撤"
    )
    label_breakout_success_10d: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="未来 10 日突破成功（0/1）"
    )
    label_failure_breakdown_10d: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="未来 10 日破位失败（0/1）"
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )

    def __repr__(self) -> str:
        return (
            f"<ResearchFeatureMatrixRow(symbol={self.symbol!r}, "
            f"trade_date={self.trade_date!r})>"
        )


if __name__ == "__main__":
    # 自测入口：验证 ORM 模型映射（无副作用，不连接数据库）
    print(
        f"ResearchFeatureMatrixRun.__tablename__="
        f"{ResearchFeatureMatrixRun.__tablename__}"
    )
    run_cols = [c.name for c in ResearchFeatureMatrixRun.__table__.columns]
    print(f"run columns ({len(run_cols)}): {run_cols}")

    print(
        f"ResearchFeatureMatrixRow.__tablename__="
        f"{ResearchFeatureMatrixRow.__tablename__}"
    )
    row_cols = [c.name for c in ResearchFeatureMatrixRow.__table__.columns]
    print(f"row columns ({len(row_cols)}): {row_cols}")

    # 验证 run 表必需列
    required_run_cols = [
        "id", "run_key", "month", "start_date", "end_date", "status",
        "instruments_count", "trade_dates_count", "rows_count", "failed_count",
        "duration_seconds", "started_at", "finished_at",
        "metadata_json", "created_at", "updated_at",
    ]
    for col in required_run_cols:
        assert col in run_cols, f"run 表缺少列: {col}"
    print("run columns ✓")

    # 验证 row 表必需列（metadata + 33 features + created_at = 39）
    required_row_metadata = ["id", "run_id", "instrument_id", "symbol", "trade_date"]
    for col in required_row_metadata:
        assert col in row_cols, f"row 表缺少 metadata 列: {col}"

    # 验证 33 个 feature 列全部存在
    expected_feature_cols = [
        # causal (16)
        "causal_atr", "causal_bb_percent_b", "causal_bb_bandwidth_pct",
        "causal_sqzmom_val", "causal_sqzmom_delta_1",
        "causal_volume_ratio_20", "causal_volume_percentile_120",
        "causal_active_swing_high", "causal_active_swing_low",
        "causal_developing_swing_high", "causal_developing_swing_low",
        "causal_active_swing_dir", "causal_developing_swing_dir",
        "causal_dsa_confirmed_segment", "causal_dsa_confirmed_direction",
        "causal_dsa_confirmed_age_bars",
        # confirmed_delay (4)
        "confirmed_delay_confirmed_swing_high",
        "confirmed_delay_confirmed_swing_low",
        "confirmed_delay_bars_since_confirmed_swing_high",
        "confirmed_delay_bars_since_confirmed_swing_low",
        # hindsight (6)
        "hindsight_dsa_finalized_segment",
        "hindsight_dsa_finalized_direction",
        "hindsight_dsa_finalized_age_bars",
        "hindsight_node_cluster_label",
        "hindsight_node_cluster_support",
        "hindsight_node_cluster_resistance",
        # label (7)
        "label_future_return_5d", "label_future_return_10d",
        "label_future_return_20d",
        "label_future_max_drawdown_10d", "label_future_max_drawdown_20d",
        "label_breakout_success_10d", "label_failure_breakdown_10d",
    ]
    assert len(expected_feature_cols) == 33, (
        f"expected 33 feature cols, got {len(expected_feature_cols)}"
    )
    for col in expected_feature_cols:
        assert col in row_cols, f"row 表缺少 feature 列: {col}"
    assert "created_at" in row_cols, "row 表缺少 created_at"
    # 总列数 = 5 metadata + 33 feature + 1 created_at = 39
    assert len(row_cols) == 39, (
        f"row 表列数应为 39 (5 metadata + 33 feature + 1 created_at), got {len(row_cols)}"
    )
    print("row columns ✓")

    # 验证索引（UniqueConstraint 在 constraints，普通 Index 在 indexes）
    run_uc_names = {
        c.name
        for c in ResearchFeatureMatrixRun.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(c, UniqueConstraint) and c.name
    }
    assert "uq_research_matrix_runs_run_key" in run_uc_names, (
        f"缺少 run 唯一约束: {run_uc_names}"
    )
    run_idx_names = {
        idx.name
        for idx in ResearchFeatureMatrixRun.__table__.indexes  # type: ignore[attr-defined]
        if idx.name
    }
    assert "ix_research_matrix_runs_month" in run_idx_names, (
        f"缺少 month 索引: {run_idx_names}"
    )
    assert "ix_research_matrix_runs_status" in run_idx_names, (
        f"缺少 status 索引: {run_idx_names}"
    )

    row_uc_names = {
        c.name
        for c in ResearchFeatureMatrixRow.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(c, UniqueConstraint) and c.name
    }
    assert "uq_research_matrix_rows_inst_date" in row_uc_names, (
        f"缺少 row 唯一约束: {row_uc_names}"
    )
    row_idx_names = {
        idx.name
        for idx in ResearchFeatureMatrixRow.__table__.indexes  # type: ignore[attr-defined]
        if idx.name
    }
    assert "ix_research_matrix_rows_trade_date" in row_idx_names, (
        f"缺少 trade_date 索引: {row_idx_names}"
    )
    assert "ix_research_matrix_rows_instrument_id" in row_idx_names, (
        f"缺少 instrument_id 索引: {row_idx_names}"
    )
    assert "ix_research_matrix_rows_run_id" in row_idx_names, (
        f"缺少 run_id 索引: {row_idx_names}"
    )
    print("indexes ✓")

    # 验证枚举常量
    assert STATUS_RUNNING in ALL_STATUSES
    assert STATUS_SUCCEEDED in ALL_STATUSES
    assert STATUS_FAILED in ALL_STATUSES
    print("enums ✓")

    print("OK")
