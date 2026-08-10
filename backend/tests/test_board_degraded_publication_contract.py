"""Board degraded publication contract 纯单元测试（Phase 4D.3, 2026-08-09）。

覆盖 PRD 决策：
- PRD 30 BA-01B：Board Analysis V1 product scope = industry + concept
- PRD 30 BA-02B：batch status 语义 succeeded / partial / failed（禁 blocked_external_population）
- PRD 31 PC-42：DEGRADED PUBLISHABLE CONTRACT（A-H）
- PC-40 / PC-41：Review 仍只消费正式 pointer，且 source_board_run_id == pointer.data_run_id

本测试为纯单元测试（PURE_UNIT_TEST），不连接数据库。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import Mock

import pytest

from app.services.board_analysis_service import _evaluate_degraded_publishable

pytestmark = pytest.mark.pure_unit

_TRADE_DATE = date(2026, 8, 4)


def _snapshot(
    *,
    status: str = "partial",
    coverage_ratio: float | None = 0.80,
    eligible_count: int | None = 100,
    ready_count: int | None = 80,
    missing_count: int | None = 20,
) -> Mock:
    snap = Mock()
    snap.status = status
    snap.coverage_ratio = coverage_ratio
    snap.eligible_count = eligible_count
    snap.ready_count = ready_count
    snap.missing_count = missing_count
    return snap


def _detail(board_id: str, snapshot: Mock | None, *, status: str) -> dict:
    # BOARD-MEMORY-01：details 改用 lightweight publication_input（scalar 描述符）。
    return {"board_id": board_id, "status": status, "publication_input": snapshot}


def _ready_details(n: int) -> list[dict]:
    return [
        _detail(f"ok-{i}", _snapshot(status="succeeded", coverage_ratio=1.0),
                status="succeeded")
        for i in range(n)
    ]


def _kwargs(**overrides):
    base = {
        "status": "partial",
        "formal_batch": True,
        "expected_count": 646,
        "computed_boards": 646,
        "execution_failed": 0,
        "not_computed": 0,
        "details": [
            *_ready_details(610),
            *[_detail(f"p-{i}", _snapshot(), status="partial") for i in range(36)],
        ],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# §20 Tests — Publication
# ---------------------------------------------------------------------------

def test_pub_a_succeeded_run_is_publishable() -> None:
    """A. succeeded run → pointer publish。"""
    ok, reason = _evaluate_degraded_publishable(**_kwargs(status="succeeded"))
    assert ok is True
    assert reason == "succeeded"


def test_pub_b_partial_degradation_only_is_publishable() -> None:
    """B. partial + degradation-only + 无 execution failure → pointer publish。

    对应 08-04 真实结构：expected=646 / ready=610 / partial=36 / execution_failed=0。
    """
    ok, reason = _evaluate_degraded_publishable(**_kwargs())
    assert ok is True
    assert reason == "degradation_only"


def test_pub_c_failed_run_is_not_publishable() -> None:
    """C. failed run → no pointer。"""
    ok, reason = _evaluate_degraded_publishable(**_kwargs(status="failed"))
    assert ok is False
    assert "failed" in reason


@pytest.mark.parametrize("status", ["pending", "running"])
def test_pub_d_non_terminal_is_not_publishable(status: str) -> None:
    """D. pending / running → no pointer（PC-42 条件 A：必须 terminal）。"""
    ok, reason = _evaluate_degraded_publishable(**_kwargs(status=status))
    assert ok is False
    assert status in reason


def test_pub_e_partial_with_execution_failure_is_not_publishable() -> None:
    """E. partial + execution failure → no pointer（PC-42 条件 C/D/E）。"""
    ok, reason = _evaluate_degraded_publishable(
        **_kwargs(execution_failed=3)
    )
    assert ok is False
    assert "execution_failed=3" in reason


def test_pub_e_partial_with_unknown_snapshot_status_is_not_publishable() -> None:
    """E. partial 原因为 UNKNOWN → no pointer（PC-42 条件 F/H，fail closed）。"""
    details = [
        *_ready_details(645),
        _detail("weird", _snapshot(status="mystery_state"), status="mystery_state"),
    ]
    ok, reason = _evaluate_degraded_publishable(**_kwargs(details=details))
    assert ok is False
    assert "unknown snapshot status" in reason


def test_pub_missing_snapshot_evidence_is_not_publishable() -> None:
    """PC-42 条件 G：partial board 缺 snapshot/evidence → 不可发布。"""
    details = [*_ready_details(645), _detail("nosnap", None, status="partial")]
    ok, reason = _evaluate_degraded_publishable(**_kwargs(details=details))
    assert ok is False
    assert "missing" in reason and "publication_input" in reason


@pytest.mark.parametrize(
    "field", ["coverage_ratio", "eligible_count", "ready_count", "missing_count"],
)
def test_pub_partial_board_requires_real_degradation_metrics(field: str) -> None:
    """PC-42 条件 G：每个 partial board 都必须有真实 coverage/eligible/ready/missing。"""
    snap = _snapshot()
    setattr(snap, field, None)
    details = [*_ready_details(645), _detail("p-0", snap, status="partial")]
    ok, reason = _evaluate_degraded_publishable(**_kwargs(details=details))
    assert ok is False
    assert field in reason


def test_pub_not_all_in_scope_boards_computed_is_not_publishable() -> None:
    """PC-42 条件 B：并非所有 in-scope board 都完成计算并持久化 snapshot。"""
    ok, reason = _evaluate_degraded_publishable(
        **_kwargs(computed_boards=640, not_computed=6)
    )
    assert ok is False
    assert "not_computed=6" in reason


def test_pub_filtered_batch_is_never_publishable() -> None:
    """filtered/limited 请求不是正式 batch，永远不可发布正式 pointer。"""
    ok, reason = _evaluate_degraded_publishable(**_kwargs(formal_batch=False))
    assert ok is False
    assert "formal batch" in reason


def test_pub_empty_scope_is_not_publishable() -> None:
    """没有任何 in-scope board 时不得发布。"""
    ok, reason = _evaluate_degraded_publishable(
        **_kwargs(expected_count=0, computed_boards=0, details=[])
    )
    assert ok is False
    assert "no in-scope board" in reason
