"""Market Dashboard 只读 API 响应模型（PRD：Dashboard 只读投影）。

只暴露派生事实（breadth ratio / EW index / ranking / compare），不暴露 count 细节，
不携带 sentiment/bull-bear/signal/PCR 等产品合同外字段。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MarketDashboardCard(BaseModel):
    """大盘快照卡。

    - 既有 MA5 / MA20 / MA50 / 全市场 EW index：供前端「市场宽度」chart header KPI chips 复用。
    - [PANJI-MARKET-OVERVIEW] 新增强势 6 卡：三大指数（点位 + 当日涨跌幅）+ 涨跌家数 + 全市场成交额
      + 涨停/跌停家数。指数当日涨跌幅由 read service 用前收（上一投影交易日收盘）派生。
    未构建投影 / 空返回 -> 字段为 None（前端显示 0% / —，绝不伪造 0）。
    """

    ma5: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    equal_weight_index: float | None = None

    # [PANJI-MARKET-OVERVIEW] 快照 6 卡
    sse_close: float | None = None
    sse_change_pct: float | None = None
    szse_close: float | None = None
    szse_change_pct: float | None = None
    chinext_close: float | None = None
    chinext_change_pct: float | None = None
    advance_count: int | None = None
    decline_count: int | None = None
    flat_count: int | None = None
    turnover_amount: float | None = None
    limit_up_count: int | None = None
    limit_down_count: int | None = None


class MarketDashboardPoint(BaseModel):
    """250 日市场轨迹 chart 单点。

    - 既有 ma5/ma10/ma20/ma50/ma120 + ew_index：市场宽度轨迹。
    - [PANJI-MARKET-OVERVIEW] 新增：三大指数原始收盘（sse/szse/chinext_close）、read-time 派生 rebased
      （首有效显示点=100，分别归一，不持久化）、涨跌家数、全市场成交额、涨停/跌停家数。
    """

    trade_date: str
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma120: float | None = None
    ew_index: float | None = None

    # [PANJI-MARKET-OVERVIEW] 250 日轨迹 additive 字段
    sse_close: float | None = None
    szse_close: float | None = None
    chinext_close: float | None = None
    sse_rebased: float | None = None
    szse_rebased: float | None = None
    chinext_rebased: float | None = None
    advance_count: int | None = None
    decline_count: int | None = None
    flat_count: int | None = None
    turnover_amount: float | None = None
    limit_up_count: int | None = None
    limit_down_count: int | None = None


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
    # [R3C0] additive：详情最新 projection row 的成员数（冻结事实，不从 membership 表重算）。
    member_count: int


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

    # [R3D0] 比较矩阵（冻结字段，全局 T / T-5 口径；additive 扩展，不破坏既有 points）。
    # current(T) 行缺失 → 全部 MA / member_count / delta = None（但 points 仍由 build_compare 提供）。
    # previous(T-5) 行缺失 → delta = None（绝不 fallback）。
    member_count: int | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma120: float | None = None
    ma5_delta: float | None = None
    ma10_delta: float | None = None


class CompareResponse(BaseModel):
    # [R3D0] 全局 T / T-5 口径（与 R2 Scope Explorer 同一 resolve_t_prev 语义）；
    # 缺市场 projection → None（不伪造日期），但不影响既有 chart points。
    projection_trade_date: str | None = None
    previous_trade_date: str | None = None
    boards: list[CompareBoard] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# [R2] Scope Explorer（行业/概念板块全集 collection）
# ---------------------------------------------------------------------------
class ScopeExplorerItem(BaseModel):
    """单个板块的 explorer 行（projection-only；全局 T / T-5 delta）。"""

    board_id: str
    board_name: str
    board_type: str
    hierarchy_level: str
    membership_version: str
    member_count: int

    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma50: float | None = None
    ma120: float | None = None

    # PREV = 全局 T-5 projection 交易日；该 board 缺 PREV row → None（绝不 fallback）
    previous_ma5: float | None = None
    previous_ma10: float | None = None
    ma5_delta: float | None = None
    ma10_delta: float | None = None


class ScopeExplorerResponse(BaseModel):
    """Scope Explorer 分页响应。

    - projection_trade_date = 全局 T（无 projection 时为 None，不伪造日期）
    - previous_trade_date = 全局 T-5（market projection 不足 T_WINDOW 时为 None）
    - total = filter 后、pagination 前总数
    """

    projection_trade_date: str | None = None
    previous_trade_date: str | None = None
    total: int = 0
    page: int = 1
    page_size: int = 20
    items: list[ScopeExplorerItem] = Field(default_factory=list)
