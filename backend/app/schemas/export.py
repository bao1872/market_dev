"""导出请求/响应 Pydantic schemas。

CHANGE-20260713-010: 列表导出 Excel
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ExportColumn(BaseModel):
    """导出列定义（前端发送，后端按 payload_key 提取值）。"""

    key: str = Field(..., description="列标识（如 stock, change_pct, dsa_dir_bars）")
    title: str = Field(..., description="表头标题")
    data_type: Literal["text", "number", "percent"] = Field(
        "text", description="数据类型: text 文本 | number 数值 | percent 百分比"
    )
    payload_key: str | None = Field(
        None,
        description="payload 中对应的键名（None 表示特殊列如 stock，由后端特殊处理）",
    )


class ExportRequest(BaseModel):
    """导出请求 body（POST /strategy-runs/{run_id}/results/export）。"""

    # universe 复用现有 strategy_runs.py 的 str 类型（避免 Literal 重复声明）
    # 合法值 "all" / "watchlist"，由 API 层校验
    universe: str = Field("all", description="股票池: all 全市场 | watchlist 仅自选股")
    keyword: str | None = Field(None, description="关键词（symbol/name/pinyin 模糊匹配）")
    industry: str | None = Field(None, description="行业板块")
    concept: str | None = Field(None, description="概念板块")
    # CHANGE-20260714-001: 股票名称独立筛选（与 keyword 独立 AND 语义，与 GET results 一致）
    stock_name: str | None = Field(None, description="股票名称独立筛选值")
    stock_name_op: str | None = Field(
        None, description="股票名称筛选操作符: contains | not_contains | eq"
    )
    metric_filters: list[dict] | None = Field(
        None, description="指标筛选条件（与 GET results 的 metric_filters 格式一致）"
    )
    sort_by: str | None = Field(None, description="排序指标名")
    sort_desc: bool = Field(False, description="是否降序")
    visible_columns: list[ExportColumn] = Field(
        ..., description="可见列定义（按顺序导出，不含操作列）"
    )
