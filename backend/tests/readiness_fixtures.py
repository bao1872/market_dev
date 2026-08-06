"""ProductReadiness 六态 canonical 场景 fixture（共享事实源，不共享算法）。

[审查报告修订 / 六状态事实证明] 六场景直接表达它要证明的 closure 状态，避免
"governance is just pending" 这类语义错位。本模块是 Pure Unit 与 PG E2E 的**唯一**
事实源：只描述输入事实矩阵（list[ProductReadinessState]）与唯一期望 closure，
不包含任何闭包判定算法（evaluate_closure / collect_states 是真源）。

六态 closure（backend/app/domain_status.py）：
    pending / blocked / core_ready / mandatory_ready_enhancing /
    degraded_ready / fully_ready

场景矩阵（每个场景有独立输入事实与唯一预期）：
    pending_no_core                → pending
    blocked_mandatory_failure      → blocked（节点 unavailable/blocked + EXTERNAL_GATE_UNSATISFIED）
    core_ready_waiting_mandatory   → core_ready
    mandatory_ready_enhancing      → mandatory_ready_enhancing
    degraded_terminal_partial      → degraded_ready
    fully_ready_all_fresh          → fully_ready

非验收诊断场景（不计入绿色验收）：
    synthetic_external_ceiling     → 证明小规模 synthetic 输入被正式外部门禁拒绝（blocked）

用法：
    from tests.readiness_fixtures import CANONICAL_SCENARIOS, get_scenario
    states, expected = get_scenario("fully_ready_all_fresh")
    assert evaluate_closure(states).closure == expected
"""
from __future__ import annotations

from app.domain_status import (
    CLOSURE_BLOCKED,
    CLOSURE_CORE_READY,
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    CLOSURE_MANDATORY_READY_ENHANCING,
    CLOSURE_PENDING,
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
)
from app.services.product_readiness_service import ProductReadinessState


def _mandatory(
    *,
    daily=READINESS_READY,
    board=READINESS_READY,
    core=READINESS_READY,
    agg=READINESS_READY,
    review=READINESS_READY,
    board_freshness="fresh",
    core_freshness="fresh",
) -> list[ProductReadinessState]:
    """构造五个 mandatory 节点（默认全部 fresh ready）。"""
    return [
        ProductReadinessState("daily_facts", daily, "fresh", is_mandatory=True),
        ProductReadinessState(
            "board_facts", board, board_freshness, is_mandatory=True,
            lineage={"reason_code": "FRESH_PUBLICATION" if board == READINESS_READY else "RUN_FAILED"},
        ),
        ProductReadinessState("stock_core", core, core_freshness, is_mandatory=True),
        ProductReadinessState("board_aggregation", agg, "fresh", is_mandatory=True),
        ProductReadinessState("review", review, "fresh", is_mandatory=True),
    ]


def _enhancement_all_ready() -> list[ProductReadinessState]:
    """四个 enhancement 节点全部 terminal 且真正 ready（auction=composite）。"""
    return [
        ProductReadinessState(
            "dsa_projection", READINESS_READY, "fresh", is_mandatory=False,
            is_terminal=True, is_product_ready=True,
        ),
        ProductReadinessState(
            "chip", READINESS_READY, "fresh", is_mandatory=False,
            is_terminal=True, is_product_ready=True,
        ),
        ProductReadinessState(
            "state_events", READINESS_READY, "fresh", is_mandatory=False,
            is_terminal=True, is_product_ready=True,
        ),
        ProductReadinessState(
            "auction_anchor", READINESS_READY, "fresh", is_mandatory=False,
            is_terminal=True, auction_mode="composite", is_product_ready=True,
        ),
    ]


def _pending_no_core() -> list[ProductReadinessState]:
    """stock_core 尚未形成（不可消费）→ pending。

    其余 mandatory 可能已 ready，但 stock_core 未发布，整个闭环未启动。
    """
    return [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh", is_mandatory=True),
        ProductReadinessState("board_facts", READINESS_READY, "fresh", is_mandatory=True),
        ProductReadinessState(
            "stock_core", READINESS_PENDING, "fresh", is_mandatory=True,
            lineage={"reason_code": "NO_PUBLICATION"},
        ),
        ProductReadinessState("board_aggregation", READINESS_PENDING, "fresh", is_mandatory=True),
        ProductReadinessState("review", READINESS_PENDING, "fresh", is_mandatory=True),
    ]


def _blocked_mandatory_failure() -> list[ProductReadinessState]:
    """任一 mandatory 节点 terminal 且 unavailable/blocked → blocked。

    外部门禁不满足时节点 reason_code=EXTERNAL_GATE_UNSATISFIED（审查报告第二节），
    最终 closure=blocked（不新增第七种 closure）。
    """
    return [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh", is_mandatory=True),
        ProductReadinessState(
            "board_facts", READINESS_UNAVAILABLE, "fresh", is_mandatory=True,
            is_terminal=True,
            lineage={"reason_code": "EXTERNAL_GATE_UNSATISFIED", "run_status": "failed"},
        ),
        ProductReadinessState("stock_core", READINESS_READY, "fresh", is_mandatory=True),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh", is_mandatory=True),
        ProductReadinessState("review", READINESS_READY, "fresh", is_mandatory=True),
    ]


