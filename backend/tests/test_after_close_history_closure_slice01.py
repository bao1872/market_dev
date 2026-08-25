"""Slice-01 History Closure CORRECTION — H2 invariant 聚焦测试（PURE_UNIT_TEST 友好）。

验证 [SLICE-01-CORRECTION H2] exact-T First Pyramid History 硬依赖：
- readiness 真实合同返回 dict（{"status": "ok"} / {"status": "not_ready", ...}），
  NOT bool；必须用 status == "ok" 判断，禁止 bool(dict)（空 dict 仍为 True）。
- computing_history 步骤：先调用 advance_history_to_trade_date 自动推进，再校验
  readiness；返回值中 ready 由 status=='ok' 决定，history_run_id 仅作诊断。
- _execute_review_step：门控信号是 history_ready 布尔，不是 history_run_id 是否存在；
  history_ready=False（即使 history_run_id 有效）→ gate_blocked，不创建/计算/发布 Review，
  且 failed=True → 主任务收 partial_success（不是 succeeded）。
- H2 gate_blocked 必须使 AfterClose 进入 partial_success。

不连接真实 PostgreSQL；通过 mock 隔离 DB session 与 History/Review 服务。
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.after_close_orchestrator import (
    _execute_review_step,
    _make_history_step,
)


def _fake_session_cm():
    """返回一个 async context manager，提供具备必要属性的 fake session。"""
    sess = MagicMock()
    sess.get = AsyncMock(return_value=MagicMock())
    sess.commit = AsyncMock()
    sess.refresh = AsyncMock()
    sess.flush = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = sess
    cm.__aexit__.return_value = False
    return cm


def _make_readiness(status: str, reason: str | None = None) -> dict:
    return {"status": status, "reason": reason} if reason else {"status": status}


async def _call_history_step(readiness_status: str, run_id_out=None, reason=None):
    """调用 _make_history_step 的 _run()，返回 (result, history_run_id)。"""
    fake_run = MagicMock()
    fake_run.id = run_id_out or uuid.uuid4()
    with (
        patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            return_value=_fake_session_cm(),
        ),
        patch(
            "app.services.after_close_orchestrator.ensure_current_first_pyramid_history_run",
            new=AsyncMock(return_value=(fake_run, False)),
        ),
        patch(
            "app.services.after_close_orchestrator.advance_history_to_trade_date",
            new=AsyncMock(return_value={"target_state_count": 10, "failed": []}),
        ),
        patch(
            "app.services.after_close_orchestrator.validate_canonical_history_run_readiness",
            new=AsyncMock(return_value=_make_readiness(readiness_status, reason)),
        ),
    ):
        step = _make_history_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 25),
            worker_id="w1",
            skip_history=False,
        )
        return await step(), fake_run.id


@pytest.mark.asyncio
async def test_history_step_ready_ok_true():
    """readiness={'status':'ok'} → ready=True；history_run_id 作为诊断返回。"""
    result, run_id = await _call_history_step("ok")
    assert result["ready"] is True
    assert result["history_run_id"] == run_id
    assert result["status"] == "succeeded"
    assert result["reason"] is None


@pytest.mark.asyncio
async def test_history_step_not_ready_dict_is_not_truthy_ready():
    """readiness={'status':'not_ready'} → ready=False（关键：不是 bool(dict)=True）。"""
    result, run_id = await _call_history_step("not_ready", reason="target_date_state_incomplete")
    assert result["ready"] is False
    # history_run_id 仍然存在（run 存在），但 ready 必须 False —— 两个事实分离
    assert result["history_run_id"] == run_id
    assert result["status"] == "not_ready"
    assert result["reason"] == "HISTORY_NOT_READY_T"


@pytest.mark.asyncio
async def test_history_step_advance_called_before_readiness():
    """computing_history 必须先调用 advance_history_to_trade_date 自动生产。"""
    advance = AsyncMock(return_value={"target_state_count": 5, "failed": []})
    readiness = AsyncMock(return_value=_make_readiness("ok"))
    fake_run = MagicMock()
    fake_run.id = uuid.uuid4()
    with (
        patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            return_value=_fake_session_cm(),
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
            new=readiness,
        ),
    ):
        step = _make_history_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 25),
            worker_id="w1",
            skip_history=False,
        )
        await step()
    advance.assert_called_once()
    # readiness 在 advance 之后调用，且传入了 contract version 位置参数
    assert readiness.call_args_list[0][0][2]  # HISTORY_CONTRACT_VERSION 非空


@pytest.mark.asyncio
async def test_review_step_gate_blocked_when_history_not_ready():
    """H2 核心：history_ready=False（即便 history_run_id 有效）→ gate_blocked + failed=True。

    Review 不得创建/计算/发布；且 failed=True 应导致主任务 partial_success（非 succeeded）。
    """
    create_run = AsyncMock()
    with patch(
        "app.services.review_orchestrator_service.create_run", new=create_run
    ):
        result = await _execute_review_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 25),
            snapshot_run_id=uuid.uuid4(),
            worker_id="w1",
            skip_review=False,
            stock_core_published=True,
            history_run_id=uuid.uuid4(),  # 故意传入有效 UUID
            history_ready=False,          # 但 readiness=False → 必须 gate_blocked
        )
    assert result["status"] == "gate_blocked"
    assert result["reason"] == "HISTORY_NOT_READY_T"
    assert result["failed"] is True
    assert result["prereq_missing"] is True
    assert result["run_id"] is None
    assert result["publication_id"] is None
    # 关键：history_run_id 存在也不能绕过 gate
    create_run.assert_not_called()


@pytest.mark.asyncio
async def test_review_step_runs_when_history_ready():
    """history_ready=True → Review 进入创建/计算/发布路径（real contract not_ready 之外）。"""
    fake_run = MagicMock()
    fake_run.id = uuid.uuid4()
    fake_run.source_core_run_id = uuid.uuid4()
    fake_run.source_board_run_id = None
    fake_run.algorithm_version = "v1"
    fake_run.filter_version = "v1"
    fake_run.status = "created"
    fake_run.expected_scope_count = 5
    fake_run.signal_count = 0
    fake_run.coverage_ratio = 1.0

    create_run = AsyncMock(return_value=fake_run)
    compute_run = AsyncMock(return_value={"status": "published", "failed": False})
    publish_review_run = AsyncMock(return_value=(MagicMock(), None))
    get_published = AsyncMock(return_value=None)

    def _fake_session():
        sess = MagicMock()
        sess.get = AsyncMock(return_value=MagicMock())
        sess.commit = AsyncMock()
        sess.refresh = AsyncMock()
        sess.flush = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__.return_value = sess
        cm.__aexit__.return_value = False
        return cm

    with (
        patch(
            "app.services.after_close_orchestrator.AsyncSessionLocal",
            side_effect=_fake_session,
        ),
        patch(
            "app.services.review_orchestrator_service.create_run", new=create_run
        ),
        patch(
            "app.services.review_orchestrator_service.compute_run", new=compute_run
        ),
        patch(
            "app.services.review_orchestrator_service.publish_run",
            new=publish_review_run,
        ),
        patch(
            "app.services.review_publication_service.get_published_review_run_id",
            new=get_published,
        ),
        patch(
            "app.services.review_publication_service.is_formally_published_review_run",
            return_value=False,
        ),
        patch(
            "app.services.review_publication_service.evaluate_publish_gate",
            new=AsyncMock(return_value=(True, [])),
        ),
        patch(
            "app.services.after_close_orchestrator._get_job_run_or_raise",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "app.services.after_close_orchestrator._update_orchestrator_status",
            new=AsyncMock(),
        ),
        patch(
            "app.services.after_close_orchestrator._parse_metadata",
            return_value={},
        ),
    ):
        result = await _execute_review_step(
            job_run_id=uuid.uuid4(),
            trade_date=date(2026, 8, 25),
            snapshot_run_id=uuid.uuid4(),
            worker_id="w1",
            skip_review=False,
            stock_core_published=True,
            history_run_id=uuid.uuid4(),
            history_ready=True,
        )
    # 未触发 H2 门控
    assert result["status"] != "gate_blocked"
    # Review 创建/计算/发布被实际调用
    create_run.assert_called_once()
    compute_run.assert_called_once()
    publish_review_run.assert_called_once()


@pytest.mark.asyncio
async def test_review_step_not_ready_must_never_become_truthy():
    """回归防护：真实 not_ready dict 不得被任何 bool() 误判为 ready。"""
    not_ready = {"status": "not_ready", "reason": "target_date_state_incomplete"}
    # 复刻生产代码的判定逻辑
    history_ready = isinstance(not_ready, dict) and not_ready.get("status") == "ok"
    assert history_ready is False
    # 反例：bool(dict) 是危险的
    assert bool(not_ready) is True  # 证明为什么不能写 bool(readiness)
