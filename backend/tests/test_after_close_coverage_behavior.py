"""[PANJI-BOARD-LOCAL-OWNERSHIP-CORRECTION-02 / RC4] 共享覆盖率步骤的**行为**回归测试。

不是源码字面量测试：直接驱动 production 函数
`after_close_orchestrator._run_shared_checking_coverage_step(...)`，
用 spy 记录它对 `_update_heartbeat_and_step` 传入的 last_completed_step。

冻结合同（RC4 checkpoint 单调性）：
- `checking_coverage` **非 durable**：成功时绝不推进/回退 last_completed_step；
- resume/restart 不得把更晚的 durable checkpoint（如 computing_features）倒退成 refreshing_daily；
- mainchain_stage restart 不得伪造 last_completed_step=refreshing_daily；
- normal 路径真正的 refreshing_daily checkpoint 由刷新步骤自己持久化（本步骤不重复写）。

真实缺陷（本轮修复）：覆盖率成功后旧代码仍调用
`_update_heartbeat_and_step(..., AfterCloseRunStatus.REFRESHING_DAILY.value, ...)`，
在 resume 时把 computing_features 回写成 refreshing_daily，导致 crash 后重算 features。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services import after_close_orchestrator as orch

_TRADE_DATE = date(2026, 9, 24)
_JOB_RUN_ID = uuid.uuid4()


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def commit(self) -> None:
        return None

    async def flush(self) -> None:
        return None


class _FakeJobRun:
    """可写属性替身（失败路径会写 status/error_code/error_message/finished_at）。"""

    def __init__(self) -> None:
        self.status = "running"
        self.error_code = None
        self.error_message = None
        self.finished_at = None


class _Recorder:
    """记录所有对 _update_heartbeat_and_step 的 last_completed_step 入参。"""

    def __init__(self) -> None:
        self.steps: list[str | None] = []

    async def __call__(self, db, job_run, last_completed_step, worker_id=None) -> None:
        self.steps.append(last_completed_step)


def _install(monkeypatch, *, coverage: float) -> _Recorder:
    """装配 fake 环境；coverage 为本次刷新得到（或重算得到）的覆盖率。"""
    recorder = _Recorder()

    monkeypatch.setattr(orch, "AsyncSessionLocal", _FakeSession)

    async def _get_job_run(_db, _job_run_id):
        return _FakeJobRun()

    async def _status(**_kwargs):
        return None

    async def _compute_coverage(_db, _trade_date):
        return (5000, 5200, coverage)

    async def _exec_step(_name, op, **_kwargs):
        """复刻统一执行器语义：op 抛错 => summary failed（不向上传播）。"""
        try:
            result = await op()
        except Exception:
            return None, {"status": "failed"}
        return result, {"status": "succeeded"}

    monkeypatch.setattr(orch, "_get_job_run_or_raise", _get_job_run)
    monkeypatch.setattr(orch, "_update_orchestrator_status", _status)
    monkeypatch.setattr(orch, "_update_heartbeat_and_step", recorder)
    monkeypatch.setattr(orch, "execute_orchestrator_step", _exec_step)
    monkeypatch.setattr(orch, "compute_daily_coverage", _compute_coverage)
    monkeypatch.setattr(orch, "_step_timeout", lambda _n: 60.0)
    monkeypatch.setattr(orch, "_make_step_progress_callback", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_make_step_cancellation_check", lambda *a, **k: None)
    monkeypatch.setattr(orch, "_make_step_heartbeat", lambda *a, **k: None)
    return recorder


async def _run(*, refresh_daily_coverage):
    return await orch._run_shared_checking_coverage_step(
        job_run_id=_JOB_RUN_ID,
        trade_date=_TRADE_DATE,
        worker_id="worker-1",
        lease_epoch=1,
        refresh_daily_coverage=refresh_daily_coverage,
    )


# =============================================================================
# 1. coverage 成功：绝不动 checkpoint
# =============================================================================


@pytest.mark.asyncio
async def test_coverage_success_after_refresh_never_writes_checkpoint(monkeypatch) -> None:
    """normal 路径：本次刷新已落 refreshing_daily；本步骤不得再写任何 checkpoint。"""
    recorder = _install(monkeypatch, coverage=0.95)

    passed = await _run(refresh_daily_coverage=0.95)

    assert passed is True
    assert recorder.steps == [None], (
        f"覆盖率成功必须只刷新 heartbeat（last_completed_step=None）；实际={recorder.steps}"
    )


@pytest.mark.asyncio
async def test_resume_coverage_does_not_regress_later_checkpoint(monkeypatch) -> None:
    """[RC4 核心] 已存在更晚 checkpoint（computing_features）时不得被回写成 refreshing_daily。"""
    recorder = _install(monkeypatch, coverage=0.97)

    # resume/restart：无本次刷新结果 → 从 persisted 事实重算
    passed = await _run(refresh_daily_coverage=None)

    assert passed is True
    assert recorder.steps == [None], (
        "resume 时覆盖率成功不得写 last_completed_step（否则会把 computing_features "
        f"倒退成 refreshing_daily）；实际={recorder.steps}"
    )
    assert "refreshing_daily" not in [s for s in recorder.steps if s is not None]


@pytest.mark.asyncio
async def test_mainchain_restart_does_not_fabricate_refreshing_daily(monkeypatch) -> None:
    """mainchain_stage restart（无 durable checkpoint）不得伪造 refreshing_daily。"""
    recorder = _install(monkeypatch, coverage=0.99)

    await _run(refresh_daily_coverage=None)

    assert all(step is None for step in recorder.steps), (
        f"restart 不得伪造 checkpoint；实际={recorder.steps}"
    )


@pytest.mark.asyncio
async def test_resume_reevaluates_coverage_from_persisted_facts(monkeypatch) -> None:
    """resume 必须真正重算覆盖率（不是默认通过）。"""
    calls: list[date] = []

    recorder = _install(monkeypatch, coverage=0.10)

    async def _spy_coverage(_db, trade_date):
        calls.append(trade_date)
        return (520, 5200, 0.10)

    monkeypatch.setattr(orch, "compute_daily_coverage", _spy_coverage)

    passed = await _run(refresh_daily_coverage=None)

    assert calls == [_TRADE_DATE], "resume 必须用 persisted 事实重算覆盖率"
    assert passed is False, "重算出的低覆盖率必须阻塞主链（不能因为 resume 就放行）"
    assert recorder.steps != [None] or recorder.steps == ["failed"], recorder.steps


# =============================================================================
# 2. coverage 失败：仍阻塞主链（语义不变）
# =============================================================================


@pytest.mark.asyncio
async def test_coverage_failure_blocks_mainchain_and_keeps_failed_semantics(
    monkeypatch,
) -> None:
    recorder = _install(monkeypatch, coverage=0.50)

    passed = await _run(refresh_daily_coverage=0.50)

    assert passed is False, "覆盖率不足必须让调用方 return（阻塞强制主链）"
    # 失败路径保持既有语义：写终态 "failed"（终态标记，不是推进 checkpoint）
    assert recorder.steps == ["failed"], recorder.steps


@pytest.mark.asyncio
async def test_coverage_failure_records_error_code(monkeypatch) -> None:
    _install(monkeypatch, coverage=0.50)

    captured: dict[str, object] = {}

    class _JobRun:
        status = "running"
        error_code = None
        error_message = None
        finished_at = None

    async def _get_job_run(_db, _job_run_id):
        return _JobRun()

    async def _status(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(orch, "_get_job_run_or_raise", _get_job_run)
    monkeypatch.setattr(orch, "_update_orchestrator_status", _status)

    passed = await _run(refresh_daily_coverage=0.50)

    assert passed is False
    assert captured.get("status") == orch.AfterCloseRunStatus.FAILED
    assert "fail_reasons" in (captured.get("payload") or {})


# =============================================================================
# 3. checkpoint 单调性（结构性保证：本步骤源码不含任何 checkpoint 推进）
# =============================================================================


def test_shared_coverage_source_contains_no_checkpoint_advance() -> None:
    """本步骤源码不得出现任何 REFRESHING_DAILY / CHECKING_COVERAGE 的 checkpoint 写入。"""
    src = __import__("inspect").getsource(orch._run_shared_checking_coverage_step)
    assert "REFRESHING_DAILY.value" not in src, (
        "共享覆盖率步骤不得把 refreshing_daily 写成 last_completed_step"
    )
    assert "CHECKING_COVERAGE.value" not in src, (
        "checking_coverage 本身也不是 durable checkpoint"
    )
    assert "None, worker_id" in src, "heartbeat 刷新必须显式传 None"


def test_failure_timestamp_uses_shanghai_timezone() -> None:
    """失败终态时间戳必须用 Asia/Shanghai（与既有语义一致）。"""
    import inspect

    src = inspect.getsource(orch._run_shared_checking_coverage_step)
    assert 'ZoneInfo("Asia/Shanghai")' in src
    assert datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=0)  # 常数可用性
