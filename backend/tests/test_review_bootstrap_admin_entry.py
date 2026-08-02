"""Review bootstrap 正式入口契约测试（纯单元，全 mock，不连 DB）。

固化的契约：
1. bootstrap 归属 admin 路由，且提交端点返回 202（异步，不同步跑全量）；
2. dry_run 默认 True，且 dry-run 路径零业务写入；
3. operator / reason 必填（审计要求）；
4. end_date 为空时解析为最近完整交易日，不使用自然日 today；
5. 返回体含全局 summary + 按 scope 的四类计数与 reason_codes；
6. 幂等：同输入范围复用活跃任务；dry-run 与 apply 使用不同 run_key；
7. scope 明细分页返回，不一次性吐出上万行。

运行：
    PURE_UNIT_TEST=1 pytest tests/test_review_bootstrap_admin_entry.py
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from app import worker
from app.api import admin_review
from app.schemas.review import ReviewBootstrapRequest
from app.services import review_bootstrap_job_service as job_service
from app.services import review_bootstrap_service as bootstrap
from app.services.review_bootstrap_job_service import (
    MAX_DETAIL_PAGE_SIZE,
    REVIEW_BOOTSTRAP_JOB_NAME,
    ReviewBootstrapJobError,
    build_job_metadata_updates,
    build_run_key,
    build_status_payload,
    submit_bootstrap_job,
)
from app.services.review_bootstrap_service import (
    BOOTSTRAP_ALGORITHM_VERSION,
    aggregate_scope_counts,
    collect_reason_codes,
    compute_input_hash,
    resolve_bootstrap_end_date,
)


def _routes() -> list[APIRoute]:
    return [r for r in admin_review.router.routes if isinstance(r, APIRoute)]


def _route(path: str) -> APIRoute:
    for route in _routes():
        if route.path == path:
            return route
    raise AssertionError(f"路由不存在: {path}")


def _job_run(metadata: dict, **overrides) -> SimpleNamespace:
    base = {
        "id": uuid.UUID("00000000-0000-0000-0000-0000000000aa"),
        "job_name": REVIEW_BOOTSTRAP_JOB_NAME,
        "status": "succeeded",
        "metadata_json": json.dumps(metadata, ensure_ascii=False),
        "started_at": None,
        "finished_at": None,
        "heartbeat_at": None,
        "error_code": None,
        "error_message": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# =============================================================================
# 1. 入口归属与异步契约
# =============================================================================


def test_bootstrap_endpoints_are_registered_under_admin_router() -> None:
    assert admin_review.router.prefix == "/v1/admin/review"
    paths = {r.path for r in _routes()}
    assert "/v1/admin/review/bootstrap" in paths
    assert "/v1/admin/review/bootstrap/{job_run_id}" in paths
    assert "/v1/admin/review/bootstrap/{job_run_id}/resume" in paths


def test_bootstrap_submit_returns_202_not_synchronous_execution() -> None:
    """提交端点必须是 202：120 日全量回填不得在单请求内同步跑完。"""
    assert _route("/v1/admin/review/bootstrap").status_code == 202


def test_bootstrap_endpoints_require_admin_dependency() -> None:
    """所有 bootstrap 端点必须挂 require_admin，不得进入普通用户 API。"""
    for path in (
        "/v1/admin/review/bootstrap",
        "/v1/admin/review/bootstrap/{job_run_id}",
        "/v1/admin/review/bootstrap/{job_run_id}/resume",
    ):
        deps = {
            d.call for d in _route(path).dependant.dependencies if d.call is not None
        }
        assert admin_review.require_admin in deps, f"{path} 缺少 require_admin"


# =============================================================================
# 1b. Worker 挂载点：提交端点必须有真实执行者
#
# 生产没有 WORKER_TYPE=all 容器（after-close 容器跑 after_close_orchestrator），
# 若 bootstrap 只在 "all" 分支注册，任务会永远停在 queued 且无任何报错。
# 以下测试固化"提交必有执行者且不重复领取"的契约。
# =============================================================================


def test_bootstrap_is_polled_by_after_close_orchestrator_loop() -> None:
    """生产执行者：after-close 主循环必须轮询 bootstrap，否则任务永远 queued。"""
    src = inspect.getsource(worker.run_after_close_orchestrator_worker)
    assert "_review_bootstrap_poll_once" in src, (
        "生产没有 WORKER_TYPE=all 容器，bootstrap 必须挂在 after-close 主循环内，"
        "否则 admin 提交的任务没有执行者"
    )


def test_bootstrap_poll_is_lowest_priority_in_after_close_loop() -> None:
    """回填不得抢占当日盘后主链：core → chip → bootstrap。"""
    src = inspect.getsource(worker.run_after_close_orchestrator_worker)
    assert (
        src.index("_after_close_poll_once")
        < src.index("_chip_consensus_poll_once")
        < src.index("_review_bootstrap_poll_once")
    ), "bootstrap 必须排在 core 与 chip consensus 之后"


def test_standalone_bootstrap_worker_not_started_in_all_mode() -> None:
    """WORKER_TYPE=all 不得再起独立 bootstrap worker，避免与主循环重复领取。"""
    src = inspect.getsource(worker.main)
    assert 'WORKER_TYPE == "review_bootstrap"' in src, (
        "独立 bootstrap worker 只应在 WORKER_TYPE=review_bootstrap 时启动"
    )
    assert 'WORKER_TYPE in ("review_bootstrap", "all")' not in src, (
        "all 模式下会与 after-close 主循环重复领取同一批任务"
    )


# =============================================================================
# 2. 请求契约：dry_run 默认、operator/reason 必填
# =============================================================================


def test_request_dry_run_defaults_to_true() -> None:
    req = ReviewBootstrapRequest(operator="ops", reason="回填")
    assert req.dry_run is True
    assert req.days_back == 120
    assert req.end_date is None
    assert req.algorithm_version is None


@pytest.mark.parametrize(
    ("operator", "reason"),
    [("", "回填"), ("ops", ""), ("", "")],
)
def test_request_rejects_blank_audit_fields(operator: str, reason: str) -> None:
    with pytest.raises(ValidationError):
        ReviewBootstrapRequest(operator=operator, reason=reason)


def test_request_rejects_days_back_below_minimum() -> None:
    with pytest.raises(ValidationError):
        ReviewBootstrapRequest(operator="ops", reason="r", days_back=30)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operator", "reason", "days_back"),
    [("   ", "回填", 120), ("ops", "   ", 120), ("ops", "回填", 10)],
)
async def test_submit_rejects_invalid_audit_or_range(
    monkeypatch: pytest.MonkeyPatch, operator: str, reason: str, days_back: int,
) -> None:
    """service 层独立校验，不依赖 pydantic（CLI 与 API 共用同一保证）。"""
    acquire = AsyncMock()
    monkeypatch.setattr(job_service, "acquire_job_run_lock", acquire)

    with pytest.raises(ReviewBootstrapJobError):
        await submit_bootstrap_job(
            AsyncMock(), operator=operator, reason=reason,
            end_date=date(2026, 7, 31), days_back=days_back,
        )
    acquire.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_rejects_mismatched_algorithm_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    acquire = AsyncMock()
    monkeypatch.setattr(job_service, "acquire_job_run_lock", acquire)

    with pytest.raises(ReviewBootstrapJobError):
        await submit_bootstrap_job(
            AsyncMock(), operator="ops", reason="r",
            end_date=date(2026, 7, 31), algorithm_version="review-1.1.0",
        )
    acquire.assert_not_awaited()


# =============================================================================
# 3. 提交只入队，不执行；end_date 解析为交易日
# =============================================================================


@pytest.mark.asyncio
async def test_submit_only_enqueues_and_never_computes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """提交路径不得调用 bootstrap_history（计算由 Worker 承担）。"""
    captured: dict = {}

    async def _fake_acquire(db, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), status="queued"), True

    monkeypatch.setattr(job_service, "acquire_job_run_lock", _fake_acquire)
    never = AsyncMock(side_effect=AssertionError("提交阶段不得执行计算"))
    monkeypatch.setattr(job_service, "bootstrap_history", never)

    job_run, is_new, resolved = await submit_bootstrap_job(
        AsyncMock(), operator="ops", reason="review-2.0.0 回填",
        end_date=date(2026, 7, 31), days_back=120,
    )

    assert is_new is True
    assert job_run.status == "queued"
    assert captured["initial_status"] == "queued", "必须入队而非直接 running"
    assert captured["job_name"] == REVIEW_BOOTSTRAP_JOB_NAME
    assert captured["metadata"]["operator"] == "ops"
    assert captured["metadata"]["reason"] == "review-2.0.0 回填"
    assert captured["metadata"]["dry_run"] is True
    assert resolved["algorithm_version"] == BOOTSTRAP_ALGORITHM_VERSION
    never.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_resolves_empty_end_date_to_latest_trading_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """end_date 为空必须走交易日历，不得直接用自然日 today。"""
    trading_day = date(2026, 7, 31)
    monkeypatch.setattr(
        bootstrap, "get_most_recent_trading_day_async",
        AsyncMock(return_value=trading_day),
    )

    async def _fake_acquire(db, **kwargs):
        return SimpleNamespace(id=uuid.uuid4(), status="queued"), True

    monkeypatch.setattr(job_service, "acquire_job_run_lock", _fake_acquire)

    _, _, resolved = await submit_bootstrap_job(
        AsyncMock(), operator="ops", reason="r", end_date=None,
    )
    assert resolved["end_date"] == trading_day.isoformat()


@pytest.mark.asyncio
async def test_resolve_end_date_prefers_calendar_over_today(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap, "get_most_recent_trading_day_async",
        AsyncMock(return_value=date(2026, 7, 31)),
    )
    resolved = await resolve_bootstrap_end_date(AsyncMock(), end_date=None)
    assert resolved == date(2026, 7, 31)
    assert resolved != date.today()


@pytest.mark.asyncio
async def test_resolve_end_date_passes_through_explicit_value() -> None:
    explicit = date(2026, 6, 30)
    assert await resolve_bootstrap_end_date(AsyncMock(), end_date=explicit) == explicit


# =============================================================================
# 4. 幂等与 run_key
# =============================================================================


@pytest.mark.asyncio
async def test_submit_is_idempotent_for_same_input_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = SimpleNamespace(id=uuid.uuid4(), status="running")

    async def _fake_acquire(db, **kwargs):
        return existing, False

    monkeypatch.setattr(job_service, "acquire_job_run_lock", _fake_acquire)

    job_run, is_new, _ = await submit_bootstrap_job(
        AsyncMock(), operator="ops", reason="r", end_date=date(2026, 7, 31),
    )
    assert is_new is False
    assert job_run is existing


def test_run_key_separates_dry_run_from_apply() -> None:
    """dry-run 不得占用 apply 的幂等槽位（先核对再执行是正常顺序）。"""
    h = compute_input_hash(
        end_date=date(2026, 7, 31), days_back=120,
        algorithm_version=BOOTSTRAP_ALGORITHM_VERSION,
    )
    assert build_run_key(input_hash=h, dry_run=True) != build_run_key(
        input_hash=h, dry_run=False,
    )


def test_input_hash_is_stable_for_same_range_and_ignores_audit_fields() -> None:
    args = {
        "end_date": date(2026, 7, 31),
        "days_back": 120,
        "algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
    }
    assert compute_input_hash(**args) == compute_input_hash(**args)
    assert compute_input_hash(**{**args, "days_back": 60}) != compute_input_hash(**args)


# =============================================================================
# 5. 四类计数与 reason_codes
# =============================================================================


def test_scope_counts_classify_all_four_buckets() -> None:
    results = [
        {
            "trade_date": "2026-07-31",
            "status": "completed",
            "scopes": [
                {"scope_type": "market", "scope_key": "market", "status": "completed"},
                {"scope_type": "style", "scope_key": "s1", "status": "insufficient_history"},
                {"scope_type": "concept", "scope_key": "c1", "status": "skipped"},
                {
                    "scope_type": "industry_l1", "scope_key": "b1",
                    "status": "bootstrap_unavailable", "reason": "pit_membership_empty",
                },
                {"scope_type": "industry_l2", "scope_key": "b2", "status": "failed"},
            ],
        },
    ]
    counts = aggregate_scope_counts(results)
    assert counts == {
        "succeeded": 2,  # completed + insufficient_history 都算算出来了
        "skipped": 1,
        "unavailable": 1,
        "failed": 1,
    }
    assert collect_reason_codes(results)["pit_membership_empty"] == 1


def test_whole_day_unavailable_marks_every_scope_unavailable() -> None:
    """整日不可用时逐 scope 计数口径必须一致，避免"计数看着正常"。"""
    counts = aggregate_scope_counts([
        {
            "trade_date": "2026-07-30",
            "status": "bootstrap_unavailable",
            "reason": "source_run_identity_missing",
            "scopes": [
                {"scope_type": "market", "scope_key": "market", "status": "insufficient_history"},
                {"scope_type": "style", "scope_key": "s1", "status": "insufficient_history"},
            ],
        },
    ])
    assert counts == {"succeeded": 0, "skipped": 0, "unavailable": 2, "failed": 0}


def test_unknown_scope_status_counts_as_failed() -> None:
    """未知状态归为 failed，不得静默吞掉。"""
    counts = aggregate_scope_counts([
        {"status": "completed", "scopes": [{"status": "something_new"}, {"status": None}]},
    ])
    assert counts["failed"] == 2


# =============================================================================
# 6. status 响应：summary + 分页明细
# =============================================================================


def _metadata_with_days(days: int, scopes_per_day: int) -> dict:
    return {
        "dry_run": True,
        "operator": "ops",
        "reason": "回填",
        "input_hash": "h",
        "end_date": "2026-07-31",
        "days_back": 120,
        "algorithm_version": BOOTSTRAP_ALGORITHM_VERSION,
        "bootstrap_status": "ok",
        "bootstrap_summary": {
            "eligible_dates": days,
            "processed": days,
            "skipped": 0,
            "written": 0,
            "scope_counts": {
                "succeeded": days * scopes_per_day,
                "skipped": 0, "unavailable": 0, "failed": 0,
            },
            "reason_codes": {},
        },
        "bootstrap_results": [
            {
                "trade_date": f"2026-07-{d + 1:02d}",
                "status": "completed",
                "scopes": [
                    {
                        "scope_type": "concept", "scope_key": f"c{s}",
                        "status": "completed", "eligible_count": 10,
                        "ready_count": 9, "coverage": 0.9,
                    }
                    for s in range(scopes_per_day)
                ],
            }
            for d in range(days)
        ],
    }


def test_status_payload_returns_summary_and_paginated_details() -> None:
    payload = build_status_payload(
        _job_run(_metadata_with_days(10, 20)), offset=0, limit=50,
    )
    assert payload["summary"]["eligible_dates"] == 10
    assert payload["summary"]["scope_counts"]["succeeded"] == 200
    assert payload["scope_results_total"] == 200
    assert len(payload["scope_results"]) == 50, "明细必须分页，不得全量返回"
    assert payload["dry_run"] is True
    assert payload["operator"] == "ops"


def test_status_payload_pagination_offset_advances_window() -> None:
    meta = _metadata_with_days(5, 4)
    first = build_status_payload(_job_run(meta), offset=0, limit=3)
    second = build_status_payload(_job_run(meta), offset=3, limit=3)
    assert first["scope_results"] != second["scope_results"]
    assert second["offset"] == 3
    assert first["scope_results_total"] == second["scope_results_total"] == 20


def test_status_payload_clamps_limit_to_max_page_size() -> None:
    payload = build_status_payload(
        _job_run(_metadata_with_days(30, 30)), offset=0, limit=100_000,
    )
    assert payload["limit"] == MAX_DETAIL_PAGE_SIZE
    assert len(payload["scope_results"]) == MAX_DETAIL_PAGE_SIZE


def test_status_payload_survives_corrupt_metadata() -> None:
    """metadata 损坏时降级为空摘要，不得让状态查询整体报错。"""
    payload = build_status_payload(
        _job_run({}, metadata_json="{not json"), offset=0, limit=10,
    )
    assert payload["summary"]["eligible_dates"] == 0
    assert payload["scope_results"] == []


def test_status_payload_propagates_whole_day_unavailable_reason() -> None:
    payload = build_status_payload(
        _job_run({
            "bootstrap_results": [{
                "trade_date": "2026-07-30",
                "status": "bootstrap_unavailable",
                "reason": "source_run_identity_missing",
                "scopes": [{"scope_type": "market", "scope_key": "market",
                            "status": "insufficient_history"}],
            }],
        }),
    )
    row = payload["scope_results"][0]
    assert row["status"] == "bootstrap_unavailable"
    assert row["reason"] == "source_run_identity_missing"


def test_job_metadata_updates_keep_summary_and_results() -> None:
    updates = build_job_metadata_updates({
        "status": "ok",
        "eligible_dates": 3, "processed": 3, "skipped": 0, "written": 3,
        "scope_counts": {"succeeded": 9, "skipped": 0, "unavailable": 1, "failed": 0},
        "reason_codes": {"pit_membership_empty": 1},
        "results": [{"trade_date": "2026-07-31", "scopes": []}],
    })
    assert updates["bootstrap_status"] == "ok"
    assert updates["bootstrap_summary"]["written"] == 3
    assert updates["bootstrap_summary"]["scope_counts"]["unavailable"] == 1
    assert len(updates["bootstrap_results"]) == 1


# =============================================================================
# 7. dry-run 零业务写入（作业层）
# =============================================================================


@pytest.mark.asyncio
async def test_execute_dry_run_rolls_back_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_service, "bootstrap_history",
        AsyncMock(return_value={"status": "ok", "processed": 1}),
    )
    db = AsyncMock()
    await job_service.execute_bootstrap_job(
        db, job_metadata={"dry_run": True, "end_date": "2026-07-31", "days_back": 120},
    )
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_apply_commits_business_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        job_service, "bootstrap_history",
        AsyncMock(return_value={"status": "ok", "processed": 1, "written": 1}),
    )
    db = AsyncMock()
    await job_service.execute_bootstrap_job(
        db, job_metadata={"dry_run": False, "end_date": "2026-07-31", "days_back": 120},
    )
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_bootstrap_history_passes_no_audit_to_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dry-run 不得把 audit 传给写入路径（审计字段只在 apply 落库）。"""
    seen: list = []

    async def _fake_single(session, **kwargs):
        seen.append(kwargs.get("audit"))
        return {"trade_date": "2026-07-31", "status": "dry_run",
                "scopes": [], "written": False}

    monkeypatch.setattr(
        bootstrap, "list_bootstrap_eligible_dates",
        AsyncMock(return_value=[(date(2026, 7, 31), uuid.uuid4())]),
    )
    monkeypatch.setattr(bootstrap, "_try_resolve_board_run_id", AsyncMock(return_value=None))
    monkeypatch.setattr(bootstrap, "bootstrap_single_date", _fake_single)

    session = AsyncMock()
    session.expunge_all = MagicMock()
    result = await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=120,
        dry_run=True, operator="ops", reason="核对",
    )

    assert seen == [None], "dry-run 必须传 audit=None"
    # 审计信息仍在返回值中可见（供人工核对），只是不落库
    assert result["operator"] == "ops"
    assert result["reason"] == "核对"
    assert result["input_hash"]
    assert result["written"] == 0


