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


class InstrumentResultOut(BaseModel):
    """个股竞价结果输出。"""

    id: uuid.UUID
    scan_run_id: uuid.UUID
    trade_date: date
    instrument_id: uuid.UUID
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
