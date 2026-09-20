"""Market Dashboard 只读 API 响应模型（PRD：Dashboard 只读投影）。

只暴露派生事实（breadth ratio / EW index / ranking / compare），不暴露 count 细节，
不携带 sentiment/bull-bear/signal/PCR 等产品合同外字段。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MarketDashboardCard(BaseModel):
    ma5: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    equal_weight_index: float | None = None


class MarketDashboardPoint(BaseModel):
    trade_date: str
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma120: float | None = None
    ew_index: float | None = None


class MarketDashboardResponse(BaseModel):
    projection_trade_date: str | None = None
    cards: MarketDashboardCard
    series: list[MarketDashboardPoint] = Field(default_factory=list)


class BreadthPair(BaseModel):
    ma5: float | None = None
    ma10: float | None = None


class RankingItem(BaseModel):
    board_id: str
    board_name: str
    board_type: str
    hierarchy_level: str
    current: BreadthPair
    previous: BreadthPair
    delta: BreadthPair


class RankingsResponse(BaseModel):
    top: list[RankingItem] = Field(default_factory=list)
    bottom: list[RankingItem] = Field(default_factory=list)
    lookback: int


class ScopeMetadata(BaseModel):
    board_id: str
    name: str
    type: str
    hierarchy_level: str
    membership_version: str


class ScopePoint(BaseModel):
    trade_date: str
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma120: float | None = None
    ew_index: float | None = None


class ScopeDetailResponse(BaseModel):
    projection_trade_date: str | None = None
    metadata: ScopeMetadata
    series: list[ScopePoint] = Field(default_factory=list)


class ComparePoint(BaseModel):
    trade_date: str
    ew_index: float | None = None


class CompareBoard(BaseModel):
    board_id: str
    board_name: str
    board_type: str
    points: list[ComparePoint] = Field(default_factory=list)


class CompareResponse(BaseModel):
    boards: list[CompareBoard] = Field(default_factory=list)