@pytest.mark.asyncio
async def test_apply_bootstrap_history_passes_audit_to_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list = []

    async def _fake_single(session, **kwargs):
        seen.append(kwargs.get("audit"))
        return {"trade_date": "2026-07-31", "status": "completed",
                "scopes": [], "written": True}

    monkeypatch.setattr(
        bootstrap, "list_bootstrap_eligible_dates",
        AsyncMock(return_value=[(date(2026, 7, 31), uuid.uuid4())]),
    )
    monkeypatch.setattr(bootstrap, "_try_resolve_board_run_id", AsyncMock(return_value=None))
    monkeypatch.setattr(bootstrap, "bootstrap_single_date", _fake_single)

    session = AsyncMock()
    session.expunge_all = MagicMock()
    await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=120,
        dry_run=False, operator="ops", reason="正式回填",
    )

    assert len(seen) == 1 and seen[0] is not None
    assert seen[0]["operator"] == "ops"
    assert seen[0]["reason"] == "正式回填"
    assert seen[0]["input_hash"]


# =============================================================================
# 8. resume 契约
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_status", ["queued", "running", "succeeded"])
async def test_resume_rejects_non_resumable_status(bad_status: str) -> None:
    job = _job_run({}, status=bad_status)
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job),
    )

    if bad_status == "queued":
        # 幂等：已入队直接返回同一任务
        assert await job_service.resume_bootstrap_job(db, job.id) is job
    else:
        with pytest.raises(ReviewBootstrapJobError):
            await job_service.resume_bootstrap_job(db, job.id)


