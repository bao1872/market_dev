"""ConsensusZone DTO - 筹码共识区数据契约（PRD V1.1 §7.4）。

Phase 5 实现：基于成交量分布的峰簇识别 + 成交量加权百分位。

核心字段：
- lower=P10, upper=P90, center=P50（成交量加权百分位）
- peakPrice: 峰值价格
- volumeRatio: 该簇成交量占总成交量比例
- strength: 簇强度（0-1）
- timeframe: 来源周期（1d 主结构 / 15m 细化）
- asOf: 截止时间（因果性保证：timestamp <= as_of）
- algorithmVersion: 算法版本
"""

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约

from __future__ import annotations

from pydantic import BaseModel, Field


class ConsensusCluster(BaseModel):
    """单个成交密集区峰簇。"""

    lower: float = Field(..., description="P10 下界（成交量加权）")
    upper: float = Field(..., description="P90 上界（成交量加权）")
    center: float = Field(..., description="P50 中位（成交量加权）")
    peakPrice: float = Field(..., description="峰值价格（最大成交量价位）")
    volumeRatio: float = Field(..., description="该簇成交量占总成交量比例（0-1）")
    strength: float = Field(..., description="簇强度（0-1，峰度归一化）")


class ConsensusZoneResult(BaseModel):
    """ConsensusZone 计算结果。"""

    symbol: str = Field(..., description="股票代码")
    timeframe: str = Field(..., description="来源周期（1d/15m）")
    asOf: str = Field(..., description="截止时间 ISO（因果性：timestamp <= as_of）")
    algorithmVersion: str = Field(..., description="算法版本")
    clusters: list[ConsensusCluster] = Field(
        default_factory=list, description="识别的峰簇列表（按成交量降序）"
    )
    totalVolume: float = Field(..., description="总成交量")
    binCount: int = Field(..., description="价格分箱数")
    isAvailable: bool = Field(
        ..., description="是否可用（数据不足时为 False）"
    )
    unavailableReason: str | None = Field(
        None, description="不可用原因（isAvailable=False 时填写）"
    )
