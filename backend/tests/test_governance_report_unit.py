"""[V2.1 Commit G] Governance 治理报告纯函数单元测试。

覆盖：
- pointer lineage：publication_pointer / run_status / derived_from_stock_core
- stale children：freshness != fresh 的产品
- unmatched active children：非终态增强/派生而其父 stock_core 已可消费
- ready / pending / blocked / unavailable 分组
- degraded reasons 透传 closure issues

运行（纯单元，不连库）：
    PURE_UNIT_TEST=1 ./.venv/bin/python -m pytest \
        tests/test_governance_report_unit.py -q -p no:cacheprovider
"""

from __future__ import annotations

from app.domain_status import (
    CLOSURE_BLOCKED,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_READY_REUSED,
    READINESS_UNAVAILABLE,
)
from app.services.product_readiness_service import (
    ProductReadinessState,
    evaluate_closure,
    evaluate_governance,
    CLOSURE_PENDING,
)


def _full_states() -> list[ProductReadinessState]:
    """九节点全部就绪（fully_ready）。"""
    return [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True),
        ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True),
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True),
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, auction_mode="composite"),
    ]


def test_pointer_lineage_full_ready():
    """fully_ready：[G 修正] pointer_lineage 返回真实血缘 dict，而非字符串来源类型。

    consumable 产品带 publication_pointer 真实字段；派生产品带 derived_from_stock_core
    与 source_core_run_id，支撑血统审计。
    """
    states = [
        ProductReadinessState(
            "daily_facts", READINESS_READY, "fresh",
            lineage={"source_type": "publication_pointer", "publication_id": "pub-daily",
                      "pointer_data_run_id": "run-daily", "algorithm_version": "v1"},
        ),
        ProductReadinessState(
            "stock_core", READINESS_READY, "fresh",
            lineage={"source_type": "publication_pointer", "publication_id": "pub-core",
                      "pointer_data_run_id": "run-core", "algorithm_version": "v2"},
        ),
        ProductReadinessState(
            "dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
            lineage={"source_type": "derived_from_stock_core", "derived_from": "stock_core",
                      "source_core_run_id": "run-core", "reason_code": "UPGRADED_FROM_PARENT"},
        ),
        ProductReadinessState(
            "chip", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
            lineage={"source_type": "publication_pointer", "publication_id": "pub-chip",
                      "pointer_data_run_id": "run-chip", "algorithm_version": "v3", "coverage": 0.91},
        ),
    ]
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    sc = gov.pointer_lineage["stock_core"]
    assert sc["source_type"] == "publication_pointer"
    assert sc["publication_id"] == "pub-core"
    assert sc["pointer_data_run_id"] == "run-core"
    assert sc["algorithm_version"] == "v2"

    ds = gov.pointer_lineage["dsa_projection"]
    assert ds["source_type"] == "derived_from_stock_core"
    assert ds["derived_from"] == "stock_core"
    assert ds["source_core_run_id"] == "run-core"
    assert ds["reason_code"] == "UPGRADED_FROM_PARENT"

    chip = gov.pointer_lineage["chip"]
    assert chip["source_type"] == "publication_pointer"
    assert chip["coverage"] == 0.91
    assert isinstance(gov.pointer_lineage["stock_core"], dict)


def test_stale_reused_children():
    """board_facts reused → stale_children 含 board_facts。"""
    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    assert "board_facts" in gov.stale_children
    assert "stock_core" not in gov.stale_children


def test_unmatched_active_children():
    """stock_core 可消费但 chip/auction 仍在运行（非终态）→ unmatched_active_children。"""
    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("chip", READINESS_PENDING, "fresh", is_mandatory=False, is_terminal=False),
        ProductReadinessState("auction_anchor", READINESS_PENDING, "fresh", is_mandatory=False, is_terminal=False),
    ]
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    assert "chip" in gov.unmatched_active_children
    assert "auction_anchor" in gov.unmatched_active_children
    # 派生 dsa_projection/state_events 随 core ready 已 terminal，不进入
    assert "dsa_projection" not in gov.unmatched_active_children


def test_unmatched_active_requires_core_consumable():
    """stock_core 未可消费时，即使 enhancement 非终态也不算 unmatched（父未就绪）。"""
    states = [
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("chip", READINESS_PENDING, "fresh", is_mandatory=False, is_terminal=False),
    ]
    closure = evaluate_closure(states)
    assert closure.closure == CLOSURE_PENDING
    gov = evaluate_governance(states, closure)
    assert gov.unmatched_active_children == []


def test_grouping_by_readiness():
    """ready / pending / unavailable 分组正确。"""
    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_PENDING),
        ProductReadinessState("board_aggregation", READINESS_PENDING),
        ProductReadinessState("review", READINESS_PENDING),
    ]
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    assert set(gov.ready_products) == {"daily_facts", "board_facts"}
    assert set(gov.pending_products) == {"stock_core", "board_aggregation", "review"}
    assert gov.unavailable_products == []


def test_unavailable_grouping():
    """mandatory unavailable → blocked，unavailable_products 分组。"""
    states = [
        ProductReadinessState("board_facts", READINESS_UNAVAILABLE),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
    ]
    closure = evaluate_closure(states)
    assert closure.closure == CLOSURE_BLOCKED
    gov = evaluate_governance(states, closure)

    assert "board_facts" in gov.unavailable_products
    assert gov.blocked_products == []


def test_degraded_reasons_passthrough():
    """degraded_reasons 透传 closure issues。"""
    states = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY_REUSED, "reused"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
    ]
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    assert gov.degraded_reasons == closure.issues
    assert any(i["code"] == "NOT_FULLY_FRESH" for i in gov.degraded_reasons)


def test_reuses_evaluate_closure():
    """治理报告与闭包评估共用 evaluate_closure（口径一致）。"""
    states = _full_states()
    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)
    assert closure.closure == "fully_ready"
    assert gov.ready_products is not None
    assert set(gov.ready_products) == {s.product for s in states}