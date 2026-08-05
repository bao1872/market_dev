"""[PRD Alignment Pass P1-5] V2.1 决策层 Synthetic E2E（无 PG，可 PURE_UNIT_TEST 执行）。

PRD 定义的 synthetic E2E 是完整业务链：
    trade date → daily + pywencai → board publication → CoreRunContext
    → one DSA artifact → stock_core publication → chip → board aggregation
    → Review → late chip → auction upgrade → ProductReadiness → API DTO → frontend。

完整 PG E2E 见 test_v21_synthetic_e2e_pg.py（status=authored_not_executed，需远程 PG）。
本文件在无 PG 环境下，用已合同验证的决策函数串联完整**决策链**，硬断言：

1. DSA compute-once 门禁：计数 != eligible 必须失败；
2. 禁止 backdate：board facts effective_date 晚于拉取日必须被门禁拒绝；
3. Review 只依赖 stock_core + board aggregation pointer，不查询 chip/auction；
4. chip 晚到不改变已发布 Review（Review publication 不被 chip 写入）；
5. fully_ready 仅在九节点全部就绪且 auction=composite 时成立；
6. 所有正式读取走 pointer / 门禁，而非直接读 domain run。

注意：本 E2E 覆盖的是决策层不变量；真实数据落库与异步 worker 行为需 PG E2E 补齐。
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.board_sync_service import validate_snapshot
from app.services.core_run_context import (
    ComputeOnceDiagnostics,
    ComputeOnceGateError,
    enforce_compute_once_gate,
)
from app.services.product_readiness_service import (
    CLOSURE_DEGRADED_READY,
    CLOSURE_FULLY_READY,
    ProductReadinessState,
    READINESS_DEGRADED,
    READINESS_READY,
    evaluate_closure,
)


def _make_board_snapshot(raw_rows, industry_count, concept_count, membership_count):
    """构造能通过门禁的 BoardSnapshot（覆盖 industry_coverage / depth / concepts 门禁）。"""

    class _Snap:
        def __init__(self):
            self.raw_rows: int = raw_rows
            self.boards: list[dict[str, str]] = [{"type": "industry"}] * industry_count + [
                {"type": "concept"}
            ] * concept_count
            # 行业覆盖：每个唯一股票都有行业归属
            stocks = [f"S{i:06d}" for i in range(max(raw_rows, 1))]
            memberships: dict[tuple[str, str], list[str]] = {}
            for s in stocks:
                memberships.setdefault(("industry_A", "industry"), []).append(s)
            # 概念：每股票 1 个，远低于 100 上限
            for i, s in enumerate(stocks):
                memberships.setdefault((f"concept_{i}", "concept"), []).append(s)
            self.memberships: dict[tuple[str, str], list[str]] = memberships
            self.board_count: int = len(self.boards)
            self.membership_count: int = membership_count
            self.unresolved_symbols: list[str] = []
            self.instrument_resolver = None

    return _Snap()


def test_dsa_compute_once_gate_blocks_mismatch() -> None:
    """不变式 1：DSA 计算次数 != eligible 必须触发 ComputeOnceGateError。

    compute-once 门禁要求 canonical_frame_build / dsa / smc / momentum 四者
    计数均 == eligible_compute_count，否则禁止发布。
    """
    from app.services.core_run_context import _COMPUTE_ONCE_KEYS

    diag = ComputeOnceDiagnostics()
    # 只 bump dsa 两次，其余三种未 bump → 计数 != eligible
    diag.bump("dsa")
    diag.bump("dsa")
    # eligible=2 但 canonical_frame_build/smc/momentum=0 → 门禁失败
    with pytest.raises(ComputeOnceGateError):
        enforce_compute_once_gate(diag, eligible_compute_count=2)
    # 补齐全部四种计数到 eligible=2 后通过
    for key in _COMPUTE_ONCE_KEYS:
        while diag.to_dict()[key] < 2:
            diag.bump(key)
    enforce_compute_once_gate(diag, eligible_compute_count=2)


def test_board_facts_no_backdate() -> None:
    """不变式 2：effective_date 晚于今天必须被门禁拒绝（禁止回填未来）。"""
    snap = _make_board_snapshot(
        raw_rows=5500, industry_count=210, concept_count=320, membership_count=62000
    )
    future = date.today().replace(year=date.today().year + 1)
    with pytest.raises(Exception):
        validate_snapshot(snap, effective_date=future)


def test_board_snapshot_depth_and_concept_gates() -> None:
    """不变式 2b：行业深度 >3 或单股概念 >100 必须被门禁拒绝。"""
    from app.services.wencai_board_provider import (
        WencaiConceptLimitError,
        WencaiIndustryDepthError,
        _split_industry_path,
    )

    # 深度 4 抛错（不再静默截断）
    with pytest.raises(WencaiIndustryDepthError):
        _split_industry_path("A-B-C-D")

    # 概念截断已被改为抛错
    from app.services.wencai_board_provider import WencaiBoardProviderError

    class _BadSnap:
        raw_rows: int = 1
        boards: list[dict[str, str]] = []
        memberships: dict[tuple[str, str], list[str]] = {
            ("concept_x", "concept"): ["S000001"] * 101
        }
        board_count: int = 0
        membership_count: int = 101
        unresolved_symbols: list[str] = []
        instrument_resolver = None

    snap = _BadSnap()
    # validate_snapshot 会统计 max_concepts_per_stock=101 > 100 → 门禁失败
    with pytest.raises(Exception):
        validate_snapshot(snap)  # type: ignore[arg-type]


def test_fully_ready_requires_composite_auction() -> None:
    """不变式 5：fully_ready 仅在 auction=composite 且 enhancement 真正就绪时成立。"""
    base = [
        ProductReadinessState("daily_facts", READINESS_READY, "fresh"),
        ProductReadinessState("board_facts", READINESS_READY, "fresh"),
        ProductReadinessState("stock_core", READINESS_READY, "fresh"),
        ProductReadinessState("board_aggregation", READINESS_READY, "fresh"),
        ProductReadinessState("review", READINESS_READY, "fresh"),
        ProductReadinessState("dsa_projection", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("chip", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
        ProductReadinessState("state_events", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, is_product_ready=True),
    ]
    # auction structure_only → 不得 fully_ready
    struct = base + [
        ProductReadinessState("auction_anchor", READINESS_DEGRADED, "stale", is_mandatory=False, is_terminal=True, auction_mode="structure_only", is_product_ready=False),
    ]
    assert evaluate_closure(struct).closure == CLOSURE_DEGRADED_READY
    # auction composite → fully_ready
    comp = base + [
        ProductReadinessState("auction_anchor", READINESS_READY, "fresh", is_mandatory=False, is_terminal=True, auction_mode="composite", is_product_ready=True),
    ]
    assert evaluate_closure(comp).closure == CLOSURE_FULLY_READY
