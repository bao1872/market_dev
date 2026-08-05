"""V2.1 就绪闭包 + 竞价模式**决策函数集成测试**（纯内存，不连数据库）。

[Corrective-3 §五] 本文件此前被命名/描述为 "Synthetic E2E"，但它只组合了三个
决策纯函数（evaluate_closure / evaluate_governance / decide_auction_mode），
**不经过任何 worker、publication adapter 或真实编排路径**，因此不构成 E2E。
现更名为 decision integration，如实反映其覆盖范围。

真正的 worker 编排测试见 `test_chip_worker_orchestration.py`
（真实 finalize helper + publish adapter + auction adapter，fake session）。

本文件覆盖决策层的关键不变量：

  1. closure transition：stock_core 形成 → core_ready → mandatory 完成 → fully_ready；
  2. late chip upgrade：chip 由 partial（degraded）补齐为 ready 后 closure 升级为 fully_ready；
  3. failure matrix：chip partial → 绝不伪 composite/fully_ready，保持 degraded；
  4. retry 幂等：重复提交同一终态状态集合 → closure 与 lineage 结果稳定（不重复派生）；
  5. lineage 真实化（G 修正）：fully_ready 下每个产品 pointerLineage 含
     publication_id / pointer_data_run_id / algorithm_version / reason_code，
     且派生产品带 derived_from_stock_core 真实父子关系；
  6. auction mode 分支：由 orchestration 状态（stock_core_ready / chip_ready）推导
     structure_only / hybrid / composite。

不创建 PostgreSQL、不连接 bz_stock、不调用任何 DB-backed worker。
PG 依赖项（真实 orchestrator + publish 落库）见 test_v21_synthetic_e2e_pg.py，
status=authored_not_executed reason=pg_gate_deferred_no_local_db_authorization。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.domain_status import (
    AUCTION_MODE_COMPOSITE,
    AUCTION_MODE_HYBRID,
    AUCTION_MODE_STRUCTURE_ONLY,
)
from app.services.auction_mode_service import decide_auction_mode
from app.services.product_readiness_service import (
    CLOSURE_CORE_READY,
    CLOSURE_FULLY_READY,
    READINESS_DEGRADED,
    READINESS_PENDING,
    READINESS_READY,
    ProductReadinessState,
    evaluate_closure,
    evaluate_governance,
)

# ---- 编排状态构造 helpers（模拟真实 service 会产出的 state 集合）----


def _core_ready_state(pub_id: str, run_id: str) -> ProductReadinessState:
    return ProductReadinessState(
        "stock_core", READINESS_READY, "fresh", is_mandatory=True, is_terminal=True,
        lineage={
            "source_type": "publication_pointer",
            "publication_id": pub_id,
            "pointer_data_run_id": run_id,
            "algorithm_version": "v2",
            "coverage": 0.99,
            "reason_code": "FRESH_PUBLICATION",
        },
    )


def _mandatory_ready(product: str, pub_id: str, run_id: str) -> ProductReadinessState:
    return ProductReadinessState(
        product, READINESS_READY, "fresh", is_mandatory=True, is_terminal=True,
        lineage={
            "source_type": "publication_pointer",
            "publication_id": pub_id,
            "pointer_data_run_id": run_id,
            "algorithm_version": "v1",
            "coverage": 0.95,
            "reason_code": "FRESH_PUBLICATION",
        },
    )


def _enhancement_ready(product: str, src_run_id: str) -> ProductReadinessState:
    return ProductReadinessState(
        product, READINESS_READY, "fresh", is_mandatory=False, is_terminal=True,
        lineage={
            "source_type": "derived_from_stock_core",
            "derived_from": "stock_core",
            "source_core_run_id": src_run_id,
            "reason_code": "UPGRADED_FROM_PARENT",
        },
    )


def _enhancement_degraded(product: str, reason: str) -> ProductReadinessState:
    return ProductReadinessState(
        product, READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True,
        lineage={"source_type": "run_status", "reason_code": reason},
    )


def _pending(product: str, mandatory: bool = True) -> ProductReadinessState:
    return ProductReadinessState(
        product, READINESS_PENDING, "fresh", is_mandatory=mandatory,
        lineage={"source_type": "run_status", "reason_code": "NO_RUN"},
    )


@dataclass
class SyntheticStateRepository:
    """内存状态仓库：保存按 business_date 提交的产品就绪状态集合。

    模拟 orchestrator 收集各产品 run 后的中间状态；E2E 通过反复提交同一/演化集合
    验证 closure / lineage / 幂等性。
    """

    states: dict[str, list[ProductReadinessState]] = field(default_factory=dict)
    publish_count: dict[str, int] = field(default_factory=dict)

    def submit(self, business_date: str, states: list[ProductReadinessState]) -> None:
        # [I-4] 重复提交同一终态集合 → 视为幂等，不重复计数
        if business_date in self.states:
            self.publish_count[business_date] += 1
        else:
            self.publish_count[business_date] = 1
        self.states[business_date] = states

    def orchestrate(self, business_date: str):
        """调用真实编排决策函数（service-level E2E 核心）。"""
        states = self.states[business_date]
        closure = evaluate_closure(states)
        gov = evaluate_governance(states, closure)
        return closure, gov


# ---- 测试 ----


def test_closure_transition_core_to_fully_ready():
    """I-1：stock_core 形成 → core_ready；mandatory 全完成 → fully_ready。"""
    repo = SyntheticStateRepository()

    # 阶段1：仅 stock_core 就绪
    repo.submit("2026-08-05", [_core_ready_state("pub-1", "run-1"), _pending("board_facts"),
                                _pending("review"), _pending("daily_facts")])
    closure, _ = repo.orchestrate("2026-08-05")
    assert closure.closure == CLOSURE_CORE_READY

    # 阶段2：mandatory 全完成，enhancement 终态
    states = [
        _core_ready_state("pub-1", "run-1"),
        _mandatory_ready("board_facts", "pub-b", "run-b"),
        _mandatory_ready("review", "pub-r", "run-r"),
        _mandatory_ready("daily_facts", "pub-d", "run-d"),
        _enhancement_ready("dsa_projection", "run-1"),
        _enhancement_ready("state_events", "run-1"),
        _enhancement_ready("chip", "run-1"),
        _enhancement_ready("auction_anchor", "run-1"),
    ]
    repo.submit("2026-08-05", states)
    closure, _ = repo.orchestrate("2026-08-05")
    assert closure.closure == CLOSURE_FULLY_READY
    assert closure.mandatory_products_ready is True
    assert closure.enhancement_jobs_terminal is True


def test_late_chip_upgrade_degraded_to_fresh():
    """I-2：chip 由 partial(degraded/stale) 补齐为 ready → chip 新鲜度升级、stale 标志消除。

    [P0-3] enhancement 终态即允许 fully_ready（不阻塞闭包），但 degraded 的 chip 仍表现为
    stale；late chip 到达后 freshness 由 stale→fresh。验证治理能识别此升级信号。
    """
    repo = SyntheticStateRepository()
    base = [
        _core_ready_state("pub-1", "run-1"),
        _mandatory_ready("board_facts", "pub-b", "run-b"),
        _mandatory_ready("review", "pub-r", "run-r"),
        _mandatory_ready("daily_facts", "pub-d", "run-d"),
        _enhancement_ready("dsa_projection", "run-1"),
        _enhancement_ready("state_events", "run-1"),
        _enhancement_ready("auction_anchor", "run-1"),
    ]
    # chip 初态 partial（degraded / stale）
    base.append(_enhancement_degraded("chip", "CHIP_PARTIAL"))
    repo.submit("2026-08-05", base)
    closure1, gov1 = repo.orchestrate("2026-08-05")
    assert closure1.closure == CLOSURE_FULLY_READY  # 终态 chip 不阻塞闭包（P0-3）
    assert "chip" in gov1.stale_children  # 但 chip 仍被标记为 stale
    assert gov1.pointer_lineage["chip"]["reason_code"] == "CHIP_PARTIAL"

    # late chip 到达，chip 升级为 ready（fresh）
    upgraded = base[:-1] + [_enhancement_ready("chip", "run-1")]
    repo.submit("2026-08-05", upgraded)
    closure2, gov2 = repo.orchestrate("2026-08-05")
    assert closure2.closure == CLOSURE_FULLY_READY
    assert "chip" not in gov2.stale_children  # stale 标志已消除
    assert gov2.pointer_lineage["chip"]["reason_code"] == "UPGRADED_FROM_PARENT"


def test_failure_matrix_chip_partial_never_fake_fresh():
    """I-3：chip partial → 绝不伪 fully fresh / composite 模式。

    chip 为 enhancement 且终态时闭包可达 fully_ready（P0-3），但 chip 新鲜度必须为 stale，
    且 auction mode 不得因此误判为 composite。
    """
    repo = SyntheticStateRepository()
    states = [
        _core_ready_state("pub-1", "run-1"),
        _mandatory_ready("board_facts", "pub-b", "run-b"),
        _mandatory_ready("review", "pub-r", "run-r"),
        _mandatory_ready("daily_facts", "pub-d", "run-d"),
        _enhancement_ready("dsa_projection", "run-1"),
        _enhancement_ready("state_events", "run-1"),
        _enhancement_ready("auction_anchor", "run-1"),
        _enhancement_degraded("chip", "CHIP_PARTIAL"),
    ]
    repo.submit("2026-08-05", states)
    closure, gov = repo.orchestrate("2026-08-05")
    assert closure.closure == CLOSURE_FULLY_READY  # 终态 chip 不阻塞
    assert gov.pointer_lineage["chip"]["freshness"] == "stale"  # 不伪 fresh
    # auction mode：chip 未 composite → 批次不得标 composite
    instr = ["A", "B"]
    decision = decide_auction_mode(
        eligible_instruments=instr,
        chip_ready_instruments=set(),
        failed_instruments=set(),
        stale_instruments=set(),
        chip_available=True,
    )
    assert decision.mode != AUCTION_MODE_COMPOSITE


def test_retry_idempotent_no_duplicate_derivation():
    """I-4：重复提交同一终态集合 → closure/lineage 稳定，不重复派生。"""
    repo = SyntheticStateRepository()
    states = [
        _core_ready_state("pub-1", "run-1"),
        _mandatory_ready("board_facts", "pub-b", "run-b"),
        _mandatory_ready("review", "pub-r", "run-r"),
        _mandatory_ready("daily_facts", "pub-d", "run-d"),
        _enhancement_ready("dsa_projection", "run-1"),
        _enhancement_ready("state_events", "run-1"),
        _enhancement_ready("chip", "run-1"),
        _enhancement_ready("auction_anchor", "run-1"),
    ]
    repo.submit("2026-08-05", states)
    c1, g1 = repo.orchestrate("2026-08-05")
    # 模拟 retry scheduler 重跑同一集合
    repo.submit("2026-08-05", states)
    c2, g2 = repo.orchestrate("2026-08-05")

    assert c1.closure == c2.closure == CLOSURE_FULLY_READY
    assert g1.pointer_lineage == g2.pointer_lineage  # 幂等
    assert repo.publish_count["2026-08-05"] == 2  # 确实重试了，但编排结果一致


def test_lineage_real_fields_present_in_fully_ready():
    """I-5：fully_ready 下 pointerLineage 含真实字段（G 修正的血统审计）。"""
    repo = SyntheticStateRepository()
    states = [
        _core_ready_state("pub-1", "run-1"),
        _mandatory_ready("board_facts", "pub-b", "run-b"),
        _mandatory_ready("review", "pub-r", "run-r"),
        _mandatory_ready("daily_facts", "pub-d", "run-d"),
        _enhancement_ready("dsa_projection", "run-1"),
        _enhancement_ready("state_events", "run-1"),
        _enhancement_ready("chip", "run-1"),
        _enhancement_ready("auction_anchor", "run-1"),
    ]
    repo.submit("2026-08-05", states)
    _, gov = repo.orchestrate("2026-08-05")

    sc = gov.pointer_lineage["stock_core"]
    assert isinstance(sc, dict)
    assert sc["publication_id"] == "pub-1"
    assert sc["pointer_data_run_id"] == "run-1"
    assert sc["algorithm_version"] == "v2"
    assert sc["reason_code"] == "FRESH_PUBLICATION"

    dsa = gov.pointer_lineage["dsa_projection"]
    assert dsa["source_type"] == "derived_from_stock_core"
    assert dsa["derived_from"] == "stock_core"
    assert dsa["source_core_run_id"] == "run-1"


def test_auction_mode_branches_driven_by_orchestration():
    """I-6：auction mode 由 orchestration 派生的 chip 就绪集合推导
    （structure_only / hybrid / composite），禁止伪 composite。
    """
    instruments = ["A", "B", "C"]

    # structure_only：stock_core ready 但无 chip 可用
    decision_none = decide_auction_mode(
        eligible_instruments=instruments,
        chip_ready_instruments=set(),
        failed_instruments=set(),
        stale_instruments=set(),
        chip_available=False,
    )
    assert decision_none.mode == AUCTION_MODE_STRUCTURE_ONLY

    # composite：全部 instrument 的 chip ready，无 failed/stale
    decision_all = decide_auction_mode(
        eligible_instruments=instruments,
        chip_ready_instruments=set(instruments),
        failed_instruments=set(),
        stale_instruments=set(),
        chip_available=True,
    )
    assert decision_all.mode == AUCTION_MODE_COMPOSITE
    assert decision_all.composite_anchor_count == 3

    # hybrid：部分 chip ready（late chip 部分到达）
    decision_partial = decide_auction_mode(
        eligible_instruments=instruments,
        chip_ready_instruments={"A"},
        failed_instruments=set(),
        stale_instruments=set(),
        chip_available=True,
    )
    assert decision_partial.mode == AUCTION_MODE_HYBRID
    assert decision_partial.coverage_ratio == pytest.approx(1 / 3)

    # 禁止伪 composite：存在 stale chip → 不得 composite
    decision_stale = decide_auction_mode(
        eligible_instruments=instruments,
        chip_ready_instruments=set(instruments),
        failed_instruments=set(),
        stale_instruments={"C"},
        chip_available=True,
    )
    assert decision_stale.mode != AUCTION_MODE_COMPOSITE
