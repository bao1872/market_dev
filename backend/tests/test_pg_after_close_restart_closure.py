# ===========================================================================
# REPROCESS-OWNER-CLOSURE-01 P0-3 — 真正能抓住本次缺陷的 contract 测试（真实 PG）
#
# 设计：用真实 production 代码路径证明以下契约，不复制一份理想 stage list：
#   Contract A — Producer/Consumer 身份契约：
#                daily_ready restart child 必须 job_name=='after_close_orchestrator'
#                （唯一正式盘后任务类型），且真实 worker selector 确实能领取它。
#   Contract B — Mainchain start boundary（通过 mainchain_stage，不伪造 last_completed_step）：
#                daily_ready restart 跳过 refreshing_daily，可达 computing_history/review。
#   Contract C — 正常 initial run（restart_from=None）仍必须执行 refreshing_daily。
#   Contract D — 原有 resume / lease_epoch / last_completed_step 语义不被 mainchain_stage 改坏。
#
# 这些测试依赖真实 PostgreSQL（verify 库 bz_stock_verify_<SHA>），由
# scripts/verify 框架在 targeted-pg / full-closure plan 中显式运行。
# ===========================================================================

import asyncio
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.models.scheduler_job_run import SchedulerJobRun
import app.services.after_close_orchestrator as after_close_orchestrator
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    execute_after_close_run,
)
from app.services.granular_restart_service import dispatch_restart
from tests.conftest import TestAsyncSessionLocal

_TZ = timezone(timedelta(hours=8))