@pytest.mark.asyncio
async def test_resume_requeues_failed_job_and_clears_lease() -> None:
    job = _job_run(
        {"dry_run": False, "operator": "ops"},
        status="failed", error_code="X", error_message="boom",
    )
    job.worker_instance_id = "w1"
    job.lease_expires_at = "later"
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job),
    )

    resumed = await job_service.resume_bootstrap_job(db, job.id)

    assert resumed.status == "queued"
    assert resumed.error_code is None
    assert resumed.error_message is None
    assert resumed.worker_instance_id is None
    assert resumed.lease_expires_at is None
    assert json.loads(resumed.metadata_json)["bootstrap_status"] == "queued"


@pytest.mark.asyncio
async def test_resume_rejects_non_bootstrap_job() -> None:
    job = _job_run({}, job_name="after_close_orchestrator", status="failed")
    db = AsyncMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalar_one_or_none=lambda: job),
    )
    with pytest.raises(ReviewBootstrapJobError):
        await job_service.resume_bootstrap_job(db, job.id)


@pytest.mark.asyncio
async def test_get_bootstrap_job_rejects_foreign_job_name() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=_job_run({}, job_name="chip_consensus"))
    with pytest.raises(ReviewBootstrapJobError):
        await job_service.get_bootstrap_job(db, uuid.uuid4())


