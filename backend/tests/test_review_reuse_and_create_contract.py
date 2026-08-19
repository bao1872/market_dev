"""Phase 4.2 corrective — T6/T7/T8 真实行为测试。

覆盖三项收口要求：

- T6: Admin 复用 signals_ready run 时 **不 recompute**
      —— compute_run.call_count == 0 且 resume_run.call_count == 0。
- T7: Admin 复用 partial/failed run 时走 `resume_run(only_pending=True)`，
      且 succeeded scope **不进入** per-scope owner（`_compute_scope_metrics_phase`），
      failed / 可重试 scope 才进入。
- T8: 公共合同锁死
      `create_run(...) -> MarketReviewRun`
      `create_run_with_result(...) -> ReviewRunCreation`
      且两者不是同一个函数对象（禁止 alias 冒充两个不同合同）。

设计约束（Phase 4.2）：
- 只使用**最小 service / API mock**，不扩大 orchestrator fake harness；
- 不为测试新增生产代码抽象；
- 直接调用 API 路由函数本体（避免起 ASGI + 真 DB 依赖），
  db 用 AsyncMock，业务函数 spy 在其所在模块属性上。

运行：PURE_UNIT_TEST=1 pytest backend/tests/test_review_reuse_and_create_contract.py
"""

from __future__ import annotations

import inspect
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import admin_review
from app.models.market_review import MarketReviewRun
from app.services import review_orchestrator_service as ros
from app.services.review_orchestrator_service import (
    ITEM_FAILED,
    ITEM_RUNNING,
    ITEM_SKIPPED,
    ITEM_SUCCEEDED,
    MAX_AUTO_RESUME_ATTEMPTS,
    PHASE_METRICS,
    RUN_STATUS_FAILED,
    RUN_STATUS_PARTIAL,
    RUN_STATUS_SIGNALS_READY,
    ReviewRunCreation,
    create_run,
    create_run_with_result,
    resume_run,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 最小 fixtures
# ---------------------------------------------------------------------------
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
    payload.idempotency_key = "idem-4.2"
    for key, value in overrides.items():
        setattr(payload, key, value)
    return payload


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _make_item(
    *,
    scope_key: str,
    status: str,
    phase: str = PHASE_METRICS,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
) -> MagicMock:
    item = MagicMock()
    item.scope_type = "market"
    item.scope_key = scope_key
    item.phase = phase
    item.status = status
    item.attempt_count = attempt_count
    item.lease_expires_at = lease_expires_at
    return item


# ===========================================================================
# T6 — Admin 复用 signals_ready run：不 recompute
# ===========================================================================
async def test_t6_admin_reuse_signals_ready_does_not_recompute():
    """signals_ready 复用：compute_run 与 resume_run 调用次数都必须为 0。"""
    run = _make_run(RUN_STATUS_SIGNALS_READY)
    db = _make_db()

    creation = ReviewRunCreation(run=run, created=False)

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(return_value=creation),
        ) as m_create,
        patch.object(admin_review, "compute_run", AsyncMock()) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        resp = await admin_review.create_review_run(
            _make_payload(),
            db=db,
            ctx=MagicMock(),
        )

    # 直接证明：既没有 compute，也没有 resume
    assert m_compute.call_count == 0, "signals_ready 复用不得触发 compute_run"
    assert m_resume.call_count == 0, "signals_ready 复用不得触发 resume_run"
    assert m_create.call_count == 1

    # 不得对已就绪 run 提交任何变更
    assert db.commit.call_count == 0
    assert db.rollback.call_count == 1

    assert resp.id == str(run.id)
    assert resp.status == RUN_STATUS_SIGNALS_READY


async def test_t6_admin_reuse_published_run_is_immutable():
    """published 复用同样不得 recompute（immutable 语义）。"""
    run = _make_run(ros.RUN_STATUS_PUBLISHED)
    db = _make_db()

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(return_value=ReviewRunCreation(run=run, created=False)),
        ),
        patch.object(admin_review, "compute_run", AsyncMock()) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        resp = await admin_review.create_review_run(
            _make_payload(), db=db, ctx=MagicMock(),
        )

    assert m_compute.call_count == 0
    assert m_resume.call_count == 0
    assert db.commit.call_count == 0
    assert resp.status == ros.RUN_STATUS_PUBLISHED


