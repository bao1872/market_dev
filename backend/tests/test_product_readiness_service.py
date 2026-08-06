"""Pure Unit golden tests：六态 canonical 场景 + 非验收诊断场景。

[审查报告修订 / 六状态事实证明] 本文件是 closure 判定算法的**纯单元**验证：
只用手工定义的 ProductReadinessState[] 调正式 evaluate_closure，断言 closure。
不调用 Seed，不连数据库，不复制判定算法（evaluate_closure 是真源）。

场景事实矩阵来自 tests.readiness_fixtures（Pure Unit 与 PG E2E 共享唯一事实源）。
"""
from __future__ import annotations

import pytest

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_PENDING,
    READINESS_DEGRADED,
)
from app.services.product_readiness_service import (
    ProductReadinessState,
    evaluate_closure,
)
from tests.readiness_fixtures import (
    CANONICAL_SCENARIOS,
    get_scenario,
)

pytestmark = pytest.mark.pure_unit


@pytest.mark.parametrize("name", list(CANONICAL_SCENARIOS.keys()))
def test_canonical_six_state_closure(name: str) -> None:
    """六态 canonical 场景：输入事实矩阵 → 唯一 closure（严格一一对应）。"""
    states, expected = get_scenario(name)
    result = evaluate_closure(states)
    assert result.closure == expected, (
        f"场景 {name}: 期望 {expected}，实际 {result.closure}，"
        f"issues={result.issues}"
    )


def test_synthetic_external_ceiling_is_blocked_diagnostic() -> None:
    """非验收诊断场景：小规模 synthetic 输入被正式外部门禁拒绝 → blocked。

    不计入 full-closure 绿色验收，仅证明外部门禁对小规模 synthetic 的真实拒绝能力。
    """
    states, expected = get_scenario("synthetic_external_ceiling")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_BLOCKED
    board = next(s for s in states if s.product == "board_facts")
    assert board.lineage.get("reason_code") == "EXTERNAL_GATE_UNSATISFIED"


def test_fully_ready_requires_composite_auction() -> None:
    """fully_ready 必须 auction composite；hybrid 应降级（非 fully_ready）。"""
    states, _ = get_scenario("fully_ready_all_fresh")
    # auction 改为 hybrid（terminal 但非 composite）
    states = [
        s if s.product != "auction_anchor" else ProductReadinessState(
            "auction_anchor", READINESS_DEGRADED, "stale", is_mandatory=False,
            is_terminal=True, auction_mode="hybrid", is_product_ready=False,
            lineage={"reason_code": "AUCTION_HYBRID"},
        )
        for s in states
    ]
    result = evaluate_closure(states)
    assert result.closure != CLOSURE_FULLY_READY


def test_degraded_ready_when_mandatory_ready_but_enhancement_partial() -> None:
    """mandatory 全部可消费，enhancement 终态但不全 truly ready → degraded_ready。"""
    states, expected = get_scenario("degraded_terminal_partial")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_DEGRADED_READY


def test_pending_when_stock_core_not_formed() -> None:
    """stock_core 未形成（不可消费）→ pending，即使其余 mandatory 已 ready。"""
    states, expected = get_scenario("pending_no_core")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_PENDING


def test_core_ready_when_review_not_ready() -> None:
    """stock_core 可消费但 review 未完成 → core_ready。"""
    states, expected = get_scenario("core_ready_waiting_mandatory")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_CORE_READY


def test_blocked_when_mandatory_unavailable() -> None:
    """任一 mandatory 节点 terminal + unavailable → blocked。"""
    states, expected = get_scenario("blocked_mandatory_failure")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_BLOCKED


def test_mandatory_ready_enhancing_when_enhancement_not_terminal() -> None:
    """mandatory 全 ready，至少一个 enhancement 未终态 → mandatory_ready_enhancing。"""
    states, expected = get_scenario("mandatory_ready_enhancing")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_MANDATORY_READY_ENHANCING


def test_fully_ready_all_fresh() -> None:
    """mandatory 全 fresh + enhancement 全 truly ready + auction composite → fully_ready。"""
    states, expected = get_scenario("fully_ready_all_fresh")
    result = evaluate_closure(states)
    assert result.closure == CLOSURE_FULLY_READY
