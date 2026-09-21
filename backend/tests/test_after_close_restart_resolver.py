# ===========================================================================
# REPROCESS-OWNER-CLOSURE-01 CORRECTION-01 — stage-resolution 纯 unit 契约
#
# 这些测试直接调用 production resolver
# app.services.after_close_orchestrator._resolve_execution_completed_steps，
# 不依赖 PostgreSQL、不复制 stage list / skip 算法。
#
# - Contract B: daily_ready (mainchain_stage=syncing_boards) 跳过 refreshing_daily，
#   其余 mainchain 阶段均 runnable。
# - Contract C: 正常 initial run (mainchain_stage=None) 无预跳过，refreshing_daily runnable。
# - Contract D: 普通 resume (last_completed_step=publishing) 保持旧 resume semantics，
#   History/Review 不被预跳过。
# - Fail-closed: 非法 mainchain_stage 显式 ValueError，不得退化为 full run。
# ===========================================================================

import pytest

from app.services.after_close_orchestrator import (
    _resolve_execution_completed_steps,
)


def test_contract_b_daily_ready_skips_refreshing_daily_reaches_mainchain() -> None:
    """Contract B — daily_ready restart (mainchain_stage=syncing_boards)：
    refreshing_daily 跳过（在 completed 中）；
    syncing_boards / computing_features / publishing / computing_history /
    computing_review 均为 runnable（不在 completed 中）。
    """
    completed = _resolve_execution_completed_steps(None, "syncing_boards")

    assert "refreshing_daily" in completed, "daily_ready 必须跳过 refreshing_daily"
    assert "syncing_boards" not in completed
    assert "computing_features" not in completed
    assert "publishing" not in completed
    assert "computing_history" not in completed
    assert "computing_review" not in completed


def test_contract_c_normal_initial_run_no_pre_skipped() -> None:
    """Contract C — 正常 initial run (mainchain_stage=None)：
    无预跳过 stage；refreshing_daily 仍 runnable。
    """
    completed = _resolve_execution_completed_steps(None, None)

    assert completed == set(), "正常 initial run 不得预跳过任何 stage"
    assert "refreshing_daily" not in completed, "正常 run 必须执行 refreshing_daily"


def test_contract_d_ordinary_resume_keeps_history_review_runnable() -> None:
    """Contract D — 普通 resume (last_completed_step=publishing)：
    保持旧 resume semantics：refreshing_daily / syncing_boards / computing_features /
    publishing 已完成；computing_history / computing_review 仍 runnable。
    """
    completed = _resolve_execution_completed_steps("publishing", None)

    assert "refreshing_daily" in completed
    assert "syncing_boards" in completed
    assert "computing_features" in completed
    assert "publishing" in completed
    # 旧 resume 语义：History/Review 不被 last_completed_step="publishing" 预跳过
    assert "computing_history" not in completed
    assert "computing_review" not in completed


def test_contract_fail_closed_invalid_mainchain_stage() -> None:
    """非法 mainchain_stage 必须 fail closed：显式 ValueError，
    禁止静默退化为 full run。
    """
    with pytest.raises(ValueError):
        _resolve_execution_completed_steps(None, "not_a_real_stage")

    # full run 不变量：refreshing_daily 在 full run 下必须 runnable（不在 completed）。
    completed_full = _resolve_execution_completed_steps(None, None)
    assert "refreshing_daily" not in completed_full


# ===========================================================================
# [PHASE-A Core→Review Source Closure] 真实 DAG = features → review → history
# 以下纯 unit 契约直接调用 production resolver，验证断点恢复映射与真实 DAG 对齐
# （KPI-A1/A2/A3）。禁止在 test 中复制 stage list / skip 算法。
# ===========================================================================

def test_legacy_completed_review_maps_to_features_only() -> None:
    """[REVIEW-V2-R1] legacy last_completed_step="computing_review" 只读兼容：
    历史语义至多说明 computing_features 已完成；retired token **自身**不得回到
    completed set，也不得隐含 computing_history（否则 History 永不 retry）。

    严禁包含 computing_history（否则 resume 会误判 skip_history=True，History 永不
    retry，违反 KPI-A2/A4）；也不含 legacy publishing（token 不得污染当前语义）。
    """
    completed = _resolve_execution_completed_steps("computing_review", None)

    assert "refreshing_daily" in completed
    assert "syncing_boards" in completed
    assert "computing_features" in completed
    assert "computing_review" not in completed, "retired computing_review token 不得回到 completed set"
    # 核心不变量：History 不被预跳过。
    assert "computing_history" not in completed, "completed(computing_review) 不得含 computing_history"
    # legacy publishing token 不得污染当前语义。
    assert "publishing" not in completed, "completed(computing_review) 不得含 legacy publishing"


def test_completed_history_includes_features_not_review() -> None:
    """current DAG = features → history（Review 已退役）：
    History 完成即 features+history 后置链整体完成；retired computing_review
    不得出现在 completed 集合里。
    """
    completed = _resolve_execution_completed_steps("computing_history", None)

    assert "refreshing_daily" in completed
    assert "syncing_boards" in completed
    assert "computing_features" in completed
    assert "computing_history" in completed
    assert "computing_review" not in completed, "retired computing_review 不得出现在 completed"
    # legacy publishing token 不得污染当前语义。
    assert "publishing" not in completed, "completed(computing_history) 不得含 legacy publishing"


def test_mainchain_stage_computing_review_is_retired_fail_closed() -> None:
    """[REVIEW-V2-R1] computing_review 已退役，不再是合法 mainchain_stage：
    作为 restart 起点必须 fail-closed（显式 ValueError），不得静默退化为 full run。
    """
    with pytest.raises(ValueError):
        _resolve_execution_completed_steps(None, "computing_review")


def test_mainchain_stage_history_preskips_pre_stages() -> None:
    """restart 正式起点 mainchain_stage=computing_history：其之前阶段
    （refreshing_daily / syncing_boards / computing_features）合并为已完成；
    history 自身（起点）仍 runnable；retired computing_review / legacy publishing 均不参与。
    """
    completed = _resolve_execution_completed_steps(None, "computing_history")

    assert "refreshing_daily" in completed
    assert "syncing_boards" in completed
    assert "computing_features" in completed
    assert "computing_history" not in completed
    assert "computing_review" not in completed
    assert "publishing" not in completed


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
