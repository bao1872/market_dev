"""Slice 1 (REVIEW-CURRENT-OWNER-01) PURE tests — Review(T) decoupled from History(T).

[CHANGE-20260826-001 Slice 1] Review(T) = Core(T) + History(<T).

PURE (no DB): proves the orchestrator gate logic:
- KPI-4: History(T) missing (history_ready=False) but stock_core published →
  Review proceeds (reaches compute_run + publish), NOT gate_blocked HISTORY_NOT_READY_T.
- KPI-6: stock_core NOT published → Review NOT computed (skipped), and critically
  NOT blocked by History(T) (never reason == HISTORY_NOT_READY_T).
  stock_core publication code is untouched.

DB-backed loader locking test (KPI-2/KPI-3/Case C) is in
test_slice1_current_facts_lock.py (postgres-marked, verify DB).
"""
import datetime
import uuid

import pytest

import app.services.after_close_orchestrator as orch
import app.services.review_orchestrator_service as ros
import app.services.review_publication_service as rps


class _DummySession:
    def __init__(self):
        self.job_run = _DummyJobRun()

    async def execute(self, *a, **k):
        return _DummyResult()
    async def commit(self, *a, **k):
        return None
    async def refresh(self, *a, **k):
        return None
    def add(self, *a, **k):
        return None
    async def flush(self, *a, **k):
        return None
    def get(self, *a, **k):
        return self.job_run


class _DummyResult:
    def scalars(self):
        return self
    def all(self):
        return []
    def first(self):
        return None
    def scalar_one_or_none(self):
        return None


class _DummyJobRun:
    """Tolerant dummy: known attrs set; any other attribute read → None."""
    metadata_json = "{}"
    id = uuid.uuid4()
    source_core_run_id = uuid.uuid4()
    source_board_run_id = uuid.uuid4()
    status = "succeeded"
    review_run_id = uuid.uuid4()

    def __getattr__(self, name):
        return None


def _fake_session_cm():
    class _CM:
        async def __aenter__(self):
            return _DummySession()
        async def __aexit__(self, *a):
            return False
    return _CM()


async def _async_none(*a, **k):
    return None


async def _async_jobrun(*a, **k):
    return _DummyJobRun()


async def _dummy_run(*a, **k):
    return _DummyJobRun()


async def _async_publish(*a, **k):
    return uuid.uuid4()


async def _async_get_run(*a, **k):
    return _DummyJobRun()


def _apply_patches(snap):
    """Patch orchestrator + review services so no real DB/publish occurs.

    The orchestrator references these symbols both via module-level aliases and
    function-local `from ... import`, so we patch them on BOTH the owning module
    (ros/rps) AND the orchestrator module (orch) to be safe.
    Returns a dict of originals for restoration.
    """
    orig = {
        "async": orch.AsyncSessionLocal,
        "compute": ros.compute_run,
        "create": ros.create_run,
        "publish": ros.publish_run,
        "get_run": ros.get_run,
        "eval": rps.evaluate_publish_gate,
        "getpub": rps.get_published_review_run_id,
        "isform": rps.is_formally_published_review_run,
        "getjob": orch._get_job_run_or_raise,
        "upd": orch._update_orchestrator_status,
        "meta": orch._parse_metadata,
    }
    orch.AsyncSessionLocal = _fake_session_cm
    orch._get_job_run_or_raise = _async_jobrun
    orch._update_orchestrator_status = _async_none
    orch._parse_metadata = lambda *a, **k: {}
    # patch on owning modules
    ros.compute_run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("compute unset"))
    ros.create_run = _dummy_run
    ros.publish_run = _async_publish
    ros.get_run = _async_get_run
    async def _async_none_ret(*a, **k):
        return None
    rps.evaluate_publish_gate = lambda *a, **k: (True, [])
    rps.get_published_review_run_id = _async_none_ret
    rps.is_formally_published_review_run = _async_none_ret
    # patch on orchestrator module (module-level aliases)
    orch.compute_run = ros.compute_run
    orch.create_run = ros.create_run
    orch.publish_run = ros.publish_run
    orch.get_run = ros.get_run
    orch.evaluate_publish_gate = rps.evaluate_publish_gate
    orch.get_published_review_run_id = rps.get_published_review_run_id
    orch.is_formally_published_review_run = rps.is_formally_published_review_run
    return orig


def _restore_patches(orig):
    orch.AsyncSessionLocal = orig["async"]
    orch.compute_run = None
    orch.create_run = None
    orch.publish_run = None
    orch.get_run = None
    orch.evaluate_publish_gate = None
    orch.get_published_review_run_id = None
    orch.is_formally_published_review_run = None
    ros.compute_run = orig["compute"]
    ros.create_run = orig["create"]
    ros.publish_run = orig["publish"]
    ros.get_run = orig["get_run"]
    rps.evaluate_publish_gate = orig["eval"]
    rps.get_published_review_run_id = orig["getpub"]
    rps.is_formally_published_review_run = orig["isform"]
    orch._get_job_run_or_raise = orig["getjob"]
    orch._update_orchestrator_status = orig["upd"]
    orch._parse_metadata = orig["meta"]


@pytest.mark.asyncio
async def test_review_not_blocked_by_missing_exact_t_history():
    """KPI-4: History(T) 缺失（history_ready=False）但 stock_core 已发布 → Review 继续。

    fake compute_run 返回成功，断言最终 status 不是 gate_blocked，且 reason 不是
    HISTORY_NOT_READY_T（证明 exact-T History(T) 不再是 Review 前置）。
    """
    async def fake_compute_run(*a, **k):
        return {
            "status": "succeeded",
            "expected_scope_count": 1,
            "signal_count": 0,
            "coverage_ratio": 1.0,
        }

    orig = _apply_patches(None)
    ros.compute_run = fake_compute_run

    jid = uuid.uuid4()
    td = datetime.date(2026, 8, 25)
    snap = uuid.UUID("55555555-5555-5555-5555-555555555555")
    try:
        result = await orch._execute_review_step(
            job_run_id=jid,
            trade_date=td,
            worker_id="w",
            skip_review=False,
            history_run_id=None,
            history_ready=False,          # History(T) 缺失
            stock_core_published=True,    # 但 Core(T) 已发布
            snapshot_run_id=snap,
        )
        assert result["status"] != "gate_blocked", result
        assert result.get("reason") != "HISTORY_NOT_READY_T", result
        # 到达 compute + publish 路径：status 为成功/幂等复用态（非 skipped / 非 blocked）
        assert result["status"] in ("succeeded", "published_already"), result
    finally:
        _restore_patches(orig)


@pytest.mark.asyncio
async def test_review_not_blocked_by_history_when_core_missing():
    """KPI-6 派生：Core(T) 未发布 → Review 不计算（skipped），但绝不因 History(T) 阻断。"""
    orig = _apply_patches(None)
    ros.compute_run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not compute"))

    jid = uuid.uuid4()
    td = datetime.date(2026, 8, 25)
    snap = uuid.UUID("66666666-6666-6666-6666-666666666666")
    try:
        result = await orch._execute_review_step(
            job_run_id=jid,
            trade_date=td,
            worker_id="w",
            skip_review=False,
            history_run_id=None,
            history_ready=False,
            stock_core_published=False,   # Core(T) 未发布
            snapshot_run_id=snap,
        )
        assert result["status"] != "gate_blocked", result
        assert result.get("reason") != "HISTORY_NOT_READY_T", result
        # Core 未发布 → Review 不应进入 compute
        assert result["status"] in ("skipped",), result
    finally:
        _restore_patches(orig)
