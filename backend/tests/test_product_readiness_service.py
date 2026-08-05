"""[V2.1 EPIC-08] ProductReadiness 闭包评估纯函数单元测试。

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_product_readiness_service.py -q -p no:cacheprovider
"""

from __future__ import annotations

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
)
from app.services.product_readiness_service import (
    ProductReadinessState,
    compute_freshness_flags,
    evaluate_closure,
)


def _full_products() -> list[ProductReadinessState]:
    return [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False),
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False),
    ]


def test_fully_ready():
    """全部 mandatory fresh + enhancement ready → fully_ready。"""
    ev = evaluate_closure(_full_products())
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


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


def test_pending_when_mandatory_pending():
    """mandatory 任一 pending → pending。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_PENDING
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


def test_core_ready_when_chip_pending():
    """mandatory 核心待 ready 但 enhancement 未 terminal → core_ready。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("chip", READINESS_PENDING, is_mandatory=False),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_DEGRADED_READY


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


def test_chip_unavailable_does_not_block():
    """enhancement（chip）unavailable 不阻断 mandatory chain（可 degraded_ready）。"""
    products = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("chip", READINESS_UNAVAILABLE, is_mandatory=False),
    ]
    ev = evaluate_closure(products)
    assert ev.closure == CLOSURE_DEGRADED_READY