def _core_ready_waiting_mandatory() -> list[ProductReadinessState]:
    """stock_core 可消费，但 board/review 等 mandatory 未完成 → core_ready。

    [P0-4] stock_core ready 但 review 未完成，整个 mandatory 链未全 consumable。
    """
    states = _mandatory(review=READINESS_PENDING)
    states[4] = ProductReadinessState(
        "review", READINESS_PENDING, "fresh", is_mandatory=True,
        lineage={"reason_code": "NO_PUBLICATION"},
    )
    return states


def _mandatory_ready_enhancing() -> list[ProductReadinessState]:
    """mandatory 全部 ready，但至少一个 enhancement 尚未终态 → mandatory_ready_enhancing。

    [审查报告] async 场景：五个 mandatory 全部 ready（含已发布 stock_core），
    dsa_projection ready + chip running + state_events ready + auction pending/running。
    """
    states = _mandatory()
    states += [
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("chip", READINESS_PENDING, is_mandatory=False, is_terminal=False),
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("auction_anchor", READINESS_PENDING, is_mandatory=False, is_terminal=False),
    ]
    return states


def _degraded_terminal_partial() -> list[ProductReadinessState]:
    """mandatory 全部可消费；enhancement 全部终态，但至少一个不 truly ready → degraded_ready。

    [审查报告] degraded 场景：mandatory 完成主链；chip partial terminal（非 truly ready）
    + auction hybrid/structure_only terminal（非 composite）+ board reused（非 fully fresh）。
    """
    states = _mandatory(board=READINESS_READY_REUSED, board_freshness="reused")
    states += [
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        # chip 失败（terminal 但不可消费）→ 非 truly ready
        ProductReadinessState("chip", READINESS_UNAVAILABLE, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=False),
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        # auction hybrid（terminal 但非 composite）→ 非 fully ready
        ProductReadinessState("auction_anchor", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True, auction_mode="hybrid", is_product_ready=False, lineage={"reason_code": "AUCTION_HYBRID"}),
    ]
    return states


def _fully_ready_all_fresh() -> list[ProductReadinessState]:
    """mandatory 全部 fresh，enhancement 全部 truly ready，auction composite → fully_ready。"""
    return _mandatory() + _enhancement_all_ready()


def _synthetic_external_ceiling() -> list[ProductReadinessState]:
    """非验收诊断场景：小规模 synthetic 输入被正式外部门禁拒绝。

    100 股小样本下 board_facts 门禁（raw_rows≥5000/industry≥200/concept≥300/
    relation≥60000/coverage≥0.99）无法合法达成 → board_facts unavailable → blocked。
    证明外部门禁对小规模 synthetic 的真实拒绝能力；不计入 full-closure 绿色验收。
    """
    states = _mandatory(board=READINESS_UNAVAILABLE)
    states[1] = ProductReadinessState(
        "board_facts", READINESS_UNAVAILABLE, "fresh", is_mandatory=True,
        is_terminal=True,
        lineage={"reason_code": "EXTERNAL_GATE_UNSATISFIED", "run_status": "failed"},
    )
    return states


# 六态 canonical 场景（验收载体）
CANONICAL_SCENARIOS: dict[str, tuple[list[ProductReadinessState], str]] = {
    "pending_no_core": (_pending_no_core(), CLOSURE_PENDING),
    "blocked_mandatory_failure": (_blocked_mandatory_failure(), CLOSURE_BLOCKED),
    "core_ready_waiting_mandatory": (_core_ready_waiting_mandatory(), CLOSURE_CORE_READY),
    "mandatory_ready_enhancing": (_mandatory_ready_enhancing(), CLOSURE_MANDATORY_READY_ENHANCING),
    "degraded_terminal_partial": (_degraded_terminal_partial(), CLOSURE_DEGRADED_READY),
    "fully_ready_all_fresh": (_fully_ready_all_fresh(), CLOSURE_FULLY_READY),
}

# 非验收诊断场景（不计入绿色验收）
DIAGNOSTIC_SCENARIOS: dict[str, tuple[list[ProductReadinessState], str]] = {
    "synthetic_external_ceiling": (_synthetic_external_ceiling(), CLOSURE_BLOCKED),
}

ALL_SCENARIOS: dict[str, tuple[list[ProductReadinessState], str]] = {
    **CANONICAL_SCENARIOS,
    **DIAGNOSTIC_SCENARIOS,
}


def get_scenario(name: str) -> tuple[list[ProductReadinessState], str]:
    """返回 (输入事实矩阵, 唯一期望 closure)。"""
    if name not in ALL_SCENARIOS:
        raise KeyError(f"未知 canonical 场景: {name}")
    return ALL_SCENARIOS[name]
