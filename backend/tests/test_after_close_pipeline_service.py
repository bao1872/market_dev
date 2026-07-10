"""after_close_pipeline_service 聚合逻辑单元测试。

覆盖：
- 5 阶段映射（market_prep/dsa_compute/quality_gate/feature_snapshot/publishing）；
- 运行中步骤 finished_at=None 且耗时=now-started（不为负/0 伪装结束）；
- 阶段结束时间由下一阶段开始推导，负耗时归零（向后兼容老数据）；
- 虚拟状态 checking_coverage/creating_dsa 归并到 market_prep；
- feature_snapshot 疑似停滞判定；
- 进度回调每阈值写一次事件且与 metadata 同次 commit（无 DB 落盘验证）。
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import after_close_orchestrator
from app.services.after_close_pipeline_service import (
    _COMPLETED_PHASE_INDEX,
    _PHASE_KEYS,
    _PHASE_REP_FOR_STATUS,
    AfterCloseRunStatus,
    _compute_feature_snapshot_stalled,
    _compute_step_states,
)

_SH = ZoneInfo("Asia/Shanghai")


class FakeEvent:
    """最小 JobRunEvent 替身，仅暴露聚合所需字段。"""

    def __init__(self, step, created_at, level="info", payload=None, message=""):
        self.step = step
        self.created_at = created_at
        self.level = level
        self.payload = payload or {}
        self.message = message


class FakeJobRun:
    """最小 SchedulerJobRun 替身，仅暴露步骤状态所需字段。"""

    def __init__(self, status, meta):
        self.status = status
        self.metadata_json = json.dumps(meta)
        self.heartbeat_at = None


def _ts(micro):
    return datetime(2026, 7, 10, 16, 44, 9, micro, tzinfo=_SH)


# ---------------------------------------------------------------------------
# 1. 5 阶段映射：running 时 feature_snapshot 运行中，前置 completed
# ---------------------------------------------------------------------------
def test_5phase_mapping_running():
    start = _ts(0)
    now = start + timedelta(seconds=100)
    job_run = FakeJobRun(
        "running",
        {
            "orchestrator_status": AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
            "last_completed_step": AfterCloseRunStatus.QUALITY_GATE.value,
        },
    )
    events = [FakeEvent(AfterCloseRunStatus.FEATURE_SNAPSHOT.value, start)]
    steps = _compute_step_states(job_run, events, False, None, now=now)
    assert len(steps) == 5
    assert {s["step"] for s in steps} == set(_PHASE_KEYS)
    by_key = {s["step"]: s for s in steps}
    assert by_key["market_prep"]["status"] == "completed"
    assert by_key["dsa_compute"]["status"] == "completed"
    assert by_key["quality_gate"]["status"] == "completed"
    assert by_key["feature_snapshot"]["status"] == "running"
    assert by_key["publishing"]["status"] == "pending"
    # 运行中：finished_at=None，耗时=now-started
    fs = by_key["feature_snapshot"]
    assert fs["finished_at"] is None
    assert fs["duration_seconds"] == 100.0


# ---------------------------------------------------------------------------
# 2. 5 阶段映射：succeeded 时全部 completed
# ---------------------------------------------------------------------------
def test_5phase_mapping_succeeded():
    job_run = FakeJobRun(
        "succeeded",
        {
            "orchestrator_status": AfterCloseRunStatus.SUCCEEDED.value,
            "last_completed_step": AfterCloseRunStatus.SUCCEEDED.value,
        },
    )
    steps = _compute_step_states(job_run, [], False, None, now=_ts(0))
    assert len(steps) == 5
    assert all(s["status"] == "completed" for s in steps)


# ---------------------------------------------------------------------------
# 3. 阶段结束时间由下一阶段开始推导；负耗时归零（向后兼容老数据）
# ---------------------------------------------------------------------------
def test_negative_duration_clamped_to_zero():
    # market_prep 开始 t2，dsa_compute 开始 t1（t1 < t2，模拟时钟/数据异常）
    t1 = _ts(100_000)
    t2 = _ts(900_000)
    now = t2 + timedelta(seconds=10)
    job_run = FakeJobRun(
        "running",
        {
            "orchestrator_status": AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
            "last_completed_step": AfterCloseRunStatus.QUALITY_GATE.value,
        },
    )
    events = [
        FakeEvent(AfterCloseRunStatus.REFRESHING_DAILY.value, t2),
        FakeEvent(AfterCloseRunStatus.WAITING_DSA_WORKER.value, t1),
    ]
    steps = _compute_step_states(job_run, events, False, None, now=now)
    by_key = {s["step"]: s for s in steps}
    # market_prep 结束时间 = dsa_compute 开始(t1) < 开始(t2) → 耗时归零，不为负
    assert by_key["market_prep"]["duration_seconds"] == 0.0
    assert by_key["market_prep"]["finished_at"] is not None


# ---------------------------------------------------------------------------
# 4. 虚拟状态 checking_coverage/creating_dsa 归并到 market_prep
# ---------------------------------------------------------------------------
def test_virtual_steps_merged_into_market_prep():
    assert (
        _PHASE_REP_FOR_STATUS[AfterCloseRunStatus.CHECKING_COVERAGE.value]
        == AfterCloseRunStatus.REFRESHING_DAILY.value
    )
    assert (
        _PHASE_REP_FOR_STATUS[AfterCloseRunStatus.CREATING_DSA.value]
        == AfterCloseRunStatus.REFRESHING_DAILY.value
    )
    # 真实执行断点步骤应存在
    assert _COMPLETED_PHASE_INDEX[AfterCloseRunStatus.QUALITY_GATE.value] == 2
    assert _COMPLETED_PHASE_INDEX[AfterCloseRunStatus.FEATURE_SNAPSHOT.value] == 3


# ---------------------------------------------------------------------------
# 5. feature_snapshot 疑似停滞判定
# ---------------------------------------------------------------------------
def test_feature_snapshot_stalled_true_when_progress_stale():
    now = datetime(2026, 7, 10, 19, 57, 45, tzinfo=_SH)
    old_progress = now - timedelta(seconds=400)
    job_run = FakeJobRun(
        "running",
        {
            "orchestrator_status": AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
            "feature_snapshot_progress": {
                "processed": 3600,
                "total": 5293,
                "updated_at": old_progress.isoformat(),
            },
        },
    )
    meta = json.loads(job_run.metadata_json)
    assert _compute_feature_snapshot_stalled(job_run, meta, now) is True


def test_feature_snapshot_stalled_false_when_progress_fresh():
    now = datetime(2026, 7, 10, 19, 57, 45, tzinfo=_SH)
    fresh_progress = now - timedelta(seconds=30)
    job_run = FakeJobRun(
        "running",
        {
            "orchestrator_status": AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
            "feature_snapshot_progress": {
                "processed": 3600,
                "total": 5293,
                "updated_at": fresh_progress.isoformat(),
            },
        },
    )
    meta = json.loads(job_run.metadata_json)
    assert _compute_feature_snapshot_stalled(job_run, meta, now) is False


def test_feature_snapshot_stalled_false_when_not_snapshot_step():
    now = datetime(2026, 7, 10, 19, 57, 45, tzinfo=_SH)
    old_progress = now - timedelta(seconds=400)
    job_run = FakeJobRun(
        "running",
        {
            "orchestrator_status": AfterCloseRunStatus.PUBLISHING.value,
            "feature_snapshot_progress": {
                "processed": 3600,
                "total": 5293,
                "updated_at": old_progress.isoformat(),
            },
        },
    )
    meta = json.loads(job_run.metadata_json)
    assert _compute_feature_snapshot_stalled(job_run, meta, now) is False


# ---------------------------------------------------------------------------
# 6. 进度回调：每阈值写一次事件，且事件与 metadata 同次 commit（无 DB 落盘验证）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_progress_callback_emits_event_per_interval(monkeypatch):
    calls: list[tuple[str, object]] = []
    adds: list[object] = []
    commits: list[int] = []

    async def fake_append_event(db, job_run_id, step, level="info", message="", payload=None):
        calls.append((step, payload))
        db.add((step, payload))
        return None

    monkeypatch.setattr(
        "app.services.after_close_orchestrator.append_event", fake_append_event
    )

    class FakeDB:
        def add(self, obj):
            adds.append(obj)

        async def commit(self):
            commits.append(1)

        async def flush(self):
            pass

    @asynccontextmanager
    async def fake_session_cm():
        yield FakeDB()

    monkeypatch.setattr(
        "app.services.after_close_orchestrator.AsyncSessionLocal", fake_session_cm
    )

    class FakeJobRunObj:
        status = "running"
        id = uuid.uuid4()
        metadata_json = None
        heartbeat_at = None
        lease_expires_at = None
        worker_instance_id = None

    async def fake_get(db, job_run_id):
        return FakeJobRunObj()

    monkeypatch.setattr(
        "app.services.after_close_orchestrator._get_job_run_or_raise", fake_get
    )

    callback = after_close_orchestrator._build_feature_snapshot_progress_callback(
        uuid.uuid4(), "w1"
    )
    interval = after_close_orchestrator._FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL
    total = 500
    processed = 0
    invocations = 0
    while processed <= total:
        await callback(
            processed=processed, total=total, snapshot_count=processed, failed_count=0
        )
        invocations += 1
        processed += interval

    expected_events = total // interval
    # 每个阈值边界恰好写一次事件（processed=0 不写，从 interval 起）
    assert len(calls) == expected_events, f"事件数={len(calls)}，期望={expected_events}"
    # 每次调用都 commit（metadata + 可能的事件同一次提交），事件不会因提交顺序丢失
    assert len(commits) == invocations
    # 每个 append_event 都 add 进了 session 并随 commit 落盘
    assert len(adds) == len(calls)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
