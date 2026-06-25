"""幂等服务 - 基于 PostgreSQL advisory lock + 部分唯一索引的调度任务幂等控制。

核心功能：
- acquire_job_run_lock(db, run_key, job_name, business_date, lease_seconds, scheduled_at, metadata):
  三保险获取任务执行权，返回 (SchedulerJobRun | None, is_new: bool)：
  1. recover_stale_scheduler_job_runs(now)：抢锁前先把僵尸 running 恢复为 interrupted，
     腾出部分唯一索引的槽位（Phase 3 实现，本函数仅调用，不重复恢复逻辑）
  2. pg_advisory_xact_lock(hashtext(run_key)) 序列化同 run_key 的并发请求（仅 PostgreSQL）
  3. SELECT ... FOR UPDATE 查询活跃记录（status IN queued/running）
     - 有活跃记录：返回 (existing, False)（调用方应 SKIPPED_DUPLICATE）
     - 无活跃记录：INSERT 新记录，部分唯一索引保证只一条 queued/running
       - INSERT 成功：返回 (job_run, True)
       - IntegrityError（并发抢锁失败）：返回 (None, False)

返回值语义（spec Phase 2）：
- (job_run, True)：本次新建任务，调用方应执行业务
- (existing, False)：已有活跃任务，调用方应 SKIPPED_DUPLICATE（existing 为已存在的活跃记录）
- (None, False)：抢锁失败（IntegrityError），调用方应 SKIPPED_DUPLICATE

设计说明：
- 部分唯一索引 uq_scheduler_job_runs_active_run_key（迁移 038）：仅约束
  run_key IS NOT NULL AND status IN ('queued','running')，允许 interrupted/failed 后新建 attempt
- advisory_lock 是事务级锁，事务结束自动释放（不会跨事务泄漏）
- INSERT 用 SAVEPOINT（begin_nested）包裹，IntegrityError 时仅回滚 SAVEPOINT，
  不破坏外层事务（关键：测试 fixture 用 nested transaction，直接 db.rollback() 会破坏 fixture）
- 不 commit：由调用方（_create_job_run / create_after_close_run）控制事务边界
- SQLite 测试环境跳过 advisory lock 与 FOR UPDATE（SQLite 不支持，with_for_update 被忽略）
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.scheduler_job_run_recovery_service import (
    recover_stale_scheduler_job_runs,
)

logger = logging.getLogger("idempotency_service")


async def acquire_job_run_lock(
    db: AsyncSession,
    run_key: str,
    job_name: str,
    business_date: str | None = None,
    lease_seconds: int = 120,
    scheduled_at: datetime | None = None,
    metadata: dict | None = None,
    worker_instance_id: str | None = None,
    initial_status: str = "running",
) -> tuple[SchedulerJobRun | None, bool]:
    """三保险获取任务执行权，返回 (SchedulerJobRun | None, is_new)。

    流程：
    1. recover_stale_scheduler_job_runs(now)：抢锁前先恢复僵尸 running 为 interrupted
       （腾出部分唯一索引槽位，允许新任务接管崩溃前的同一 run_key）
    2. pg_advisory_xact_lock(hashtext(run_key)) 序列化并发（仅 PostgreSQL）
    3. SELECT 查询活跃记录（status IN queued/running，PostgreSQL 加 FOR UPDATE 行锁）
       - 有活跃记录：返回 (existing, False)
       - 无活跃记录：INSERT 新记录（SAVEPOINT 包裹，IntegrityError 时返回 (None, False)）

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        run_key: 业务幂等键
        job_name: 任务名称
        business_date: 业务日期 YYYY-MM-DD
        lease_seconds: 租约时长（秒）
        scheduled_at: 计划执行时间
        metadata: 元数据 dict
        worker_instance_id: Worker 实例标识
        initial_status: 新建任务的初始状态（默认 running；
            after_close_orchestrator 传 queued 让独立 Worker 领取）

    Returns:
        (SchedulerJobRun | None, is_new)：
        - (job_run, True)：新建成功，调用方应执行业务
        - (existing, False)：已有活跃任务，调用方应 SKIPPED_DUPLICATE
        - (None, False)：抢锁失败（IntegrityError），调用方应 SKIPPED_DUPLICATE
    """
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    # [Idempotency] - 1. 抢锁前先恢复僵尸任务（Phase 3 实现），腾出部分唯一索引槽位
    # recover_stale 不 commit，仅 flush，由本函数的调用方控制事务边界
    await recover_stale_scheduler_job_runs(db, now=now)

    # [Idempotency] - 2. advisory lock 序列化（仅 PostgreSQL，SQLite 测试环境跳过）
    dialect_name = db.bind.dialect.name if db.bind else "unknown"
    if dialect_name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:run_key))"),
            {"run_key": run_key},
        )

    # [Idempotency] - 3. SELECT 活跃记录（仅 queued/running，部分唯一索引只约束这些状态）
    # PostgreSQL 加 FOR UPDATE 行锁防止并发 SELECT 后并发 INSERT；SQLite 忽略 with_for_update
    stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.run_key == run_key,
            SchedulerJobRun.status.in_(["queued", "running"]),
        )
        .with_for_update()
    )
    result = await db.execute(stmt)
    existing_obj = result.scalar_one_or_none()
    if existing_obj is not None:
        # 已有活跃任务，返回 (existing, False)，调用方应 SKIPPED_DUPLICATE
        logger.info(
            "SKIPPED_DUPLICATE run_key=%s existing_status=%s existing_id=%s",
            run_key, existing_obj.status, existing_obj.id,
        )
        return (existing_obj, False)

    # [Idempotency] - 4. 无活跃记录，INSERT 新记录（SAVEPOINT 包裹，IntegrityError 仅回滚 SAVEPOINT）
    # SAVEPOINT 关键：外层可能是 nested transaction（测试 fixture）或业务事务，
    # IntegrityError 时仅回滚 SAVEPOINT，不破坏外层事务，session 仍可用
    job_run = SchedulerJobRun(
        id=uuid4(),
        job_name=job_name,
        business_date=business_date,
        run_key=run_key,
        status=initial_status,
        scheduled_at=scheduled_at if scheduled_at is not None else now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        worker_instance_id=worker_instance_id,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(job_run)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as e:
        # 部分唯一索引冲突：并发场景下另一个事务已先 INSERT 同 run_key 的活跃记录
        # SAVEPOINT 已自动 rollback，外层事务保持可用
        logger.info(
            "SKIPPED_DUPLICATE run_key=%s (integrity error: %s)", run_key, e,
        )
        return (None, False)

    return (job_run, True)


if __name__ == "__main__":
    # 自测入口：验证函数签名和模块导入（不连接数据库）
    import inspect

    sig = inspect.signature(acquire_job_run_lock)
    expected_params = {
        "db", "run_key", "job_name", "business_date", "lease_seconds",
        "scheduled_at", "metadata", "worker_instance_id", "initial_status",
    }
    actual_params = set(sig.parameters.keys())
    assert expected_params == actual_params, f"参数不匹配: {actual_params}"
    assert sig.parameters["initial_status"].default == "running", (
        "initial_status 默认值应为 running"
    )
    print("acquire_job_run_lock 签名验证 ✓")
    print(f"参数列表: {sorted(actual_params)}")

    # 验证返回类型注解为 tuple（from __future__ import annotations 使注解为字符串）
    return_anno = sig.return_annotation
    assert "tuple" in str(return_anno), f"返回类型应为 tuple, 实际: {return_anno}"
    print(f"返回类型: {return_anno} ✓")

    # 验证 recover_stale_scheduler_job_runs 已导入
    assert recover_stale_scheduler_job_runs is not None
    assert callable(recover_stale_scheduler_job_runs)
    print("recover_stale_scheduler_job_runs 导入 ✓")

    print("OK")
