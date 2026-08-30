"""PHASE F1C — scope accounting contract + final-status evidence。

核心目的：证明 "declared > persisted" **不再**能伪装成 "all succeeded"。

772 vs 749 的根因是：合法跳过的 23 个 scope 被旧实现计成 succeeded。
本文件用最小 fixture 把这件事钉死 —— 尤其 §4 的 3-scope 负向对照：
旧语义会得到 succeeded=3，新语义必须得到 declared=3 / eligible=2 /
succeeded=2 / skipped=1，**且显式断言 succeeded != 3**。

纯单元测试：不需要 DB（_count_scope_status 的计数逻辑用 fake session 驱动）。
"""
from __future__ import annotations

import inspect
import uuid

import pytest

from app.services import review_orchestrator_service as ros
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    _derive_after_close_final_status,
)

# ===========================================================================
# 常量 / fake 基础设施
# ===========================================================================

SUCCEEDED = ros.ITEM_SUCCEEDED
SKIPPED = ros.ITEM_SKIPPED
FAILED = ros.ITEM_FAILED


class _FakeResult:
    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """仅用于驱动 _count_scope_status 的纯计数逻辑（不发 SQL）。"""

    def __init__(self, rows: list[tuple[str, str, str]]) -> None:
        self._rows = rows

    async def execute(self, _stmt):  # noqa: ANN001
        return _FakeResult(self._rows)


def _rows(*statuses: str) -> list[tuple[str, str, str]]:
    return [(f"type{i}", f"key{i}", st) for i, st in enumerate(statuses)]


# ===========================================================================
# §3 — scope execution metadata contract
# ===========================================================================


def test_scope_execution_metadata_production_shape():
    """生产历史形状：declared=772 / skipped=23 => eligible=749 / ratio=1.0。"""
    meta = ros.build_scope_execution_metadata(
        declared=772, succeeded=749, skipped=23, failed=0,
    )
    assert meta == {
        "declared": 772,
        "eligible": 749,
        "succeeded": 749,
        "skipped": 23,
        "failed": 0,
        "execution_success_ratio": 1.0,
    }


def test_scope_execution_metadata_invariant_declared_equals_sum():
    meta = ros.build_scope_execution_metadata(
        declared=10, succeeded=7, skipped=2, failed=1,
    )
    assert meta["declared"] == (
        meta["succeeded"] + meta["skipped"] + meta["failed"]
    )
    assert meta["eligible"] == 8
    assert meta["execution_success_ratio"] == pytest.approx(7 / 8)


def test_scope_execution_metadata_eligible_zero_is_safe():
    """eligible=0（全部合法跳过）不得除零，按约定为 1.0。"""
    meta = ros.build_scope_execution_metadata(
        declared=3, succeeded=0, skipped=3, failed=0,
    )
    assert meta["eligible"] == 0
    assert meta["execution_success_ratio"] == 1.0


def test_scope_execution_ratio_is_not_all_declared():
    """真实失败必须压低 execution_success_ratio（不得永远 1.0）。"""
    meta = ros.build_scope_execution_metadata(
        declared=3, succeeded=1, skipped=1, failed=1,
    )
    assert meta["execution_success_ratio"] == pytest.approx(0.5)


# ===========================================================================
# §4 — 3-SCOPE NEGATIVE CONTROL（false-green detector）
# ===========================================================================


@pytest.mark.asyncio
async def test_negative_control_3_scopes_skipped_not_counted_as_succeeded():
    """A/B succeeded，C legal skipped。

    正确：declared=3, eligible=2, succeeded=2, skipped=1, failed=0
    旧语义（skipped 并进 succeeded）会得到 succeeded=3 —— 必须 FAIL。
    """
    session = _FakeSession(_rows(SUCCEEDED, SUCCEEDED, SKIPPED))
    succeeded, skipped, failed = await ros._count_scope_status(session, uuid.uuid4())

    assert (succeeded, skipped, failed) == (2, 1, 0)

    meta = ros.build_scope_execution_metadata(
        declared=3, succeeded=succeeded, skipped=skipped, failed=failed,
    )
    assert meta == {
        "declared": 3,
        "eligible": 2,
        "succeeded": 2,
        "skipped": 1,
        "failed": 0,
        "execution_success_ratio": 1.0,
    }

    # ---- FALSE-GREEN DETECTOR ----
    # 旧语义会把 skipped 也算成 succeeded => 3。这里显式断言它不成立，
    # 否则 "declared=3 但只有 2 条 Fact/Composition" 会被伪装成全部成功。
    assert succeeded != 3, "skipped 不得被计入 succeeded（旧 false-green 语义）"
    assert meta["succeeded"] < meta["declared"], "declared > persisted 必须可见"


