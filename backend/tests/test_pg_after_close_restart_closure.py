# ===========================================================================
# REPROCESS-OWNER-CLOSURE-01 CORRECTION-01 — 契约收口（真实 production owner）
#
# 设计原则（来自 CORRECTION-01）：
#   - 不再用 MagicMock 驱动整个 execute_after_close_run 来证明 stage-selection。
#   - stage-resolution 的单一真相源是 production helper
#     app.services.after_close_orchestrator._resolve_execution_completed_steps，
#     execute_after_close_run 与测试共同调用它；禁止在 test 中复制 stage list / skip 算法。
#   - Contract A 是唯一真实跨 owner PG 测试：真实 dispatch_restart 生成 daily_ready child，
#     再由真实 worker claim selector 接受；业务 execution 被截断（不依赖 DSA/History/Review/
#     Publishing 结果）。
#   - Contract B/C/D 直接调用 production resolver 证明 stage-resolution 语义（纯 unit，无需 PG）。
#   - 非法 mainchain_stage 必须 fail closed（corrupt/typo metadata 不得退化为 full run）。
# ===========================================================================

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

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


# ===========================================================================
# Contract A — Producer/Consumer 身份契约（真实 dispatch_restart → 真实 worker claim）
#   纯 unit 的 stage-resolution 契约（B/C/D/fail-closed）见
#   tests/test_after_close_restart_resolver.py（不依赖 PG）。
# ===========================================================================

@pytest.mark.postgres
@pytest.mark.asyncio
async def test_contract_a_daily_ready_child_claimed_by_after_close_worker() -> None:
    """Contract A — 真实 dispatch_restart 创建 daily_ready child（producer 身份），
    再由真实 worker claim selector 领取（consumer 身份）。业务 execution 被截断。

    必须证明：
      child.job_name == 'after_close_orchestrator'（唯一正式盘后任务类型）
      child.status == 'queued'
      child.metadata.mainchain_stage == 'syncing_boards'
      child.metadata.parent_job_run_id preserved
      child.metadata.restart_from == 'daily_ready'
    且真实 worker selector 领取后：
      child.status == 'running' 且 worker_instance_id 被设置
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
        async def fake_handler(db, **kw):
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

    # --- consumer: 真实 worker selector 领取该 child；截断业务 execution ---
    mock_exec = AsyncMock(return_value=None)
    try:
        with patch(
            "app.services.after_close_orchestrator.execute_after_close_run",
            new=mock_exec,
        ):
            claimed = await _after_close_poll_once()
        assert claimed is True, "worker 应领取到 daily_ready child"

        # 验证被领取后状态 + worker_instance_id（claim contract 证据）
        async with TestAsyncSessionLocal() as db:
            result = await db.get(SchedulerJobRun, child.id)
            assert result.status == "running"
            assert result.worker_instance_id == _WORKER_INSTANCE_ID
            # mainchain_stage 在领取过程中不被改写
            assert json.loads(result.metadata_json).get("mainchain_stage") == "syncing_boards"
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


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
