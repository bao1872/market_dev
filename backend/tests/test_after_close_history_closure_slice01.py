"""Slice-01 History Closure — H2 invariant 聚焦测试（PURE_UNIT_TEST 友好）。

验证 [SLICE-01 H2] exact-T First Pyramid History 硬依赖：
- computing_history 步骤：validate_canonical_history_run_readiness 决定
  history_run_id 是否可用；
- _execute_review_step：history_run_id=None（History 未 ready）→ 返回
  gate_blocked，不创建/计算/发布 Review；
- history_run_id 有效 → Review 正常走创建/计算/发布路径。

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


async def _call_history_step(ready: bool):
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
            "app.services.after_close_orchestrator.validate_canonical_history_run_readiness",
            new=AsyncMock(return_value=ready),
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
async def test_history_step_ready_true_returns_run_id():
    result, run_id = await _call_history_step(ready=True)
    assert result["ready"] is True
    assert result["history_run_id"] == run_id
    assert result["status"] == "succeeded"
    assert result["reason"] is None


@pytest.mark.asyncio
async def test_history_step_ready_false_returns_not_ready():
    result, run_id = await _call_history_step(ready=False)
    assert result["ready"] is False
    assert result["history_run_id"] == run_id
    assert result["status"] == "not_ready"
    assert result["reason"] == "HISTORY_NOT_READY_T"


@pytest.mark.asyncio
async def test_review_step_gate_blocked_when_history_missing():
    """H2 核心：history_run_id=None → Review 不计算/不发布，返回 gate_blocked。"""
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
            history_run_id=None,
        )
    assert result["status"] == "gate_blocked"
    assert result["reason"] == "HISTORY_NOT_READY_T"
    assert result["prereq_missing"] is True
    assert result["run_id"] is None
    assert result["publication_id"] is None
    # 关键：未进入 create_run，即 Review 未计算/发布
    create_run.assert_not_called()


@pytest.mark.asyncio
async def test_review_step_runs_when_history_ready():
    """History ready（history_run_id 有效）→ Review 进入创建/计算/发布路径。"""
    fake_run = MagicMock()
    fake_run.id = uuid.uuid4()
    fake_run.source_core_run_id = uuid.uuid4()
    fake_run.source_board_run_id = None
    fake_run.algorithm_version = "v1"
    fake_run.filter_version = "v1"
    fake_run.status = "created"
    fake_run.expected_scope_count = 0
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
            "app.services.review_publication_service.evaluate_publish_gate",
            new=AsyncMock(return_value=(True, [])),
        ),
        patch(
            "app.services.review_publication_service.is_formally_published_review_run",
            return_value=False,
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
        )
    # 未触发 H2 门控
    assert result["status"] != "gate_blocked"
    # Review 创建/计算/发布被实际调用
    create_run.assert_called_once()
    compute_run.assert_called_once()
    publish_review_run.assert_called_once()
