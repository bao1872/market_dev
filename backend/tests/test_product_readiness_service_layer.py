"""[V2.1 EPIC-08] ProductReadinessService 动态聚合服务层单元测试。

运行（纯单元，mock DB，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_product_readiness_service_layer.py -q -p no:cacheprovider
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.domain_status import (
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_PENDING,
    READINESS_PENDING,
    READINESS_READY,
)
from app.services.product_readiness_service import (
    ProductReadinessService,
    evaluate_closure,
)


class _FakeDB:
    """按顺序返回 scalar 结果的假 DB（服务按固定顺序查 6 个产品）。"""

    def __init__(self, returns):
        self._returns = list(returns)

    async def scalar(self, _stmt):
        if not self._returns:
            return None
        return self._returns.pop(0)


def _board_run(status="published"):
    return SimpleNamespace(status=status)


def _pub():
    return SimpleNamespace()  # 非 None 即视为存在发布指针


def _review_run(status="published"):
    return SimpleNamespace(status=status)


def _chip_run(status="succeeded"):
    return SimpleNamespace(status=status)


def _auction_run(status="succeeded"):
    return SimpleNamespace(status=status)


async def _evaluate(returns):
    db = _FakeDB(returns)
    service = ProductReadinessService()
    return await service.evaluate_for_trade_date(db, date(2026, 8, 4))


async def test_full_chain_fully_ready():
    """全部产品就绪 → fully_ready。"""
    ev = await _evaluate([
        _board_run("published"),          # board_facts
        _pub(),                            # stock_core pointer
        _pub(),                            # board_aggregation pointer
        _review_run("published"),          # review
        _chip_run("succeeded"),            # chip
        _auction_run("succeeded"),         # auction_anchor
    ])
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is True


async def test_board_facts_unavailable_blocks():
    """board_facts failed → unavailable → blocked。"""
    ev = await _evaluate([
        _board_run("failed"),              # board_facts
        _pub(), _pub(), _review_run("published"),
        _chip_run("succeeded"), _auction_run("succeeded"),
    ])
    assert ev.closure == "blocked"
    assert ev.mandatory_products_ready is False
    assert any(i["severity"] == "critical" for i in ev.issues)


async def test_board_facts_reused_degrades():
    """board_facts reused_previous → ready_reused → degraded_ready。"""
    ev = await _evaluate([
        _board_run("reused_previous"),     # board_facts
        _pub(), _pub(), _review_run("published"),
        _chip_run("succeeded"), _auction_run("succeeded"),
    ])
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_ready is True
    assert any(i["code"] == "NOT_FULLY_FRESH" for i in ev.issues)


async def test_no_publish_pending():
    """无任何 run/pointer → pending。"""
    ev = await _evaluate([None, None, None, None, None, None])
    assert ev.closure == CLOSURE_PENDING
    assert ev.mandatory_products_ready is False


async def test_chip_failed_does_not_block():
    """chip（enhancement）failed → mandatory 仍可 degraded_ready。"""
    ev = await _evaluate([
        _board_run("published"), _pub(), _pub(), _review_run("published"),
        _chip_run("failed"),              # chip unavailable
        _auction_run("succeeded"),
    ])
    assert ev.closure == CLOSURE_DEGRADED_READY


async def test_stock_core_pointer_missing_pending():
    """stock_core 无 pointer → mandatory pending。"""
    ev = await _evaluate([
        _board_run("published"),
        None,                             # stock_core 无 pointer
        _pub(), _review_run("published"),
        _chip_run("succeeded"), _auction_run("succeeded"),
    ])
    assert ev.closure == CLOSURE_PENDING


def test_pure_evaluator_contract():
    """服务层复用纯评估器（契约一致性）。"""
    from app.services.product_readiness_service import ProductReadinessState

    states = [
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
    ]
    ev = evaluate_closure(states)
    assert ev.closure == CLOSURE_FULLY_READY
    # 缺少 board_aggregation/review → 仍可 degraded（空 enhancement）
    states2 = [
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
        ProductReadinessState("review", READINESS_PENDING),
    ]
    ev2 = evaluate_closure(states2)
    assert ev2.closure == CLOSURE_PENDING