async def test_t6_admin_created_run_computes_and_never_resumes():
    """对照组：created=True 必须走 compute_run，且不得走 resume_run。"""
    run = _make_run(ros.RUN_STATUS_CREATED)
    db = _make_db()

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(return_value=ReviewRunCreation(run=run, created=True)),
        ),
        patch.object(
            admin_review, "compute_run", AsyncMock(return_value={"ok": True}),
        ) as m_compute,
        patch.object(admin_review, "resume_run", AsyncMock()) as m_resume,
    ):
        await admin_review.create_review_run(
            _make_payload(), db=db, ctx=MagicMock(),
        )

    assert m_compute.call_count == 1
    assert m_resume.call_count == 0
    assert db.commit.call_count == 1


# ===========================================================================
# T7 — 复用 partial/failed run：resume_run(only_pending=True)，
#      succeeded scope 不重跑
# ===========================================================================
@pytest.mark.parametrize("reused_status", [RUN_STATUS_PARTIAL, RUN_STATUS_FAILED])
async def test_t7_admin_reuse_unpublished_run_calls_resume_only_pending(reused_status):
    """Admin 复用 partial/failed run 必须以 only_pending=True 调用 resume_run。"""
    run = _make_run(reused_status)
    db = _make_db()

    with (
        patch.object(
            admin_review, "create_run_with_result",
            AsyncMock(return_value=ReviewRunCreation(run=run, created=False)),
        ),
        patch.object(admin_review, "compute_run", AsyncMock()) as m_compute,
        patch.object(
            admin_review, "resume_run", AsyncMock(return_value={"resumed_scopes": 1}),
        ) as m_resume,
    ):
        await admin_review.create_review_run(
            _make_payload(), db=db, ctx=MagicMock(),
        )

    assert m_compute.call_count == 0, "复用未发布 run 不得整段 compute_run 重算"
    assert m_resume.call_count == 1
    # 直接证明关键字参数：only_pending=True（禁止 only_pending=False 整段重算）
    assert m_resume.call_args.kwargs.get("only_pending") is True
    assert db.commit.call_count == 1


