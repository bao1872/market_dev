"""Instrument Pydantic schemas - 股票主数据响应模型。

提供：
- InstrumentResponse: 单个股票响应
- InstrumentListResponse: 分页列表响应
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InstrumentResponse(BaseModel):
    """单个股票主数据响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(..., description="主键 UUID")
    symbol: str = Field(..., description="股票代码，如 '000001'")
    name: str = Field(..., description="股票名称")
    pinyin_initials: str | None = Field(None, description="名称拼音首字母（小写，如 'dmgf'）")
    market: str = Field(..., description="市场：SH/SZ/BJ")
    status: str = Field(..., description="状态：active/delisted/suspended")
    listing_date: date | None = Field(None, description="上市日期")
    created_at: datetime = Field(..., description="创建时间")
    updated_at: datetime = Field(..., description="更新时间")


class InstrumentListResponse(BaseModel):
    """股票列表分页响应。"""

    items: list[InstrumentResponse] = Field(default_factory=list, description="股票列表")
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码（从 1 开始）")
    page_size: int = Field(..., description="每页大小")
    pages: int = Field(..., description="总页数")


class InstrumentBatchRequest(BaseModel):
    """批量查询股票请求（按 ID 列表）。"""

    ids: list[UUID] = Field(..., min_length=1, max_length=1000, description="股票 ID 列表（最多 1000）")


class InstrumentBatchResponse(BaseModel):
    """批量查询股票响应。"""

    items: list[InstrumentResponse] = Field(default_factory=list, description="股票列表")
    total: int = Field(..., description="返回的记录数")


if __name__ == "__main__":
    # 自测入口：验证 schema 字段定义
    print(f"InstrumentResponse fields={list(InstrumentResponse.model_fields.keys())}")
    print(f"InstrumentListResponse fields={list(InstrumentListResponse.model_fields.keys())}")
    print("OK")
