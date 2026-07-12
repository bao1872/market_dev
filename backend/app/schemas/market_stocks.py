"""行情列表 API Pydantic schemas - MarketStockRow / MarketStocksResponse。

对应 PRD §8.1 行情列表契约：
- GET /market/stocks?scope&query&page&page_size&sort&industry&concept&state
- 返回 items + page + page_size + total + price_as_of + state_as_of + boards_as_of
- 每行一次返回页面所需全部字段，不再追加结构因子/时序特征请求

设计说明：
- industry / concepts 在 Phase 6 qstock 同步后填充，当前阶段固定 null / 空。
- dsa_state 来自最新 stock_feature_snapshot.summary_payload.daily_developing_swing_dir。
- structure_state 来自 summary_payload.cost_position_zone。
- latest_event_title / latest_event_time 来自最新 strategy_event。
- is_watchlisted 仅认证用户有意义。
- price_as_of: 最新日线 trade_date（定价所用最新 bar 的日期）。
- state_as_of: 最新 stock_feature_snapshot.created_at（特征快照写入时间）。
- boards_as_of: 板块数据时间戳（qstock 同步前为 null）。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class MarketStockRow(BaseModel):
    """行情列表单行 - 包含页面展示所需的全部字段。"""

    instrument_id: UUID = Field(..., description="股票 ID")
    symbol: str = Field(..., description="股票代码")
    name: str = Field(..., description="股票名称")
    latest_price: float | None = Field(None, description="最新价（最新日线 close）")
    change_pct: float | None = Field(None, description="涨跌幅百分比")
    industry: str | None = Field(None, description="行业（Phase 6 qstock 同步后填充）")
    concepts: list[str] = Field(default_factory=list, description="概念标签（Phase 6）")
    dsa_state: str | None = Field(None, description="DSA 核心状态（上行/下行）")
    structure_state: str | None = Field(None, description="形态状态（成本区间）")
    latest_event_title: str | None = Field(None, description="最近客观事件标题")
    latest_event_time: str | None = Field(None, description="最近客观事件时间 ISO")
    is_watchlisted: bool = Field(False, description="是否在当前用户自选中")


class MarketStocksResponse(BaseModel):
    """行情列表分页响应。"""

    items: list[MarketStockRow] = Field(default_factory=list, description="行情列表")
    page: int = Field(..., description="当前页码（从 1 开始）")
    page_size: int = Field(..., description="每页大小")
    total: int = Field(..., description="总记录数")
    price_as_of: str | None = Field(None, description="最新日线 trade_date ISO（定价所用 bar 日期）")
    state_as_of: str | None = Field(None, description="最新特征快照 created_at ISO")
    boards_as_of: str | None = Field(None, description="板块数据时间戳 ISO（qstock 同步前 null）")


# ===== 板块目录 API schemas（C9: 行业/概念筛选下拉支持）=====


class MarketBoardItem(BaseModel):
    """板块目录单行。"""

    id: UUID = Field(..., description="板块 ID")
    name: str = Field(..., description="板块名称")
    type: str = Field(..., description="板块类型：industry | concept")
    external_code: str = Field(..., description="外部代码（qstock 原始代码）")


class MarketBoardsResponse(BaseModel):
    """板块目录列表响应（只读，供前端筛选下拉使用）。"""

    items: list[MarketBoardItem] = Field(default_factory=list, description="板块列表")
    available: bool = Field(False, description="是否有可用板块数据（同步成功后 true）")
    reason_code: str | None = Field(
        None,
        description="不可用原因：board_provider_unavailable=provider 未就绪/被反爬拦截",
    )
    updated_at: str | None = Field(None, description="板块数据最后同步时间 ISO")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"MarketStockRow fields={list(MarketStockRow.model_fields.keys())}")
    print(f"MarketStocksResponse fields={list(MarketStocksResponse.model_fields.keys())}")
    print(f"MarketBoardItem fields={list(MarketBoardItem.model_fields.keys())}")
    print(f"MarketBoardsResponse fields={list(MarketBoardsResponse.model_fields.keys())}")
    print("OK")