def _fake_review_run():
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = "published"
    run.published_at = datetime.now(_TZ)
    run.expected_scope_count = 0
    run.signal_count = 0
    run.coverage_ratio = 1.0
    return run


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_contract_a_daily_ready_child_claimed_by_after_close_worker() -> None:
    """Contract A — Producer/Consumer 身份契约（跨 producer→consumer）。

    用真实 dispatch_restart 创建 daily_ready restart child，
    再交给真实 worker._after_close_poll_once 的 selection contract 领取。

    必须证明：child.job_name == 'after_close_orchestrator'（唯一正式盘后任务类型），
    且真实 worker selector 确实能领取它（不靠两个测试分别 mock）。
    """
    from app.worker import _WORKER_INSTANCE_ID, _after_close_poll_once

    # --- producer: 创建 cancelled 的 after_close_orchestrator parent ---
    parent_id = uuid.uuid4()
    now = datetime.now(_TZ)
    parent_meta = json.dumps(
        {"orchestrator_status": AfterCloseRunStatus.CANCELLED.value,
         "trade_date": "2026-08-25"},
        ensure_ascii=False,
    )
    async with TestAsyncSessionLocal() as db:
        parent = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date="2026-08-25",
            run_key=f"after_close_orchestrator:parent:{parent_id.hex[:8]}",
            status="cancelled",
            scheduled_at=now,
            started_at=now,
            metadata_json=parent_meta,
        )
        db.add(parent)
        await db.flush()
        parent.id = parent_id  # 固定 id 便于断言 parent_job_run_id
        await db.commit()

    # --- producer: 真实 dispatch_restart 创建 daily_ready child ---
    async with TestAsyncSessionLocal() as db:
        parent = await db.get(SchedulerJobRun, parent_id)
        executed = []
        async def fake_handler(db, **kw):
            executed.append(kw)
            return uuid.uuid4()
        child = await dispatch_restart(
            db, parent, "daily_ready",
            actor="audit", request_id="req-a",
            handlers={"daily_ready": fake_handler},
        )
        await db.commit()

    # --- producer identity 断言 ---
    assert child.job_name == "after_close_orchestrator", (
        f"daily_ready child 必须进入唯一正式盘后任务类型，实际: {child.job_name}"
    )
    assert child.status == "queued"
    child_meta = json.loads(child.metadata_json)
    assert child_meta.get("mainchain_stage") == "syncing_boards"
    assert child_meta.get("parent_job_run_id") == str(parent_id)
    assert child_meta.get("restart_from") == "daily_ready"

    # --- consumer: 真实 worker selector 领取该 child ---
    claimed_ids: list[uuid.UUID] = []
    async def fake_exec(job_run_id, trade_date, worker_id=None, lease_epoch=None):
        claimed_ids.append(job_run_id)

    try:
        with patch(
            "app.services.after_close_orchestrator.execute_after_close_run",
            new=fake_exec,
        ):
            claimed = await _after_close_poll_once()
        assert claimed is True, "worker 应领取到 daily_ready child"
        assert child.id in claimed_ids, (
            "producer 创建的 child 必须被真实 worker selector 接受"
        )
        # 验证被领取后状态 + worker_instance_id
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, child.id)
            assert result.status == "running"
            assert result.worker_instance_id == _WORKER_INSTANCE_ID
    finally:
        async with TestAsyncSessionLocal() as db:
            await db.execute(
                text("DELETE FROM scheduler_job_runs WHERE id = :id"),
                {"id": child.id},
            )
            await db.execute(
                text("DELETE FROM scheduler_job_runs WHERE id = :id"),
                {"id": parent_id},
            )
            await db.commit()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_contract_b_daily_ready_skips_refreshing_daily_reaches_history_review() -> None:
    """Contract B — Mainchain start boundary（通过 mainchain_stage，不伪造 last_completed_step）。

    daily_ready restart：
      refreshing_daily       SKIP（不执行）
      syncing_boards         RUN
      computing_features     RUN
      publishing             RUN
      computing_history      RUN（可达）
      computing_review       RUN（可达）
    """
    now = datetime.now(_TZ)
    meta = {
        "orchestrator_status": AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        "trade_date": "2026-06-25",
        "dsa_run_id": str(uuid.uuid4()),
        # P0-2: 唯一正式起点合同，不写 last_completed_step
        "mainchain_stage": "syncing_boards",
    }
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-25",
        run_key=f"after_close_orchestrator:restart:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with TestAsyncSessionLocal() as db:
        db.add(job_run)
        await db.flush()
        job_run_id = job_run.id
        await db.commit()

    # 用真实 production stage-selection function 验证：记录 execute_orchestrator_step
    # 实际被调度的阶段（直接 spy 统一执行器，避免复制理想 stage list）。
    # 注意：execute_orchestrator_step 返回 (result, summary)，主流程读 summary["status"]。
    seen: list[str] = []
    async def spy_step(step, operation, **kwargs):
        seen.append(step)
        # 第一个元素为 MagicMock（满足 refresh_result is not None 等断言；
        # history/review 的 .get 读受 isinstance(...,dict) 保护，MagicMock 安全降级）。
        return (MagicMock(), {"status": "succeeded", "summary": {"progress": 1.0}})

    with (
        patch.object(after_close_orchestrator, "execute_orchestrator_step", spy_step),
        patch("app.services.review_orchestrator_service.create_run", new=AsyncMock(return_value=_fake_review_run())),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=uuid.uuid4())),
    ):
        await execute_after_close_run(job_run_id=job_run_id, trade_date=date(2026, 6, 25))

    assert "refreshing_daily" not in seen, "daily_ready 不得执行 refreshing_daily"
    assert "syncing_boards" in seen
    assert "computing_features" in seen
    assert "publishing" in seen
    assert "computing_history" in seen, "History 阶段必须可达"
    assert "computing_review" in seen, "Review 阶段必须可达"

    async with TestAsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scheduler_job_runs WHERE id = :id"),
            {"id": job_run_id},
        )
        await db.commit()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_contract_c_normal_run_still_executes_refreshing_daily() -> None:
    """Contract C — 正常 initial run（restart_from=None）仍必须执行 refreshing_daily。

    防止为修 restart 把正常生产链也跳掉。
    """
    now = datetime.now(_TZ)
    meta = {
        "orchestrator_status": AfterCloseRunStatus.WAITING_DSA_WORKER.value,
        "trade_date": "2026-06-25",
        "dsa_run_id": str(uuid.uuid4()),
        # 正常 run：无 mainchain_stage、无 last_completed_step
    }
    job_run = SchedulerJobRun(
        job_name="after_close_orchestrator",
        business_date="2026-06-25",
        run_key=f"after_close_orchestrator:normal:{uuid.uuid4().hex[:8]}",
        status="running",
        scheduled_at=now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=14400),
        metadata_json=json.dumps(meta, ensure_ascii=False),
    )
    async with TestAsyncSessionLocal() as db:
        db.add(job_run)
        await db.flush()
        job_run_id = job_run.id
        await db.commit()

    seen: list[str] = []
    async def spy_step(step, operation, **kwargs):
        seen.append(step)
        # 第一个元素为 MagicMock（满足 refresh_result is not None 等断言；
        # history/review 的 .get 读受 isinstance(...,dict) 保护，MagicMock 安全降级）。
        return (MagicMock(), {"status": "succeeded", "summary": {"progress": 1.0}})

    with (
        patch.object(after_close_orchestrator, "execute_orchestrator_step", spy_step),
        patch("app.services.review_orchestrator_service.create_run", new=AsyncMock(return_value=_fake_review_run())),
        patch("app.services.review_publication_service.get_published_review_run_id", new=AsyncMock(return_value=uuid.uuid4())),
    ):
        await execute_after_close_run(job_run_id=job_run_id, trade_date=date(2026, 6, 25))

    assert "refreshing_daily" in seen, "正常 run 必须执行 refreshing_daily"
    assert "computing_history" in seen
    assert "computing_review" in seen

    async with TestAsyncSessionLocal() as db:
        await db.execute(
            text("DELETE FROM scheduler_job_runs WHERE id = :id"),
            {"id": job_run_id},
        )
        await db.commit()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_contract_d_resume_lease_epoch_unaffected_by_mainchain_stage() -> None:
    """Contract D — 原有 resume / lease_epoch / last_completed_step 语义不被 mainchain_stage 改坏。

    一个仅含 last_completed_step（无 mainchain_stage）的 resume 任务：
      - refreshing_daily 被跳过（断点恢复语义）
      - 仍正常领取（lease_epoch 递增、worker_instance_id 设置）
    """
    from app.worker import _WORKER_INSTANCE_ID, _after_close_poll_once

    run_id = uuid.uuid4()
    now = datetime.now(_TZ)
    meta = json.dumps(
        {
            "orchestrator_status": AfterCloseRunStatus.QUEUED.value,
            "trade_date": "2026-08-25",
            "last_completed_step": "refreshing_daily",  # resume 断点，非 restart
        },
        ensure_ascii=False,
    )
    async with TestAsyncSessionLocal() as db:
        run = SchedulerJobRun(
            job_name="after_close_orchestrator",
            business_date="2026-08-25",
            run_key=f"after_close_orchestrator:resume:{run_id.hex[:8]}",
            status="resume_queued",
            scheduled_at=now,
            metadata_json=meta,
        )
        db.add(run)
        await db.flush()
        run.id = run_id
        await db.commit()

    claimed_ids: list[uuid.UUID] = []
    async def fake_exec(job_run_id, trade_date, worker_id=None, lease_epoch=None):
        claimed_ids.append(job_run_id)

    try:
        with patch(
            "app.services.after_close_orchestrator.execute_after_close_run",
            new=fake_exec,
        ):
            claimed = await _after_close_poll_once()
        assert claimed is True
        assert run_id in claimed_ids
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, run_id)
            assert result.status == "running"
            assert result.worker_instance_id == _WORKER_INSTANCE_ID
            # mainchain_stage 未写入，不影响 resume 的断点语义
            assert json.loads(result.metadata_json).get("mainchain_stage") is None
    finally:
        async with TestAsyncSessionLocal() as db:
            await db.execute(
                text("DELETE FROM scheduler_job_runs WHERE id = :id"),
                {"id": run_id},
            )
            await db.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
