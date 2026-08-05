"""[V2.1 EPIC-08] ProductReadinessService 动态聚合服务层单元测试（P0 修正版）。

覆盖：
- P0-1：九节点完整纳入（daily_facts/state_events/dsa_projection）
- P0-2：product readiness 由 publication pointer 决定，latest run 单列
- P0-3：terminal 与 consumable 分离（chip 失败不再永久"仍在运行"）
- P0-4：stock_core 未形成 → pending；stock_core ready 但 review 未完成 → core_ready

FakeDB 按顺序返回 scalar 结果。evaluate_for_trade_date 的查询顺序：
  1. daily_facts publication
  2. board_facts publication
  3. [board pub 存在] board data run / [board pub 缺失] board latest run
  4. stock_core publication
  5. board_aggregation publication
  6. review run
  7. chip publication
  8. [chip pub 缺失] chip run
  9. auction publication
  10. [auction pub 缺失] auction run

运行（纯单元，mock DB，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_product_readiness_service_layer.py -q -p no:cacheprovider
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from app.domain_status import (
    CLOSURE_CORE_READY,
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
    """按顺序返回 scalar 结果的假 DB（见文件 docstring 的查询顺序）。"""

    def __init__(self, returns):
        self._returns = list(returns)

    async def scalar(self, _stmt):
        if not self._returns:
            return None
        return self._returns.pop(0)


def _pub():
    """非 None 即视为存在发布指针；data_run_id 供 board_facts 复用检查。"""
    return SimpleNamespace(data_run_id="00000000-0000-0000-0000-000000000001")


def _bf_run(status="published"):
    return SimpleNamespace(status=status)


def _review_run(status="published"):
    return SimpleNamespace(status=status)


async def _evaluate(returns):
    db = _FakeDB(returns)
    service = ProductReadinessService()
    return await service.evaluate_for_trade_date(db, date(2026, 8, 4))


# 全就绪公共输入：daily_pub, bf_pub, bf_data_run(published), sc_pub, ba_pub,
# review(published), chip_pub, auction_pub
_FULL_READY_INPUT = [
    _pub(),                          # 1 daily_facts pub
    _pub(),                          # 2 board_facts pub
    _bf_run("published"),            # 3 board data run（非 reused）
    _pub(),                          # 4 stock_core pub
    _pub(),                          # 5 board_aggregation pub
    _review_run("published"),        # 6 review
    _pub(),                          # 7 chip pub
    _pub(),                          # 8 auction pub
]


async def test_full_chain_fully_ready():
    """九节点全部就绪 → fully_ready。"""
    ev = await _evaluate(list(_FULL_READY_INPUT))
    assert ev.closure == CLOSURE_FULLY_READY
    assert ev.mandatory_products_ready is True
    assert ev.mandatory_products_full_fresh is True
    assert ev.enhancement_jobs_terminal is True


async def test_board_facts_reused_degrades():
    """P0-7：board_facts 指针 data run 为 reused_previous → ready_reused → degraded_ready。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        _pub(),                          # 2 board_facts pub
        _bf_run("reused_previous"),      # 3 board data run（reused）
        _pub(),                          # 4 stock_core pub
        _pub(),                          # 5 board_aggregation pub
        _review_run("published"),        # 6 review
        _pub(),                          # 7 chip pub
        _pub(),                          # 8 auction pub
    ])
    assert ev.closure == CLOSURE_DEGRADED_READY
    assert ev.mandatory_products_ready is True
    assert any(i["code"] == "NOT_FULLY_FRESH" for i in ev.issues)


async def test_board_facts_unavailable_blocks():
    """P0-2：board_facts 无指针且 latest run failed → unavailable → blocked。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        None,                            # 2 board_facts pub 缺失
        _bf_run("failed"),               # 3 board latest run failed
        _pub(),                          # 4 stock_core pub
        _pub(),                          # 5 board_aggregation pub
        _review_run("published"),        # 6 review
        _pub(),                          # 7 chip pub
        _pub(),                          # 8 auction pub
    ])
    assert ev.closure == "blocked"
    assert ev.mandatory_products_ready is False
    assert any(i["severity"] == "critical" for i in ev.issues)


async def test_old_pointer_ignores_failed_retry():
    """P0-2：指针存在时，即使有 failed 重试 run，readiness 仍为 ready（latest attempt 单列）。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        _pub(),                          # 2 board_facts pub（旧指针仍在）
        _bf_run("published"),            # 3 board data run（指针指向的旧成功 run）
        _pub(),                          # 4 stock_core pub
        _pub(),                          # 5 board_aggregation pub
        _review_run("published"),        # 6 review
        _pub(),                          # 7 chip pub
        _pub(),                          # 8 auction pub
    ])
    assert ev.closure == CLOSURE_FULLY_READY


async def test_no_publish_pending():
    """无任何 run/pointer → pending（stock_core 未形成）。"""
    ev = await _evaluate([None] * 10)
    assert ev.closure == CLOSURE_PENDING
    assert ev.mandatory_products_ready is False


async def test_failed_enhancement_terminal_fully_ready():
    """P0-3：chip 失败（terminal+unavailable）→ enhancement_jobs_terminal=True，不阻断。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        _pub(),                          # 2 board_facts pub
        _bf_run("published"),            # 3 board data run
        _pub(),                          # 4 stock_core pub
        _pub(),                          # 5 board_aggregation pub
        _review_run("published"),        # 6 review
        None,                            # 7 chip pub 缺失
        _bf_run("failed"),               # 8 chip latest run failed
        _pub(),                          # 9 auction pub
    ])
    assert ev.enhancement_jobs_terminal is True
    assert ev.closure == CLOSURE_FULLY_READY


async def test_stock_core_pointer_missing_pending():
    """P0-4：stock_core 无 pointer → pending。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        _pub(),                          # 2 board_facts pub
        _bf_run("published"),            # 3 board data run
        None,                            # 4 stock_core pub 缺失
        _pub(),                          # 5 board_aggregation pub
        _review_run("published"),        # 6 review
        _pub(),                          # 7 chip pub
        _pub(),                          # 8 auction pub
    ])
    assert ev.closure == CLOSURE_PENDING


async def test_core_ready_when_review_pending():
    """P0-4：stock_core ready 但 review 未完成 → core_ready（而非 pending）。"""
    ev = await _evaluate([
        _pub(),                          # 1 daily_facts pub
        _pub(),                          # 2 board_facts pub
        _bf_run("published"),            # 3 board data run
        _pub(),                          # 4 stock_core pub
        _pub(),                          # 5 board_aggregation pub
        _review_run("pending"),          # 6 review 未完成
        _pub(),                          # 7 chip pub
        _pub(),                          # 8 auction pub
    ])
    assert ev.closure == CLOSURE_CORE_READY
    assert ev.mandatory_products_ready is False


def test_pure_evaluator_contract():
    """服务层复用纯评估器（契约一致性）。"""
    from app.services.product_readiness_service import ProductReadinessState

    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    ev = evaluate_closure(states)
    assert ev.closure == CLOSURE_FULLY_READY
    # 缺少 dsa_projection/state_events（enhancement）不影响 fully_ready（空 enhancement）
    states2 = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
        ProductReadinessState("review", READINESS_PENDING),
    ]
    ev2 = evaluate_closure(states2)
    assert ev2.closure == CLOSURE_PENDING