async def test_t7_resume_run_only_pending_skips_succeeded_scopes():
    """resume_run(only_pending=True)：succeeded/skipped scope 不进入 pipeline，
    failed / 过期 running scope 才进入。"""
    run = _make_run(RUN_STATUS_PARTIAL)

    expired = datetime.now(UTC) - timedelta(hours=1)
    items = [
        _make_item(scope_key="scope_ok", status=ITEM_SUCCEEDED),
        _make_item(scope_key="scope_skipped", status=ITEM_SKIPPED),
        _make_item(scope_key="scope_failed", status=ITEM_FAILED),
        _make_item(
            scope_key="scope_stale_running",
            status=ITEM_RUNNING,
            lease_expires_at=expired,
        ),
        # 租约未过期且未超限：视为在跑，不重入
        _make_item(
            scope_key="scope_live_running",
            status=ITEM_RUNNING,
            lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        # attempt 超限：需人工介入，不自动重入
        _make_item(
            scope_key="scope_exhausted",
            status=ITEM_FAILED,
            attempt_count=MAX_AUTO_RESUME_ATTEMPTS,
        ),
    ]

    session = AsyncMock()
    session.flush = AsyncMock()

    with (
        patch.object(ros, "list_run_items", AsyncMock(return_value=items)),
        patch.object(
            ros, "prepare_current_scope_observations_batch", AsyncMock(return_value={}),
        ),
        patch.object(
            ros, "_bind_or_reuse_canonical_history_source",
            AsyncMock(return_value=(uuid.uuid4(), "h-v2")),
        ),
        patch.object(ros, "load_day_fact_maps", AsyncMock(return_value={})),
        patch.object(ros, "_resolve_all_discovery_scopes", AsyncMock(return_value=[])),
        patch.object(
            ros, "_compute_scope_metrics_phase", AsyncMock(return_value=(None, None)),
        ) as m_metrics,
        patch.object(
            ros, "evaluate_all_active_trackings", AsyncMock(return_value=0),
        ),
        patch.object(ros, "_count_scope_status", AsyncMock(return_value=(1, 1))),
        patch.object(
            ros, "_aggregate_run_data_coverage", AsyncMock(return_value=0),
        ),
    ):
        result = await resume_run(session, run, only_pending=True)

    redone = {call.args[2].scope_key for call in m_metrics.call_args_list}

    # succeeded / skipped 绝不重跑
    assert "scope_ok" not in redone
    assert "scope_skipped" not in redone
    # 未过期 running / attempt 超限 不自动重入
    assert "scope_live_running" not in redone
    assert "scope_exhausted" not in redone
    # failed / 过期 running 必须重跑
    assert redone == {"scope_failed", "scope_stale_running"}
    assert result["resumed_scopes"] == 2


async def test_t7_resume_run_only_pending_false_includes_succeeded():
    """对照组：only_pending=False 时 succeeded scope 才会被重算。

    锁定 only_pending 的真实语义差异，防止将来把 True 退化成 False。
    """
    run = _make_run(RUN_STATUS_PARTIAL)
    items = [
        _make_item(scope_key="scope_ok", status=ITEM_SUCCEEDED),
        _make_item(scope_key="scope_failed", status=ITEM_FAILED),
    ]

    session = AsyncMock()
    session.flush = AsyncMock()

    with (
        patch.object(ros, "list_run_items", AsyncMock(return_value=items)),
        patch.object(
            ros, "prepare_current_scope_observations_batch", AsyncMock(return_value={}),
        ),
        patch.object(
            ros, "_bind_or_reuse_canonical_history_source",
            AsyncMock(return_value=(uuid.uuid4(), "h-v2")),
        ),
        patch.object(ros, "load_day_fact_maps", AsyncMock(return_value={})),
        patch.object(ros, "_resolve_all_discovery_scopes", AsyncMock(return_value=[])),
        patch.object(
            ros, "_compute_scope_metrics_phase", AsyncMock(return_value=(None, None)),
        ) as m_metrics,
        patch.object(
            ros, "evaluate_all_active_trackings", AsyncMock(return_value=0),
        ),
        patch.object(ros, "_count_scope_status", AsyncMock(return_value=(2, 0))),
        patch.object(
            ros, "_aggregate_run_data_coverage", AsyncMock(return_value=0),
        ),
    ):
        await resume_run(session, run, only_pending=False)

    redone = {call.args[2].scope_key for call in m_metrics.call_args_list}
    assert redone == {"scope_ok", "scope_failed"}


# ===========================================================================
# T8 — create_run / create_run_with_result 公共合同锁死
# ===========================================================================
async def test_t8_create_run_and_with_result_are_distinct_contracts():
    """禁止 `create_run_with_result = create_run` 这类 alias 冒充两个合同。"""
    assert create_run is not create_run_with_result
    assert create_run.__name__ == "create_run"
    assert create_run_with_result.__name__ == "create_run_with_result"

    # 返回注解必须是两个不同的公共合同
    assert (
        inspect.signature(create_run).return_annotation
        == "MarketReviewRun"
    )
    assert (
        inspect.signature(create_run_with_result).return_annotation
        == "ReviewRunCreation"
    )


async def test_t8_create_run_and_with_result_share_same_parameters():
    """两个入口除返回合同外参数完全一致（向后兼容，不允许悄悄漂移）。"""
    sig_a = inspect.signature(create_run)
    sig_b = inspect.signature(create_run_with_result)
    assert list(sig_a.parameters) == list(sig_b.parameters)
    for name, param_a in sig_a.parameters.items():
        param_b = sig_b.parameters[name]
        assert param_a.kind == param_b.kind
        assert param_a.default == param_b.default


async def test_t8_create_run_returns_market_review_run_object():
    """`create_run(...)` 运行时返回 run 本体，而非 (run, created) tuple。"""
    sentinel = MagicMock(spec=MarketReviewRun)

    with patch.object(
        ros, "_create_run_impl", AsyncMock(return_value=(sentinel, True)),
    ):
        out = await create_run(AsyncMock(), trade_date=date(2026, 8, 7))

    assert out is sentinel
    assert not isinstance(out, tuple)


async def test_t8_create_run_with_result_returns_review_run_creation():
    """`create_run_with_result(...)` 运行时返回 ReviewRunCreation(run, created)。"""
    sentinel = MagicMock(spec=MarketReviewRun)

    with patch.object(
        ros, "_create_run_impl", AsyncMock(return_value=(sentinel, False)),
    ):
        out = await create_run_with_result(AsyncMock(), trade_date=date(2026, 8, 7))

    assert isinstance(out, ReviewRunCreation)
    assert out.run is sentinel
    assert out.created is False


async def test_t8_production_callers_use_plain_create_run():
    """生产调用方（after_close orchestrator / review CLI）沿用原 create_run 合同。

    直接断言导入符号身份，防止把生产路径悄悄换成 with_result 版本而破坏兼容。
    """
    from app.services import after_close_orchestrator as aco

    src = inspect.getsource(aco._execute_review_step)
    assert "create_run_with_result" not in src, (
        "after_close orchestrator 必须使用向后兼容的 create_run 合同"
    )
