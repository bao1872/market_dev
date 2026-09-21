"""[CHANGE-20260831-ADMIN-TIMELINE] 盘后流水线 steps[] 聚合的 current canonical 合同测试。

行为级验证：直接调用 _compute_step_states 真实聚合函数，不是 grep 源码字符串。

覆盖：
1. current/default pipeline 不合成 publishing
2. current canonical 顺序：
   computing_features < computing_review < computing_history < watchlist_ready
3. 历史 legacy run 的真实 publishing 事件不被吞掉
4. 无真实 publishing 事件时绝不合成
5. NON-GOAL 保护：AfterCloseRunStatus.PUBLISHING 枚举未被删除

测试策略：
- 纯单元：in-memory SchedulerJobRun / JobRunEvent，不连数据库；
- 可在 PURE_UNIT_TEST=1 下运行。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.after_close_orchestrator import AfterCloseRunStatus
from app.services.after_close_pipeline_service import (
    _LEGACY_EVENT_STEPS,
    _PIPELINE_STEPS,
    _compute_step_states,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TEST_DATE_STR = "2026-06-24"
_BASE = datetime(2026, 6, 24, 15, 0, 0, tzinfo=SHANGHAI)


def _make_job_run(
    status: str,
    orchestrator_status: str | None = None,
    last_completed_step: str | None = None,
) -> SchedulerJobRun:
    """构造 in-memory after_close_orchestrator job_run（不落库）。"""
    meta: dict[str, object] = {
        "orchestrator_status": orchestrator_status,
        "trade_date": TEST_DATE_STR,
    }
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    return SchedulerJobRun(
        id=uuid.uuid4(),
        job_name="after_close_orchestrator",
        business_date=TEST_DATE_STR,
        run_key=f"after_close_orchestrator:{TEST_DATE_STR}",
        status=status,
        metadata_json=json.dumps(meta),
    )


def _event(idx: int, step: str, level: str = "info") -> JobRunEvent:
    """构造 in-memory JobRunEvent（不落库），按 idx 递增时间戳。"""
    return JobRunEvent(
        job_run_id=uuid.uuid4(),
        step=step,
        level=level,
        message=f"{step} event",
        payload={},
        created_at=_BASE + timedelta(minutes=idx),
    )


def _step_names(steps: list[dict]) -> list[str]:
    return [s["step"] for s in steps]


# ==================== 1. current/default 不合成 publishing ====================

def test_current_default_has_no_publishing() -> None:
    """无 run（default 兜底）时，steps 不含 publishing。"""
    steps = _compute_step_states(None, [], watchlist_ready=False)
    names = _step_names(steps)
    assert "publishing" not in names, f"current default 不得包含 publishing，实际: {names}"
    assert names == list(_PIPELINE_STEPS)


def test_current_running_run_has_no_publishing() -> None:
    """当前运行中的 run 也不合成 publishing。"""
    job_run = _make_job_run("running", orchestrator_status="computing_review")
    events = [
        _event(0, "refreshing_daily"),
        _event(1, "syncing_boards"),
        _event(2, "checking_coverage"),
        _event(3, "computing_features"),
        _event(4, "computing_review"),
    ]
    steps = _compute_step_states(job_run, events, watchlist_ready=False)
    names = _step_names(steps)
    assert "publishing" not in names, f"current run 不得合成 publishing，实际: {names}"


# ==================== 2. current canonical 顺序 ====================

def test_current_canonical_order() -> None:
    """[REVIEW-V2-R1] current canonical 7 步顺序（旧 computing_review 已退役）。

    冻结顺序：
        refreshing_daily → syncing_boards → checking_coverage
        → rebuilding_market_dashboard（复盘计算）→ computing_features
        → computing_history → watchlist_ready
    """
    steps = _compute_step_states(None, [], watchlist_ready=False)
    names = _step_names(steps)

    # 旧 computing_review 必须彻底移出 current pipeline
    assert "computing_review" not in names, (
        f"computing_review 已退役，不得出现在 current pipeline，实际: {names}"
    )
    # 新 canonical 复盘计算必须在位
    assert "rebuilding_market_dashboard" in names, (
        f"current pipeline 必须包含 rebuilding_market_dashboard，实际: {names}"
    )

    dash = names.index("rebuilding_market_dashboard")
    feat = names.index("computing_features")
    hist = names.index("computing_history")
    wl = names.index("watchlist_ready")
    assert dash < feat < hist < wl, (
        "顺序必须为 rebuilding_market_dashboard < computing_features "
        f"< computing_history < watchlist_ready，实际: "
        f"dash={dash}, feat={feat}, hist={hist}, wl={wl} ({names})"
    )
    assert len(names) == 7, f"current canonical 应为 7 步，实际: {len(names)}"


# ==================== 3/4. legacy 真实事件保留 vs 绝不合成 ====================

def test_legacy_real_publishing_event_preserved() -> None:
    """历史 run 真实产生过 publishing 事件 → 必须呈现，且位置在 computing_features 之后。"""
    job_run = _make_job_run("succeeded", last_completed_step="computing_history")
    events = [
        _event(0, "refreshing_daily"),
        _event(1, "syncing_boards"),
        _event(2, "checking_coverage"),
        _event(3, "computing_features"),
        _event(4, "publishing"),  # legacy 真实事件
        _event(5, "computing_review"),
        _event(6, "computing_history"),
    ]
    steps = _compute_step_states(job_run, events, watchlist_ready=True)
    names = _step_names(steps)
    assert "publishing" in names, f"历史真实 publishing 事件不得被吞掉，实际: {names}"
    assert names.index("publishing") == names.index("computing_features") + 1, (
        f"publishing 应位于 computing_features 之后，实际: {names}"
    )
    pub = next(s for s in steps if s["step"] == "publishing")
    assert pub["started_at"] is not None, "真实 publishing 事件应保留 started_at"
    assert pub["status"] == "completed"


def test_no_synthetic_publishing_without_real_event() -> None:
    """没有真实 publishing 事件时绝不合成（含 legacy last_completed_step 场景）。"""
    job_run = _make_job_run("succeeded", last_completed_step="publishing")
    events = [
        _event(0, "refreshing_daily"),
        _event(1, "computing_features"),
        _event(2, "computing_review"),
        _event(3, "computing_history"),
    ]
    steps = _compute_step_states(job_run, events, watchlist_ready=True)
    names = _step_names(steps)
    assert "publishing" not in names, f"无真实事件时不得合成 publishing，实际: {names}"


def test_legacy_last_completed_step_publishing_keeps_core_progress() -> None:
    """legacy last_completed_step=publishing 的历史 run：核心步骤仍显示已完成。"""
    job_run = _make_job_run("running", last_completed_step="publishing")
    steps = _compute_step_states(job_run, [], watchlist_ready=False)
    by_name = {s["step"]: s for s in steps}
    assert by_name["computing_features"]["status"] == "completed", (
        "legacy publishing token 应保持 computing_features 已完成"
    )
    # [REVIEW-V2-R1] computing_review 已退役，不属于 current pipeline，不得再被索引
    assert "computing_review" not in by_name, (
        f"computing_review 不得出现在 current steps，实际: {sorted(by_name)}"
    )
    assert by_name["computing_history"]["status"] == "pending"
    # rebuilding_market_dashboard 是 optional 非 checkpoint：
    # 当前 attempt 无真实 summary/event 时，按现有 fallback 合同判断（不得伪造为
    # 「durable checkpoint 已完成」以外的语义），此处仅锁定它仍属于 current steps。
    assert "rebuilding_market_dashboard" in by_name


# ==================== 5. NON-GOAL 保护 ====================

def test_orchestrator_publishing_enum_preserved() -> None:
    """NON-GOAL：AfterCloseRunStatus.PUBLISHING 未被删除（orchestrator 业务 DAG 未改）。"""
    assert AfterCloseRunStatus.PUBLISHING.value == "publishing"
    # legacy 事件识别白名单保留 publishing，避免历史真实事件被吞掉
    assert "publishing" in _LEGACY_EVENT_STEPS
