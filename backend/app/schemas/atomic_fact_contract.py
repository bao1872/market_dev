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
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.schemas.stock_state import StockContextDataQuality


class PublicAtomicFactItem(BaseModel):
    """单个原子事实项（普通用户侧，无内部 ID / 路径泄露）。"""

    publicKey: str = Field(..., description="稳定公开键（不随内部 factId 变动）")
    dimension: str = Field(..., description="维度：trend/momentum/structure/volume")
    label: str = Field(..., description="通俗中文短标签")
    visualKind: Literal["metric", "value_with_category", "relation", "position", "distance", "ratio", "confirmed_position"] = Field(
        ..., description="前端渲染类型（禁止解析中文推断类型/状态）"
    )
    value: float | None = Field(None, description="原始数值（分类类事实为 None）")
    valueText: str | None = Field(None, description="用户可读短原子值（无内部术语）；关系类事实为 None，仅以 categoryLabel 承载")
    categoryCode: str | None = Field(None, description="机器分类码（UI 可选）")
    categoryLabel: str | None = Field(None, description="中文分类标签")
    secondaryText: str | None = Field(None, description="弱说明（单位/补充）")
    unit: str | None = Field(None, description="单位（如 ATR）")
    thresholdEnabled: bool = Field(True, description="分类阈值是否已启用（T5/V3 为 False）")


class ProductObservationItem(BaseModel):
    """产品观察扩展项（CHANGE-20260716-006）。

    不在冻结 Core 14 中，不参与 14/14 统计。基于底层已计算的结构因子生成，
    用于补充展示（如最近确认区间位置）。scope 恒为 "product"。
    """

    publicKey: str = Field(..., description="产品观察公开键（如 confirmed_swing_position）")
    label: str = Field(..., description="通俗中文短标签")
    visualKind: Literal["confirmed_position"] = Field(
        ..., description="产品观察渲染类型"
    )
    group: str = Field(..., description="所属组（如 structure）")
    value: float | None = Field(None, description="区间内为 0–1 值，区间外为 None")
    rawValue: float | None = Field(None, description="原始值（可能 <0 或 >1，不静默 clip）")
    valueText: str | None = Field(None, description="用户可读短值（区间内）")
    categoryLabel: str | None = Field(None, description="中文分类标签（含区间外说明）")
    confirmedHigh: float | None = Field(None, description="已确认区间上沿（调试/UI 参考）")
    confirmedLow: float | None = Field(None, description="已确认区间下沿（调试/UI 参考）")
    scope: Literal["product"] = Field("product", description="标记为产品观察，非 V4.13 Core")


class ProductObservations(BaseModel):
    """产品观察扩展集合（CHANGE-20260716-006）。

    按 group 分组（structure 等），不计入 Core 14/14 统计。
    """

    structure: list[ProductObservationItem] = Field(
        default_factory=list, description="结构组产品观察项"
    )


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


