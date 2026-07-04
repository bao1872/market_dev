"""策略运行与结果 Pydantic schemas - 请求/响应模型。

提供：
- TriggerRunRequest: 触发策略运行请求
- StrategyRunResponse: 策略运行响应（含 effective_config/published_at 等扩展字段）
- StrategyResultResponse: 策略结果响应
- StrategyResultListResponse: 结果列表响应（分页+筛选+排序）
- MetricFilter: 指标筛选条件（支持 gt/gte/lt/lte/eq/between）
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TriggerRunRequest(BaseModel):
    """触发策略运行请求 - admin 触发策略运行。

    仅创建 queued 运行记录，Worker 异步执行（不在 HTTP 请求内计算全市场）。

    Attributes:
        trade_date: 交易日（默认当天）
        instrument_ids: 指定标的列表（None 表示全市场）
        run_type: 触发方式（manual/scheduled/replay，默认 manual）
    """

    trade_date: date | None = Field(None, description="交易日（默认当天）")
    instrument_ids: list[UUID] | None = Field(
        None, description="指定标的列表（None 表示全市场）"
    )
    run_type: str = Field("manual", description="触发方式：manual/scheduled/replay")


class StrategyRunResponse(BaseModel):
    """策略运行响应。

    含迁移 015/016/030 新增字段：
    - effective_config/effective_config_hash: 运行时配置快照
    - total/succeeded/failed/skipped_count: 批量统计
    - published_at: 发布时间（非空表示已发布）
    - attempt_no: 业务重试序号
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="运行 ID")
    strategy_version_id: UUID = Field(..., description="策略版本 ID")
    run_type: str = Field(..., description="触发方式")
    trade_date: date | None = Field(None, description="交易日")
    data_cutoff: datetime | None = Field(None, description="数据截止时间")
    status: str = Field(..., description="运行状态：queued/running/completed/partial_failed/published/failed")
    input_overrides: dict[str, Any] = Field(
        default_factory=dict, description="输入参数覆盖"
    )
    started_at: datetime | None = Field(None, description="开始时间")
    finished_at: datetime | None = Field(None, description="完成时间")
    idempotency_key: str = Field(..., description="幂等键")
    # 迁移 015 新增字段
    effective_config: dict[str, Any] | None = Field(None, description="运行时配置快照")
    effective_config_hash: str | None = Field(None, description="配置哈希")
    total_instruments: int | None = Field(None, description="标的总数")
    succeeded_count: int | None = Field(None, description="成功数")
    failed_count: int | None = Field(None, description="失败数")
    skipped_count: int | None = Field(None, description="跳过数")
    # 迁移 016 新增字段
    published_at: datetime | None = Field(None, description="发布时间")
    # 迁移 030 新增字段
    attempt_no: int = Field(1, description="业务重试序号")


class StrategyRunListResponse(BaseModel):
    """策略运行列表响应。"""

    items: list[StrategyRunResponse] = Field(default_factory=list)
    total: int = Field(..., description="总数")


class StrategyResultResponse(BaseModel):
    """策略结果响应。

    支持全量 universe 展示：
    - succeeded 行: id/run_id/strategy_version_id/trade_date/payload/created_at 来自 strategy_results
    - skipped/failed 行: id=None, payload=None, item_status/reason_code/error_message 来自 strategy_run_items
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID | None = Field(None, description="结果 ID（skipped/failed 行为 None）")
    run_id: UUID | None = Field(None, description="所属运行 ID")
    strategy_version_id: UUID | None = Field(None, description="策略版本 ID")
    instrument_id: UUID = Field(..., description="标的 ID")
    trade_date: date | None = Field(None, description="交易日")
    payload: dict[str, Any] | None = Field(
        None, description="完整结果 JSON（skipped/failed 行为 None）"
    )
    created_at: datetime | None = Field(None, description="创建时间")
    # [选股] - 标的主数据冗余字段，避免前端二次查询
    instrument_symbol: str | None = Field(None, description="股票代码")
    instrument_name: str | None = Field(None, description="股票名称")
    instrument_market: str | None = Field(None, description="市场（SH/SZ/BJ）")
    # [StrategyRunItem] - 全量 universe 展示新增字段
    item_status: str = Field(
        ..., description="item 状态: pending/running/succeeded/failed/skipped"
    )
    reason_code: str | None = Field(None, description="跳过/失败原因代码")
    error_message: str | None = Field(None, description="失败错误信息")


class StrategyResultListResponse(BaseModel):
    """结果列表响应（分页+筛选+排序）。"""

    items: list[StrategyResultResponse] = Field(default_factory=list)
    total: int = Field(..., description="过滤后总数")
    source_total: int = Field(0, description="过滤前总数（向后兼容）")
    filtered_total: int = Field(0, description="过滤后总数")
    run_source_total: int = Field(0, description="运行过滤前总数（全市场）")
    universe_total: int = Field(0, description="股票池内总数（watchlist 时为自选股范围，all 时等于 run_source_total）")
    page: int = Field(..., description="当前页码（从 1 开始）")
    page_size: int = Field(..., description="每页大小")


class MetricFilter(BaseModel):
    """指标筛选条件（增强版）。

    支持 operator: gt/gte/lt/lte/eq/between
    - gt/lt/eq: 使用 value 字段
    - gte/lte: 使用 value 字段
    - between: 使用 value1（下界）和 value2（上界），闭区间
    """

    metric_key: str = Field(..., description="指标名（必须在 manifest outputs.filterable 白名单中）")
    operator: str = Field(..., description="比较操作：gt/gte/lt/lte/eq/between")
    value: float | None = Field(None, description="主值（非 between 操作）")
    value1: float | None = Field(None, description="下界（between 操作）")
    value2: float | None = Field(None, description="上界（between 操作）")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"TriggerRunRequest fields={list(TriggerRunRequest.model_fields.keys())}")
    print(f"StrategyRunResponse fields={list(StrategyRunResponse.model_fields.keys())}")
    print(f"StrategyResultResponse fields={list(StrategyResultResponse.model_fields.keys())}")
    print(f"StrategyResultListResponse fields={list(StrategyResultListResponse.model_fields.keys())}")
    print(f"MetricFilter fields={list(MetricFilter.model_fields.keys())}")

    # 验证 StrategyRunResponse 新增字段
    run_fields = set(StrategyRunResponse.model_fields.keys())
    assert "effective_config" in run_fields
    assert "effective_config_hash" in run_fields
    assert "total_instruments" in run_fields
    assert "succeeded_count" in run_fields
    assert "failed_count" in run_fields
    assert "skipped_count" in run_fields
    assert "published_at" in run_fields
    assert "attempt_no" in run_fields
    print("StrategyRunResponse 新增字段验证 ✓")

    # 验证 MetricFilter operator 字段
    mf_fields = set(MetricFilter.model_fields.keys())
    assert "operator" in mf_fields
    assert "value" in mf_fields
    assert "value1" in mf_fields
    assert "value2" in mf_fields
    print("MetricFilter 增强字段验证 ✓")

    print("OK")