@pytest.mark.asyncio
async def test_get_bootstrap_job_raises_when_missing() -> None:
    db = AsyncMock()
    db.get = AsyncMock(return_value=None)
    with pytest.raises(ReviewBootstrapJobError):
        await job_service.get_bootstrap_job(db, uuid.uuid4())


# =============================================================================
# 9. 内存上限契约（[FIX-20260802] 60 日全 scope dry-run 曾在 3.4GB RSS 被 OOM 杀死）
#
# 固化四条不变量，防止回归到"单会话 + 全量明细累积"的无上限实现：
#   1. 按 chunk_days 分片，每片结束释放 ORM identity map；
#   2. 返回体不保留全部逐日 scope 明细（detail_limit 之外只留聚合摘要）；
#   3. 聚合计数与压缩前等价（压缩不得丢失 succeeded/skipped/unavailable/failed）；
#   4. RSS 超预算时安全停止并如实上报 partial，绝不静默截断或假装成功。
# =============================================================================


def _patch_eligible_days(monkeypatch: pytest.MonkeyPatch, days: int) -> None:
    """构造 N 个可 bootstrap 日期，并让每日返回固定 scope 明细。"""
    eligible = [
        (date(2026, 7, 31) - timedelta(days=offset), uuid.uuid4())
        for offset in range(days)
    ]
    monkeypatch.setattr(
        bootstrap, "list_bootstrap_eligible_dates", AsyncMock(return_value=eligible),
    )
    monkeypatch.setattr(
        bootstrap, "_try_resolve_board_run_id", AsyncMock(return_value=None),
    )

    async def _fake_single(session, **kwargs):
        return {
            "trade_date": kwargs["trade_date"].isoformat(),
            "run_id": None,
            "status": "dry_run",
            "written": False,
            # 每日 3 个 scope：2 成功 + 1 不可用
            "scopes": [
                {"scope_type": "market", "scope_key": "market",
                 "status": "insufficient_history"},
                {"scope_type": "industry_l1", "scope_key": "b1",
                 "status": "insufficient_history"},
                {"scope_type": "concept", "scope_key": "c1",
                 "status": "bootstrap_unavailable", "reason": "pit_membership_empty"},
            ],
        }

    monkeypatch.setattr(bootstrap, "bootstrap_single_date", _fake_single)


