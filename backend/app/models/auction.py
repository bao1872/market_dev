"""竞价分析 ORM 模型 — 锚点、扫描、聚合、事件追踪。

对应迁移 077_auction_analysis 中的 7 张表。
设计原则：
- 所有表含 trade_date、algorithm_version、source_core_run_id
- coverage、status、reason_codes 标准化
- 唯一键保证幂等
- lifecycle: formed → confirmed → continued/weakened → failed/transformed/expired
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AuctionAnchorSnapshot(Base):
    """每日锚点快照（run 级状态）。

    使用方式：
    1. 计算开始：upsert 一条 status=running 记录
    2. 计算完成：更新 status=succeeded/structure_only/failed
    3. 发布：写入 auction_anchor_publications
    4. 读请求：先查 publications 获取 snapshot_id，再查本表
    """

    __tablename__ = "auction_anchor_snapshots"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_chip_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    price_adjustment_version: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False,
                                        comment="running/succeeded/failed/partial/structure_only")
    eligible_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    ready_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    missing_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    missing_reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    structure_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    chip_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    composite_anchor_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<AuctionAnchorSnapshot(trade_date={self.trade_date!r}, "
                f"status={self.status!r}, coverage={self.coverage_ratio:.3f})>")


class AuctionAnchorItem(Base):
    """个股锚点（structure/chip/composite）。

    anchor_type=structure: 来源第一金字塔结构维度
    anchor_type=chip: 来源筹码共识维度
    anchor_type=composite: 近距离结构+筹码合并为复合锚点

    [P0-6/P0-7 修复 2026-07-31] 唯一键改为 (snapshot_id, instrument_id, anchor_key)，
    保存同方向同类型的多个 OB/BOS；source 拆为 source_kind (core/chip) 和 source_run_id。
    """

    __tablename__ = "auction_anchor_items"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_anchor_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    anchor_type: Mapped[str] = mapped_column(Text(), nullable=False,
                                              comment="structure/chip/composite")
    anchor_key: Mapped[str] = mapped_column(
        Text(), nullable=False,
        comment="同股同 snapshot 内唯一键：bos_<event_id>/ob_<event_id>/poc/...",
    )
    anchor_subtype: Mapped[str | None] = mapped_column(
        Text(), nullable=True,
        comment="bos/choch/ob_created/trailing_top/trailing_bottom/poc/vah/val/cross/composite",
    )
    source_kind: Mapped[str] = mapped_column(
        Text(), nullable=False, comment="core/chip（composite 取 core）",
    )
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False,
        comment="source_core_run_id 或 source_chip_run_id",
    )
    source_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True,
        comment="关联结构/筹码事件 ID（如有）",
    )
    source_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="锚点来源事件时间（occurredAt 等）",
    )
    direction: Mapped[str] = mapped_column(Text(), nullable=False, comment="up/down")
    lower_price: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False)
    upper_price: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False)
    center_price: Mapped[Any] = mapped_column(Numeric(12, 4), nullable=False)
    strength: Mapped[float] = mapped_column(Float(), nullable=False, comment="0.0-1.0")
    priority_rank: Mapped[int | None] = mapped_column(
        Integer(), nullable=True,
        comment="活跃锚点优先级（lower=higher priority，扫描时按此排序）",
    )
    freshness: Mapped[str] = mapped_column(Text(), nullable=False,
                                           comment="fresh/stale/expired")
    validity: Mapped[str] = mapped_column(Text(), nullable=False,
                                           comment="valid/invalid/invalidated")
    price_adjustment_version: Mapped[str] = mapped_column(Text(), nullable=False)
    structure_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    chip_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    distance_at_close: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (f"<AuctionAnchorItem(instrument_id={self.instrument_id!r}, "
                f"type={self.anchor_type!r}, key={self.anchor_key!r}, "
                f"dir={self.direction!r}, strength={self.strength:.2f}, active={self.is_active})>")


class AuctionAnchorPublication(Base):
    """锚点发布指针。

    唯一键 (trade_date, algorithm_version) 保证每日只发布一个版本。
    superseded_by 指向新版本（重算时）。
    """

    __tablename__ = "auction_anchor_publications"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_anchor_snapshots.id", ondelete="CASCADE"),
        nullable=False,
    )
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    source_core_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_chip_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (f"<AuctionAnchorPublication(trade_date={self.trade_date!r}, "
                f"snapshot_id={self.snapshot_id!r}, coverage={self.coverage_ratio:.3f})>")


class AuctionScanRun(Base):
    """竞价扫描 run（最终竞价/开盘验证）。

    auction_type=final: 次日最终竞价扫描
    auction_type=opening: 开盘后验证
    """

    __tablename__ = "auction_scan_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    auction_type: Mapped[str] = mapped_column(Text(), nullable=False,
                                               comment="final/opening")
    source_anchor_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_anchor_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_anchor_publication_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_anchor_publications.id", ondelete="SET NULL"),
        nullable=True,
    )
    algorithm_version: Mapped[str] = mapped_column(Text(), nullable=False)
    price_adjustment_version: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False,
                                        comment="queued/running/succeeded/failed/partial")
    attempt_count: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=1, server_default="1",
        comment="尝试次数（succeeded/running 租约有效时不递增；failed/partial 重试递增）",
    )
    eligible_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    ready_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    missing_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    missing_reasons: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    lease_epoch: Mapped[int | None] = mapped_column(
        Integer(), nullable=True,
        comment="lease fencing epoch（旧 Worker 写入被拒绝）",
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<AuctionScanRun(trade_date={self.trade_date!r}, "
                f"type={self.auction_type!r}, status={self.status!r})>")


class AuctionInstrumentResult(Base):
    """个股竞价结果。

    包含竞价数据（价格/量额/参与度）和位置分析（结构/筹码位置、事件类型）。
    """

    __tablename__ = "auction_instrument_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    final_auction_price: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    prev_close: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    auction_volume: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    auction_amount: Mapped[Any | None] = mapped_column(Numeric(18, 2), nullable=True)
    relative_volume_median_20d: Mapped[float | None] = mapped_column(Float(), nullable=True)
    volume_percentile: Mapped[float | None] = mapped_column(Float(), nullable=True)
    atr_distance_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    is_suspended: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_limit_up: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_limit_down: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_ex_right: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    structure_position: Mapped[str | None] = mapped_column(Text(), nullable=True)
    chip_position: Mapped[str | None] = mapped_column(Text(), nullable=True)
    event_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    event_lifecycle: Mapped[str | None] = mapped_column(Text(), nullable=True)
    participation_level: Mapped[str | None] = mapped_column(Text(), nullable=True)
    trend_background: Mapped[str | None] = mapped_column(Text(), nullable=True)
    anchor_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    detail_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (f"<AuctionInstrumentResult(instrument_id={self.instrument_id!r}, "
                f"event={self.event_type!r}, lifecycle={self.event_lifecycle!r})>")


class AuctionScopeResult(Base):
    """板块/市场竞价聚合。

    scope_type=market: 全市场聚合
    scope_type=industry: 行业聚合
    scope_type=concept: 概念聚合
    """

    __tablename__ = "auction_scope_results"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    scope_type: Mapped[str] = mapped_column(Text(), nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    scope_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    total_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    valid_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage_ratio: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    open_high_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    open_flat_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    open_low_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    median_change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    p25_change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    p75_change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    equal_weight_change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    amount_weight_change_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    structure_breakout_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    structure_breakdown_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    chip_cross_up_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    chip_cross_down_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    dual_breakout_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    dual_breakdown_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    resistance_zone_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    support_zone_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    participation_median: Mapped[float | None] = mapped_column(Float(), nullable=True)
    abnormal_volume_pct: Mapped[float | None] = mapped_column(Float(), nullable=True)
    top3_contribution: Mapped[float | None] = mapped_column(Float(), nullable=True)
    top5_contribution: Mapped[float | None] = mapped_column(Float(), nullable=True)
    hhi: Mapped[float | None] = mapped_column(Float(), nullable=True)
    leader_median_gap: Mapped[float | None] = mapped_column(Float(), nullable=True)
    positive_coverage: Mapped[float | None] = mapped_column(Float(), nullable=True)
    negative_coverage: Mapped[float | None] = mapped_column(Float(), nullable=True)
    dispersion: Mapped[float | None] = mapped_column(Float(), nullable=True)
    status_label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(Text(), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (f"<AuctionScopeResult(scope_type={self.scope_type!r}, "
                f"scope_name={self.scope_name!r}, label={self.status_label!r})>")


class AuctionEventTracking(Base):
    """竞价事件生命周期追踪。

    lifecycle: formed → confirmed → continued/weakened → failed/transformed/expired
    开盘后验证更新 lifecycle 并回流 Review tracking。
    """

    __tablename__ = "auction_event_trackings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_scan_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(Text(), nullable=False)
    lifecycle: Mapped[str] = mapped_column(Text(), nullable=False,
                                            comment="formed/confirmed/continued/weakened/failed/transformed/expired")
    anchor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trigger_price: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    trigger_condition: Mapped[str | None] = mapped_column(Text(), nullable=True)
    formed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    continued_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="confirmed 后开盘窗口维持触发时记录",
    )
    weakened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    transformed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="板块扩散失败/龙头孤立/指数背离等结构性变化时记录",
    )
    expired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<AuctionEventTracking(instrument_id={self.instrument_id!r}, "
                f"event={self.event_type!r}, lifecycle={self.lifecycle!r})>")


class AuctionQuoteCaptureRun(Base):
    """竞价行情采集 run（[CHANGE-20260731-001] 数据源合同）。

    每次 09:25:05 触发创建一条 run，记录 expected/received/valid/coverage。
    test_namespace 用于隔离 Canary 数据与正式生产数据。
    """

    __tablename__ = "auction_quote_capture_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    source: Mapped[str] = mapped_column(Text(), nullable=False,
                                        comment="mootdx/tushare")
    test_namespace: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False,
                                        comment="running/succeeded/failed/partial")
    expected_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    received_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    valid_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    coverage: Mapped[float] = mapped_column(Float(), nullable=False, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="最近心跳时间，用于 fencing 判定僵尸 run",
    )
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    code_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, onupdate=func.now())

    def __repr__(self) -> str:
        return (f"<AuctionQuoteCaptureRun(trade_date={self.trade_date!r}, "
                f"source={self.source!r}, status={self.status!r}, coverage={self.coverage})>")


class AuctionFinalQuote(Base):
    """个股最终竞价报价（[CHANGE-20260731-001] 数据源合同）。

    09:25:05 后从 mootdx/pytdx 实时行情写入。
    auction_scan_service 从此表读取，不再依赖 bars_minute。
    """

    __tablename__ = "auction_final_quotes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trade_date: Mapped[date] = mapped_column(Date(), nullable=False)
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("instruments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    capture_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("auction_quote_capture_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    test_namespace: Mapped[str] = mapped_column(Text(), nullable=False)
    source: Mapped[str] = mapped_column(Text(), nullable=False)
    source_server: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    final_price: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    prev_close: Mapped[Any | None] = mapped_column(Numeric(12, 4), nullable=True)
    volume: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    amount: Mapped[Any | None] = mapped_column(Numeric(18, 2), nullable=True)
    matched_volume: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    unmatched_volume: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    is_final: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default="false")
    quality_status: Mapped[str] = mapped_column(Text(), nullable=False, server_default="'ok'",
                                                comment="ok/suspended/zero_volume/missing_field/api_error/limit_up/limit_down")
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def __repr__(self) -> str:
        return (f"<AuctionFinalQuote(instrument_id={self.instrument_id!r}, "
                f"trade_date={self.trade_date!r}, final_price={self.final_price}, "
                f"quality={self.quality_status!r})>")
