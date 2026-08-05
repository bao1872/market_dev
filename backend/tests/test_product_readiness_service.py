"""[V2.1 EPIC-08] ProductReadiness 闭包评估纯函数单元测试（P0 修正版）。

覆盖：
- P0-1：九节点完整纳入（daily_facts/state_events/dsa_projection）
- P0-3：terminal 与 consumable 分离（chip 失败不再永久"仍在运行"）
- P0-4：以 stock_core 为轴心的分阶段判定（core_ready 而非 pending）

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_product_readiness_service.py -q -p no:cacheprovider
"""

from __future__ import annotations

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_DEGRADED,
    READINESS_UNAVAILABLE,
)
from app.services.product_readiness_service import (
    ProductReadinessState,
    compute_freshness_flags,
    evaluate_closure,
)

# 九节点 helper：mandatory 5 + enhancement 4，全部 terminal
_MANDATORY = {
    "daily_facts": ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
    "board_facts": ProductReadinessState("board_facts", READINESS_READY, "fresh"),
    "stock_core": ProductReadinessState("stock_core", READINESS_READY, "fresh"),
    "board_aggregation": ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
    "review": ProductReadinessState("review", READINESS_READY, "fresh"),
}
_ENHANCEMENT = {
    "dsa_projection": ProductReadinessState(
        "dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
        is_product_ready=True,
    ),
    "chip": ProductReadinessState(
        "chip", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
        is_product_ready=True,
    ),
    "state_events": ProductReadinessState(
        "state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
        is_product_ready=True,
    ),
    # [PRD Alignment Pass P0-1] auction 必须是 composite 才算 fully_ready
    "auction_anchor": ProductReadinessState(
        "auction_anchor", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
        auction_mode="composite", is_product_ready=True,
    ),
}


def _full_products() -> list[ProductReadinessState]:
    return list(_MANDATORY.values()) + list(_ENHANCEMENT.values())


def test_fully_ready():
    """全部 mandatory fresh + enhancement 真正就绪且 auction=composite → fully_ready。"""
    ev = evaluate_closure(_full_products())
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


def test_fully_ready_requires_composite_auction():
    """[PRD Alignment Pass P0-1] auction 非 composite（terminal 但 structure_only/hybrid）
    不得误判 fully_ready，应为 degraded_ready。"""
    products = _full_products()
    new_products = []
    for p in products:
        if p.product == "auction_anchor":
            new_products.append(
                ProductReadinessState(
                    "auction_anchor", READINESS_DEGRADED, "stale",
                    is_mandatory=False, is_terminal=True,
                    auction_mode="structure_only", is_product_ready=False,
                    lineage={**p.lineage, "reason_code": "AUCTION_STRUCTURE_ONLY"},
                )
            )
        else:
            new_products.append(p)
    ev = evaluate_closure(new_products)
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_full_fresh is True


def test_blocked_when_mandatory_unavailable():
    """mandatory 任一 unavailable → blocked。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_UNAVAILABLE),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_BLOCKED
    assert ev.mandatory_products_ready is False
    assert any(i["severity"] == "critical" for i in ev.issues)


def test_pending_when_stock_core_not_formed():
    """P0-4：stock_core 尚未形成（不可消费）→ pending。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_PENDING
    assert ev.mandatory_products_ready is False


def test_core_ready_when_review_pending():
    """P0-4：stock_core ready 但 review 尚未完成 → core_ready（而非 pending）。"""
    products = list(_MANDATORY.values())
    products[4] = ProductReadinessState("review", READINESS_PENDING, "fresh")
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_CORE_READY
    assert ev.mandatory_products_ready is False


def test_degraded_ready_when_board_reused():
    """board facts ready_reused → degraded_ready（mandatory ready 但非 fully fresh）。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is False
    assert any(i["code"] == "NOT_FULLY_FRESH" for i in ev.issues)


def test_enhancement_not_terminal_degrades():
    """P0-3：enhancement（chip）未 terminal → 非 fully_ready（degraded_ready）。"""
    products = list(_MANDATORY.values())
    products.append(
        ProductReadinessState("chip", READINESS_PENDING, is_mandatory=False, is_terminal=False),
    )
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.enhancement_jobs_terminal is False


def test_failed_enhancement_is_terminal_and_does_not_block():
    """P0-3 修复：chip 失败（terminal+unavailable）→ enhancementJobsTerminal=True 且不阻断 mandatory chain。

    [PRD Alignment Pass P0-1] 但失败的 chip 不是"真正就绪"，故闭包为 degraded_ready，
    不得误判 fully_ready（failed/failed 的 enhancement 不可消费）。
    """
    products = list(_MANDATORY.values())
    products.append(
        ProductReadinessState(
            "chip", READINESS_UNAVAILABLE, is_mandatory=False, is_terminal=True,
            is_product_ready=False,
        ),
    )
    # 补齐其余 enhancement（含 composite auction）以隔离"chip 失败"这一变量
    products.append(
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
    )
    products.append(
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
    )
    products.append(
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, auction_mode="composite", is_product_ready=True),
    )
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_DEGRADED_READY  # mandatory fresh，但 chip 失败未真正就绪
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


def test_freshness_flags_separate():
    """freshness 标志独立返回（E08-T03）。"""
    full = _full_products()
    flags = compute_freshness_flags(full)
    assert flags["mandatoryProductsReady"] is True
    assert flags["mandatoryProductsFullyFresh"] is True

    degraded = [
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
    ]
    flags2 = compute_freshness_flags(degraded)
    assert flags2["mandatoryProductsReady"] is True
    assert flags2["mandatoryProductsFullyFresh"] is False


def test_missing_daily_facts_prevents_fully_ready():
    """P0-1：daily_facts 未就绪（pending）→ 不是 fully_ready。"""
    products = list(_ENHANCEMENT.values())
    products += [
        ProductReadinessState("daily_facts", READINESS_PENDING),  # mandatory 未就绪
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_CORE_READY  # stock_core ready 但 daily_facts 未就绪
    assert ev.mandatory_products_ready is False
