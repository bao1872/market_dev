"""[Corrective-3 §三/§四] ProductReadiness 统一 lineage + 后端治理动作测试（不连数据库）。

覆盖：
  - 每个节点 lineage 均包含 LINEAGE_KEYS 全部键（缺失显式 None，不允许键缺席）；
  - chip run succeeded 但 publication 缺失 → degraded + retry_chip_publication；
  - auction structure_only → 体现等待 chip 升级的 reason/freshness；
  - auction terminal failure → 包含 run_id 与 reason；
  - pending 节点也必须给出 NO_RUN / NO_PUBLICATION 等 reason_code；
  - source_core_run_id 不得默认 None（由 publication + 领域 run 联查补齐）；
  - 治理动作由后端解析（前端不再猜测）。

运行（不连库）：
    PURE_UNIT_TEST=1 pytest backend/tests/test_readiness_lineage_governance.py
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.domain_status import (
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
    READINESS_UNAVAILABLE,
)
from app.services.product_readiness_service import (
    ACTION_RETRY_CHIP_PUBLICATION,
    LINEAGE_KEYS,
    ProductReadinessState,
    _product_lineage,
    _publication_lineage,
    evaluate_closure,
    evaluate_governance,
    resolve_governance_action,
)

TRADE_DATE = date(2026, 8, 5)


# =============================================================================
# §三：统一 lineage 结构
# =============================================================================


def test_every_lineage_key_present_even_when_missing() -> None:
    """lineage 必须包含全部 LINEAGE_KEYS，缺失值显式 None（键不得缺席）。"""
    state = ProductReadinessState(
        "chip", READINESS_PENDING, "fresh", is_mandatory=False,
        lineage={"source_type": "domain_run", "reason_code": "NO_CHIP_RUN"},
    )
    lineage = _product_lineage(state)
    for key in LINEAGE_KEYS:
        assert key in lineage, f"lineage 缺少键: {key}"


def test_pending_node_must_have_reason_code() -> None:
    """pending 节点也必须给出 reason_code，不得为空。"""
    state = ProductReadinessState(
        "daily_facts", READINESS_PENDING, "fresh",
        lineage={"source_type": "publication_pointer",
                 "reason_code": "NO_PUBLICATION"},
    )
    lineage = _product_lineage(state)
    assert lineage["reason_code"] == "NO_PUBLICATION"
    assert lineage["recommended_action"] == "await_publication"
    assert lineage["retryable"] is True


def test_lineage_defaults_reason_code_to_none_string() -> None:
    state = ProductReadinessState("review", READINESS_READY, "fresh")
    lineage = _product_lineage(state)
    assert lineage["reason_code"] == "NONE"


# =============================================================================
# §三：publication + 领域 run 联查，source_core_run_id 不得默认 None
# =============================================================================


@dataclass
class _Pub:
    id: uuid.UUID
    data_run_id: uuid.UUID
    algorithm_version: str = "v2"
    coverage_ratio: float = 0.97
    published_at: datetime | None = None
    parameter_hash: str | None = "ph-1"


@dataclass
class _DomainRun:
    id: uuid.UUID
    source_core_run_id: uuid.UUID | None = None
    source_board_run_id: uuid.UUID | None = None
    algorithm_version: str = "v2"
    coverage_ratio: float = 0.97
    status: str = "succeeded"
    finished_at: datetime | None = None


def test_publication_lineage_fills_source_core_run_id_from_domain_run() -> None:
    core_run = uuid.uuid4()
    pub = _Pub(
        id=uuid.uuid4(), data_run_id=uuid.uuid4(),
        published_at=datetime(2026, 8, 5, 15, 30, tzinfo=UTC),
    )
    run = _DomainRun(
        id=pub.data_run_id, source_core_run_id=core_run,
        finished_at=datetime(2026, 8, 5, 15, 20, tzinfo=UTC),
    )

    lineage = _publication_lineage(pub, run)

    assert lineage["source_core_run_id"] == str(core_run)
    assert lineage["source_core_run_id"] is not None
    assert lineage["publication_id"] == str(pub.id)
    assert lineage["pointer_data_run_id"] == str(pub.data_run_id)
    assert lineage["domain_run_id"] == str(run.id)
    assert lineage["algorithm_version"] == "v2"
    assert lineage["coverage"] == 0.97
    assert lineage["published_at"] == "2026-08-05T15:30:00+00:00"
    assert lineage["calculated_at"] == "2026-08-05T15:20:00+00:00"
    assert lineage["status"] == "succeeded"


def test_publication_lineage_without_domain_run_uses_explicit_core_run() -> None:
    explicit = str(uuid.uuid4())
    pub = _Pub(id=uuid.uuid4(), data_run_id=uuid.uuid4())
    lineage = _publication_lineage(pub, None, explicit)
    assert lineage["source_core_run_id"] == explicit


def test_publication_lineage_ids_are_none_not_empty_string() -> None:
    """id 缺失时必须是 None，不得退化为空串（空串会被误认为有效血缘）。"""

    @dataclass
    class _Empty:
        id: Any = None
        data_run_id: Any = None

    lineage = _publication_lineage(_Empty(), None)
    assert lineage["publication_id"] is None
    assert lineage["pointer_data_run_id"] is None


# =============================================================================
# §二.4 / §四：chip publication 缺失必须可治理
# =============================================================================


def test_chip_publication_missing_is_governable() -> None:
    state = ProductReadinessState(
        "chip", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True,
        lineage={
            "source_type": "domain_run",
            "domain_run_id": "run-chip",
            "status": "succeeded",
            "reason_code": "CHIP_PUBLICATION_MISSING",
        },
    )
    lineage = _product_lineage(state)

    assert lineage["reason_code"] == "CHIP_PUBLICATION_MISSING"
    assert lineage["retryable"] is True
    assert lineage["recommended_action"] == ACTION_RETRY_CHIP_PUBLICATION
    assert lineage["operation"] == "republish_chip_consensus"
    assert lineage["target_run_id"] == "run-chip"
    # chip run 成功但 pointer 缺失 → 不得呈现为 ready
    assert lineage["readiness"] == READINESS_DEGRADED


def test_chip_lineage_conflict_is_not_retryable() -> None:
    retryable, action, operation = resolve_governance_action(
        "CHIP_PUBLICATION_LINEAGE_REJECTED", READINESS_DEGRADED,
    )
    assert retryable is False
    assert action == "inspect_chip_lineage_conflict"
    assert operation == "manual_investigation"


# =============================================================================
# §三：auction structure_only / terminal failure
# =============================================================================


def test_auction_structure_only_shows_pending_chip_upgrade() -> None:
    state = ProductReadinessState(
        "auction_anchor", READINESS_DEGRADED, "stale",
        is_mandatory=False, is_terminal=True,
        lineage={
            "source_type": "domain_run",
            "domain_run_id": "run-auction",
            "status": "structure_only",
            "reason_code": "AUCTION_STRUCTURE_ONLY",
        },
    )
    lineage = _product_lineage(state)

    assert lineage["reason_code"] == "AUCTION_STRUCTURE_ONLY"
    assert lineage["freshness"] == "stale"
    assert lineage["recommended_action"] == "await_chip_upgrade"
    assert lineage["readiness"] == READINESS_DEGRADED


def test_auction_terminal_failure_carries_run_id_and_reason() -> None:
    state = ProductReadinessState(
        "auction_anchor", READINESS_UNAVAILABLE, "fresh",
        is_mandatory=False, is_terminal=True,
        lineage={
            "source_type": "domain_run",
            "domain_run_id": "run-auction-2",
            "run_id": "run-auction-2",
            "status": "failed",
            "reason_code": "AUCTION_FAILED",
            "error_message": "锚点生成异常",
        },
    )
    lineage = _product_lineage(state)

    assert lineage["reason_code"] == "AUCTION_FAILED"
    assert lineage["domain_run_id"] == "run-auction-2"
    assert lineage["target_run_id"] == "run-auction-2"
    assert lineage["recommended_action"] == "rerun_auction_anchor"


# =============================================================================
# §三：review / 派生投影
# =============================================================================


def test_review_run_published_without_pointer_is_degraded_action() -> None:
    retryable, action, _op = resolve_governance_action(
        "REVIEW_NOT_PUBLISHED", READINESS_DEGRADED,
    )
    assert retryable is True
    assert action == "publish_market_review"


def test_missing_projection_has_rebuild_action() -> None:
    _r, action, _op = resolve_governance_action("NO_PROJECTION", READINESS_PENDING)
    assert action == "rebuild_dsa_projection"


def test_missing_state_events_has_rebuild_action() -> None:
    _r, action, _op = resolve_governance_action("NO_STATE_EVENTS", READINESS_PENDING)
    assert action == "rebuild_state_events"


def test_consumable_fresh_publication_needs_no_action() -> None:
    retryable, action, operation = resolve_governance_action(
        "FRESH_PUBLICATION", READINESS_READY,
    )
    assert retryable is False
    assert action == "none"
    assert operation == "no_operation"


# =============================================================================
# [Corrective-3.1 §P1] 精确 lineage：产物必须归属当前 core run
# =============================================================================


def test_projection_lineage_mismatch_is_governable() -> None:
    """当日有快照但不属于当前 core run → 必须可治理，且不得判 ready。"""
    retryable, action, _op = resolve_governance_action(
        "PROJECTION_LINEAGE_MISMATCH", READINESS_DEGRADED,
    )
    assert retryable is True
    assert action == "rebuild_dsa_projection"


def test_state_events_lineage_mismatch_is_governable() -> None:
    retryable, action, _op = resolve_governance_action(
        "STATE_EVENTS_LINEAGE_MISMATCH", READINESS_DEGRADED,
    )
    assert retryable is True
    assert action == "rebuild_state_events"


def test_review_pointer_missing_is_governable() -> None:
    """run 自称 published 但 factor_publications 无 pointer → 可治理。"""
    retryable, action, _op = resolve_governance_action(
        "REVIEW_POINTER_MISSING", READINESS_DEGRADED,
    )
    assert retryable is True
    assert action == "publish_market_review"


def test_dsa_counter_signature_requires_core_run() -> None:
    """_count_dsa_projections 必须接受 source_core_run_id 并返回 matched/total。

    Corrective-3 只按 trade_date 计数，任何当日快照都会让节点变 ready，
    无法区分"上一轮 run 的残留投影"。
    """
    import inspect

    from app.services.product_readiness_service import ProductReadinessService

    sig = inspect.signature(ProductReadinessService._count_dsa_projections)
    assert "source_core_run_id" in sig.parameters, (
        "DSA 投影计数必须按 core run 过滤"
    )
    src = inspect.getsource(ProductReadinessService._count_dsa_projections)
    assert "source_run_id" in src, "必须绑定 StockFeatureSnapshot.source_run_id"
    assert '"matched"' in src and '"total"' in src


def test_state_events_counter_requires_core_run_and_algo() -> None:
    """_count_state_events 必须按 core run 过滤并暴露 algorithm_version。"""
    import inspect

    from app.services.product_readiness_service import ProductReadinessService

    sig = inspect.signature(ProductReadinessService._count_state_events)
    assert "source_core_run_id" in sig.parameters
    src = inspect.getsource(ProductReadinessService._count_state_events)
    assert "source_run_id" in src
    assert "algorithm_version" in src, "state events lineage 必须暴露算法版本"


def test_review_state_reads_factor_publication_pointer() -> None:
    """_review_state 必须真正查询 FactorPublication，而不是只看 MarketReviewRun。"""
    import inspect

    from app.services.product_readiness_service import ProductReadinessService

    src = inspect.getsource(ProductReadinessService._review_state)
    assert "FactorPublication" in src, (
        "review 就绪必须以 factor_publications pointer 为准"
    )
    assert "PUBLICATION_KIND_MARKET_REVIEW" in src
    # 无 pointer 时不得因 run.status == published 就判 ready
    assert "REVIEW_POINTER_MISSING" in src


# =============================================================================
# 集成：governance report 输出统一 lineage
# =============================================================================


def _pub_state(product: str, *, mandatory: bool = True) -> ProductReadinessState:
    return ProductReadinessState(
        product, READINESS_READY, "fresh",
        is_mandatory=mandatory, is_terminal=True,
        lineage={
            "source_type": "publication_pointer",
            "publication_id": f"pub-{product}",
            "pointer_data_run_id": f"run-{product}",
            "source_core_run_id": "run-core",
            "algorithm_version": "v2",
            "reason_code": "FRESH_PUBLICATION",
        },
    )


def test_governance_report_lineage_is_uniform_across_nodes() -> None:
    states = [
        _pub_state("daily_facts"),
        _pub_state("board_facts"),
        _pub_state("stock_core"),
        _pub_state("board_aggregation"),
        _pub_state("review"),
        ProductReadinessState(
            "dsa_projection", READINESS_READY, "fresh",
            is_mandatory=False, is_terminal=True,
            lineage={"source_type": "derived_projection",
                     "parent_product": "stock_core",
                     "parent_run_id": "run-stock_core",
                     "reason_code": "PROJECTION_PRESENT"},
        ),
        ProductReadinessState(
            "chip", READINESS_DEGRADED, "stale",
            is_mandatory=False, is_terminal=True,
            lineage={"source_type": "domain_run", "domain_run_id": "run-chip",
                     "reason_code": "CHIP_PUBLICATION_MISSING"},
        ),
        ProductReadinessState(
            "state_events", READINESS_READY, "fresh",
            is_mandatory=False, is_terminal=True,
            lineage={"source_type": "derived_projection",
                     "parent_product": "stock_core",
                     "reason_code": "STATE_EVENTS_PRESENT"},
        ),
        ProductReadinessState(
            "auction_anchor", READINESS_DEGRADED, "stale",
            is_mandatory=False, is_terminal=True,
            lineage={"source_type": "domain_run", "domain_run_id": "run-auction",
                     "reason_code": "AUCTION_STRUCTURE_ONLY"},
        ),
    ]

    closure = evaluate_closure(states)
    gov = evaluate_governance(states, closure)

    assert len(gov.pointer_lineage) == 9
    for product, lineage in gov.pointer_lineage.items():
        for key in LINEAGE_KEYS:
            assert key in lineage, f"{product} 缺少 lineage 键 {key}"
        assert lineage["reason_code"], f"{product} reason_code 为空"
        assert "recommended_action" in lineage

    # chip / auction 的治理动作由后端给出
    assert gov.pointer_lineage["chip"]["recommended_action"] == (
        ACTION_RETRY_CHIP_PUBLICATION
    )
    assert gov.pointer_lineage["auction_anchor"]["recommended_action"] == (
        "await_chip_upgrade"
    )
    # 陈旧子产品被正确识别
    assert "chip" in gov.stale_children
    assert "auction_anchor" in gov.stale_children