@pytest.mark.asyncio
async def test_bootstrap_history_chunks_and_releases_identity_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分片执行：每个分片结束必须 expunge_all 释放 ORM identity map。"""
    _patch_eligible_days(monkeypatch, days=10)
    session = AsyncMock()
    session.expunge_all = MagicMock()  # 真实 AsyncSession.expunge_all 是同步方法

    result = await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=60,
        dry_run=True, operator="ops", reason="内存契约", chunk_days=3,
    )

    # 10 日 / 每片 3 日 => 4 个分片
    assert result["chunks"] == 4, f"分片数应为 4，实际 {result['chunks']}"
    assert session.expunge_all.call_count == 4, (
        "每个分片结束都必须释放 ORM identity map，否则跨日对象长期驻留导致 OOM"
    )
    assert result["processed"] == 10


@pytest.mark.asyncio
async def test_bootstrap_history_does_not_retain_all_scope_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """返回体不得线性累积全部 scope 明细（这是 OOM 的第二个根因）。"""
    _patch_eligible_days(monkeypatch, days=20)
    session = AsyncMock()
    session.expunge_all = MagicMock()

    result = await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=60,
        dry_run=True, operator="ops", reason="内存契约",
        chunk_days=5, detail_limit=3,
    )

    detailed = [day for day in result["results"] if "scopes" in day]
    assert len(detailed) == 3, (
        f"仅前 detail_limit=3 天保留完整明细，实际 {len(detailed)} 天"
    )
    # 其余天必须保留聚合摘要，信息不丢失
    for day in result["results"][3:]:
        assert "scope_counts" in day, "压缩后的日结果必须保留四类计数"
        assert day["scope_total"] == 3, "压缩后必须保留 scope 总数"


@pytest.mark.asyncio
async def test_bootstrap_history_compaction_preserves_aggregate_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """压缩明细不得改变全批次四类计数与原因码（增量聚合与全量聚合等价）。"""
    _patch_eligible_days(monkeypatch, days=12)
    session = AsyncMock()
    session.expunge_all = MagicMock()

    result = await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=60,
        dry_run=True, operator="ops", reason="内存契约",
        chunk_days=4, detail_limit=1,
    )

    # 每日 2 成功 + 1 不可用 × 12 日
    assert result["scope_counts"]["succeeded"] == 24
    assert result["scope_counts"]["unavailable"] == 12
    assert result["scope_counts"]["failed"] == 0
    assert result["reason_codes"]["pit_membership_empty"] == 12


@pytest.mark.asyncio
async def test_bootstrap_history_stops_safely_when_memory_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RSS 超预算必须安全停止并如实上报，不得静默截断或报告成功。"""
    _patch_eligible_days(monkeypatch, days=20)
    session = AsyncMock()
    session.expunge_all = MagicMock()
    # 模拟第一个分片结束时 RSS 就已超预算
    monkeypatch.setattr(bootstrap, "_current_rss_mb", lambda: 9999.0)

    result = await bootstrap.bootstrap_history(
        session, end_date=date(2026, 7, 31), days_back=60,
        dry_run=True, operator="ops", reason="内存契约",
        chunk_days=5, memory_budget_mb=1024,
    )

    assert result["status"] == "memory_budget_exceeded", (
        "超预算必须以专用状态返回，不得伪装为 ok"
    )
    assert result["chunks"] == 1, "必须在第一个分片后立即停止"
    assert result["processed"] == 5, "已处理天数必须如实上报"
    assert result["eligible_dates"] == 20, "未处理天数不得被抹去"
    assert result["peak_rss_mb"] == 9999.0


@pytest.mark.asyncio
async def test_bootstrap_history_rejects_invalid_memory_params() -> None:
    """非法分片/预算参数必须直接拒绝，避免退化为无上限执行。"""
    with pytest.raises(ValueError):
        await bootstrap.bootstrap_history(
            AsyncMock(), days_back=60, chunk_days=0,
            operator="ops", reason="x",
        )
    with pytest.raises(ValueError):
        await bootstrap.bootstrap_history(
            AsyncMock(), days_back=60, memory_budget_mb=1,
            operator="ops", reason="x",
        )