class NodeAvailabilityInfo(BaseModel):
    """Node Cluster 可用性状态（Canonical Node 结果的诊断视图）。

    [CHANGE-20260721-001] StockContext 只读，从已发布 snapshot 的
    primary.1d.node_cluster 提取，禁止与 indicators API 实时计算混用。

    state 取值：
    - available: POC/VAH/VAL 齐全（日线 + 15m 均有，profile 非空）
    - degraded: 15m 缺失但日线 profile 可用（仍可展示 POC，但不能做 15m 节点分析）
    - unavailable: 引擎失败 / profile 空 / 日线不足 / 旧快照无 node_cluster 字段
    - unknown: 旧 schema_version<4 快照无 availability 字段（不应出现，schema_version 已 bump）

    reasonCode（state != available 时非 null）：
    - NODE_PROFILE_EMPTY: profile_rows 为空（engine 无法生成有效价格档位）
    - NODE_15M_MISSING: 15m bars 缺失（state=degraded）
    - NODE_COMPUTE_FAILED: engine 抛异常（含原始异常信息）
    - NODE_INSUFFICIENT_DAILY_BARS: 日线 bars 不足 10 根
    - LEGACY_SNAPSHOT_NO_NODE_CLUSTER: 旧快照缺少 node_cluster 字段（兼容场景）
    """

    state: Literal["available", "degraded", "unavailable", "unknown"] = Field(
        ..., description="Node Cluster 可用性状态"
    )
    reasonCode: str | None = Field(
        None, description="state != available 时的稳定码（NODE_PROFILE_EMPTY/NODE_15M_MISSING/NODE_COMPUTE_FAILED/NODE_INSUFFICIENT_DAILY_BARS/LEGACY_SNAPSHOT_NO_NODE_CLUSTER）"
    )
    pocPrice: float | None = Field(None, description="POC 价格（Canonical Node 结果）")
    profileHash: str | None = Field(None, description="Node Cluster profile_hash（三链一致性基础）")
    dailySourceHash: str | None = Field(
        None, description="日线 source_bar_hash（与 indicators API 帧比对）"
    )
    bars15mSourceHash: str | None = Field(None, description="15m source_bar_hash")
    algorithmVersion: str | None = Field(None, description="Node Cluster engine 算法版本")
    dailyBarsCount: int = Field(0, description="日线 bars 数量（诊断用）")
    bars15mCount: int = Field(0, description="15m bars 数量（诊断用）")


class AtomicFactChange(BaseModel):
    """近期变化记录（between consecutive published snapshots）。

    按各事实公开显示精度比较，仅描述变化类型，不解释利好利空（非 Core）。
    """

    publicKey: str = Field(..., description="事实 publicKey")
    label: str = Field(..., description="通俗中文短标签（前端展示，禁止 publicKey）")
    dimension: str = Field(..., description="维度")
    fromText: str | None = Field(None, description="变化前展示文案")
    toText: str | None = Field(None, description="变化后展示文案")
    deltaText: str = Field(..., description="变化类型：分类调整/数值变动/状态更新")
    asOf: str = Field(..., description="后一个快照 trade_date（point-in-time）")


class AtomicFactsMeta(BaseModel):
    """公共响应 meta：三版本字段（前端禁止硬编码 V4.13）。"""

    payloadVersion: str = Field(..., description="持久化 payload schema 版本（当前 1）")
    researchFreezeVersion: str = Field(..., description="研究合同冻结版本（V4.13）")
    presentationVersion: str = Field(..., description="产品展示合同版本")


class AtomicFactsContextResponse(BaseModel):
    """GET /stocks/{symbol}/context 用户侧响应（只读）。"""

    contractVersion: str = Field(..., description="合同版本：Atomic Fact Contract V1")
    meta: AtomicFactsMeta = Field(..., description="三版本元数据（前端禁止硬编码 V4.13）")
    asOf: str | None = Field(None, description="状态截止交易日（point-in-time）")
    core: dict[str, list[PublicAtomicFactItem]] = Field(
        default_factory=dict, description="四组 Core 事实（trend/momentum/structure/volume），仅非缺失项"
    )
    auxiliary: list[PublicAtomicFactItem] = Field(
        default_factory=list, description="Auxiliary 事实（默认隐藏，仅非缺失+flag开启项）"
    )
    availability: AtomicFactAvailability = Field(..., description="可用性统计")
    recentChanges: list[AtomicFactChange] = Field(
        default_factory=list, description="近期变化（仅最近一个交易日发生变化的项）"
    )
    latestChangesFrom: str | None = Field(
        None, description="近期变化起始交易日（前一发布交易日；无对比时为 None）"
    )
    latestChangesAsOf: str | None = Field(
        None, description="近期变化截止交易日（最新发布交易日；无快照时为 None）"
    )
    productObservations: ProductObservations = Field(
        default_factory=ProductObservations,
        description="产品观察扩展（CHANGE-20260716-006，不计入 Core 14/14）",
    )
    dataQuality: StockContextDataQuality = Field(..., description="数据质量（含 reasonCode）")
    nodeAvailability: NodeAvailabilityInfo = Field(
        ..., description="Node Cluster 可用性（CHANGE-20260721-001，区分 NODE_PROFILE_EMPTY/NODE_15M_MISSING/NODE_COMPUTE_FAILED）"
    )


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


