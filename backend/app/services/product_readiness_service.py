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
from datetime import date
from typing import Any

from sqlalchemy import select

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_BLOCKED,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
)

# 产品分类
MANDATORY_PRODUCTS = frozenset({
    "daily_facts",
    "board_facts",
    "stock_core",
    "board_aggregation",
    "review",
})
ENHANCEMENT_PRODUCTS = frozenset({
    "chip",
    "auction_anchor",
    "state_events",
})

# readiness 可消费集合（ready / ready_reused）
CONSUMABLE_READINESS = frozenset({READINESS_READY, READINESS_READY_REUSED})


@dataclass(frozen=True)
class ProductReadinessState:
    """单一产品的就绪状态。"""

    product: str
    readiness: str  # pending / ready / ready_reused / degraded / unavailable / blocked
    freshness: str = "fresh"  # fresh / stale / reused
    is_mandatory: bool = True

    @property
    def is_consumable(self) -> bool:
        return self.readiness in CONSUMABLE_READINESS

    @property
    def is_fully_fresh(self) -> bool:
        return self.readiness == READINESS_READY and self.freshness == "fresh"


@dataclass(frozen=True)
class ClosureEvaluation:
    """闭包评估结果（E08-T02/T03）。

    - closure: pending / blocked / core_ready / degraded_ready / fully_ready
    - mandatory_products_ready: 全部 mandatory 产品 consumable
    - mandatory_products_full_fresh: 全部 mandatory 产品 is_fully_fresh
    - enhancement_jobs_terminal: 全部 enhancement 产品 consumable/terminal
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


def evaluate_closure(
    products: list[ProductReadinessState],
) -> ClosureEvaluation:
    """评估一次产品 ready 集合的闭包状态（E08-T02/T03）。

    Args:
        products: 全部产品就绪状态（mandatory + enhancement）

    Returns:
        ClosureEvaluation
    """
    mandatory = [p for p in products if p.is_mandatory]
    enhancement = [p for p in products if not p.is_mandatory]

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
            enhancement_jobs_terminal=all(e.is_consumable for e in enhancement)
            if enhancement else True,
            issues=issues,
        )

    # 2. pending：mandatory 任一 pending
    if any(p.readiness == READINESS_PENDING for p in mandatory):
        return ClosureEvaluation(
            closure=CLOSURE_PENDING,
            mandatory_products_ready=False,
            mandatory_products_full_fresh=False,
            enhancement_jobs_terminal=all(e.is_consumable for e in enhancement)
            if enhancement else True,
            issues=issues,
        )

    mandatory_ready = all(p.is_consumable for p in mandatory)
    mandatory_full_fresh = all(p.is_fully_fresh for p in mandatory)
    enhancement_terminal = all(e.is_consumable for e in enhancement) if enhancement else True

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

    # 3. fully_ready
    if mandatory_ready and mandatory_full_fresh and enhancement_terminal:
        return ClosureEvaluation(
            closure=CLOSURE_FULLY_READY,
            mandatory_products_ready=True,
            mandatory_products_full_fresh=True,
            enhancement_jobs_terminal=True,
            issues=issues,
        )

    # 4. degraded_ready：mandatory ready（含 ready_reused/degraded）或 enhancement 未 terminal
    if mandatory_ready:
        return ClosureEvaluation(
            closure=CLOSURE_DEGRADED_READY,
            mandatory_products_ready=True,
            mandatory_products_full_fresh=mandatory_full_fresh,
            enhancement_jobs_terminal=enhancement_terminal,
            issues=issues,
        )

    # 5. core_ready：mandatory 核心已 ready 但 enhancement 未 terminal
    return ClosureEvaluation(
        closure=CLOSURE_CORE_READY,
        mandatory_products_ready=False,
        mandatory_products_full_fresh=False,
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

    async def evaluate_for_trade_date(
        self,
        db: Any,
        trade_date: date,
    ) -> ClosureEvaluation:
        """评估指定交易日的产品闭包状态。

        Args:
            db: 异步数据库会话
            trade_date: 业务交易日

        Returns:
            ClosureEvaluation
        """
        states = [
            await self._board_facts_state(db, trade_date),
            await self._stock_core_state(db, trade_date),
            await self._board_aggregation_state(db, trade_date),
            await self._review_state(db, trade_date),
            await self._chip_state(db, trade_date),
            await self._auction_state(db, trade_date),
        ]
        return evaluate_closure(states)

    # ---- 各产品状态映射 ----

    async def _board_facts_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """board_facts：BoardFactsRun 当日最新 run。"""
        from app.models.board_facts_run import BoardFactsRun

        run = await db.scalar(
            select(BoardFactsRun)
            .where(BoardFactsRun.trade_date == trade_date)
            .order_by(BoardFactsRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState("board_facts", READINESS_PENDING, "fresh")
        if run.status == "published":
            return ProductReadinessState("board_facts", READINESS_READY, "fresh")
        if run.status == "reused_previous":
            return ProductReadinessState(
                "board_facts", READINESS_READY_REUSED, "reused",
            )
        if run.status in ("failed", "cancelled", "interrupted"):
            return ProductReadinessState("board_facts", READINESS_UNAVAILABLE, "fresh")
        # queued/fetching/normalizing/validating/persisting → 进行中
        return ProductReadinessState("board_facts", READINESS_PENDING, "fresh")

    async def _stock_core_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """stock_core：FactorPublication 发布指针（market scope）。"""
        from app.models.factor_publication import (
            PUBLICATION_KIND_STOCK_CORE,
            SCOPE_TYPE_MARKET,
            FactorPublication,
        )

        pub = await db.scalar(
            select(FactorPublication)
            .where(
                FactorPublication.publication_kind == PUBLICATION_KIND_STOCK_CORE,
                FactorPublication.scope_type == SCOPE_TYPE_MARKET,
                FactorPublication.trade_date == trade_date,
            )
            .limit(1)
        )
        if pub is not None:
            return ProductReadinessState("stock_core", READINESS_READY, "fresh")
        return ProductReadinessState("stock_core", READINESS_PENDING, "fresh")

    async def _board_aggregation_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """board_aggregation：market_aggregation 发布指针。"""
        from app.models.factor_publication import (
            PUBLICATION_KIND_MARKET_AGGREGATION,
            SCOPE_TYPE_MARKET,
            FactorPublication,
        )

        pub = await db.scalar(
            select(FactorPublication)
            .where(
                FactorPublication.publication_kind
                == PUBLICATION_KIND_MARKET_AGGREGATION,
                FactorPublication.scope_type == SCOPE_TYPE_MARKET,
                FactorPublication.trade_date == trade_date,
            )
            .limit(1)
        )
        if pub is not None:
            return ProductReadinessState("board_aggregation", READINESS_READY, "fresh")
        return ProductReadinessState("board_aggregation", READINESS_PENDING, "fresh")

    async def _review_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """review：MarketReviewRun 当日最新 run。"""
        from app.models.market_review import MarketReviewRun

        run = await db.scalar(
            select(MarketReviewRun)
            .where(MarketReviewRun.trade_date == trade_date)
            .order_by(MarketReviewRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState("review", READINESS_PENDING, "fresh")
        if run.status == "published":
            return ProductReadinessState("review", READINESS_READY, "fresh")
        if run.status in ("failed",):
            return ProductReadinessState("review", READINESS_UNAVAILABLE, "fresh")
        return ProductReadinessState("review", READINESS_PENDING, "fresh")

    async def _chip_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """chip：增强产品，不阻断 mandatory chain。"""
        from app.models.chip_consensus_run import ChipConsensusRun

        run = await db.scalar(
            select(ChipConsensusRun)
            .where(ChipConsensusRun.trade_date == trade_date)
            .order_by(ChipConsensusRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState("chip", READINESS_PENDING, "fresh", is_mandatory=False)
        if run.status == "succeeded":
            return ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False)
        if run.status == "partial":
            return ProductReadinessState("chip", READINESS_READY, "stale", is_mandatory=False)
        if run.status in ("failed", "cancelled", "interrupted"):
            return ProductReadinessState(
                "chip", READINESS_UNAVAILABLE, "fresh", is_mandatory=False,
            )
        return ProductReadinessState("chip", READINESS_PENDING, "fresh", is_mandatory=False)

    async def _auction_state(
        self, db: Any, trade_date: date,
    ) -> ProductReadinessState:
        """auction_anchor：增强产品，不阻断 mandatory chain。"""
        from app.models.auction_anchor_run import AuctionAnchorRun

        run = await db.scalar(
            select(AuctionAnchorRun)
            .where(AuctionAnchorRun.trade_date == trade_date)
            .order_by(AuctionAnchorRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            return ProductReadinessState(
                "auction_anchor", READINESS_PENDING, "fresh", is_mandatory=False,
            )
        if run.status == "succeeded":
            return ProductReadinessState(
                "auction_anchor", READINESS_READY, "fresh", is_mandatory=False,
            )
        if run.status in ("failed", "cancelled", "interrupted"):
            return ProductReadinessState(
                "auction_anchor", READINESS_UNAVAILABLE, "fresh", is_mandatory=False,
            )
        return ProductReadinessState(
            "auction_anchor", READINESS_PENDING, "fresh", is_mandatory=False,
        )


if __name__ == "__main__":
    # fully_ready
    full = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False),
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False),
    ]
    assert evaluate_closure(full).closure == CLOSURE_FULLY_READY

    # blocked
    blocked = [
        ProductReadinessState("board_facts", READINESS_UNAVAILABLE),
    ]
    assert evaluate_closure(blocked).closure == CLOSURE_BLOCKED

    # degraded_ready（board facts ready_reused）
    degraded = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    assert evaluate_closure(degraded).closure == CLOSURE_DEGRADED_READY

    print("OK: closure evaluator verified")
