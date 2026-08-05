"""ProductReadiness 闭包评估（EPIC-08 E08-T02/T03）。

[PRD V2.1 §10 / next.md EPIC-08]
- closure evaluator：pending / blocked / core_ready / degraded_ready / fully_ready。
- freshness 单独返回：mandatoryProductsReady / mandatoryProductsFullyFresh。
- 本模块为**纯函数**，不连接数据库，可 PURE_UNIT_TEST=1 测试。
  动态聚合（读取 parent/domain runs/publications/pointers/heartbeat/coverage/
  staleness）由调用方（ProductReadinessService 服务层）负责，决策逻辑在此集中。

产品分类：
- mandatory（核心链，缺任一即 blocked）：daily_facts / board_facts / stock_core /
  board_aggregation / review
- enhancement（异步增强，不阻断 mandatory chain）：chip / auction_anchor / state_events

闭包状态语义（E08-T02）：
- pending：任一 mandatory 产品处于 pending（未开始/进行中）
- blocked：任一 mandatory 产品 unavailable / blocked
- fully_ready：全部 mandatory 产品 ready 且 fully fresh，且 enhancement 全部 ready/terminal
- degraded_ready：全部 mandatory 产品 ready（可含 ready_reused/degraded），
  但存在非 fully fresh 或 enhancement 未 terminal
- core_ready：mandatory 核心已 ready，但 enhancement 未 ready（页面可消费核心）

freshness 标志（E08-T03）：
- mandatoryProductsReady：全部 mandatory 产品 ready 或 ready_reused
- mandatoryProductsFullyFresh：全部 mandatory 产品 ready 且 fully fresh
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import func, select

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_BLOCKED,
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
    RUN_STATUS_SUCCEEDED,
    TERMINAL_RUN_STATUS,
)
from app.models.factor_publication import (
    PUBLICATION_KIND_AUCTION_ANCHOR,
    PUBLICATION_KIND_BOARD_FACTS,
    PUBLICATION_KIND_CHIP_CONSENSUS,
    PUBLICATION_KIND_HISTORY_CROSS_SECTION,
    PUBLICATION_KIND_MARKET_AGGREGATION,
    PUBLICATION_KIND_STOCK_CORE,
    SCOPE_TYPE_MARKET,
)

# [V2.1 P1-3] readiness 完整性门槛：matched/eligible 覆盖率须达门槛才 ready，
# 仅 matched>0 不得判 ready（避免存在性检查）。默认 1.0（全量），可配置化。
_DSA_PROJECTION_COVERAGE_THRESHOLD = 1.0
_STATE_EVENTS_COVERAGE_THRESHOLD = 1.0

# 产品分类（P0-1：九节点完整纳入）
MANDATORY_PRODUCTS = frozenset({
    "daily_facts",
    "board_facts",
    "stock_core",
    "board_aggregation",
    "review",
})
# dsa_projection 为 stock_core 的派生投影，随 stock_core 就绪，划为增强不阻断
ENHANCEMENT_PRODUCTS = frozenset({
    "dsa_projection",
    "chip",
    "state_events",
    "auction_anchor",
})
NINE_NODES = MANDATORY_PRODUCTS | ENHANCEMENT_PRODUCTS

# readiness 可消费集合（ready / ready_reused）
CONSUMABLE_READINESS = frozenset({READINESS_READY, READINESS_READY_REUSED})


@dataclass(frozen=True)
class ProductReadinessState:
    """单一产品的就绪状态。

    P0-3：terminal 与 consumable 分离。
    - is_terminal：run 是否已达终态（succeeded/partial/skipped/failed/cancelled），不再运行
    - is_consumable：产品当前是否可安全消费（ready/ready_reused）
    - is_fully_fresh：既 ready 又 fresh
    - lineage：真实数据血缘（G 修正）：run_id / publication_id / pointer_data_run_id /
      source_core_run_id / algorithm_version / coverage / reason_code。
      审计 API 必须返回真实父子关系，而非仅"数据来源类型"字符串。
    """

    product: str
    readiness: str  # pending / ready / ready_reused / degraded / unavailable / blocked
    freshness: str = "fresh"  # fresh / stale / reused
    is_mandatory: bool = True
    is_terminal: bool = False
    lineage: dict[str, Any] = field(default_factory=dict)
    # [PRD Alignment Pass] 增强语义字段，供 evaluate_closure 做合同级闭包判定：
    # - auction: mode（structure_only / hybrid / composite），composite 才是完整就绪
    # - chip / state_events / dsa_projection：是否"真正就绪"（ready 且 lineage 匹配当前 core）
    #   terminal 只代表 run 已终结，不等同于产品就绪。
    auction_mode: str | None = None
    is_product_ready: bool | None = None  # 显式就绪（区分 terminal）；None 由 readiness 推导

    @property
    def is_consumable(self) -> bool:
        return self.readiness in CONSUMABLE_READINESS

    @property
    def is_fully_fresh(self) -> bool:
        return self.readiness == READINESS_READY and self.freshness == "fresh"

    @property
    def is_truly_ready(self) -> bool:
        """[PRD Alignment Pass] 产品"完全就绪"的合同语义。

        优先用 is_product_ready（调用方显式标注）；否则回退到 is_fully_fresh。
        用于 fully_ready 判定：chip/state_events/dsa_projection 必须真正 ready，
        而非仅 run terminal（failed/partial/cancelled 也 terminal 但不可消费）。
        """
        if self.is_product_ready is not None:
            return self.is_product_ready
        return self.is_fully_fresh


@dataclass(frozen=True)
class ClosureEvaluation:
    """闭包评估结果（E08-T02/T03）。

    - closure: pending / blocked / core_ready / degraded_ready / fully_ready
    - mandatory_products_ready: 全部 mandatory 产品 consumable
    - mandatory_products_full_fresh: 全部 mandatory 产品 is_fully_fresh
    - enhancement_jobs_terminal: 全部 enhancement 产品 is_terminal
    - issues: 按产品生成的问题列表（code/severity/product/recommended_action）
    """

    closure: str
    mandatory_products_ready: bool
    mandatory_products_full_fresh: bool
    enhancement_jobs_terminal: bool
    issues: list[dict[str, Any]] = field(default_factory=list)


def _issue(product: str, code: str, severity: str, recommended_action: str) -> dict[str, Any]:
    return {
        "product": product,
        "code": code,
        "severity": severity,
        "recommendedAction": recommended_action,
    }


def _enhancement_terminal(enhancement: list[ProductReadinessState]) -> bool:
    """全部 enhancement 产品是否已达终态（P0-3）。"""
    return all(e.is_terminal for e in enhancement) if enhancement else True


def evaluate_closure(
    products: list[ProductReadinessState],
) -> ClosureEvaluation:
    """评估一次产品 ready 集合的闭包状态（E08-T02/T03 修正版）。

    [P0-4] 分阶段、以 stock_core 为轴心的判定顺序：
        1. blocked：任一 mandatory 产品 unavailable/blocked
        2. pending：stock_core 尚未形成（不可消费）
        3. core_ready：stock_core 可消费，但 board/review 等其他 mandatory 尚未完成
        4. mandatory 全部可消费：
           - fully fresh 且 enhancement 全部终态 → fully_ready
           - 否则 → degraded_ready

    [P0-3] enhancement 终态用 is_terminal，而非 is_consumable，
    避免 chip 失败后永久表现为"仍在运行"。

    Args:
        products: 全部产品就绪状态（mandatory + enhancement，九节点）

    Returns:
        ClosureEvaluation
    """
    by_product = {p.product: p for p in products}
    mandatory = [p for p in products if p.is_mandatory]
    enhancement = [p for p in products if not p.is_mandatory]
    stock_core = by_product.get("stock_core")

    issues: list[dict[str, Any]] = []

    # 1. blocked：mandatory 任一 unavailable/blocked
    for p in mandatory:
        if p.readiness in (READINESS_UNAVAILABLE, READINESS_BLOCKED):
            issues.append(
                _issue(
                    p.product, "MANDATORY_UNAVAILABLE", "critical",
                    f"mandatory product {p.product} unavailable，需恢复或回退",
                )
            )
    if any(p.readiness in (READINESS_UNAVAILABLE, READINESS_BLOCKED) for p in mandatory):
        return ClosureEvaluation(
            closure=CLOSURE_BLOCKED,
            mandatory_products_ready=False,
            mandatory_products_full_fresh=False,
            enhancement_jobs_terminal=_enhancement_terminal(enhancement),
            issues=issues,
        )

    # 2. pending：stock_core 尚未形成
    if stock_core is None or not stock_core.is_consumable:
        return ClosureEvaluation(
            closure=CLOSURE_PENDING,
            mandatory_products_ready=False,
            mandatory_products_full_fresh=False,
            enhancement_jobs_terminal=_enhancement_terminal(enhancement),
            issues=issues,
        )

    # 非 fully fresh 的产品问题（degraded_ready 提示）
    for p in mandatory:
        if p.is_consumable and not p.is_fully_fresh:
            issues.append(
                _issue(
                    p.product, "NOT_FULLY_FRESH", "warning",
                    f"mandatory product {p.product} 非 fully fresh"
                    f"（readiness={p.readiness}, freshness={p.freshness}）",
                )
            )

    mandatory_ready = all(p.is_consumable for p in mandatory)

    # 3. core_ready：stock_core 可消费，但其他 mandatory 未完成
    if not mandatory_ready:
        return ClosureEvaluation(
            closure=CLOSURE_CORE_READY,
            mandatory_products_ready=False,
            mandatory_products_full_fresh=False,
            enhancement_jobs_terminal=_enhancement_terminal(enhancement),
            issues=issues,
        )

    mandatory_full_fresh = all(p.is_fully_fresh for p in mandatory)
    enhancement_terminal = _enhancement_terminal(enhancement)
    # [PRD Alignment Pass P0-1] enhancement 必须"真正就绪"，而非仅 run terminal。
    # failed/partial/cancelled 也 terminal 但不可消费，必须排除。
    enhancement_all_ready = all(e.is_truly_ready for e in enhancement)
    # auction 必须 composite 才构成完整就绪；仅当 auction 已终态才强制该约束，
    # 未终态（pending/running）不应降级为 degraded，而是 core_ready / enhanced_pending。
    auction = by_product.get("auction_anchor")
    if auction is not None and auction.is_terminal:
        auction_composite = auction.auction_mode == "composite"
    else:
        auction_composite = True

    # 4. fully_ready
    if mandatory_full_fresh and enhancement_all_ready and auction_composite:
        return ClosureEvaluation(
            closure=CLOSURE_FULLY_READY,
            mandatory_products_ready=True,
            mandatory_products_full_fresh=True,
            enhancement_jobs_terminal=True,
            issues=issues,
        )

    # 5. degraded_ready
    return ClosureEvaluation(
        closure=CLOSURE_DEGRADED_READY,
        mandatory_products_ready=True,
        mandatory_products_full_fresh=mandatory_full_fresh,
        enhancement_jobs_terminal=enhancement_terminal,
        issues=issues,
    )


def compute_freshness_flags(
    products: list[ProductReadinessState],
) -> dict[str, bool]:
    """独立返回 freshness 标志（E08-T03）。

    Returns:
        {
            "mandatoryProductsReady": bool,
            "mandatoryProductsFullyFresh": bool,
        }
    """
    mandatory = [p for p in products if p.is_mandatory]
    return {
        "mandatoryProductsReady": all(p.is_consumable for p in mandatory) if mandatory else True,
        "mandatoryProductsFullyFresh": all(p.is_fully_fresh for p in mandatory) if mandatory else True,
    }


# ============================================================================
# Governance 治理报告（Commit G）
# ============================================================================

# 派生投影产品：readiness 直接继承 stock_core，其"数据源"标为派生
_DERIVED_PRODUCTS = frozenset({"dsa_projection", "state_events"})


@dataclass(frozen=True)
class GovernanceReport:
    """一次闭包评估的治理视图（Commit G，已修正真实 lineage）。

    - pointer_lineage: 每个产品的真实数据血缘 dict（run_id / publication_id /
      pointer_data_run_id / source_core_run_id / algorithm_version / coverage /
      reason_code / source_type），用于审计"谁支撑该产品的 readiness"
    - stale_children: freshness != fresh 的产品（stale / reused）
    - unmatched_active_children: 增强/派生产品仍 active（非终态）而其父
      stock_core 已可消费 → 表明子产品仍在运行、父已就绪的边缘态
    - ready_products / pending_products / blocked_products / unavailable_products:
      按 readiness 分组的产品清单
    - degraded_reasons: 闭包评估产生的问题列表（含 code/severity）
    """

    pointer_lineage: dict[str, dict[str, Any]] = field(default_factory=dict)
    stale_children: list[str] = field(default_factory=list)
    unmatched_active_children: list[str] = field(default_factory=list)
    ready_products: list[str] = field(default_factory=list)
    pending_products: list[str] = field(default_factory=list)
    blocked_products: list[str] = field(default_factory=list)
    unavailable_products: list[str] = field(default_factory=list)
    degraded_reasons: list[dict[str, Any]] = field(default_factory=list)
    # [PRD Alignment Pass P1-2] 父任务 + 子任务真实聚合
    scheduler: SchedulerReadiness | None = None


@dataclass
class SchedulerReadiness:
    """[PRD Alignment Pass P1-2] 父任务（AfterCloseRun）+ 子 SchedulerJobRun 真实聚合。"""

    scheduler_job_run_id: str | None = None
    status: str | None = None
    latest_heartbeat: str | None = None
    lease_epoch: int | None = None
    is_stale: bool | None = None
    total_children: int = 0
    processed_children: int = 0
    unreconciled_children: int = 0


# [Corrective-3 §三] 统一 lineage 结构：每个节点都必须返回全部这些键。
# 缺失值显式为 None，不允许键缺席，使前端与审计可以稳定消费。
LINEAGE_KEYS: tuple[str, ...] = (
    "source_type",
    "publication_id",
    "pointer_data_run_id",
    "domain_run_id",
    "parent_product",
    "parent_run_id",
    "source_core_run_id",
    "source_board_run_id",
    "algorithm_version",
    "parameter_hash",
    "coverage",
    "status",
    "reason_code",
    "published_at",
    "calculated_at",
    "freshness",
    "retryable",
    "recommended_action",
)

# [Corrective-3 §四] 治理动作由后端输出。前端只展示，不重新解释 reason code。
# 与 chip_consensus_run_lifecycle.ACTION_RETRY_CHIP_PUBLICATION 保持同一取值。
ACTION_RETRY_CHIP_PUBLICATION = "retry_chip_publication"

# reason_code → (retryable, recommended_action, operation)
_ACTION_BY_REASON: dict[str, tuple[bool, str, str]] = {
    # chip：run 成功但 publication 缺失 —— 必须可治理
    "CHIP_PUBLICATION_MISSING": (
        True, ACTION_RETRY_CHIP_PUBLICATION, "republish_chip_consensus",
    ),
    "CHIP_PUBLICATION_FAILED": (
        True, ACTION_RETRY_CHIP_PUBLICATION, "republish_chip_consensus",
    ),
    "CHIP_PUBLICATION_LINEAGE_REJECTED": (
        False, "inspect_chip_lineage_conflict", "manual_investigation",
    ),
    "CHIP_PARTIAL": (True, "retry_failed_chip_instruments", "rerun_chip_consensus"),
    "CHIP_FAILED": (True, "rerun_chip_consensus", "rerun_chip_consensus"),
    "CHIP_CANCELLED": (True, "rerun_chip_consensus", "rerun_chip_consensus"),
    "NO_CHIP_RUN": (True, "trigger_chip_consensus", "trigger_chip_consensus"),
    # auction
    "AUCTION_STRUCTURE_ONLY": (
        True, "await_chip_upgrade", "regenerate_auction_anchor",
    ),
    "AUCTION_FAILED": (True, "rerun_auction_anchor", "rerun_auction_anchor"),
    "AUCTION_CANCELLED": (True, "rerun_auction_anchor", "rerun_auction_anchor"),
    "NO_AUCTION_RUN": (True, "trigger_auction_anchor", "trigger_auction_anchor"),
    # board facts
    "REUSED_PREVIOUS_RUN": (True, "rerun_board_facts", "rerun_board_facts"),
    "NO_RUN": (True, "trigger_upstream_job", "trigger_upstream_job"),
    "RUN_FAILED": (True, "rerun_board_facts", "rerun_board_facts"),
    "RUN_CANCELLED": (True, "rerun_board_facts", "rerun_board_facts"),
    # review
    "NO_REVIEW_RUN": (True, "trigger_market_review", "trigger_market_review"),
    "REVIEW_NOT_PUBLISHED": (True, "publish_market_review", "publish_market_review"),
    # [Corrective-3.1] run 自称 published 但 factor_publications 无 pointer
    "REVIEW_POINTER_MISSING": (
        True, "publish_market_review", "publish_market_review",
    ),
    "REVIEW_FAILED": (True, "rerun_market_review", "rerun_market_review"),
    "REVIEW_CANCELLED": (True, "rerun_market_review", "rerun_market_review"),
    # 派生投影
    "PARENT_NOT_CONSUMABLE": (False, "await_parent_product", "no_operation"),
    "NO_PROJECTION": (True, "rebuild_dsa_projection", "rebuild_dsa_projection"),
    "NO_STATE_EVENTS": (True, "rebuild_state_events", "rebuild_state_events"),
    # [Corrective-3.1 §P1] 当日有产物但不属于当前 core run → 必须重建而非放行
    "PROJECTION_LINEAGE_MISMATCH": (
        True, "rebuild_dsa_projection", "rebuild_dsa_projection",
    ),
    "STATE_EVENTS_LINEAGE_MISMATCH": (
        True, "rebuild_state_events", "rebuild_state_events",
    ),
    # 通用 pending
    "NO_PUBLICATION": (True, "await_publication", "trigger_upstream_job"),
}

_DEFAULT_ACTION: tuple[bool, str, str] = (False, "none", "no_operation")


def resolve_governance_action(
    reason_code: str | None,
    readiness: str,
) -> tuple[bool, str, str]:
    """[Corrective-3 §四] 后端解析治理动作，返回 (retryable, action, operation)。

    这是治理动作的**唯一**事实源。前端不得再自行根据 reason code 猜测业务动作。
    """
    if readiness in CONSUMABLE_READINESS and reason_code in (
        None, "NONE", "FRESH_PUBLICATION", "REVIEW_PUBLISHED", "CHIP_SUCCEEDED",
        "UPGRADED_FROM_PARENT", "AUCTION_SUCCEEDED",
    ):
        return _DEFAULT_ACTION
    return _ACTION_BY_REASON.get(reason_code or "", _DEFAULT_ACTION)


def _product_lineage(p: ProductReadinessState) -> dict[str, Any]:
    """[Corrective-3 §三] 输出统一结构的真实数据血缘。

    每个节点返回 `LINEAGE_KEYS` 的全部键（缺失显式 None），并由后端补齐
    status / freshness / retryable / recommended_action。
    """
    reason_code = p.lineage.get("reason_code")
    retryable, action, operation = resolve_governance_action(reason_code, p.readiness)

    base: dict[str, Any] = {key: p.lineage.get(key) for key in LINEAGE_KEYS}
    base["source_type"] = p.lineage.get("source_type", "unknown")
    base["reason_code"] = reason_code or "NONE"
    base["status"] = p.lineage.get("status", p.readiness)
    base["freshness"] = p.freshness
    base["readiness"] = p.readiness
    # 后端权威治理动作（§四）
    base["retryable"] = p.lineage.get("retryable", retryable)
    base["recommended_action"] = p.lineage.get("recommended_action", action)
    base["operation"] = p.lineage.get("operation", operation)
    base["target_run_id"] = (
        p.lineage.get("domain_run_id")
        or p.lineage.get("run_id")
        or p.lineage.get("review_run_id")
        or p.lineage.get("pointer_data_run_id")
    )
    # 兼容既有键
    if "run_id" in p.lineage:
        base["run_id"] = p.lineage["run_id"]
    if "review_run_id" in p.lineage:
        base["review_run_id"] = p.lineage["review_run_id"]
    if "derived_from" in p.lineage:
        base["derived_from"] = p.lineage["derived_from"]
    return base


def evaluate_governance(
    products: list[ProductReadinessState],
    closure: ClosureEvaluation,
    scheduler: SchedulerReadiness | None = None,
) -> GovernanceReport:
    """纯函数：从产品就绪状态 + 闭包评估生成治理报告（Commit G，已修正真实 lineage）。

    不连接数据库；所有信号均由 ProductReadinessState 推导，可 PURE_UNIT_TEST 测试。
    [G 修正] pointer_lineage 返回每个产品的真实数据血缘 dict，而非字符串来源类型。
    [PRD Alignment Pass P1-2] scheduler 承载父任务 + 子任务真实聚合；
    当提供时，unmatched_active_children 优先采用真实子任务状态。

    Args:
        products: 全部九节点产品状态
        closure: 对应的闭包评估结果
        scheduler: 父任务 + 子任务真实聚合（可选）

    Returns:
        GovernanceReport
    """
    by_product = {p.product: p for p in products}
    stock_core = by_product.get("stock_core")
    core_consumable = stock_core is not None and stock_core.is_consumable

    pointer_lineage: dict[str, dict[str, Any]] = {}
    stale_children: list[str] = []
    unmatched_active_children: list[str] = []
    ready_products: list[str] = []
    pending_products: list[str] = []
    blocked_products: list[str] = []
    unavailable_products: list[str] = []

    for p in products:
        pointer_lineage[p.product] = _product_lineage(p)
        if p.freshness != "fresh":
            stale_children.append(p.product)
        if not p.is_mandatory and not p.is_terminal and core_consumable:
            unmatched_active_children.append(p.product)
        if p.readiness == READINESS_READY or p.readiness == READINESS_READY_REUSED:
            ready_products.append(p.product)
        elif p.readiness == READINESS_PENDING:
            pending_products.append(p.product)
        elif p.readiness in (READINESS_UNAVAILABLE, READINESS_BLOCKED):
            unavailable_products.append(p.product)
        elif p.readiness == READINESS_DEGRADED:
            blocked_products.append(p.product)

    # [PRD Alignment Pass P1-2] 优先采用真实子任务聚合判定未对账子任务
    if scheduler is not None and scheduler.unreconciled_children > 0:
        unmatched_active_children = unmatched_active_children or []
        for p in products:
            if not p.is_mandatory and p.is_terminal is False and core_consumable:
                if p.product not in unmatched_active_children:
                    unmatched_active_children.append(p.product)

    return GovernanceReport(
        pointer_lineage=pointer_lineage,
        stale_children=sorted(stale_children),
        unmatched_active_children=sorted(unmatched_active_children),
        ready_products=sorted(ready_products),
        pending_products=sorted(pending_products),
        blocked_products=sorted(blocked_products),
        unavailable_products=sorted(unavailable_products),
        scheduler=scheduler,
        degraded_reasons=list(closure.issues),
    )


def _iso(value: Any) -> str | None:
    """安全地把 datetime 转 ISO 字符串。"""
    return value.isoformat() if hasattr(value, "isoformat") else None


def _sid(value: Any) -> str | None:
    """安全地把 id 转字符串；None 保持 None（不得退化为空串）。"""
    return str(value) if value is not None else None


def _num(value: Any) -> float | None:
    """安全地把 Decimal/数值转 float（JSON 可序列化）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _publication_lineage(
    pub: Any,
    domain_run: Any | None,
    source_core_run_id: str | None = None,
) -> dict[str, Any]:
    """[Corrective-3 §三] 由 FactorPublication + 领域 run 联查生成完整 lineage。

    `source_core_run_id` 优先取领域 run 的真实字段，其次取显式传入值，
    不得默认 None。
    """
    run_core = _sid(getattr(domain_run, "source_core_run_id", None))
    return {
        "source_type": "publication_pointer",
        "publication_id": _sid(getattr(pub, "id", None)),
        "pointer_data_run_id": _sid(getattr(pub, "data_run_id", None)),
        "domain_run_id": _sid(getattr(domain_run, "id", None))
        or _sid(getattr(pub, "data_run_id", None)),
        "source_core_run_id": run_core or source_core_run_id,
        "source_board_run_id": _sid(getattr(domain_run, "source_board_run_id", None)),
        "algorithm_version": getattr(pub, "algorithm_version", None)
        or getattr(domain_run, "algorithm_version", None),
        "parameter_hash": getattr(pub, "parameter_hash", None)
        or getattr(domain_run, "parameter_hash", None),
        "coverage": _num(
            getattr(pub, "coverage_ratio", None)
            if getattr(pub, "coverage_ratio", None) is not None
            else getattr(domain_run, "coverage_ratio", None),
        ),
        "status": getattr(domain_run, "status", None) or "published",
        "reason_code": "FRESH_PUBLICATION",
        "published_at": _iso(getattr(pub, "published_at", None)),
        "calculated_at": _iso(
            getattr(domain_run, "finished_at", None)
            or getattr(domain_run, "created_at", None),
        ),
    }