# ---------------------------------------------------------------------------
# 持久化 Payload 严格校验 schema（替换旧手写 _is_valid_stored_afc）
# ---------------------------------------------------------------------------

# 冻结合同级：publicKey → dimension（事实消失时仍可校验维度归属）
# 注意：此处硬编码与 atomic_fact_contract_service.FACT_DIMENSION_BY_ID 保持一致；
# 不直接导入 service 是为了避免 schema 层循环依赖。
_PUBLIC_KEY_DIMENSION: dict[str, str] = {
    # trend
    "trend_direction": "trend",
    "aligned_slope": "trend",
    "trend_duration": "trend",
    "slope_ratio": "trend",
    # momentum
    "momentum_alignment": "momentum",
    "aligned_momentum": "momentum",
    "momentum_delta": "momentum",
    "squeeze_state": "momentum",
    # structure
    "boundary_relation": "structure",
    "active_dir_relation": "structure",
    "active_position": "structure",
    "dist_favorable": "structure",
    "dist_adverse": "structure",
    # volume
    "volume_ratio": "volume",
}

# T3/T6/V1 永不进入持久化 payload（feature flag 关闭 / 拒绝项）
_FORBIDDEN_PUBLIC_KEYS: set[str] = {
    "trend_efficiency",        # T3
    "efficiency_delta",        # T6
    "cumulative_volume_ratio", # V1
}

_EXPECTED_CORE_KEYS: tuple[str, ...] = ("trend", "momentum", "structure", "volume")


class PersistedAtomicFactsPayload(BaseModel):
    """严格校验 summary_payload.atomic_fact_contract_v1 的持久化结构。

    校验规则（任一不满足 → ValidationError → 调用方 fallback 重算，不得 500）：
    - 四版本字段完全匹配（payloadVersion/researchContractVersion/
      researchFreezeVersion/presentationVersion）；
    - core 键恰好 trend/momentum/structure/volume（不多不少）；
    - 每一项均通过 PublicAtomicFactItem；
    - publicKey 属于正确维度且无重复/未知；
    - T3/T6/V1 不存在；
    - availability 与实际数组及固定分母 14 一致；
    - 不含 debug。
    """

    payloadVersion: str
    researchContractVersion: str
    researchFreezeVersion: str
    presentationVersion: str
    core: dict[str, list[PublicAtomicFactItem]]
    auxiliary: list[PublicAtomicFactItem]
    availability: AtomicFactAvailability

    model_config = {"extra": "forbid"}  # 不含 debug / 其他未知字段

    @model_validator(mode="after")
    def _validate_strict(self) -> PersistedAtomicFactsPayload:
        # 1. core 键恰好四个维度
        core_keys = set(self.core.keys())
        expected = set(_EXPECTED_CORE_KEYS)
        if core_keys != expected:
            raise ValueError(
                f"core 键必须恰好为 {sorted(expected)}，实际为 {sorted(core_keys)}"
            )

        # 2. 收集所有 publicKey（core + auxiliary），校验维度归属、无重复、无未知
        seen: set[str] = set()
        for dim in _EXPECTED_CORE_KEYS:
            for item in self.core[dim]:
                pk = item.publicKey
                # T3/T6/V1 禁止出现
                if pk in _FORBIDDEN_PUBLIC_KEYS:
                    raise ValueError(f"禁止的 publicKey 出现在 core.{dim}: {pk}")
                # publicKey 必须属于该维度
                expected_dim = _PUBLIC_KEY_DIMENSION.get(pk)
                if expected_dim is None:
                    raise ValueError(f"未知 publicKey: {pk}")
                if expected_dim != dim:
                    raise ValueError(
                        f"publicKey {pk} 维度错误：期望 {expected_dim}，实际在 {dim}"
                    )
                if pk in seen:
                    raise ValueError(f"重复 publicKey: {pk}")
                seen.add(pk)

        for item in self.auxiliary:
            pk = item.publicKey
            if pk in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"禁止的 publicKey 出现在 auxiliary: {pk}")
            # auxiliary publicKey 不在 _PUBLIC_KEY_DIMENSION（仅 Core）
            # 但仍需唯一
            if pk in seen:
                raise ValueError(f"重复 publicKey: {pk}")
            seen.add(pk)

        # 3. availability 与实际数组一致
        av = self.availability
        if av.coreDenominator != 14:
            raise ValueError(
                f"availability.coreDenominator 必须为 14，实际 {av.coreDenominator}"
            )
        # corePresent = 实际 core 数组中非缺失项总数
        actual_present = sum(len(self.core[dim]) for dim in _EXPECTED_CORE_KEYS)
        if av.corePresent != actual_present:
            raise ValueError(
                f"availability.corePresent={av.corePresent} 与实际 core 数组项数 "
                f"{actual_present} 不一致"
            )
        # coreMissing 数量 + corePresent = coreDenominator
        if av.corePresent + len(av.coreMissing) != av.coreDenominator:
            raise ValueError(
                f"corePresent({av.corePresent}) + coreMissing({len(av.coreMissing)}) "
                f"!= coreDenominator({av.coreDenominator})"
            )
        # coreMissing 中每一项必须是合法的 Core publicKey
        for pk in av.coreMissing:
            if pk not in _PUBLIC_KEY_DIMENSION:
                raise ValueError(f"coreMissing 含未知 publicKey: {pk}")
            if pk in _FORBIDDEN_PUBLIC_KEYS:
                raise ValueError(f"coreMissing 含禁止 publicKey: {pk}")

        # 4. V1/rejected 永远 False
        if av.v1Present or av.rejectedPresent:
            raise ValueError("v1Present / rejectedPresent 必须为 False")

        return self


