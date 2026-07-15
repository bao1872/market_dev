"""Atomic Fact Contract V1 - API 响应 schema。

普通用户响应替换旧 state/events 为：
  contractVersion / asOf / core / auxiliary / availability / recentChanges / dataQuality

管理员 debug 继续返回原始 payload + 原子事实可追溯信息
（Fact ID / 真实路径 / raw value / 阈值来源 / feature flag）。

用法：
    from app.schemas.atomic_fact_contract import AtomicFactsContextResponse

模块自测：
    python -m app.schemas.atomic_fact_contract
"""

from __future__ import annotations

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约（contractVersion/asOf 等）
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.stock_state import StockContextDataQuality


class AtomicFactItem(BaseModel):
    """单个原子事实项（Core 或 Auxiliary）。"""

    factId: str = Field(..., description="事实 ID（Canonical Registry）")
    dimension: str = Field(..., description="维度：trend/momentum/structure/volume")
    label: str = Field(..., description="通俗中文短标签")
    value: float | None = Field(None, description="原始数值（分类类事实为 None）")
    unit: str | None = Field(None, description="单位（如 ATR/bar、ATR）")
    category: str | None = Field(None, description="分类（中文或机器码）")
    displayText: str = Field(..., description="用户可读完整文案")
    thresholdEnabled: bool = Field(True, description="分类阈值是否已启用（T5/V3 为 False）")
    missing: bool = Field(False, description="事实缺失（直接省略，不伪装）")
    hiddenByDefault: bool = Field(False, description="Auxiliary 默认隐藏项")
    sourcePath: str | None = Field(None, description="真实来源字段路径")


class AtomicFactAvailability(BaseModel):
    """可用性统计。"""

    coreDenominator: int = Field(..., description="Core 分母，固定 14")
    corePresent: int = Field(..., description="Core 实际可用数")
    coreMissing: list[str] = Field(default_factory=list, description="缺失事实 ID 列表")
    auxiliaryAvailable: list[str] = Field(default_factory=list, description="可用 Auxiliary ID")
    auxiliaryHidden: list[str] = Field(
        default_factory=list, description="默认隐藏（不在用户 UI 展示）的 Auxiliary ID 列表"
    )
    v1Present: bool = Field(False, description="V1 是否出现（永远 False）")
    rejectedPresent: bool = Field(False, description="Rejected 事实是否出现（永远 False）")


class AtomicFactChange(BaseModel):
    """近期变化记录（between consecutive published snapshots）。"""

    factId: str = Field(..., description="事实 ID")
    dimension: str = Field(..., description="维度")
    fromCategory: str | None = Field(None, description="变化前分类")
    toCategory: str | None = Field(None, description="变化后分类")
    fromValue: float | None = Field(None, description="变化前数值")
    toValue: float | None = Field(None, description="变化后数值")
    asOf: str = Field(..., description="后一个快照 trade_date（point-in-time）")


class AtomicFactsContextResponse(BaseModel):
    """GET /stocks/{symbol}/context 用户侧响应（只读）。"""

    contractVersion: str = Field(..., description="合同版本：Atomic Fact Contract V1")
    asOf: str | None = Field(None, description="状态截止交易日（point-in-time）")
    core: dict[str, list[AtomicFactItem]] = Field(
        default_factory=dict, description="四组 Core 事实（trend/momentum/structure/volume）"
    )
    auxiliary: list[AtomicFactItem] = Field(
        default_factory=list, description="Auxiliary 事实（默认隐藏）"
    )
    availability: AtomicFactAvailability = Field(..., description="可用性统计")
    recentChanges: list[AtomicFactChange] = Field(
        default_factory=list, description="近期变化（≤10 快照只读计算）"
    )
    dataQuality: StockContextDataQuality = Field(..., description="数据质量（含 reasonCode）")


class AdminAtomicFactDebugItem(BaseModel):
    """管理员调试：单事实可追溯信息。"""

    factId: str = Field(..., description="事实 ID")
    sourcePath: str | None = Field(None, description="真实来源字段路径")
    rawValue: float | None = Field(None, description="原始值")
    thresholdRef: str | None = Field(None, description="阈值来源（合同 thresholds 键）")
    thresholdEnabled: bool = Field(False, description="分类阈值是否启用")
    featureFlag: bool = Field(False, description="feature flag 是否开启（T3/T6）")


class AdminStockDebugResponse(AtomicFactsContextResponse):
    """管理员调试响应：在用户响应基础上补充原始 payload 与可追溯信息。"""

    rawDebug: dict[str, Any] | None = Field(
        None, description="原始 payload（structural/temporal/summary）+ run 元数据"
    )
    atomicFactsDebug: list[AdminAtomicFactDebugItem] = Field(
        default_factory=list, description="每事实可追溯信息"
    )


if __name__ == "__main__":
    from datetime import date

    resp = AtomicFactsContextResponse(
        contractVersion="Atomic Fact Contract V1",
        asOf=date(2026, 7, 15).isoformat(),
        core={"trend": [], "momentum": [], "structure": [], "volume": []},
        auxiliary=[],
        availability=AtomicFactAvailability(
            coreDenominator=14, corePresent=0, coreMissing=[], auxiliaryAvailable=[]
        ),
        recentChanges=[],
        dataQuality=StockContextDataQuality(
            hasSucceededRun=False,
            hasSnapshot=False,
            reasonCode="no_published_full_run",
            degradedReasons=[],
            runTradeDate=None,
            runPublishedAt=None,
            instrumentStatus="active",
        ),
    )
    assert resp.contractVersion == "Atomic Fact Contract V1"
    assert resp.availability.coreDenominator == 14
    print("OK: atomic_fact_contract schema 验证通过")
