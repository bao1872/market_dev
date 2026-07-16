"""Atomic Fact Contract V1 - API 响应 schema。

普通用户响应与管理员 debug 响应严格分离：
- 普通用户（AtomicFactsContextResponse）只暴露稳定 publicKey / 中文 label /
  visualKind / value / valueText / categoryCode / categoryLabel / secondaryText /
  unit / thresholdEnabled；**绝不**含 factId / sourcePath / 公式 / 阈值引用。
- 管理员 debug（AdminStockDebugResponse）额外返回 rawDebug（原始 payload）+ 每事实
  可追溯信息（factId / publicKey / sourcePath / rawValue / thresholdRef /
  thresholdEnabled / featureFlag / missing）。

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


class PublicAtomicFactItem(BaseModel):
    """单个原子事实项（普通用户侧，无内部 ID / 路径泄露）。"""

    publicKey: str = Field(..., description="稳定公开键（不随内部 factId 变动）")
    dimension: str = Field(..., description="维度：trend/momentum/structure/volume")
    label: str = Field(..., description="通俗中文短标签")
    visualKind: str = Field(
        ..., description="前端渲染类型：value/relation/position/distance/ratio/category"
    )
    value: float | None = Field(None, description="原始数值（分类类事实为 None）")
    valueText: str = Field(..., description="用户可读完整文案（无内部术语）")
    categoryCode: str | None = Field(None, description="机器分类码（UI 可选）")
    categoryLabel: str | None = Field(None, description="中文分类标签")
    secondaryText: str | None = Field(None, description="弱说明（单位/补充）")
    unit: str | None = Field(None, description="单位（如 ATR）")
    thresholdEnabled: bool = Field(True, description="分类阈值是否已启用（T5/V3 为 False）")


class AtomicFactAvailability(BaseModel):
    """可用性统计（固定分母 14；coreMissing/auxiliary* 均用 publicKey）。"""

    coreDenominator: int = Field(..., description="Core 分母，固定 14")
    corePresent: int = Field(..., description="Core 实际可用数（非缺失项）")
    coreMissing: list[str] = Field(
        default_factory=list, description="缺失事实 publicKey 列表（从用户数组省略）"
    )
    auxiliaryAvailable: list[str] = Field(
        default_factory=list, description="可用 Auxiliary publicKey"
    )
    auxiliaryHidden: list[str] = Field(
        default_factory=list, description="默认隐藏（不在用户 UI 展示）的 Auxiliary publicKey"
    )
    v1Present: bool = Field(False, description="V1 是否出现（永远 False）")
    rejectedPresent: bool = Field(False, description="Rejected 事实是否出现（永远 False）")
    warnings: list[str] = Field(
        default_factory=list, description="数据质量异常（如 m5_inconsistent）"
    )


class AtomicFactChange(BaseModel):
    """近期变化记录（between consecutive published snapshots）。

    按各事实公开显示精度比较，仅描述变化类型，不解释利好利空（非 Core）。
    """

    publicKey: str = Field(..., description="事实 publicKey")
    dimension: str = Field(..., description="维度")
    fromText: str | None = Field(None, description="变化前展示文案")
    toText: str | None = Field(None, description="变化后展示文案")
    deltaText: str = Field(..., description="变化类型：分类调整/数值变动/状态更新")
    asOf: str = Field(..., description="后一个快照 trade_date（point-in-time）")


class AtomicFactsContextResponse(BaseModel):
    """GET /stocks/{symbol}/context 用户侧响应（只读）。"""

    contractVersion: str = Field(..., description="合同版本：Atomic Fact Contract V1")
    asOf: str | None = Field(None, description="状态截止交易日（point-in-time）")
    core: dict[str, list[PublicAtomicFactItem]] = Field(
        default_factory=dict, description="四组 Core 事实（trend/momentum/structure/volume），仅非缺失项"
    )
    auxiliary: list[PublicAtomicFactItem] = Field(
        default_factory=list, description="Auxiliary 事实（默认隐藏，仅非缺失+flag开启项）"
    )
    availability: AtomicFactAvailability = Field(..., description="可用性统计")
    recentChanges: list[AtomicFactChange] = Field(
        default_factory=list, description="近期变化（≤10 快照只读计算）"
    )
    dataQuality: StockContextDataQuality = Field(..., description="数据质量（含 reasonCode）")


class AdminAtomicFactDebugItem(BaseModel):
    """管理员调试：单事实可追溯信息（保留内部 ID / 路径）。"""

    factId: str = Field(..., description="事实 ID（Canonical Registry）")
    publicKey: str = Field(..., description="稳定公开键")
    sourcePath: str | None = Field(None, description="真实来源字段路径")
    rawValue: float | None = Field(None, description="原始值")
    thresholdRef: str | None = Field(None, description="阈值来源（合同 thresholds 键）")
    thresholdEnabled: bool = Field(False, description="分类阈值是否启用")
    featureFlag: bool = Field(False, description="feature flag 是否开启（T3/T6）")
    missing: bool = Field(False, description="事实是否缺失")


class AdminStockDebugResponse(AtomicFactsContextResponse):
    """管理员调试响应：在用户响应基础上补充原始 payload 与可追溯信息。"""

    rawDebug: dict[str, Any] | None = Field(
        None, description="原始 payload（structural/temporal/summary）+ run 元数据"
    )
    atomicFactsDebug: list[AdminAtomicFactDebugItem] = Field(
        default_factory=list, description="每事实可追溯信息（含缺失）"
    )


if __name__ == "__main__":
    from datetime import date

    resp = AtomicFactsContextResponse(
        contractVersion="Atomic Fact Contract V1",
        asOf=date(2026, 7, 15).isoformat(),
        core={"trend": [], "momentum": [], "structure": [], "volume": []},
        auxiliary=[],
        availability=AtomicFactAvailability(
            coreDenominator=14, corePresent=0, coreMissing=[]
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
    # 用户响应不得含 factId / sourcePath 字段
    sample_item = PublicAtomicFactItem(
        publicKey="trend_direction",
        dimension="trend",
        label="主趋势方向",
        visualKind="category",
        value=1.0,
        valueText="主趋势方向为上行",
        categoryCode="UP",
        categoryLabel="上行",
    )
    assert "factId" not in sample_item.model_dump()
    assert "sourcePath" not in sample_item.model_dump()
    print("OK: atomic_fact_contract schema 验证通过")