@pytest.mark.asyncio
async def test_negative_control_all_skipped_is_not_all_succeeded():
    session = _FakeSession(_rows(SKIPPED, SKIPPED, SKIPPED))
    succeeded, skipped, failed = await ros._count_scope_status(session, uuid.uuid4())
    assert (succeeded, skipped, failed) == (0, 3, 0)
    assert succeeded != 3


# ===========================================================================
# §5 — RESUME CONTRACT
# ===========================================================================


@pytest.mark.asyncio
async def test_resume_recount_keeps_three_states():
    """resume 重算后不得把 skipped 重新并回 succeeded（硬 Gate）。"""
    session = _FakeSession(_rows(SUCCEEDED, SUCCEEDED, SKIPPED, FAILED))
    succeeded, skipped, failed = await ros._count_scope_status(session, uuid.uuid4())
    assert (succeeded, skipped, failed) == (2, 1, 1)

    meta = ros.build_scope_execution_metadata(
        declared=4, succeeded=succeeded, skipped=skipped, failed=failed,
    )
    assert meta["declared"] == 4
    assert meta["succeeded"] == 2, "resume 后不得变回 3（skipped 被误并）"
    assert meta["skipped"] == 1
    assert meta["failed"] == 1


@pytest.mark.asyncio
async def test_count_scope_status_ignores_non_metrics_state():
    """未知状态不得被静默计成 succeeded。"""
    session = _FakeSession(_rows("pending", "running", SUCCEEDED))
    succeeded, skipped, failed = await ros._count_scope_status(session, uuid.uuid4())
    assert (succeeded, skipped, failed) == (1, 0, 0)


# ===========================================================================
# §7 — OBSERVED-23 EVIDENCE（历史只读取证固化为 audit fixture）
# ===========================================================================

# 仅作为 audit evidence：不是业务 SSOT。业务判定继续由正式 policy owner 决定
# （如 is_scope_observation_persistence_excluded）。
OBSERVED_SKIP_MATRIX = {
    "market": 1,
    "major_index": 2,
    "style": 2,
    "concept_name_exclusion": 13,
    "concept_member_le_10": 5,
}


def test_observed_skip_matrix_totals_23():
    assert sum(OBSERVED_SKIP_MATRIX.values()) == 23
    assert OBSERVED_SKIP_MATRIX["concept_name_exclusion"] == 13
    assert OBSERVED_SKIP_MATRIX["concept_member_le_10"] == 5
    assert OBSERVED_SKIP_MATRIX["market"] == 1
    assert OBSERVED_SKIP_MATRIX["major_index"] == 2
    assert OBSERVED_SKIP_MATRIX["style"] == 2


def test_observed_23_reconciles_with_production_counts():
    """772 - 23 = 749 == succeeded == Fact == Composition。"""
    declared, skipped = 772, sum(OBSERVED_SKIP_MATRIX.values())
    assert declared - skipped == 749
    meta = ros.build_scope_execution_metadata(
        declared=declared, succeeded=749, skipped=skipped, failed=0,
    )
    assert meta["eligible"] == 749
    assert meta["execution_success_ratio"] == 1.0


# ===========================================================================
# §8 — FINAL STATUS CONTRACT
# ===========================================================================


def _step(name: str, status: str, optional: bool) -> dict:
    return {"status": status, "optional": optional}


def test_final_status_case_a_all_current_success():
    status, optional_failures = _derive_after_close_final_status(
        {
            "refreshing_daily": _step("refreshing_daily", "succeeded", False),
            "computing_features": _step("computing_features", "succeeded", False),
            "computing_review": _step("computing_review", "succeeded", True),
        }
    )
    assert status == AfterCloseRunStatus.SUCCEEDED
    assert optional_failures == []


