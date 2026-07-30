"""[CHANGE-20260730-011] 板块分析 V1 Schema - API DTO。

对应迁移 074 中的 board_analysis_snapshots 表：
- 单条记录既是 run 又是 snapshot（含 status/started_at/finished_at）
- 复用 factor_publications 表发布指针：
  publication_kind=market_aggregation, scope_type=board, scope_key=board_id::text

字段命名约定（与前端 JSON API 契约一致）：
- snake_case（与现有 /market/stocks、/admin/visitors 等保持一致）
- 日期字段：ISO 字符串（trade_date / started_at / finished_at / created_at）
- UUID 字段：字符串（id / board_id / source_core_run_id）
- payload 为 JSONB 透传，前端按 payload 字段自行解析

模块自测：
    python -m app.schemas.board_analysis
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BoardAnalysisSnapshotDTO(BaseModel):
    """板块分析快照响应体（/boards/analysis 列表与详情共用）。"""

    model_config = ConfigDict(populate_by_name=True, from_attributes=True)

    id: str = Field(..., description="快照 ID（UUID）")
    trade_date: str = Field(..., description="业务交易日 ISO（YYYY-MM-DD）")
    board_id: str = Field(..., description="板块 ID（UUID）")
    board_type: str = Field(..., description="板块类型：industry | concept")
    board_name: str = Field(..., description="板块名称（冗余存储）")
    source_core_run_id: str = Field(..., description="输入 stock_core snapshot_run_id")
    algorithm_version: str = Field(..., description="板块分析算法版本")
    parameter_hash: str = Field(..., description="参数 hash")
    eligible_count: int = Field(..., description="板块成员总数")
    ready_count: int = Field(..., description="有效股票数")
    coverage_ratio: float = Field(..., description="覆盖率 = ready/eligible")
    missing_count: int = Field(..., description="缺失股票数")
    missing_reasons: dict[str, Any] = Field(
        default_factory=dict, description="缺失原因分布 {reason_code: count}",
    )
    status: str = Field(..., description="状态：pending/running/succeeded/failed/partial")
    payload: dict[str, Any] = Field(
        default_factory=dict, description="板块分析指标 payload JSON",
    )
    error_message: str | None = Field(None, description="失败原因（status=failed 时）")
    started_at: str | None = Field(None, description="计算开始时间 ISO")
    finished_at: str | None = Field(None, description="计算完成时间 ISO")
    created_at: str = Field(..., description="记录创建时间 ISO")
    updated_at: str = Field(..., description="记录更新时间 ISO")
    is_stale: bool = Field(
        False,
        description=(
            "快照是否过期（trade_date < MAX(bars_daily.trade_date)）；"
            "由 API 层根据当前行情最新交易日计算后注入"
        ),
    )
    is_published: bool = Field(
        False,
        description="是否已发布（factor_publications 存在对应 board scope 指针）",
    )


class BoardAnalysisListResponse(BaseModel):
    """板块分析列表响应（分页）。"""

    items: list[BoardAnalysisSnapshotDTO] = Field(
        default_factory=list, description="板块分析快照列表",
    )
    total: int = Field(0, description="总数")
    page: int = Field(1, description="当前页码")
    page_size: int = Field(20, description="每页大小")
    has_more: bool = Field(False, description="是否有下一页")


class BoardAnalysisDetailResponse(BaseModel):
    """板块分析详情响应（含 payload 完整内容）。"""

    snapshot: BoardAnalysisSnapshotDTO = Field(..., description="板块分析快照")
    # payload 已内嵌在 snapshot.payload 中，此处保留响应结构便于未来扩展


if __name__ == "__main__":
    # 自测：构造最小合法 DTO
    dto = BoardAnalysisSnapshotDTO(
        id="00000000-0000-0000-0000-000000000001",
        trade_date="2026-07-29",
        board_id="00000000-0000-0000-0000-000000000002",
        board_type="industry",
        board_name="银行",
        source_core_run_id="00000000-0000-0000-0000-000000000003",
        algorithm_version="board-v1-20260730",
        parameter_hash="hash-placeholder",
        eligible_count=50,
        ready_count=48,
        coverage_ratio=0.96,
        missing_count=2,
        missing_reasons={"M15_BARS_INSUFFICIENT": 2},
        status="succeeded",
        payload={
            "trend_dist": {"up": 30, "down": 15, "neutral": 3},
            "structure_events": {"bos_count": 5, "choch_count": 2},
        },
        started_at="2026-07-30T18:00:00+08:00",
        finished_at="2026-07-30T18:00:30+08:00",
        created_at="2026-07-30T18:00:00+08:00",
        updated_at="2026-07-30T18:00:30+08:00",
        is_stale=False,
        is_published=True,
    )
    dumped = dto.model_dump()
    assert dumped["trade_date"] == "2026-07-29"
    assert dumped["coverage_ratio"] == 0.96
    assert dumped["payload"]["trend_dist"]["up"] == 30
    print("OK: BoardAnalysisSnapshotDTO verified")
