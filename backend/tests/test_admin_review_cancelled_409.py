"""Phase 4.3 P1-1 — Admin cancelled run HTTP 状态码边界测试（纯单元，无 PG）。

核心回归点：create_review_run 内部 cancelled 分支 raise HTTPException(409)，
曾经被外围 `except Exception` 二次捕获并包装成 500。修正后异常边界必须保证：

- cancelled              → HTTP 409（直通，不被二次包装）
- ReviewOrchestratorError → HTTP 409
- unexpected exception   → HTTP 500（由通用 except Exception 兜底）

本测试直接驱动 API 路由函数本体（不启 ASGI），db 用 AsyncMock，
业务函数 spy 在模块属性上，最小 mock。

运行：PURE_UNIT_TEST=1 pytest backend/tests/test_admin_review_cancelled_409.py
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, status

from app.api import admin_review
from app.services import review_orchestrator_service as ros
from app.services.review_orchestrator_service import (
    RUN_STATUS_CANCELLED,
    ReviewRunCreation,
)

pytestmark = pytest.mark.asyncio


def _make_run(status: str) -> MagicMock:
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.trade_date = date(2026, 8, 7)
    run.source_core_run_id = uuid.uuid4()
    run.source_board_run_id = uuid.uuid4()
    run.source_chip_run_id = None
    run.metadata_json = {}
    run.degraded_reasons = []
    run.algorithm_version = "v1"
    run.filter_version = "v1"
    run.baseline_window = 120
    run.expected_scope_count = 2
    run.succeeded_scope_count = 0
    run.failed_scope_count = 0
    run.coverage_ratio = 0
    run.signal_count = 0
    run.created_at = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    run.updated_at = datetime(2026, 8, 7, 1, 0, tzinfo=UTC)
    run.started_at = None
    run.completed_at = None
    run.published_at = None
    return run


def _make_payload(**overrides):
    payload = MagicMock()
    payload.trade_date = "2026-08-07"
    payload.source_core_run_id = None
    payload.source_board_run_id = None
    payload.algorithm_version = None
    payload.filter_version = None
    payload.baseline_window = 120
    payload.canary = False
    payload.symbols = None
    payload.dry_run = False
    payload.idempotency_key = "idem-4.3-cancelled"
    for key, value in overrides.items():
        setattr(payload, key, value)
    return payload


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


async def test_p1_1_cancelled_run_returns_http_409_and_no_recompute():
    """复用 cancelled run：最终捕获的 HTTPException.status_code 必须 == 409，
    compute_run == 0, resume_run == 0。"""
    run = _make_run(RUN_STATUS_CANCELLED)
    db = _make_db()

    # create_run_with_result 返回已存在的 cancelled run（复用分支）。
    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(return_value=ReviewRunCreation(run=run, created=False)),
        ) as m_create,
        patch.object(
            admin_review, "compute_run", AsyncMock(return_value={"ok": True}),
        ) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await admin_review.create_review_run(
                _make_payload(), db=db, ctx=MagicMock(),
            )

    # 关键回归点：cancelled 必须直通 409，而非被二次包装成 500。
    assert exc_info.value.status_code == status.HTTP_409_CONFLICT, (
        f"cancelled run 应返回 409，实际 {exc_info.value.status_code}"
    )
    # cancelled 是复用分支：既不得 compute，也不得 resume。
    assert m_compute.call_count == 0, "cancelled 复用不得触发 compute_run"
    assert m_resume.call_count == 0, "cancelled 复用不得触发 resume_run"
    assert m_create.call_count == 1

    # cancelled run 为不可变语义，不得提交任何变更。
    assert db.commit.call_count == 0


async def test_p1_1_review_orchestrator_error_returns_http_409():
    """create_run_with_result 抛 ReviewOrchestratorError 必须 → HTTP 409。"""
    db = _make_db()

    class _RoiErr(ros.ReviewOrchestratorError):
        pass

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(side_effect=_RoiErr("dup run")),
        ),
        patch.object(admin_review, "compute_run", AsyncMock()) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await admin_review.create_review_run(
                _make_payload(), db=db, ctx=MagicMock(),
            )

    assert exc_info.value.status_code == status.HTTP_409_CONFLICT, (
        f"ReviewOrchestratorError 应返回 409，实际 {exc_info.value.status_code}"
    )
    assert m_compute.call_count == 0
    assert m_resume.call_count == 0


async def test_p1_1_unexpected_exception_returns_http_500():
    """create_run_with_result 抛非 ReviewOrchestratorError 的非 HTTP 异常 → HTTP 500。"""
    db = _make_db()

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch.object(admin_review, "compute_run", AsyncMock()) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await admin_review.create_review_run(
                _make_payload(), db=db, ctx=MagicMock(),
            )

    assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR, (
        f"未预期异常应返回 500，实际 {exc_info.value.status_code}"
    )
    assert m_compute.call_count == 0
    assert m_resume.call_count == 0