# publication_kind → 领域 run 模型（延迟导入，避免循环依赖）
def _domain_run_model_map() -> dict[str, Any]:
    from app.models.auction_anchor_run import AuctionAnchorRun
    from app.models.board_facts_run import BoardFactsRun
    from app.models.chip_consensus_run import ChipConsensusRun

    return {
        PUBLICATION_KIND_BOARD_FACTS: BoardFactsRun,
        PUBLICATION_KIND_CHIP_CONSENSUS: ChipConsensusRun,
        PUBLICATION_KIND_AUCTION_ANCHOR: AuctionAnchorRun,
    }


class _LazyModelMap(dict):  # type: ignore[type-arg]
    """延迟解析的 publication_kind → 领域 run 模型映射。"""

    _loaded = False

    def get(self, key: Any, default: Any = None) -> Any:
        if not self._loaded:
            try:
                self.update(_domain_run_model_map())
            except Exception:
                pass
            self._loaded = True
        return super().get(key, default)


_DOMAIN_RUN_MODEL_BY_KIND: Any = _LazyModelMap()


# ============================================================================
# ProductReadinessService — 动态聚合服务层（E08-T01）
# ============================================================================


class ProductReadinessService:
    """动态聚合各领域 run/publication 到 ProductReadinessState，并调用闭包评估。

    服务层职责（EPIC-08 E08-T01）：
    - 读取当日各产品 run / publication / pointer
    - 映射为 ProductReadinessState（readiness + freshness）
    - 调用 evaluate_closure 得到闭包状态
    纯决策逻辑保留在 evaluate_closure；本服务只负责 DB 查询与状态映射。

    用法：
        service = ProductReadinessService()
        ev = await service.evaluate_for_trade_date(db, trade_date)
        print(ev.closure, ev.issues)
    """

    async def collect_states(
        self,
        db: Any,
        trade_date: date,
    ) -> list[ProductReadinessState]:
        """聚合指定交易日的九节点就绪状态（Commit G）。

        供 evaluate_for_trade_date（求闭包）与 admin readiness API（治理报告）共用，
        保证同一入口、同一查询顺序，避免治理报告与闭包评估口径不一致。

        Args:
            db: 异步数据库会话
            trade_date: 业务交易日

        Returns:
            九节点 ProductReadinessState 列表
        """
        # stock_core 只计算一次，派生投影复用（compute-once）
        daily = await self._daily_facts_state(db, trade_date)
        board_facts = await self._board_facts_state(db, trade_date)
        stock_core = await self._stock_core_state(db, trade_date)
        board_aggregation = await self._board_aggregation_state(db, trade_date)
        review = await self._review_state(db, trade_date)
        return [
            daily,
            board_facts,
            stock_core,
            board_aggregation,
            review,
            await self._dsa_projection_state(db, trade_date, stock_core),
            await self._chip_state(db, trade_date),
            await self._state_events_state(db, trade_date, stock_core),
            await self._auction_state(db, trade_date),
        ]

    async def collect_scheduler(
        self,
        db: Any,
        trade_date: date,
    ) -> SchedulerReadiness:
        """[PRD Alignment Pass P1-2] 聚合父任务（AfterCloseRun）+ 子 SchedulerJobRun 真实状态。

        查询当前 trade_date 的 after_close_orchestrator job_run（父任务），
        及其 enhancement 子任务（chip_consensus / auction_anchor / state_events 等）
        的真实 job_run，计算 total/processed/unreconciled 与 heartbeat/lease 新鲜度。

        若父任务不存在，返回全 None 的 SchedulerReadiness（不报错，纯聚合）。

        Args:
            db: 异步数据库会话
            trade_date: 业务交易日

        Returns:
            SchedulerReadiness
        """
        from app.models.scheduler_job_run import SchedulerJobRun

        trade_date_str = trade_date.isoformat()
        parent = await db.scalar(
            select(SchedulerJobRun)
            .where(
                SchedulerJobRun.job_name == "after_close_orchestrator",
                SchedulerJobRun.business_date == trade_date_str,
            )
            .order_by(SchedulerJobRun.created_at.desc())
            .limit(1)
        )
        if parent is None:
            return SchedulerReadiness()

        # enhancement 子任务：同一 business_date 的派生产品 job_run
        enhancement_jobs = {
            "chip_consensus",
            "auction_anchor",
            "state_events",
        }
        children = (
            await db.scalars(
                select(SchedulerJobRun)
                .where(
                    SchedulerJobRun.business_date == trade_date_str,
                    SchedulerJobRun.job_name.in_(enhancement_jobs),
                )
            )
        ).all()

        total = len(children)
        processed = sum(1 for c in children if c.status in TERMINAL_RUN_STATUS)
        unreconciled = total - processed

        # 僵尸 worker 判定：父任务 running 但租约/心跳已过期
        is_stale: bool | None = None
        if getattr(parent, "status", None) == "running":
            lease_expires = getattr(parent, "lease_expires_at", None)
            now = datetime.now(UTC)
            if lease_expires is not None:
                is_stale = lease_expires < now

        return SchedulerReadiness(
            scheduler_job_run_id=_sid(getattr(parent, "id", None)),
            status=getattr(parent, "status", None),
            latest_heartbeat=_iso(getattr(parent, "heartbeat_at", None)),
            lease_epoch=getattr(parent, "lease_epoch", None),
            is_stale=is_stale,
            total_children=total,
            processed_children=processed,
            unreconciled_children=unreconciled,
        )

    async def evaluate_for_trade_date(
        self,
        db: Any,
        trade_date: date,
    ) -> ClosureEvaluation:
        """评估指定交易日的产品闭包状态（P0-1：完整九节点）。

        九节点：daily_facts / board_facts / stock_core / board_aggregation /
        review（mandatory）+ dsa_projection / chip / state_events / auction_anchor（enhancement）。

        Args:
            db: 异步数据库会话
            trade_date: 业务交易日

        Returns:
            ClosureEvaluation
        """
        states = await self.collect_states(db, trade_date)
        return evaluate_closure(states)

    # ---- 通用 helper：publication pointer 决定 readiness（P0-2）----

    async def _publication_readiness(
        self,
        db: Any,
        trade_date: date,
        publication_kind: str,
        product: str,
        *,
        is_mandatory: bool,
        scope_type: str = SCOPE_TYPE_MARKET,
        source_core_run_id: str | None = None,
    ) -> ProductReadinessState | None:
        """读取发布指针；存在则返回 ready（terminal=True）并携带真实 lineage。

        [G 修正] lineage 返回真实 run/publication/pointer/coverage/reason，
        而非仅"数据来源类型"字符串，支撑真正的血统审计。
        """
        from app.models.factor_publication import FactorPublication

        pub = await db.scalar(
            select(FactorPublication)
            .where(
                FactorPublication.publication_kind == publication_kind,
                FactorPublication.scope_type == scope_type,
                FactorPublication.trade_date == trade_date,
            )
            .limit(1)
        )
        if pub is None:
            return None

        # [Corrective-3 §三] publication 节点必须与对应领域 run 联查，
        # source_core_run_id 不得默认 None。
        domain_run = await self._load_domain_run(db, publication_kind, pub.data_run_id)
        lineage = _publication_lineage(pub, domain_run, source_core_run_id)
        return ProductReadinessState(
            product, READINESS_READY, "fresh",
            is_mandatory=is_mandatory, is_terminal=True, lineage=lineage,
        )

    @staticmethod
    async def _load_domain_run(
        db: Any, publication_kind: str, data_run_id: Any,
    ) -> Any | None:
        """按 publication_kind 联查对应领域 run，用于补全真实 lineage。"""
        if data_run_id is None:
            return None
        model = _DOMAIN_RUN_MODEL_BY_KIND.get(publication_kind)
        if model is None:
            return None
        try:
            return await db.scalar(
                select(model).where(model.id == data_run_id).limit(1)
            )
        except Exception:  # 领域 run 查询失败不得阻断 readiness 评估
            return None

    # ---- 各产品状态映射 ----

    async def _daily_facts_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """daily_facts：历史截面（history_cross_section）发布指针。"""
        st = await self._publication_readiness(
            db, trade_date, PUBLICATION_KIND_HISTORY_CROSS_SECTION,
            "daily_facts", is_mandatory=True,
        )
        if st is not None:
            return st
        return ProductReadinessState(
            "daily_facts", READINESS_PENDING, "fresh",
            lineage={"source_type": "publication_pointer",
                     "reason_code": "NO_PUBLICATION"},
        )

    async def _board_facts_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """board_facts：以发布指针为准；latest run 单列（P0-2/P0-7）。

        P0-7：若指针 data_run 是 reused 旧 run，则 readiness=ready_reused（degraded）。
        [G 修正] 填充真实 lineage（publication_id / pointer_data_run_id / algorithm_version /
        coverage / reason_code）。
        """
        from app.models.factor_publication import FactorPublication

        pub = await db.scalar(
            select(FactorPublication)
            .where(
                FactorPublication.publication_kind == PUBLICATION_KIND_BOARD_FACTS,
                FactorPublication.scope_type == SCOPE_TYPE_MARKET,
                FactorPublication.trade_date == trade_date,
            )
            .limit(1)
        )
        if pub is not None:
            from app.models.board_facts_run import BoardFactsRun

            data_run = await db.scalar(
                select(BoardFactsRun)
                .where(BoardFactsRun.id == pub.data_run_id)
                .limit(1)
            )
            reused = data_run is not None and data_run.status == "reused_previous"
            lineage = _publication_lineage(pub, data_run)
            lineage["reason_code"] = (
                "REUSED_PREVIOUS_RUN" if reused else "FRESH_PUBLICATION"
            )
            if reused:
                return ProductReadinessState(
                    "board_facts", READINESS_READY_REUSED, "reused",
                    is_terminal=True, lineage=lineage,
                )
            return ProductReadinessState(
                "board_facts", READINESS_READY, "fresh",
                is_terminal=True, lineage=lineage,
            )
        from app.models.board_facts_run import BoardFactsRun

        run = await db.scalar(
            select(BoardFactsRun)
            .where(BoardFactsRun.trade_date == trade_date)
            .order_by(BoardFactsRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState(
                "board_facts", READINESS_PENDING, "fresh",
                lineage={"source_type": "run_status", "reason_code": "NO_RUN"},
            )
        if run.status in TERMINAL_RUN_STATUS and run.status != RUN_STATUS_SUCCEEDED:
            return ProductReadinessState(
                "board_facts", READINESS_UNAVAILABLE, "fresh", is_terminal=True,
                lineage={
                    "source_type": "run_status",
                    "run_id": str(getattr(run, "id", "")),
                    "reason_code": f"RUN_{run.status.upper()}",
                },
            )
        return ProductReadinessState(
            "board_facts", READINESS_PENDING, "fresh",
            lineage={"source_type": "run_status", "run_id": str(getattr(run, "id", ""))},
        )

    async def _stock_core_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """stock_core：stock_core 发布指针。"""
        st = await self._publication_readiness(
            db, trade_date, PUBLICATION_KIND_STOCK_CORE,
            "stock_core", is_mandatory=True,
        )
        if st is not None:
            return st
        return ProductReadinessState(
            "stock_core", READINESS_PENDING, "fresh",
            lineage={"source_type": "publication_pointer",
                     "reason_code": "NO_PUBLICATION"},
        )

    async def _board_aggregation_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """board_aggregation：market_aggregation 发布指针。"""
        st = await self._publication_readiness(
            db, trade_date, PUBLICATION_KIND_MARKET_AGGREGATION,
            "board_aggregation", is_mandatory=True,
        )
        if st is not None:
            return st
        return ProductReadinessState(
            "board_aggregation", READINESS_PENDING, "fresh",
            lineage={"source_type": "publication_pointer",
                     "reason_code": "NO_PUBLICATION"},
        )

    async def _review_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """review：以正式 FactorPublication pointer 为准（[Corrective-3.1 §P1]）。

        Corrective-3 只查 MarketReviewRun 并检查 run.status/published_at，会把一个
        "曾经发布过、但已不是当前 pointer" 的旧 run 误判为 ready。现在真正读取
        `factor_publications`（publication_kind=market_review），并要求 pointer 的
        data_run_id 与 latest run 一致，否则判定 lineage 失配。
        """
        from app.models.factor_publication import FactorPublication
        from app.models.market_review import MarketReviewRun
        from app.services.review_publication_service import (
            PUBLICATION_KIND_MARKET_REVIEW,
        )

        pub = await db.scalar(
            select(FactorPublication)
            .where(
                FactorPublication.publication_kind == PUBLICATION_KIND_MARKET_REVIEW,
                FactorPublication.trade_date == trade_date,
            )
            .limit(1)
        )
        if pub is not None:
            pub_run = await db.scalar(
                select(MarketReviewRun)
                .where(MarketReviewRun.id == pub.data_run_id)
                .limit(1)
            )
            lineage = _publication_lineage(pub, pub_run)
            lineage["source_type"] = "review_publication"
            lineage["review_run_id"] = _sid(getattr(pub, "data_run_id", None))
            lineage["reason_code"] = "REVIEW_PUBLISHED"
            return ProductReadinessState(
                "review", READINESS_READY, "fresh", is_terminal=True,
                lineage=lineage,
            )

        # 无正式 pointer → 回落到 run 状态，但绝不判 ready。
        run = await db.scalar(
            select(MarketReviewRun)
            .where(MarketReviewRun.trade_date == trade_date)
            .order_by(MarketReviewRun.created_at.desc())
            .limit(1)
        )

        if run is None:
            return ProductReadinessState(
                "review", READINESS_PENDING, "fresh",
                lineage={"source_type": "review_publication",
                         "reason_code": "NO_REVIEW_RUN"},
            )

        published_at = getattr(run, "published_at", None)
        base = {
            "source_type": "review_publication",
            "domain_run_id": _sid(getattr(run, "id", None)),
            "review_run_id": _sid(getattr(run, "id", None)),
            "algorithm_version": getattr(run, "algorithm_version", None),
            "parameter_hash": getattr(run, "filter_version", None),
            "coverage": _num(getattr(run, "coverage_ratio", None)),
            "status": run.status,
            "published_at": _iso(published_at),
            "calculated_at": _iso(
                getattr(run, "completed_at", None) or getattr(run, "created_at", None),
            ),
        }
        # [Corrective-3.1 §P1] 走到这里说明 factor_publications 中没有 market_review
        # pointer。此时即使 run.status=published 也**不得**判 ready —— 那只是一个
        # 历史发布过、现已不是当前 pointer 的旧 run。
        if run.status == "published":
            return ProductReadinessState(
                "review", READINESS_DEGRADED, "stale", is_terminal=True,
                lineage={
                    **base,
                    "reason_code": "REVIEW_POINTER_MISSING" if published_at is not None
                    else "REVIEW_NOT_PUBLISHED",
                },
            )
        if run.status in TERMINAL_RUN_STATUS:
            return ProductReadinessState(
                "review", READINESS_UNAVAILABLE, "fresh", is_terminal=True,
                lineage={**base, "reason_code": f"REVIEW_{run.status.upper()}"},
            )
        return ProductReadinessState(
            "review", READINESS_PENDING, "fresh",
            lineage={**base, "reason_code": "REVIEW_RUNNING"},
        )

    async def _dsa_projection_state(
        self, db: Any, trade_date: date, core: ProductReadinessState,
    ) -> ProductReadinessState:
        """[Corrective-3 §三] dsa_projection 必须检查真实投影产物，不得随 stock_core 自动 ready。"""
        parent = {
            "source_type": "derived_projection",
            "parent_product": "stock_core",
            "derived_from": "stock_core",
            "parent_run_id": core.lineage.get("pointer_data_run_id"),
            "source_core_run_id": core.lineage.get("pointer_data_run_id")
            or core.lineage.get("domain_run_id"),
        }
        if not core.is_consumable:
            return ProductReadinessState(
                "dsa_projection", READINESS_PENDING, "fresh",
                is_mandatory=False, is_terminal=False,
                lineage={**parent, "reason_code": "PARENT_NOT_CONSUMABLE"},
            )

        # [Corrective-3.1 §P1] 必须按当前 core run 精确归属，不能只看"当日有快照"。
        core_run_id = parent["source_core_run_id"]
        counts = await self._count_dsa_projections(db, trade_date, core_run_id)
        matched, total = counts["matched"], counts["total"]
        detail = {"projection_matched": matched, "projection_total": total}

        # [V2.1 P1-3] 完整性门槛：coverage = matched / total，须达阈值才 ready；
        # 仅 matched>0 不得判 ready（存在性检查已被禁止）。
        coverage_ratio = (matched / total) if total > 0 else 0.0
        detail = {
            **detail,
            "eligible_count": total,
            "matched_count": matched,
            "coverage_ratio": round(coverage_ratio, 4),
            "coverage_threshold": _DSA_PROJECTION_COVERAGE_THRESHOLD,
        }
        if matched > 0 and coverage_ratio >= _DSA_PROJECTION_COVERAGE_THRESHOLD:
            return ProductReadinessState(
                "dsa_projection", READINESS_READY, "fresh",
                is_mandatory=False, is_terminal=True,
                lineage={**parent, **detail, "reason_code": "PROJECTION_FULL_COVERAGE",
                         "coverage": matched, "status": "present"},
            )
        if matched > 0:
            # 存在归属当前 core run 的投影，但覆盖率未达门槛（有残留或 lineage 不匹配）
            return ProductReadinessState(
                "dsa_projection", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=True,
                lineage={**parent, **detail,
                         "reason_code": "PROJECTION_PARTIAL_COVERAGE",
                         "coverage": matched, "status": "partial"},
            )
        if total > 0:
            # 当日存在快照但没有一条归属当前 core run → 是上一轮残留，不得判 ready。
            return ProductReadinessState(
                "dsa_projection", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=False,
                lineage={**parent, **detail,
                         "reason_code": "PROJECTION_LINEAGE_MISMATCH",
                         "coverage": 0, "status": "stale_lineage"},
            )
        return ProductReadinessState(
            "dsa_projection", READINESS_PENDING, "stale",
            is_mandatory=False, is_terminal=False,
            lineage={**parent, **detail, "reason_code": "NO_PROJECTION",
                     "coverage": 0, "status": "missing"},
        )

    async def _state_events_state(
        self, db: Any, trade_date: date, core: ProductReadinessState,
    ) -> ProductReadinessState:
        """[Corrective-3 §三] state_events 必须检查真实 candidate/confirmed 状态。"""
        parent = {
            "source_type": "derived_projection",
            "parent_product": "stock_core",
            "derived_from": "stock_core",
            "parent_run_id": core.lineage.get("pointer_data_run_id"),
            "source_core_run_id": core.lineage.get("pointer_data_run_id")
            or core.lineage.get("domain_run_id"),
        }
        if not core.is_consumable:
            return ProductReadinessState(
                "state_events", READINESS_PENDING, "fresh",
                is_mandatory=False, is_terminal=False,
                lineage={**parent, "reason_code": "PARENT_NOT_CONSUMABLE"},
            )

        # [Corrective-3.1 §P1] 事件必须归属当前 core run；算法版本一并暴露。
        core_run_id = parent["source_core_run_id"]
        counts = await self._count_state_events(db, trade_date, core_run_id)
        matched, total = counts["matched"], counts["total"]
        detail = {
            "event_type_counts": counts["by_type"],
            "state_events_matched": matched,
            "state_events_total": total,
            "algorithm_versions": counts["algorithm_versions"],
        }

        # [V2.1 P1-3] 完整性门槛：coverage = matched / total，须达阈值才 ready；
        # 仅 matched>0 不得判 ready（存在性检查已被禁止）。完整生命周期由 by_type 非空佐证。
        coverage_ratio = (matched / total) if total > 0 else 0.0
        lifecycle_complete = bool(counts["by_type"]) and coverage_ratio >= _STATE_EVENTS_COVERAGE_THRESHOLD
        detail = {
            **detail,
            "eligible_count": total,
            "matched_count": matched,
            "coverage_ratio": round(coverage_ratio, 4),
            "coverage_threshold": _STATE_EVENTS_COVERAGE_THRESHOLD,
            "lifecycle_complete": lifecycle_complete,
        }
        if matched > 0 and coverage_ratio >= _STATE_EVENTS_COVERAGE_THRESHOLD:
            return ProductReadinessState(
                "state_events", READINESS_READY, "fresh",
                is_mandatory=False, is_terminal=True,
                lineage={**parent, **detail, "reason_code": "STATE_EVENTS_FULL_COVERAGE",
                         "coverage": matched, "status": "present"},
            )
        if matched > 0:
            # 存在归属当前 core run 的事件，但覆盖率未达门槛或生命周期不完整
            return ProductReadinessState(
                "state_events", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=True,
                lineage={**parent, **detail,
                         "reason_code": "STATE_EVENTS_PARTIAL_COVERAGE",
                         "coverage": matched, "status": "partial"},
            )
        if total > 0:
            # 当日有事件但均不属于当前 core run → stale，禁止判 ready。
            return ProductReadinessState(
                "state_events", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=False,
                lineage={**parent, **detail,
                         "reason_code": "STATE_EVENTS_LINEAGE_MISMATCH",
                         "coverage": 0, "status": "stale_lineage"},
            )
        return ProductReadinessState(
            "state_events", READINESS_PENDING, "stale",
            is_mandatory=False, is_terminal=False,
            lineage={**parent, **detail, "reason_code": "NO_STATE_EVENTS",
                     "coverage": 0, "status": "missing"},
        )

    @staticmethod
    async def _count_dsa_projections(
        db: Any, trade_date: date, source_core_run_id: Any = None,
    ) -> dict[str, int]:
        """[Corrective-3.1 §P1] 统计当日特征快照，并区分是否属于当前 core run。

        返回 {"total": 当日全部, "matched": 归属当前 core run}。
        `matched` 才是精确 lineage 证据；`total > matched` 说明存在上一轮 run
        的残留投影，不能据此认定当前 pointer 的投影已完整。
        """
        try:
            from app.models.stock_feature_snapshot import StockFeatureSnapshot

            total = int(
                await db.scalar(
                    select(func.count())
                    .select_from(StockFeatureSnapshot)
                    .where(StockFeatureSnapshot.trade_date == trade_date)
                ) or 0
            )
            if source_core_run_id is None:
                return {"total": total, "matched": 0}
            matched = int(
                await db.scalar(
                    select(func.count())
                    .select_from(StockFeatureSnapshot)
                    .where(
                        StockFeatureSnapshot.trade_date == trade_date,
                        StockFeatureSnapshot.source_run_id == source_core_run_id,
                    )
                ) or 0
            )
            return {"total": total, "matched": matched}
        except Exception:
            return {"total": 0, "matched": 0}

    @staticmethod
    async def _count_state_events(
        db: Any, trade_date: date, source_core_run_id: Any = None,
    ) -> dict[str, Any]:
        """[Corrective-3.1 §P1] 按 event_type 统计当日状态事件，并按 core run 归属拆分。

        返回 {"total": int, "matched": int, "by_type": {...}, "algorithm_versions": [...]}。
        `by_type` 仅统计归属当前 core run 的事件；无 core run 时退化为全量并把
        matched 记为 0，由调用方降级处理，不得据此判定 ready。
        """
        try:
            from app.models.stock_state_event import StockStateEvent

            rows = await db.execute(
                select(StockStateEvent.event_type, func.count())
                .where(StockStateEvent.current_as_of == trade_date)
                .group_by(StockStateEvent.event_type)
            )
            all_by_type = {str(t): int(c) for t, c in rows.all()}
            total = sum(all_by_type.values())
            if source_core_run_id is None:
                return {
                    "total": total, "matched": 0,
                    "by_type": {}, "algorithm_versions": [],
                }

            rows2 = await db.execute(
                select(
                    StockStateEvent.event_type,
                    StockStateEvent.algorithm_version,
                    func.count(),
                )
                .where(
                    StockStateEvent.current_as_of == trade_date,
                    StockStateEvent.source_run_id == source_core_run_id,
                )
                .group_by(StockStateEvent.event_type, StockStateEvent.algorithm_version)
            )
            by_type: dict[str, int] = {}
            versions: set[str] = set()
            for event_type, algo_version, count in rows2.all():
                by_type[str(event_type)] = by_type.get(str(event_type), 0) + int(count)
                if algo_version is not None:
                    versions.add(str(algo_version))
            return {
                "total": total,
                "matched": sum(by_type.values()),
                "by_type": by_type,
                "algorithm_versions": sorted(versions),
            }
        except Exception:
            return {"total": 0, "matched": 0, "by_type": {}, "algorithm_versions": []}

    async def _chip_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """chip：增强产品，以 chip_consensus 发布指针为准。"""
        st = await self._publication_readiness(
            db, trade_date, PUBLICATION_KIND_CHIP_CONSENSUS,
            "chip", is_mandatory=False,
        )
        if st is not None:
            return st
        from app.models.chip_consensus_run import ChipConsensusRun

        run = await db.scalar(
            select(ChipConsensusRun)
            .where(ChipConsensusRun.trade_date == trade_date)
            .order_by(ChipConsensusRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState(
                "chip", READINESS_PENDING, "fresh", is_mandatory=False,
                lineage={"source_type": "run_status", "reason_code": "NO_CHIP_RUN"},
            )
        base = {
            "source_type": "domain_run",
            "domain_run_id": _sid(getattr(run, "id", None)),
            "run_id": _sid(getattr(run, "id", None)),
            "source_core_run_id": _sid(getattr(run, "source_core_run_id", None)),
            "algorithm_version": getattr(run, "algorithm_version", None),
            "coverage": _num(getattr(run, "coverage_ratio", None)),
            "status": run.status,
            "calculated_at": _iso(
                getattr(run, "finished_at", None) or getattr(run, "created_at", None),
            ),
        }

        # [Corrective-3 §二.4] chip run 已成功但 publication 缺失 —— 必须可治理。
        # 修复前：此分支返回 ready，运维完全看不到 chip pointer 未发布。
        if run.status == "succeeded":
            return ProductReadinessState(
                "chip", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=True,
                lineage={**base, "reason_code": "CHIP_PUBLICATION_MISSING"},
            )
        if run.status == "partial":
            return ProductReadinessState(
                "chip", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=True,
                lineage={**base, "reason_code": "CHIP_PARTIAL"},
            )
        if run.status in TERMINAL_RUN_STATUS:
            return ProductReadinessState(
                "chip", READINESS_UNAVAILABLE, "fresh",
                is_mandatory=False, is_terminal=True,
                lineage={**base, "reason_code": f"CHIP_{run.status.upper()}"},
            )
        return ProductReadinessState(
            "chip", READINESS_PENDING, "fresh", is_mandatory=False,
            lineage={**base, "reason_code": "CHIP_RUNNING"},
        )

    async def _auction_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """auction_anchor：增强产品，以 auction_anchor 发布指针为准。

        [PRD Alignment Pass P0-1] 无论经由 publication 还是 domain run，都读取 run.mode：
        fully_ready 要求 auction mode == composite。publication 指针本身不存 mode，
        故先取 domain run 的 mode 再与 publication 状态合并。
        """
        from app.models.auction_anchor_run import AuctionAnchorRun

        run = await db.scalar(
            select(AuctionAnchorRun)
            .where(AuctionAnchorRun.trade_date == trade_date)
            .order_by(AuctionAnchorRun.created_at.desc())
            .limit(1)
        )
        current_mode = getattr(run, "mode", None) if run is not None else None

        st = await self._publication_readiness(
            db, trade_date, PUBLICATION_KIND_AUCTION_ANCHOR,
            "auction_anchor", is_mandatory=False,
        )
        if st is not None:
            # publication 存在 → 附加 run.mode 供 fully_ready 判定（state 不可变，重建）
            if run is not None and run.status == "succeeded":
                is_ready = current_mode == "composite"
            else:
                is_ready = False
            return ProductReadinessState(
                "auction_anchor",
                st.readiness,
                st.freshness,
                is_mandatory=False,
                is_terminal=st.is_terminal,
                auction_mode=current_mode,
                is_product_ready=is_ready,
                lineage={
                    **st.lineage,
                    "mode": current_mode,
                    "reason_code": st.lineage.get("reason_code", "AUCTION_PUBLISHED"),
                },
            )
        if run is None:
            return ProductReadinessState(
                "auction_anchor", READINESS_PENDING, "fresh",
                is_mandatory=False,
                lineage={"source_type": "run_status", "reason_code": "NO_AUCTION_RUN"},
            )
        base = {
            "source_type": "domain_run",
            "domain_run_id": _sid(getattr(run, "id", None)),
            "run_id": _sid(getattr(run, "id", None)),
            "source_core_run_id": _sid(getattr(run, "source_core_run_id", None)),
            "algorithm_version": getattr(run, "algorithm_version", None),
            "status": run.status,
            "calculated_at": _iso(
                getattr(run, "finished_at", None) or getattr(run, "created_at", None),
            ),
        }

        # [Corrective-3 §三] structure_only 必须体现"等待 chip 升级"，
        # 不得与 succeeded 一样呈现为 fresh/ready。
        # [PRD Alignment Pass P0-1] auction_mode 暴露当前模式，fully_ready 要求 composite。
        if run.status == "structure_only":
            return ProductReadinessState(
                "auction_anchor", READINESS_DEGRADED, "stale",
                is_mandatory=False, is_terminal=True,
                auction_mode=current_mode,
                is_product_ready=False,
                lineage={**base, "reason_code": "AUCTION_STRUCTURE_ONLY", "mode": current_mode},
            )
        if run.status == "succeeded":
            return ProductReadinessState(
                "auction_anchor", READINESS_READY, "fresh",
                is_mandatory=False, is_terminal=True,
                auction_mode=current_mode,
                # 仅 composite 视为完整就绪；hybrid/structure_only 不算 fully_ready
                is_product_ready=(current_mode == "composite"),
                lineage={**base, "reason_code": "AUCTION_SUCCEEDED", "mode": current_mode},
            )
        # [Corrective-3 §三] terminal failure 必须包含 run_id 与 reason
        if run.status in TERMINAL_RUN_STATUS:
            return ProductReadinessState(
                "auction_anchor", READINESS_UNAVAILABLE, "fresh",
                is_mandatory=False, is_terminal=True,
                auction_mode=current_mode,
                is_product_ready=False,
                lineage={
                    **base,
                    "reason_code": f"AUCTION_{run.status.upper()}",
                    "mode": current_mode,
                    "error_message": getattr(run, "error_message", None),
                },
            )
        return ProductReadinessState(
            "auction_anchor", READINESS_PENDING, "fresh",
            is_mandatory=False, is_terminal=False,
            auction_mode=current_mode,
            lineage={**base, "reason_code": "AUCTION_RUNNING", "mode": current_mode},
        )


if __name__ == "__main__":
    # fully_ready（九节点，enhancement 真正就绪且 auction composite）
    full = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, auction_mode="composite", is_product_ready=True),
    ]
    assert evaluate_closure(full).closure == CLOSURE_FULLY_READY

    # [PRD Alignment Pass P0-1] chip partial（terminal）不得误判 fully_ready
    chip_partial = list(full)
    chip_partial[6] = ProductReadinessState("chip", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True, is_product_ready=False, lineage={"reason_code": "CHIP_PARTIAL"})
    assert evaluate_closure(chip_partial).closure == CLOSURE_DEGRADED_READY

    # [PRD Alignment Pass P0-1] auction structure_only（terminal）不得误判 fully_ready
    auction_struct = list(full)
    auction_struct[8] = ProductReadinessState("auction_anchor", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True, auction_mode="structure_only", is_product_ready=False, lineage={"reason_code": "AUCTION_STRUCTURE_ONLY", "mode": "structure_only"})
    assert evaluate_closure(auction_struct).closure == CLOSURE_DEGRADED_READY

    # [PRD Alignment Pass P0-1] auction hybrid（terminal但非composite）不得误判 fully_ready
    auction_hybrid = list(full)
    auction_hybrid[8] = ProductReadinessState("auction_anchor", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True, auction_mode="hybrid", is_product_ready=False, lineage={"reason_code": "AUCTION_HYBRID", "mode": "hybrid"})
    assert evaluate_closure(auction_hybrid).closure == CLOSURE_DEGRADED_READY

    # blocked
    blocked = [
        ProductReadinessState("board_facts", READINESS_UNAVAILABLE),
    ]
    assert evaluate_closure(blocked).closure == CLOSURE_BLOCKED

    # degraded_ready（board facts ready_reused → 非 fully fresh）
    degraded = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    assert evaluate_closure(degraded).closure == CLOSURE_DEGRADED_READY

    # P0-4：stock_core ready 但 review 未完成 → core_ready（而非 pending）
    core_ready_case = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_PENDING, "fresh"),
    ]
    assert evaluate_closure(core_ready_case).closure == CLOSURE_CORE_READY

    # P0-3：stock_core 未形成 → pending
    pending_case = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_PENDING, "fresh"),
        ProductReadinessState("review", READINESS_PENDING, "fresh"),
    ]
    assert evaluate_closure(pending_case).closure == CLOSURE_PENDING

    print("OK: closure evaluator verified")
