"""复盘模块 ORM 模型 - 市场复盘工作台 8 张表。

对应迁移 076_market_review_workbench（PRD §5.1-5.8）：
- MarketReviewRun：某交易日完整复盘版本
- MarketReviewRunItem：范围×阶段检查点
- MarketReviewScopeSnapshot：每个范围的 P/Q/U/C/V 与证据
- MarketReviewSignal：三类偏差筛选器命中结果
- MarketReviewSignalAttribution：第二级范围下钻归因
- MarketReviewSignalInstrument：代表股票与贡献
- MarketReviewTracking：用户追踪
- MarketReviewTrackingEvaluation：逐日追踪评估结果

设计说明：
- 全部使用 UUID 主键（迁移层 server_default=gen_random_uuid()）
- 状态枚举使用 CheckConstraint 约束
- JSONB 字段用于 P/Q/U/C/V payload、证据、排序键等存储
- run→items/scope_snapshots/signals/evaluations 通过 ON DELETE CASCADE 级联
- trackings.source_signal_id / instrument_id 等可选外键使用 ON DELETE SET NULL
- signals.previous_signal_id / transformed_to_signal_id 自引用 SET NULL
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
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


class MarketReviewRun(Base):
    """复盘 run 版本 - 某交易日完整复盘版本（PRD §5.1）。

    唯一约束：trade_date + source_core_run_id + source_board_run_id +
              algorithm_version + filter_version

    状态机：
    - created → computing → partial / signals_ready → published
    - 任意阶段可失败：failed / completed_with_errors / cancelled

    使用方式：
    1. create_run：根据 (trade_date, source_core_run_id, source_board_run_id,
       algorithm_version, filter_version) 唯一键创建或复用
    2. 各阶段 worker 更新 expected/succeeded/failed_scope_count
    3. 所有 scope 完成后状态置为 signals_ready
    4. 通过 publish pointer 写入 factor_publications 后置为 published
    """

    __tablename__ = "market_review_runs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日",
    )
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="输入 stock_core snapshot_run_id（factor_publications.data_run_id）",
    )
    source_board_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment="输入 board_analysis_snapshot 的 source_core_run_id",
    )
    # [QM-63 review 依赖矩阵 2026-08-04] chip 来源 run id。
    # None 表示本次 run 未解析到 chip run（chip 不可用 → core-only 降级）。
    source_chip_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="输入 chip 共识 run id；None 表示 chip 不可用，run 降级为 core-only",
    )
    # [QM-63 review 依赖矩阵 2026-08-04] 降级原因列表（JSON 数组）。
    # 例如 chip 不可用 / auction 失败。空数组表示无降级。
    degraded_reasons: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        comment="降级原因列表（chip不可用/auction失败等）；空数组=无降级",
    )
    algorithm_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="复盘算法版本（如 review-1.0.0）",
    )
    filter_version: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="筛选器配置版本（如 filters-1.0.0）",
    )
    baseline_window: Mapped[int] = mapped_column(
        Integer(),
        nullable=False,
        default=120,
        comment="历史基线窗口（默认120，最低60）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment=(
            "状态：created/computing/partial/signals_ready/published/"
            "completed_with_errors/failed/cancelled"
        ),
    )
    expected_scope_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="期望扫描范围总数",
    )
    succeeded_scope_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="扫描成功范围数",
    )
    failed_scope_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="扫描失败范围数",
    )
    signal_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="命中信号总数",
    )
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(),
        nullable=False,
        comment="整体覆盖率 = succeeded_scope_count / expected_scope_count",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计算开始时间",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计算完成时间",
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="发布时间（写入 factor_publications 的时间）",
    )
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        comment="run 级元数据 JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "trade_date",
            "source_core_run_id",
            "source_board_run_id",
            "algorithm_version",
            "filter_version",
            name="uq_review_runs_date_core_board_algo_filter",
        ),
        Index("ix_review_runs_status", "status"),
        Index("ix_review_runs_trade_date", "trade_date"),
        CheckConstraint(
            "status IN ("
            "'created','computing','partial','signals_ready','published',"
            "'completed_with_errors','failed','cancelled'"
            ")",
            name="review_runs_status_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewRun("
            f"trade_date={self.trade_date!r}, status={self.status!r}, "
            f"algorithm_version={self.algorithm_version!r}, "
            f"filter_version={self.filter_version!r}, "
            f"coverage_ratio={self.coverage_ratio!r})>"
        )


class MarketReviewRunItem(Base):
    """复盘范围×阶段检查点（PRD §5.2）。

    每个范围（scope_type + scope_key）的每个阶段（phase）独立检查点：
    - metrics：P/Q/U/C/V 计算
    - signals：偏差筛选器评估
    - attribution：第二级下钻与个股归因
    - tracking：追踪评估

    唯一约束：review_run_id + scope_type + scope_key + phase

    幂等规则：
    - 相同 input_hash + 版本的 succeeded item 不重算
    - 重启只处理 pending / 可重试 failed / 过期 running
    - lease_epoch + lease_expires_at 用于并发 claim
    """

    __tablename__ = "market_review_run_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_runs.id",
    )
    scope_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment=(
            "范围类型：market/major_index/style/industry_l1/"
            "industry_l2/industry_l3/concept/instrument"
        ),
    )
    scope_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围标识（如 industry_l1 的行业代码）",
    )
    phase: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="阶段：metrics/signals/attribution/tracking",
    )
    status: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="状态：pending/running/succeeded/failed/skipped",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0, comment="尝试次数",
    )
    input_hash: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment="输入 hash（用于幂等校验，相同 hash+版本不重算）",
    )
    lease_epoch: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="租约 epoch（并发 claim 用）",
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="租约过期时间",
    )
    last_error: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="最近错误信息（status=failed 时填充）",
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="阶段开始时间",
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="阶段完成时间",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            "phase",
            name="uq_review_items_run_scope_phase",
        ),
        Index("ix_review_items_run_status", "review_run_id", "status"),
        Index("ix_review_items_scope", "scope_type", "scope_key"),
        CheckConstraint(
            "phase IN ('metrics','signals','attribution','tracking')",
            name="review_items_phase_check",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped')",
            name="review_items_status_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewRunItem("
            f"review_run_id={self.review_run_id!r}, "
            f"scope_type={self.scope_type!r}, scope_key={self.scope_key!r}, "
            f"phase={self.phase!r}, status={self.status!r})>"
        )


class MarketReviewScopeSnapshot(Base):
    """复盘范围 P/Q/U/C/V 快照（PRD §5.3）。

    保存每个市场范围的聚合变量 P（价格表现强度）、Q（内部结构质量）、
    U（参与范围）、C（集中程度）、V（成交活跃与效率）当前值、变化与历史分位。

    唯一约束：review_run_id + scope_type + scope_key

    P/Q/U/C/V payload 结构见 PRD §7.1，包含 value/rawValue/delta1d/delta5d/
    historyPercentile120d/crossSectionPercentile/components/coverage/status。
    """

    __tablename__ = "market_review_scope_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_runs.id",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日",
    )
    scope_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围类型",
    )
    scope_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围标识",
    )
    scope_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围名称（冗余存储，便于查询展示）",
    )
    parent_scope_type: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="父范围类型（下钻时填充）",
    )
    parent_scope_key: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="父范围标识",
    )
    source_board_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("board_analysis_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联 board_analysis_snapshots.id（行业/概念范围填充）",
    )
    taxonomy_version: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="该交易日有效 taxonomy version",
    )
    taxonomy_compatibility_key: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="taxonomy 兼容序列键",
    )
    membership_version: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="该交易日有效 membership version",
    )
    eligible_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="范围成员总数",
    )
    ready_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, comment="有效成员数",
    )
    coverage_ratio: Mapped[Decimal] = mapped_column(
        Numeric(),
        nullable=False,
        comment="覆盖率 = ready_count / eligible_count",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="快照状态：ready/insufficient_history/partial/unavailable",
    )
    p_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="P 价格表现强度 payload（PRD §7.2）",
    )
    q_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="Q 内部结构质量 payload（PRD §7.3）",
    )
    u_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="U 参与范围 payload（PRD §7.4）",
    )
    c_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="C 集中程度 payload（PRD §7.5）",
    )
    v_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="V 成交活跃与效率 payload（PRD §7.6）",
    )
    data_quality_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="数据质量明细 JSON",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            name="uq_review_scope_snapshots_run_scope",
        ),
        Index("ix_review_scope_snapshots_run_type", "review_run_id", "scope_type"),
        Index("ix_review_scope_snapshots_date_type", "trade_date", "scope_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewScopeSnapshot("
            f"review_run_id={self.review_run_id!r}, "
            f"scope_type={self.scope_type!r}, scope_key={self.scope_key!r}, "
            f"status={self.status!r}, coverage_ratio={self.coverage_ratio!r})>"
        )


class MarketReviewMetricObservation(Base):
    """Versioned raw metric observation used for PIT historical normalization."""

    __tablename__ = "market_review_metric_observations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    scope_type: Mapped[str] = mapped_column(Text(), nullable=False)
    scope_key: Mapped[str] = mapped_column(Text(), nullable=False)
    metric_code: Mapped[str] = mapped_column(Text(), nullable=False)
    component_name: Mapped[str] = mapped_column(Text(), nullable=False)
    raw_value: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    denominator: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    field_source_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    weight_mode: Mapped[str] = mapped_column(Text(), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    input_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    membership_version: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "review_run_id",
            "scope_type",
            "scope_key",
            "metric_code",
            "component_name",
            name="uq_review_metric_observation_run_scope_component",
        ),
        Index(
            "ix_review_metric_observation_history",
            "scope_type",
            "scope_key",
            "algorithm_version",
            "metric_code",
            "component_name",
            "trade_date",
        ),
    )


class MarketReviewSignal(Base):
    """复盘三类偏差筛选器命中信号（PRD §5.4）。

    三类筛选器（PRD §8）：
    - A 类：表面表现与内部质量偏差（surface_strong_internal_weak 等）
    - B 类：当前状态与变化速度偏差（high_level_slowing 等）
    - C 类：成交、参与与集中度偏差（volume_without_breadth 等）

    信号生命周期（PRD §10.1）：
    new → continuing → confirmed / weakened / invalidated / transformed

    唯一约束：review_run_id + filter_family + signal_type + scope_type + scope_key

    排序键（rank_key）包含：偏差历史分位、当日变化分位、持续日数、
    coverage、scope_type 优先级、scope_name 稳定第二键。
    """

    __tablename__ = "market_review_signals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_runs.id",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日",
    )
    filter_family: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="筛选器族：A/B/C",
    )
    signal_type: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="信号类型（如 surface_strong_internal_weak）",
    )
    scope_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围类型",
    )
    scope_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围标识",
    )
    scope_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="范围名称（冗余存储）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment=(
            "信号生命周期状态："
            "new/continuing/confirmed/weakened/invalidated/transformed"
        ),
    )
    first_seen_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="信号首次出现日期",
    )
    previous_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_signals.id", ondelete="SET NULL"),
        nullable=True,
        comment="前一交易日同 scope 同 signal_type 的信号 ID",
    )
    transformed_to_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_signals.id", ondelete="SET NULL"),
        nullable=True,
        comment="转化后的新信号 ID（status=transformed 时填充）",
    )
    trigger_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="触发条件 payload",
    )
    baseline_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="基线 payload",
    )
    evidence_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="证据 payload",
    )
    confirmation_rule: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="确认规则 JSON",
    )
    invalidation_rule: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="失效规则 JSON",
    )
    coverage_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True, comment="覆盖率",
    )
    rank_key: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment=(
            "排序键 JSON：偏差历史分位/当日变化分位/持续日数/coverage/"
            "scope_type 优先级/scope_name"
        ),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "review_run_id",
            "filter_family",
            "signal_type",
            "scope_type",
            "scope_key",
            name="uq_review_signals_run_family_type_scope",
        ),
        Index("ix_review_signals_run_scope", "review_run_id", "scope_type"),
        Index("ix_review_signals_run_family", "review_run_id", "filter_family"),
        Index("ix_review_signals_date_status", "trade_date", "status"),
        Index("ix_review_signals_scope", "scope_type", "scope_key"),
        CheckConstraint(
            "filter_family IN ('A','B','C','D')",
            name="review_signals_filter_family_check",
        ),
        CheckConstraint(
            "status IN ("
            "'new','continuing','confirmed','weakened','invalidated','transformed'"
            ")",
            name="review_signals_status_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewSignal("
            f"review_run_id={self.review_run_id!r}, "
            f"filter_family={self.filter_family!r}, "
            f"signal_type={self.signal_type!r}, status={self.status!r}, "
            f"scope_type={self.scope_type!r}, scope_key={self.scope_key!r})>"
        )


class MarketReviewSignalAttribution(Base):
    """复盘信号子范围归因（PRD §5.5）。

    保存第二级范围下钻结果，对每个命中信号：
    - 找到父范围的直接子范围和关联概念
    - 计算子范围对父范围 P/Q/U/C/V 变化的贡献
    - 保留正贡献和负贡献，按绝对贡献排序
    - API 支持分页读取全部

    归因不得仅按涨幅排序（PRD §9.1）。
    """

    __tablename__ = "market_review_signal_attributions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_signals.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_signals.id",
    )
    child_scope_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="子范围类型",
    )
    child_scope_key: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="子范围标识",
    )
    child_scope_name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="子范围名称（冗余存储）",
    )
    relation_type: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="与父范围关系类型",
    )
    contribution_value: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True, comment="贡献值（可正可负）",
    )
    contribution_rank: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="贡献排名（按绝对贡献排序）",
    )
    metrics_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="子范围指标 payload",
    )
    evidence_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="证据 payload",
    )
    coverage_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True, comment="覆盖率",
    )
    source_board_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("board_analysis_snapshots.id", ondelete="SET NULL"),
        nullable=True,
        comment="子范围对应的 Board snapshot",
    )
    taxonomy_version: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="子范围 taxonomy version",
    )
    taxonomy_compatibility_key: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="子范围 taxonomy 兼容键",
    )
    membership_version: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="子范围 PIT membership version",
    )
    eligible_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="子范围 PIT eligible_count",
    )
    ready_count: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="子范围 ready_count",
    )
    data_quality_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="子范围 readiness/coverage 质量证据",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_review_attributions_signal", "signal_id"),
        Index(
            "ix_review_attributions_signal_rank",
            "signal_id",
            "contribution_rank",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewSignalAttribution("
            f"signal_id={self.signal_id!r}, "
            f"child_scope_type={self.child_scope_type!r}, "
            f"child_scope_key={self.child_scope_key!r}, "
            f"contribution_value={self.contribution_value!r}, "
            f"contribution_rank={self.contribution_rank!r})>"
        )


class MarketReviewSignalInstrument(Base):
    """复盘信号代表股票与贡献（PRD §5.6）。

    每只成员股票对信号的贡献（PRD §9.2）：
    - 对 P 的表面变化贡献
    - 对 Q 的趋势/结构/动量贡献
    - 对 U 的参与确认
    - 对 C 的集中度贡献
    - 对 V 的成交贡献
    - 新鲜结构/动量事件
    - 与板块状态的关系

    board_role 枚举：core/second_line/elasticity/follower/laggard/unclassified
    relation_to_scope 枚举：
    - synchronized_strengthening（同步增强）
    - synchronized_weakening（同步减弱）
    - instrument_leads_scope（个股领先板块）
    - scope_strong_instrument_lags（板块强个股滞后）
    - instrument_strong_scope_unsupported（个股强板块无支撑）
    - unconfirmed（未确认）
    """

    __tablename__ = "market_review_signal_instruments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_signals.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_signals.id",
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id"),
        nullable=False,
        comment="关联 instruments.id",
    )
    symbol: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="股票代码（冗余存储）",
    )
    name: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="股票名称（冗余存储）",
    )
    board_role: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment=(
            "板块角色：core/second_line/elasticity/follower/laggard/unclassified"
        ),
    )
    relation_to_scope: Mapped[str | None] = mapped_column(
        Text(),
        nullable=True,
        comment=(
            "与板块关系：synchronized_strengthening/synchronized_weakening/"
            "instrument_leads_scope/scope_strong_instrument_lags/"
            "instrument_strong_scope_unsupported/unconfirmed"
        ),
    )
    contribution_value: Mapped[Decimal | None] = mapped_column(
        Numeric(), nullable=True, comment="贡献值",
    )
    contribution_rank: Mapped[int | None] = mapped_column(
        Integer(), nullable=True, comment="贡献排名",
    )
    first_pyramid_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="第一金字塔 payload（趋势/结构/动量/筹码）",
    )
    fresh_events_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="新鲜事件 payload",
    )
    contribution_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="P/Q/U/C/V 分项贡献与分母",
    )
    role_evidence: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="角色判定结构化证据",
    )
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        comment="来源单股快照 ID（stock_feature_snapshots.id）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        Index("ix_review_instruments_signal", "signal_id"),
        Index(
            "ix_review_instruments_signal_rank",
            "signal_id",
            "contribution_rank",
        ),
        Index("ix_review_instruments_instrument", "instrument_id"),
        CheckConstraint(
            "board_role IN ("
            "'core','second_line','elasticity','follower','laggard','unclassified'"
            ")",
            name="review_instruments_board_role_check",
        ),
        CheckConstraint(
            "relation_to_scope IN ("
            "'synchronized_strengthening','synchronized_weakening',"
            "'instrument_leads_scope','scope_strong_instrument_lags',"
            "'instrument_strong_scope_unsupported','unconfirmed'"
            ")",
            name="review_instruments_relation_to_scope_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewSignalInstrument("
            f"signal_id={self.signal_id!r}, symbol={self.symbol!r}, "
            f"board_role={self.board_role!r}, "
            f"relation_to_scope={self.relation_to_scope!r}, "
            f"contribution_rank={self.contribution_rank!r})>"
        )


class MarketReviewTracking(Base):
    """复盘用户追踪（PRD §5.7）。

    用户可以追踪：
    - signal：一条信号（source_signal_id 填充）
    - scope：一个命中范围（scope_type/scope_key 填充）
    - instrument：一只代表股票（instrument_id 填充）

    状态机：active → confirmed / invalidated / closed
    用户关闭追踪不删除历史（status=closed，closed_at 填充）。
    """

    __tablename__ = "market_review_trackings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 users.id",
    )
    source_signal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_signals.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联 market_review_signals.id（追踪 signal 时填充）",
    )
    tracking_type: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="追踪类型：signal/scope/instrument",
    )
    scope_type: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="范围类型（追踪 scope 时填充）",
    )
    scope_key: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="范围标识（追踪 scope 时填充）",
    )
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联 instruments.id（追踪 instrument 时填充）",
    )
    status: Mapped[str] = mapped_column(
        Text(),
        nullable=False,
        comment="状态：active/confirmed/invalidated/closed",
    )
    confirmation_conditions: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="确认条件 JSON",
    )
    invalidation_conditions: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="失效条件 JSON",
    )
    note: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="用户备注",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="关闭时间（status=closed 时填充）",
    )

    __table_args__ = (
        Index("ix_review_trackings_user_status", "user_id", "status"),
        Index("ix_review_trackings_signal", "source_signal_id"),
        Index("ix_review_trackings_instrument", "instrument_id"),
        CheckConstraint(
            "tracking_type IN ('signal','scope','instrument')",
            name="review_trackings_tracking_type_check",
        ),
        CheckConstraint(
            "status IN ('active','confirmed','invalidated','closed')",
            name="review_trackings_status_check",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewTracking("
            f"user_id={self.user_id!r}, tracking_type={self.tracking_type!r}, "
            f"status={self.status!r})>"
        )


class MarketReviewTrackingEvaluation(Base):
    """复盘逐日追踪评估结果（PRD §5.8）。

    每天 Review Run 完成后自动生成 evaluation（PRD §10.2）。
    用户关闭追踪不删除历史 evaluation。

    唯一约束：tracking_id + trade_date

    previous_state / current_state 记录信号生命周期状态变化：
    new / continuing / confirmed / weakened / invalidated / transformed
    """

    __tablename__ = "market_review_tracking_evaluations"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    tracking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_trackings.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_trackings.id",
    )
    review_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("market_review_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="关联 market_review_runs.id",
    )
    trade_date: Mapped[date] = mapped_column(
        Date(), nullable=False, comment="业务交易日",
    )
    previous_state: Mapped[str | None] = mapped_column(
        Text(), nullable=True, comment="前一交易日状态",
    )
    current_state: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="当日状态",
    )
    evaluation_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="评估 payload（证据与触发条件检查结果）",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "tracking_id",
            "trade_date",
            name="uq_review_evaluations_tracking_date",
        ),
        Index("ix_review_evaluations_run", "review_run_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<MarketReviewTrackingEvaluation("
            f"tracking_id={self.tracking_id!r}, trade_date={self.trade_date!r}, "
            f"previous_state={self.previous_state!r}, "
            f"current_state={self.current_state!r})>"
        )


if __name__ == "__main__":
    # 自测：验证 8 表字段与迁移定义一致
    _models = [
        ("market_review_runs", MarketReviewRun),
        ("market_review_run_items", MarketReviewRunItem),
        ("market_review_scope_snapshots", MarketReviewScopeSnapshot),
        ("market_review_signals", MarketReviewSignal),
        ("market_review_signal_attributions", MarketReviewSignalAttribution),
        ("market_review_signal_instruments", MarketReviewSignalInstrument),
        ("market_review_trackings", MarketReviewTracking),
        ("market_review_tracking_evaluations", MarketReviewTrackingEvaluation),
    ]

    _expected_cols: dict[str, set[str]] = {
        "market_review_runs": {
            "id", "trade_date", "source_core_run_id", "source_board_run_id",
            "source_chip_run_id", "degraded_reasons",
            "algorithm_version", "filter_version", "baseline_window", "status",
            "expected_scope_count", "succeeded_scope_count", "failed_scope_count",
            "signal_count", "coverage_ratio",
            "started_at", "completed_at", "published_at", "metadata_json",
            "created_at", "updated_at",
        },
        "market_review_run_items": {
            "id", "review_run_id", "scope_type", "scope_key", "phase", "status",
            "attempt_count", "input_hash", "lease_epoch", "lease_expires_at",
            "last_error", "started_at", "completed_at", "created_at", "updated_at",
        },
        "market_review_scope_snapshots": {
            "id", "review_run_id", "trade_date", "scope_type", "scope_key",
            "scope_name", "parent_scope_type", "parent_scope_key",
            "source_board_snapshot_id", "taxonomy_version",
            "taxonomy_compatibility_key", "membership_version",
            "eligible_count", "ready_count", "coverage_ratio", "status",
            "p_payload", "q_payload", "u_payload", "c_payload", "v_payload",
            "data_quality_json", "created_at", "updated_at",
        },
        "market_review_signals": {
            "id", "review_run_id", "trade_date", "filter_family", "signal_type",
            "scope_type", "scope_key", "scope_name", "status", "first_seen_date",
            "previous_signal_id", "transformed_to_signal_id",
            "trigger_payload", "baseline_payload", "evidence_payload",
            "confirmation_rule", "invalidation_rule", "coverage_ratio",
            "rank_key", "created_at", "updated_at",
        },
        "market_review_signal_attributions": {
            "id", "signal_id", "child_scope_type", "child_scope_key",
            "child_scope_name", "relation_type", "contribution_value",
            "contribution_rank", "metrics_payload", "evidence_payload",
            "coverage_ratio", "source_board_snapshot_id", "taxonomy_version",
            "taxonomy_compatibility_key", "membership_version",
            "eligible_count", "ready_count", "data_quality_json", "created_at",
        },
        "market_review_signal_instruments": {
            "id", "signal_id", "instrument_id", "symbol", "name",
            "board_role", "relation_to_scope", "contribution_value",
            "contribution_rank", "first_pyramid_payload", "fresh_events_payload",
            "contribution_payload", "role_evidence", "source_snapshot_id", "created_at",
        },
        "market_review_trackings": {
            "id", "user_id", "source_signal_id", "tracking_type",
            "scope_type", "scope_key", "instrument_id", "status",
            "confirmation_conditions", "invalidation_conditions", "note",
            "created_at", "closed_at",
        },
        "market_review_tracking_evaluations": {
            "id", "tracking_id", "review_run_id", "trade_date",
            "previous_state", "current_state", "evaluation_payload", "created_at",
        },
    }

    for table_name, model_cls in _models:
        assert model_cls.__tablename__ == table_name, (
            f"{model_cls.__name__} tablename 不匹配: "
            f"expected={table_name}, actual={model_cls.__tablename__}"
        )
        actual_cols = {c.name for c in model_cls.__table__.columns}
        expected_cols = _expected_cols[table_name]
        assert actual_cols == expected_cols, (
            f"{model_cls.__name__} 字段不匹配: "
            f"missing={expected_cols - actual_cols}, "
            f"extra={actual_cols - expected_cols}"
        )
        print(f"OK: {model_cls.__name__} ({table_name}) columns verified")

    # 验证关键 unique 约束
    _expected_uq: dict[str, set[str]] = {
        "MarketReviewRun": {"uq_review_runs_date_core_board_algo_filter"},
        "MarketReviewRunItem": {"uq_review_items_run_scope_phase"},
        "MarketReviewScopeSnapshot": {"uq_review_scope_snapshots_run_scope"},
        "MarketReviewSignal": {"uq_review_signals_run_family_type_scope"},
        "MarketReviewTrackingEvaluation": {"uq_review_evaluations_tracking_date"},
    }
    for model_name, expected_set in _expected_uq.items():
        model_cls = next(m for _, m in _models if m.__name__ == model_name)
        actual_uq = {
            c.name for c in model_cls.__table__.constraints  # type: ignore[attr-defined]
            if c.name and c.name.startswith("uq_")
        }
        missing = expected_set - actual_uq
        assert not missing, f"{model_name} 缺少 unique 约束: {missing}"
    print("OK: unique constraints verified")

    # 验证关键 CheckConstraint
    _expected_checks: dict[str, set[str]] = {
        "MarketReviewRun": {"review_runs_status_check"},
        "MarketReviewRunItem": {
            "review_items_phase_check", "review_items_status_check",
        },
        "MarketReviewSignal": {
            "review_signals_filter_family_check",
            "review_signals_status_check",
        },
        "MarketReviewSignalInstrument": {
            "review_instruments_board_role_check",
            "review_instruments_relation_to_scope_check",
        },
        "MarketReviewTracking": {
            "review_trackings_tracking_type_check",
            "review_trackings_status_check",
        },
    }
    for model_name, expected_set in _expected_checks.items():
        model_cls = next(m for _, m in _models if m.__name__ == model_name)
        actual_checks = {
            c.name for c in model_cls.__table__.constraints  # type: ignore[attr-defined]
            if c.name and c.name.endswith("_check")
        }
        missing = expected_set - actual_checks
        assert not missing, f"{model_name} 缺少 check 约束: {missing}"
    print("OK: check constraints verified")

    print(f"OK: all {len(_models)} review models verified")
