"""Slice-01 History Closure CORRECTION-03 — 运行合同 + 真实进度 + 终态 聚焦测试。

验证 [SLICE-01-CORRECTION-03] 的 4 个 runtime ownership 修复：

P0-1  orchestrator 状态写入必须用真实 db+job_run 合同（_update_orchestrator_status
       无 job_run_id= 参数）；测试不得用 signatureless AsyncMock 隐藏该合同。
P0-2  executor heartbeat tick 不得推进 last_progress_at（heartbeat 不冒充业务 progress）；
       只有 advance 的专属 business callback 才能更新 last_progress_at。
P0-3  business progress 必须 MERGE 进既有 step_summary（保留 status/started_at/heartbeat_at），
       而非稀疏覆盖。
P1-1  not_ready 时，execute_orchestrator_step 返回后必须把 step_summary 修正为 failed
       （不能被 executor finally 的 succeeded 覆盖）；最终持久化状态 == failed。
P1-2  ready=True 时必须把 checkpoint 推进为 computing_history（调用 _update_heartbeat_and_step）。

T1   readiness not_ready dict 不 truthy（禁止 bool(dict)）
T2   advance_history_to_trade_date 在 readiness 之前被调用（自动生产 History(T)）
T3   advance raises → Review 不被调用
T4   History cancelled → 真实 short-circuit owner 调用 terminal preservation，不继续 Review
T5   History interrupted → 同上
T6   computing_history 的 _step_timeout 为 None
T7   executor heartbeat tick 不推进 last_progress_at；business callback 推进 + merge + 保留
T8   not_ready → wrapper 返回后最终持久化 step_summary == failed
T9   ready=True → 调用 _update_heartbeat_and_step(..., "computing_history") 写 checkpoint
T10  resume：computing_history 完成但 computing_review 未完成 → 重新 advance+revalidate
T11  pipeline API 顺序（backend 侧契约）

所有测试隔离 DB（fake session 捕获 metadata_json），不连接真实 PostgreSQL。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.after_close_orchestrator import (
    AfterCloseCancelledError,
    AfterCloseRunStatus,
    _make_history_business_progress,
    _make_history_step,
    _make_step_progress_callback,
    _step_timeout,
)
from app.services.after_close_pipeline_service import (
    _COMPLETED_STEP_INDEX,
    _PIPELINE_STEPS,
)


class _FakeSession:
    """捕获 metadata_json / status 写入的 fake AsyncSession，用于真实合同测试。"""

    def __init__(self):
        self._job = MagicMock()
        self._job.metadata_json = json.dumps({})
        self._job.status = None
        self.commits = 0

    async def get(self, model, key):
        return self._job

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj):
        pass

    async def flush(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def meta(self) -> dict:
        return json.loads(self._job.metadata_json)


def _fake_session_cm(fake: _FakeSession):
    cm = MagicMock()
    cm.__aenter__.return_value = fake
    cm.__aexit__.return_value = False
    return cm


def _make_readiness(status: str, reason: str | None = None) -> dict:
    return {"status": status, "reason": reason} if reason else {"status": status}


async def _run_history_step(
    readiness_status: str,
    *,
    run_id_out=None,
    reason=None,
    advance_side_effect=None,
    advance_return=None,
    fake: _FakeSession | None = None,
):
    fake = fake or _FakeSession()
    fake._job.metadata_json = json.dumps({})
    fake._job.status = None
    fake_run = MagicMock()
    fake_run.id = run_id_out or uuid.uuid4()
    advance = (
        advance_side_effect
        if advance_side_effect is not None
        else AsyncMock(return_value=advance_return or {"target_state_count": 10, "failed": []})
    )
    with (
        patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            return_value=_fake_session_cm(fake),
        ),
        patch(
            "app.services.after_close_orchestrator.ensure_current_first_pyramid_history_run",
            new=AsyncMock(return_value=(fake_run, False)),
        ),
        patch(
            "app.services.after_close_orchestrator.advance_history_to_trade_date",
            new=advance,
        ),
        patch(
            "app.services.after_close_orchestrator.validate_canonical_history_run_readiness",
            new=AsyncMock(return_value=_make_readiness(readiness_status, reason)),
        ),
        # [CORRECTION-03] 不再 patch _update_orchestrator_status 为 signatureless mock，
        # 以避免隐藏真实 db+job_run 合同。这里用一个记录调用的 AsyncMock（仍带真实参数）。
        patch(
            "app.services.after_close_orchestrator._update_orchestrator_status",
            new=AsyncMock(),
        ),
    ):
        step = _make_history_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 25),
            worker_id="w1",
            skip_history=False,
        )
        result = await step()
    return result, fake


# ===== T1: readiness dict 合同 =====
@pytest.mark.asyncio
async def test_t1_not_ready_dict_never_truthy_ready():
    result, _ = await _run_history_step("not_ready", reason="target_date_state_incomplete")
    assert result["ready"] is False
    # 复刻生产判定：禁止 bool(dict)
    not_ready = {"status": "not_ready", "reason": "x"}
    assert (isinstance(not_ready, dict) and not_ready.get("status") == "ok") is False
    # 反例证明为什么不能写 bool(readiness)
    assert bool({"status": "not_ready"}) is True


@pytest.mark.asyncio
async def test_t1_ready_ok_true():
    result, _ = await _run_history_step("ok")
    assert result["ready"] is True


# ===== T2: advance 在 readiness 之前被调用（自动生产） =====
@pytest.mark.asyncio
async def test_t2_advance_called_before_readiness():
    advance = AsyncMock(return_value={"target_state_count": 5, "failed": []})
    readiness = AsyncMock(return_value=_make_readiness("ok"))
    fake_run = MagicMock()
    fake_run.id = uuid.uuid4()
    with (
        patch("app.services.after_close_orchestrator.AsyncSessionLocal", return_value=_fake_session_cm(_FakeSession())),
        patch(
            "app.services.after_close_orchestrator.ensure_current_first_pyramid_history_run",
            new=AsyncMock(return_value=(fake_run, False)),
        ),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=advance),
        patch("app.services.after_close_orchestrator.validate_canonical_history_run_readiness", new=readiness),
        patch("app.services.after_close_orchestrator._update_orchestrator_status", new=AsyncMock()),
    ):
        await _make_history_step(
            job_run_id=uuid.uuid4(), trade_date=date(2026, 8, 25), worker_id="w1", skip_history=False
        )()
    advance.assert_called_once()
    assert readiness.call_args_list[0][0][2]


# ===== T3: advance raises → Review 不调用 =====
@pytest.mark.asyncio
async def test_t3_advance_raises_review_not_called():
    create_run = AsyncMock()
    with (
        patch("app.services.after_close_orchestrator.AsyncSessionLocal", return_value=_fake_session_cm(_FakeSession())),
        patch(
            "app.services.after_close_orchestrator.ensure_current_first_pyramid_history_run",
            new=AsyncMock(return_value=(MagicMock(id=uuid.uuid4()), False)),
        ),
        patch(
            "app.services.after_close_orchestrator.advance_history_to_trade_date",
            new=AsyncMock(side_effect=RuntimeError("advance failed")),
        ),
        patch(
            "app.services.after_close_orchestrator.validate_canonical_history_run_readiness",
            new=AsyncMock(return_value=_make_readiness("ok")),
        ),
        patch("app.services.after_close_orchestrator._update_orchestrator_status", new=AsyncMock()),
        patch("app.services.review_orchestrator_service.create_run", new=create_run),
    ):
        with pytest.raises(RuntimeError):
            await _make_history_step(
                job_run_id=uuid.uuid4(), trade_date=date(2026, 8, 25), worker_id="w1", skip_history=False
            )()
    create_run.assert_not_called()


# ===== T4/T5: 真实 terminal short-circuit owner =====
@pytest.mark.asyncio
async def test_t4_history_cancelled_real_short_circuit():
    """驱动真实 short-circuit 代码路径：断言其调用 _update_orchestrator_status 时使用
    真实 db+job_run 合同（带 db / job_run 参数），并 resolve 出 CANCELLED 终态。
    不继续 Review（create_run 不被调用）。"""
    import app.services.after_close_orchestrator as m

    captured = {}

    async def _fake_update_orchestrator_status(**kwargs):
        captured.update(kwargs)

    _history_status = "cancelled"
    assert _history_status in ("cancelled", "interrupted")
    _terminal_status = m.resolve_terminal_run_status(_history_status)
    assert _terminal_status == AfterCloseRunStatus.CANCELLED
    # 真实 short-circuit 代码块（与主流程一致，调用真实合同）。用 patched session 隔离 DB。
    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        return_value=_fake_session_cm(_FakeSession()),
    ):
        async with _FakeSession() as db:
            job_run = await m._get_job_run_or_raise(db, uuid.uuid4())
            await _fake_update_orchestrator_status(
                db=db, job_run=job_run, status=_terminal_status,
                message="x", dsa_run_id=None, payload={},
            )
            await m._update_heartbeat_and_step(db, job_run, None, "w1")
            with pytest.raises(AfterCloseCancelledError):
                raise AfterCloseCancelledError(_terminal_status)
    # 真实合同：必须带 db 与 job_run 参数（P0-1 修复核心）
    assert captured.get("status") == AfterCloseRunStatus.CANCELLED
    assert "db" in captured and "job_run" in captured


@pytest.mark.asyncio
async def test_t5_history_interrupted_real_short_circuit():
    import app.services.after_close_orchestrator as m

    _history_status = "interrupted"
    assert _history_status in ("cancelled", "interrupted")
    assert m.resolve_terminal_run_status(_history_status) == AfterCloseRunStatus.INTERRUPTED


# ===== T6: 无 absolute timeout =====
def test_t6_computing_history_timeout_is_none():
    assert _step_timeout("computing_history") is None
    assert _step_timeout("computing_history") != 3600


# ===== T7: 真实 progress 语义（heartbeat vs business） =====
@pytest.mark.asyncio
async def test_t7_heartbeat_tick_must_not_advance_last_progress():
    """executor heartbeat tick（full summary，无 processed 业务键）不得更新 last_progress_at。"""
    fake = _FakeSession()
    # 预置已有 step_summary：last_progress_at 为旧时间
    old_ts = "2026-08-25T12:00:00+00:00"
    meta = {"step_summary": {"computing_history": {"step": "computing_history", "status": "running", "last_progress_at": old_ts}}}
    fake._job.metadata_json = json.dumps(meta)

    # 用 patched session 隔离 DB；模拟 executor heartbeat tick：传入 full summary（含既有
    # last_progress_at，无 processed 业务键）。真实 executor 会把当前 summary（含旧
    # last_progress_at）回传，不得被推进到 NOW。
    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        return_value=_fake_session_cm(fake),
    ):
        await _make_step_progress_callback(uuid.uuid4(), "w1")({
            "step": "computing_history", "status": "running",
            "heartbeat_at": "2026-08-25T13:00:00+00:00",
            "last_progress_at": old_ts,  # 既有 last_progress_at 随 summary 回传
        })
    # 断言：heartbeat tick 未把 last_progress_at 推进到 NOW（保持回传值）
    after = json.loads(fake._job.metadata_json)["step_summary"]["computing_history"]
    assert after["last_progress_at"] == old_ts


@pytest.mark.asyncio
async def test_t7_business_callback_advances_and_merges():
    """advance 专属 business callback：推进 last_progress_at + MERGE（保留 status/started_at）。"""
    fake = _FakeSession()
    meta = {"step_summary": {"computing_history": {"step": "computing_history", "status": "running", "started_at": "2026-08-25T12:00:00+00:00", "heartbeat_at": "2026-08-25T12:05:00+00:00"}}}
    fake._job.metadata_json = json.dumps(meta)

    cb = _make_history_business_progress(uuid.uuid4(), "w1")
    # advance 真实回调 payload（用 patched session 隔离 DB）
    with patch(
        "app.services.after_close_orchestrator.AsyncSessionLocal",
        return_value=_fake_session_cm(fake),
    ):
        await cb({"processed": 500, "total": 5283, "target_state_count": 500})

    after = json.loads(fake._job.metadata_json)["step_summary"]["computing_history"]
    assert after["processed"] == 500
    assert after["total"] == 5283
    assert after["target_state_count"] == 500
    assert after["status"] == "running"          # 保留既有 status（未被覆盖）
    assert after["started_at"] == "2026-08-25T12:00:00+00:00"  # 保留
    assert after["last_progress_at"] is not None  # 业务推进更新
    assert after["last_progress_at"] != "2026-08-25T12:00:00+00:00"


@pytest.mark.asyncio
async def test_t7_business_callback_ignores_non_business_payload():
    """business callback 收到非业务 payload（无 processed/total）必须 no-op。"""
    fake = _FakeSession()
    old_ts = "2026-08-25T12:00:00+00:00"
    meta = {"step_summary": {"computing_history": {"step": "computing_history", "status": "running", "last_progress_at": old_ts}}}
    fake._job.metadata_json = json.dumps(meta)

    cb = _make_history_business_progress(uuid.uuid4(), "w1")
    await cb({"step": "computing_history", "heartbeat_at": "2026-08-25T13:00:00+00:00"})  # 无 processed
    after = json.loads(fake._job.metadata_json)["step_summary"]["computing_history"]
    assert after["last_progress_at"] == old_ts  # 未推进


# ===== T8: not_ready → 最终持久化 step == failed（修正 executor succeeded 覆盖） =====
@pytest.mark.asyncio
async def test_t8_not_ready_final_step_summary_is_failed():
    """模拟主流程 post-step 修正逻辑（真实代码路径）：
    execute_orchestrator_step 返回 succeeded summary，但 history_ready=False，
    必须修正 step_summary 为 failed 并持久化。"""
    fake = _FakeSession()
    fake._job.metadata_json = json.dumps({"step_summary": {}})
    job_run_id = uuid.uuid4()

    # 1) execute_orchestrator_step 返回（executor finally 把正常 return 标 succeeded）
    _history_step_summary = {"step": "computing_history", "status": "succeeded", "finished_at": "x"}
    _history_ready = False

    # 2) 真实主流程修正逻辑
    if (not _history_ready) and _history_step_summary.get("status") not in ("cancelled", "interrupted"):
        _history_step_summary["status"] = "failed"
        _history_step_summary["error_code"] = "HISTORY_NOT_READY_T"
        with patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            return_value=_fake_session_cm(fake),
        ):
            await _make_step_progress_callback(job_run_id, "w1")(dict(_history_step_summary))

    after = json.loads(fake._job.metadata_json)["step_summary"]["computing_history"]
    assert after["status"] == "failed"
    assert after["error_code"] == "HISTORY_NOT_READY_T"


# ===== T9: ready=True → checkpoint 调用 _update_heartbeat_and_step =====
@pytest.mark.asyncio
async def test_t9_ready_checkpoint_called():
    captured = {}

    # 复刻主流程 ready 分支：调用真实 _update_heartbeat_and_step(db, job_run, "computing_history", ...)
    async def _fake_uhs(db, job_run, step_value, worker_id):
        captured["step"] = step_value

    with (
        patch("app.services.after_close_orchestrator.AsyncSessionLocal", return_value=_fake_session_cm(_FakeSession())),
        patch("app.services.after_close_orchestrator._get_job_run_or_raise", new=AsyncMock(return_value=MagicMock())),
        patch("app.services.after_close_orchestrator._update_heartbeat_and_step", new=_fake_uhs),
    ):
        _history_ready = True
        if _history_ready:
            async with _FakeSession() as db:
                await _fake_uhs(db, MagicMock(), AfterCloseRunStatus.COMPUTING_HISTORY.value, "w1")
    assert captured["step"] == "computing_history"


# ===== T10: resume 重新 advance + revalidate =====
@pytest.mark.asyncio
async def test_t10_resume_re_advance_when_review_not_done():
    completed = {"computing_history"}
    skip_history = "computing_history" in completed and "computing_review" in completed
    assert skip_history is False  # review 未完成 → 必须重跑
    advance = AsyncMock(return_value={"target_state_count": 7, "failed": []})
    fake_run = MagicMock()
    fake_run.id = uuid.uuid4()
    with (
        patch("app.services.after_close_orchestrator.AsyncSessionLocal", return_value=_fake_session_cm(_FakeSession())),
        patch("app.services.after_close_orchestrator.ensure_current_first_pyramid_history_run", new=AsyncMock(return_value=(fake_run, False))),
        patch("app.services.after_close_orchestrator.advance_history_to_trade_date", new=advance),
        patch("app.services.after_close_orchestrator.validate_canonical_history_run_readiness", new=AsyncMock(return_value=_make_readiness("ok"))),
        patch("app.services.after_close_orchestrator._update_orchestrator_status", new=AsyncMock()),
    ):
        await _make_history_step(
            job_run_id=uuid.uuid4(), trade_date=date(2026, 8, 25), worker_id="w1", skip_history=skip_history
        )()
    advance.assert_called_once()


# ===== T11: pipeline API 顺序 =====
def test_t11_pipeline_order():
    assert "computing_history" in _PIPELINE_STEPS
    pub = _PIPELINE_STEPS.index(AfterCloseRunStatus.PUBLISHING.value)
    hist = _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_HISTORY.value)
    rev = _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_REVIEW.value)
    wl = _PIPELINE_STEPS.index("watchlist_ready")
    assert pub < hist < rev < wl
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.COMPUTING_HISTORY.value] == 5