def test_final_status_case_b_optional_syncing_boards_failure():
    """生产 08-27 / 08-28 的真实形状：stock_core 缺失 + syncing_boards failed。"""
    status, optional_failures = _derive_after_close_final_status(
        {
            "refreshing_daily": _step("refreshing_daily", "succeeded", False),
            "syncing_boards": _step("syncing_boards", "failed", True),
            "checking_coverage": _step("checking_coverage", "succeeded", False),
            "computing_features": _step("computing_features", "succeeded", False),
        }
    )
    assert status == AfterCloseRunStatus.PARTIAL_SUCCESS
    assert optional_failures == ["syncing_boards"]


def test_final_status_case_c_mandatory_failure():
    status, optional_failures = _derive_after_close_final_status(
        {
            "refreshing_daily": _step("refreshing_daily", "failed", False),
            "syncing_boards": _step("syncing_boards", "failed", True),
        }
    )
    assert status == AfterCloseRunStatus.FAILED
    assert optional_failures == ["syncing_boards"]


def test_final_status_has_no_stock_core_input():
    """结构性断言：stock_core 对终态完全没有输入权。

    只断言**签名与行为**（不扫源码注释 —— 注释里说明"已排除 stock_core"
    不代表代码仍依赖它，扫描注释既脆弱又无意义）。
    """
    sig = inspect.signature(_derive_after_close_final_status)
    assert list(sig.parameters) == ["step_summary"], (
        "终态 owner 的输入只能是 step_summary，无法传入任何 stock_core 信号"
    )


def test_final_status_ignores_stock_core_shaped_keys():
    """行为断言：即使 step_summary 混入 stock_core 形状的条目，也不改变终态。

    这是"stock_core 无输入权"的真正证明 —— 旧实现的降级因子不是函数参数，
    而是来自 retired 内部状态；现在任何 stock_core 痕迹都只是普通 step 条目，
    且其 optional 标记决定行为，而非"叫 stock_core"这件事本身。
    """
    base = {
        "refreshing_daily": _step("refreshing_daily", "succeeded", False),
        "computing_features": _step("computing_features", "succeeded", False),
    }
    # 混入 mandatory 形状的 stock_core 条目但状态 succeeded => 仍 succeeded
    with_core_ok = {**base, "stock_core_published": _step("x", "succeeded", False)}
    assert _derive_after_close_final_status(with_core_ok)[0] == AfterCloseRunStatus.SUCCEEDED

    # 关键：stock_core 条目**缺失**时也不得降级（历史 08-27/08-28 的真实形状）
    assert _derive_after_close_final_status(base)[0] == AfterCloseRunStatus.SUCCEEDED


def test_final_status_stock_core_absent_does_not_downgrade():
    """§9：stock_core 缺失本身不降级；只有当前 workflow 信号决定。"""
    summary = {
        "refreshing_daily": _step("refreshing_daily", "succeeded", False),
        "computing_features": _step("computing_features", "succeeded", False),
    }
    # 注意：summary 中**没有**任何 stock_core 步骤 —— 仍然 succeeded
    status, _ = _derive_after_close_final_status(summary)
    assert status == AfterCloseRunStatus.SUCCEEDED


# ===========================================================================
# §10 — RECONCILE REGRESSION
# ===========================================================================


def _code_without_comments(func) -> str:
    """去掉 # 注释后的代码体（避免断言被说明性注释误导）。"""
    lines = []
    for line in inspect.getsource(func).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # 去行尾注释（保守：只在 # 前存在空格时切）
        cut = line.find(" #")
        if cut != -1:
            line = line[:cut]
        lines.append(line)
    return "\n".join(lines)


def test_reconcile_no_stock_core_contradiction_authority():
    """stock_core artifact 可保留供审计，但不得构成 contradiction。

    断言的是**去掉注释后的实际代码**：不再 append 这两个 contradiction 常量。
    """
    from app.services.after_close_orchestrator import reconcile_after_close_run

    code = _code_without_comments(reconcile_after_close_run).lower()
    assert "run_succeeded_but_no_stock_core_publication" not in code
    assert "stock_core_published_but_run_not_succeeded" not in code
    # 审计字段仍然存在（只是不再有 contradiction authority）
    assert "reconcile_artifacts" in code
    assert 'meta["reconcile_contradictions"]' in code or "reconcile_contradictions" in code
