"""ProductReadiness 管理 API Schema（Commit G / PRD V2.1 §10）。

用于 Admin 盘后工作台展示九节点就绪状态、闭包评估与治理报告。

字段命名约定：
- 与前端 JSON API 契约一致，使用 camelCase（closure / products / pointerLineage 等）
- 闭包状态取值：pending / blocked / core_ready / degraded_ready / fully_ready
- readiness 取值：pending / ready / ready_reused / degraded / unavailable / blocked
- freshness 取值：fresh / stale / reused

模块自测：
    python -m app.schemas.product_readiness
"""

from __future__ import annotations

# ruff: noqa: N815 - camelCase 字段为前端 JSON API 契约
from pydantic import BaseModel, Field


class ProductReadinessDTO(BaseModel):
    """单个产品的就绪状态（九节点之一）。

    - product: 产品名（daily_facts/board_facts/stock_core/board_aggregation/review +
      dsa_projection/chip/state_events/auction_anchor）
    - readiness: pending/ready/ready_reused/degraded/unavailable/blocked
    - freshness: fresh/stale/reused
    - isMandatory: 是否核心链（mandatory）
    - isTerminal: run 是否已达终态（不再运行）
    - isConsumable: 当前是否可安全消费（ready/ready_reused）
    """

    product: str = Field(..., description="产品名（九节点之一）")
    readiness: str = Field(..., description="就绪状态")
    freshness: str = Field("fresh", description="新鲜度 fresh/stale/reused")
    isMandatory: bool = Field(True, description="是否核心链")
    isTerminal: bool = Field(False, description="run 是否已达终态")
    isConsumable: bool = Field(False, description="是否可安全消费")
    dataSource: str = Field(
        "run_status",
        description="readiness 数据来源类型：publication_pointer/run_status/derived_from_stock_core/review_publication",
    )
    lineage: dict[str, object] = Field(
        default_factory=dict,
        description="[Corrective-3 §三] 统一结构的真实数据血缘：source_type/"
        "publication_id/pointer_data_run_id/domain_run_id/parent_product/"
        "parent_run_id/source_core_run_id/source_board_run_id/algorithm_version/"
        "parameter_hash/coverage/status/reason_code/published_at/calculated_at/"
        "freshness/retryable/recommended_action",
    )
    reasonCode: str = Field(
        "NONE",
        description="[Corrective-3 §三] 该节点当前状态的原因码（pending 节点也必须给出）",
    )
    retryable: bool = Field(
        False,
        description="[Corrective-3 §四] 是否可重试（后端权威判定，前端不得自行推断）",
    )
    recommendedAction: str = Field(
        "none",
        description="[Corrective-3 §四] 后端输出的推荐恢复动作，前端只展示",
    )
    operation: str = Field(
        "no_operation",
        description="[Corrective-3 §四] 对应的可执行治理操作标识",
    )
    targetRunId: str | None = Field(
        None,
        description="[Corrective-3 §四] 治理操作的目标 run id",
    )


class BenefitsIssueDTO(BaseModel):
    """闭包评估产生的问题项（degraded_reasons 元素）。"""

    product: str = Field(..., description="问题所属产品")
    code: str = Field(..., description="问题码（MANDATORY_UNAVAILABLE/NOT_FULLY_FRESH 等）")
    severity: str = Field("warning", description="error/warning/info")
    recommendedAction: str = Field("", description="建议动作")


class SchedulerReadinessDTO(BaseModel):
    """[PRD Alignment Pass P1-2] 父任务（AfterCloseRun）与其子 SchedulerJobRun 的真实聚合。

    - schedulerJobRunId: 当前 AfterCloseRun 的 job_run_id
    - status: 父任务状态（queued/running/succeeded/failed/...）
    - latestHeartbeat: 父任务最近心跳时间（ISO）
    - leaseEpoch: 当前租约代际
    - isStale: 租约/心跳是否过期（僵尸 worker 判定）
    - totalChildren: enhancement/派生子任务总数
    - processedChildren: 已终态子任务数
    - unreconciledChildren: 未对账子任务（active 但父已就绪 / 状态异常）
    """

    schedulerJobRunId: str | None = Field(
        None, description="[PRD §10] 当前 AfterCloseRun 的 job_run_id"
    )
    status: str | None = Field(None, description="父任务状态")
    latestHeartbeat: str | None = Field(None, description="父任务最近心跳时间 ISO")
    leaseEpoch: int | None = Field(None, description="当前租约代际")
    isStale: bool | None = Field(None, description="租约/心跳是否过期")
    totalChildren: int = Field(0, description="子任务总数")
    processedChildren: int = Field(0, description="已终态子任务数")
    unreconciledChildren: int = Field(0, description="未对账子任务数")