if __name__ == "__main__":
    from datetime import date

    resp = AtomicFactsContextResponse(
        contractVersion="Atomic Fact Contract V1",
        meta=AtomicFactsMeta(
            payloadVersion="1",
            researchFreezeVersion="V4.13",
            presentationVersion="Atomic Fact Presentation V1",
        ),
        asOf=date(2026, 7, 15).isoformat(),
        core={"trend": [], "momentum": [], "structure": [], "volume": []},
        auxiliary=[],
        availability=AtomicFactAvailability(
            coreDenominator=14, corePresent=0, coreMissing=[]
        ),
        recentChanges=[],
        latestChangesFrom=None,
        latestChangesAsOf=None,
        productObservations=ProductObservations(structure=[]),
        dataQuality=StockContextDataQuality(
            hasSucceededRun=False,
            hasSnapshot=False,
            reasonCode="no_published_full_run",
            degradedReasons=[],
            runTradeDate=None,
            runPublishedAt=None,
            instrumentStatus="active",
        ),
        nodeAvailability=NodeAvailabilityInfo(
            state="unknown",
            reasonCode="LEGACY_SNAPSHOT_NO_NODE_CLUSTER",
        ),
    )
    assert resp.contractVersion == "Atomic Fact Contract V1"
    assert resp.meta.researchFreezeVersion == "V4.13"
    assert resp.meta.payloadVersion == "1"
    # 用户响应不得含 factId / sourcePath 字段
    sample_item = PublicAtomicFactItem(
        publicKey="trend_direction",
        dimension="trend",
        label="主趋势方向",
        visualKind="value_with_category",
        value=1.0,
        valueText="主趋势方向为上行",
        categoryCode="UP",
        categoryLabel="上行",
    )
    assert "factId" not in sample_item.model_dump()
    assert "sourcePath" not in sample_item.model_dump()
    print("OK: atomic_fact_contract schema 验证通过")
