"""竞价分析 Pydantic Schema — API 请求/响应合同。

对应 ORM 模型 auction.py，定义所有对外暴露的 DTO。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# 锚点
# ---------------------------------------------------------------------------


class AnchorItemOut(BaseModel):
    """个股锚点输出。"""

    id: uuid.UUID
    snapshot_id: uuid.UUID
    trade_date: date
    instrument_id: uuid.UUID
    # [P0-FE 2026-07-31] 个体识别字段：DTO 必须返回 symbol + name
    # 解决旧合同只回 instrument_id（UUID）导致前端无法直接展示股票代码/名称的问题
    symbol: str | None = Field(default=None, description="股票代码（如 000021）")
    name: str | None = Field(default=None, description="股票名称（如 深科技）")
    anchor_type: str = Field(description="structure/chip/composite")
    direction: str = Field(description="up/down")
    lower_price: Decimal
    upper_price: Decimal
    center_price: Decimal
    strength: float
    freshness: str
    validity: str
    price_adjustment_version: str
    structure_payload: dict[str, Any] | None = None
    chip_payload: dict[str, Any] | None = None
    distance_at_close: Decimal | None = None
    is_active: bool
    reason_codes: list[str] = Field(default_factory=list)


class AnchorSnapshotOut(BaseModel):
    """锚点快照输出。"""

    id: uuid.UUID
    trade_date: date
    source_core_run_id: uuid.UUID
    source_chip_run_id: uuid.UUID | None = None
    algorithm_version: str
    price_adjustment_version: str
    status: str
    eligible_count: int
    ready_count: int
    coverage_ratio: float
    missing_count: int
    missing_reasons: dict[str, Any] = Field(default_factory=dict)
    structure_anchor_count: int
    chip_anchor_count: int
    composite_anchor_count: int
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AnchorPublicationOut(BaseModel):
    """锚点发布指针输出。"""

    id: uuid.UUID
    trade_date: date
    snapshot_id: uuid.UUID
    algorithm_version: str
    source_core_run_id: uuid.UUID
    source_chip_run_id: uuid.UUID | None = None
    coverage_ratio: float
    published_at: datetime
    superseded_by: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# 竞价扫描
# ---------------------------------------------------------------------------


class ScanRunOut(BaseModel):
    """竞价扫描 run 输出。"""

    id: uuid.UUID
    trade_date: date
    auction_type: str = Field(description="final/opening")
    source_anchor_snapshot_id: uuid.UUID | None = None
    source_anchor_publication_id: uuid.UUID | None = None
    algorithm_version: str
    price_adjustment_version: str
    status: str
    eligible_count: int
    ready_count: int
    coverage_ratio: float
    missing_count: int
    missing_reasons: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AuctionFinalQuoteOut(BaseModel):
    """通过独立来源门禁后的最终竞价报价合同。"""

    symbol: str
    market: str
    final_price: Decimal | None
    prev_close: Decimal | None
    volume: int | None
    amount: Decimal | None
    source_timestamp: datetime | None = None
    source_server: str | None = None
    raw_payload: dict[str, Any]
    capture_time: datetime
    is_final_auction: bool


class InstrumentResultOut(BaseModel):
    """个股竞价结果输出。"""

    id: uuid.UUID
    scan_run_id: uuid.UUID
    trade_date: date
    instrument_id: uuid.UUID
    # [P0-FE 2026-07-31] 个体识别字段：DTO 必须返回 symbol + name
    symbol: str | None = Field(default=None, description="股票代码（如 000021）")
    name: str | None = Field(default=None, description="股票名称（如 深科技）")
    final_quote: AuctionFinalQuoteOut | None = None
    final_auction_price: Decimal | None = None
    prev_close: Decimal | None = None
    change_pct: float | None = None
    auction_volume: int | None = None
    auction_amount: Decimal | None = None
    relative_volume_median_20d: float | None = None
    volume_percentile: float | None = None
    atr_distance_pct: float | None = None
    is_suspended: bool = False
    is_limit_up: bool = False
    is_limit_down: bool = False
    is_ex_right: bool = False
    structure_position: str | None = None
    chip_position: str | None = None
    event_type: str | None = None
    event_lifecycle: str | None = None
    participation_level: str | None = None
    trend_background: str | None = None
    anchor_ids: list[str] | None = None
    detail_payload: dict[str, Any] | None = None
    reason_codes: list[str] = Field(default_factory=list)


class ScopeResultOut(BaseModel):
    """板块/市场竞价聚合输出。"""

    id: uuid.UUID
    scan_run_id: uuid.UUID
    trade_date: date
    scope_type: str
    scope_id: uuid.UUID | None = None
    scope_name: str | None = None
    total_count: int
    valid_count: int
    coverage_ratio: float
    open_high_count: int
    open_flat_count: int
    open_low_count: int
    median_change_pct: float | None = None
    p25_change_pct: float | None = None
    p75_change_pct: float | None = None
    equal_weight_change_pct: float | None = None
    amount_weight_change_pct: float | None = None
    structure_breakout_count: int = 0
    structure_breakdown_count: int = 0
    chip_cross_up_count: int = 0
    chip_cross_down_count: int = 0
    dual_breakout_count: int = 0
    dual_breakdown_count: int = 0
    resistance_zone_count: int = 0
    support_zone_count: int = 0
    participation_median: float | None = None
    abnormal_volume_pct: float | None = None
    top3_contribution: float | None = None
    top5_contribution: float | None = None
    hhi: float | None = None
    leader_median_gap: float | None = None
    positive_coverage: float | None = None
    negative_coverage: float | None = None
    dispersion: float | None = None
    status_label: str | None = None
    confidence_level: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)


class EventTrackingOut(BaseModel):
    """竞价事件追踪输出。"""

    id: uuid.UUID
    scan_run_id: uuid.UUID
    trade_date: date
    instrument_id: uuid.UUID
    # [P0-FE 2026-07-31] 个体识别字段：DTO 必须返回 symbol + name
    symbol: str | None = Field(default=None, description="股票代码（如 000021）")
    name: str | None = Field(default=None, description="股票名称（如 深科技）")
    event_type: str
    lifecycle: str
    anchor_id: uuid.UUID | None = None
    trigger_price: Decimal | None = None
    trigger_condition: str | None = None
    formed_at: datetime | None = None
    confirmed_at: datetime | None = None
    weakened_at: datetime | None = None
    failed_at: datetime | None = None
    expired_at: datetime | None = None
    confirmation_data: dict[str, Any] | None = None
    reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# API 请求
# ---------------------------------------------------------------------------


class AuctionMarketPageData(BaseModel):
    """`/auction` 市场级页面数据。"""

    trade_date: date
    algorithm_version: str
    publication_id: uuid.UUID | None = None
    scan_run_id: uuid.UUID | None = None
    source_core_run_id: uuid.UUID | None = None
    source_chip_run_id: uuid.UUID | None = None
    coverage: float | None = None
    reason_codes: list[str] = Field(default_factory=list)
    market_scope: ScopeResultOut | None = None
    industry_scopes: list[ScopeResultOut] = Field(default_factory=list)
    concept_scopes: list[ScopeResultOut] = Field(default_factory=list)
    top_events: list[EventTrackingOut] = Field(default_factory=list)


class AuctionBoardPageData(BaseModel):
    """`/auction/board/:board_id` 板块级页面数据。"""

    trade_date: date
    algorithm_version: str
    scan_run_id: uuid.UUID | None = None
    scope: ScopeResultOut | None = None
    top_instruments: list[InstrumentResultOut] = Field(default_factory=list)
    events: list[EventTrackingOut] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


class AuctionInstrumentPageData(BaseModel):
    """`/auction/stock/:symbol` 个股级页面数据。"""

    trade_date: date
    algorithm_version: str
    scan_run_id: uuid.UUID | None = None
    instrument_id: uuid.UUID | None = None
    anchors: list[AnchorItemOut] = Field(default_factory=list)
    result: InstrumentResultOut | None = None
    events: list[EventTrackingOut] = Field(default_factory=list)
    reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 竞价事件回流（ReviewPage 第二金字塔 + 竞价事件回流面板）
# ---------------------------------------------------------------------------


class AuctionAnchorFreshnessBucket(BaseModel):
    """锚点新鲜度分布桶（按 freshness 字段聚合）。"""

    freshness: str = Field(description="today/3d/7d/30d/stale")
    anchor_count: int
    active_count: int


class AuctionEventMigrationRow(BaseModel):
    """事件迁移行：lifecycle 转换计数。"""

    from_lifecycle: str | None = Field(
        default=None,
        description="前一生命周期（首次为 None）",
    )
    to_lifecycle: str
    event_count: int
    sample_instrument_ids: list[uuid.UUID] = Field(default_factory=list)


class AuctionBackflowData(BaseModel):
    """`/review` 页第二金字塔 + 竞价事件回流数据。

    回答四个问题（PRD §75 第二金字塔）：
    - 分布：事件按 event_type / lifecycle 分布
    - 迁移：confirmed→continued/weakened/failed/transformed/expired 的转换计数
    - 新鲜度：锚点按 freshness 桶分布
    - 集中度：top N 贡献度、HHI、龙头-中位数差距

    与 review_overview 同 trade_date 关联，由 GET /api/v1/auction/backflow/{trade_date} 暴露。
    数据来源：
    - AuctionEventTracking（事件 + lifecycle）
    - AuctionAnchorItem（新鲜度 + is_active）
    - AuctionScopeResult（集中度 HHI、top3/5、leader_median_gap）
    """

    trade_date: date
    algorithm_version: str
    scan_run_id: uuid.UUID | None = None
    anchor_publication_id: uuid.UUID | None = None
    source_core_run_id: uuid.UUID | None = None
    source_chip_run_id: uuid.UUID | None = None
    # 分布
    event_type_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="event_type → count",
    )
    lifecycle_distribution: dict[str, int] = Field(
        default_factory=dict,
        description="lifecycle → count（含 formed/confirmed/continued/weakened/failed/transformed/expired）",
    )
    # 迁移
    event_migrations: list[AuctionEventMigrationRow] = Field(default_factory=list)
    # 新鲜度
    anchor_freshness_buckets: list[AuctionAnchorFreshnessBucket] = Field(
        default_factory=list,
    )
    # 集中度（市场 scope + top3 行业）
    market_concentration: dict[str, Any] = Field(default_factory=dict)
    top_industry_concentration: list[dict[str, Any]] = Field(default_factory=list)
    # 事件回流（与 review 信号匹配的事件）
    backflow_events: list[EventTrackingOut] = Field(
        default_factory=list,
        description="当日竞价事件，按 formed_at desc 排序，限 50 条",
    )
    reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# AUCTION V3.2 — scope-first workspace contracts
# ---------------------------------------------------------------------------
class AuctionScopeListItemOut(BaseModel):
    """One row of the full family snapshot (list-first workspace).

    Every numeric field is nullable: unavailable is ``None``, never 0.
    Technical identifiers (run / publication / scope UUID) are intentionally
    absent here — they belong to the diagnostics block only.
    """

    scope_key: str = Field(description="Business scope identity (MarketBoard.externalCode)")
    scope_name: str | None = None

    # repricing
    equal_weight_gap: float | None = None
    amount_weighted_gap: float | None = None
    capital_tilt: float | None = None
    positive_gap_breadth: float | None = None
    negative_gap_breadth: float | None = None
    unchanged_gap_breadth: float | None = None
    gap_dispersion: float | None = None
    price_normalized_hhi: float | None = None

    # historical dynamics
    ew_position: float | None = None
    ew_velocity: float | None = None
    ew_acceleration: float | None = None

    # participation
    amount_historical_position: float | None = None
    amount_multiple: float | None = None
    amount_abnormal_breadth: float | None = None
    total_auction_amount: float | None = None
    normalized_hhi: float | None = None

    # cross-sectional (same-family 0..100 positions)
    cross_sectional: dict[str, float | None] = Field(default_factory=dict)

    # leadership
    leadership_migration: float | None = None

    #: Unavailable stays None (Missing != Zero) — never default to 0.
    price_valid_count: int | None = None


class AuctionScopeListOut(BaseModel):
    """COMPLETE family snapshot — never a backend Top-N slice.

    The frontend filters/sorts/paginates locally so the user can switch
    sort keys instantly inside the 09:25-09:30 window.
    """

    trade_date: date
    family: str
    algorithm_version: str
    schema_version: str
    total_scopes: int
    scopes: list[AuctionScopeListItemOut]


class AuctionScopeDetailOut(BaseModel):
    """Selected scope detail: five canonical groups + diagnostics."""

    trade_date: date
    family: str
    scope_key: str
    scope_name: str | None = None
    repricing: dict[str, Any] = Field(default_factory=dict)
    historical_dynamics: dict[str, Any] = Field(default_factory=dict)
    participation: dict[str, Any] = Field(default_factory=dict)
    cross_sectional: dict[str, Any] = Field(default_factory=dict)
    member_attribution: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class AuctionMetaDatesOut(BaseModel):
    trade_dates: list[date]
    latest: date | None = None
