"""after_close_pipeline_service 纯函数单测（从 incident 模块归并，去重后保留）。

仅覆盖真实回归点：
- resolve_failed_step（失败步骤定位）
- _compute_watchlist_reason（自选可用原因）

chunking 回归由 test_eod_daily_refresh_service 覆盖，不在此重复。
"""
from types import SimpleNamespace

from app.services import after_close_pipeline_service as pipeline_mod


def test_resolve_failed_step_prefers_latest_failed_step():
    steps = {
        "refreshing_daily": {"status": "succeeded"},
        "computing_features": {"status": "failed"},
        "computing_review": {"status": "failed"},
    }
    assert pipeline_mod.resolve_failed_step(steps) == "computing_review"


def test_resolve_failed_step_returns_none_when_all_succeeded():
    steps = {
        "refreshing_daily": {"status": "succeeded"},
        "computing_features": {"status": "succeeded"},
        "computing_review": {"status": "succeeded"},
    }
    assert pipeline_mod.resolve_failed_step(steps) is None


def test_resolve_failed_step_returns_none_when_all_completed():
    steps = {
        "refreshing_daily": {"status": "completed"},
        "computing_features": {"status": "completed"},
        "computing_review": {"status": "completed"},
    }
    assert pipeline_mod.resolve_failed_step(steps) is None


def test_resolve_failed_step_returns_none_on_empty_step_summary():
    assert pipeline_mod.resolve_failed_step({}) is None


def test_resolve_failed_step_returns_none_when_step_missing_status():
    steps = {"refreshing_daily": {"foo": "bar"}, "computing_features": {}}
    assert pipeline_mod.resolve_failed_step(steps) is None


def test_compute_watchlist_reason_when_watchlist_ready():
    reason = pipeline_mod._compute_watchlist_reason(
        watchlist_ready=True, job_run=None, snapshot_summary=None,
        has_backfill_full=False, failed_step=None,
    )
    assert reason == "after_close 已 succeeded，feature_snapshot full/published，自选股可读"


def test_compute_watchlist_reason_when_not_ready_and_after_close_succeeded():
    reason = pipeline_mod._compute_watchlist_reason(
        watchlist_ready=False,
        job_run=SimpleNamespace(status="succeeded"),
        snapshot_summary=None, has_backfill_full=False, failed_step=None,
    )
    assert reason == "after_close 已完成，但未找到 feature_snapshot_run 记录"


def test_compute_watchlist_reason_when_not_ready_and_backfill_full_succeeded():
    reason = pipeline_mod._compute_watchlist_reason(
        watchlist_ready=False,
        job_run=SimpleNamespace(status="failed"),
        snapshot_summary=None, has_backfill_full=True, failed_step=None,
    )
    assert reason == "after_close 状态为 failed"
