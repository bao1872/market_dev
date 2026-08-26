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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