class GovernanceReportDTO(BaseModel):
    """治理报告（Commit G）。

    - pointerLineage: 每个产品的数据来源（publication_pointer/run_status/
      derived_from_stock_core），审计"谁支撑该产品的 readiness"
    - staleChildren: freshness != fresh 的产品（stale/reused）
    - unmatchedActiveChildren: 增强/派生产品仍 active（非终态）而其父 stock_core
      已可消费 → 子产品仍在运行、父已就绪的边缘态
    - readyProducts/pendingProducts/blockedProducts/unavailableProducts:
      按 readiness 分组的产品清单
    - degradedReasons: 闭包评估产生的问题列表
    - scheduler: [PRD Alignment Pass P1-2] 父任务 + 子任务的真实聚合
    """

    pointerLineage: dict[str, dict[str, object]] = Field(default_factory=dict)
    staleChildren: list[str] = Field(default_factory=list)
    unmatchedActiveChildren: list[str] = Field(default_factory=list)
    readyProducts: list[str] = Field(default_factory=list)
    pendingProducts: list[str] = Field(default_factory=list)
    blockedProducts: list[str] = Field(default_factory=list)
    unavailableProducts: list[str] = Field(default_factory=list)
    degradedReasons: list[BenefitsIssueDTO] = Field(default_factory=list)
    scheduler: SchedulerReadinessDTO = Field(default_factory=SchedulerReadinessDTO)


class ProductReadinessResponse(BaseModel):
    """GET /api/v1/admin/readiness/{trade_date} 响应。

    [CHANGE-20260806-005 / Phase 4 / 六态] 显式返回闭包判定所需全部字段，前端禁止
    按时间/空值/颜色猜测 readiness：
    - closure / productionClosure: 闭包状态（六态：pending/blocked/core_ready/
      mandatory_ready_enhancing/degraded_ready/fully_ready）。productionClosure 为
      面向生产的别名，closure 保留向后兼容。
    - mandatoryProductsReady: 全部 mandatory 产品可消费
    - mandatoryProductsFullyFresh: 全部 mandatory 产品 fully fresh
    - requiredCompatibilityReady: 全部 required_compatibility 产品（dsa_projection）
      已真正就绪。[AUD-10] 三分类新增维度：兼容输出不阻断核心，但未就绪时
      不得宣称完整就绪，需可归因
    - enhancementJobsTerminal: 全部 enhancement 产品已达终态
    - allProductsReady: 全部九节点产品可消费（fully_ready 的充要信号）
    - unreconciledChildren: 父任务下未对账（非终态）的增强子任务数（mandatory_ready_enhancing 依据）
    - products: 九节点就绪明细
    - governance: 治理报告
    """

    tradeDate: str = Field(..., description="业务交易日（ISO YYYY-MM-DD）")
    closure: str = Field(..., description="闭包状态（六态）")
    productionClosure: str = Field(..., description="面向生产的闭包状态别名（= closure）")
    mandatoryProductsReady: bool = Field(False)
    mandatoryProductsFullyFresh: bool = Field(False)
    requiredCompatibilityReady: bool = Field(
        False, description="[Phase4.1 corrective] fail-safe 默认 False：调用方漏填时"
        "不得自动报告 ready，避免掩盖 required_compatibility（dsa_projection）缺失"
    )
    enhancementJobsTerminal: bool = Field(False)
    allProductsReady: bool = Field(False)
    unreconciledChildren: int = Field(0)
    products: list[ProductReadinessDTO] = Field(default_factory=list)
    governance: GovernanceReportDTO = Field(default_factory=GovernanceReportDTO)


if __name__ == "__main__":
    # 自测：构造最小合法响应
    resp = ProductReadinessResponse(
        tradeDate="2026-08-05",
        closure="fully_ready",
        productionClosure="fully_ready",
        mandatoryProductsReady=True,
        mandatoryProductsFullyFresh=True,
        enhancementJobsTerminal=True,
        allProductsReady=True,
        unreconciledChildren=0,
        products=[
            ProductReadinessDTO(
                product="stock_core",
                readiness="ready",
                freshness="fresh",
                isMandatory=True,
                isTerminal=True,
                isConsumable=True,
                dataSource="publication_pointer",
            ),
        ],
    )
    assert resp.closure == "fully_ready"
    assert resp.products[0].isConsumable is True
    assert resp.governance.readyProducts == []
    print(f"OK: ProductReadinessResponse closure={resp.closure}")
    print("OK: product_readiness schemas verified")
