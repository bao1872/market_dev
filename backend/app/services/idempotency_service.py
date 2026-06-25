"""幂等服务 - 基于 PostgreSQL advisory lock + 唯一索引的调度任务幂等控制。

核心功能：
- acquire_job_run_lock(db, run_key, job_name, business_date, lease_seconds, scheduled_at, metadata):
  双保险获取任务执行权：
  1. pg_advisory_xact_lock(hashtext(run_key)) 序列化同 run_key 的并发请求（仅 PostgreSQL）
  2. INSERT ... ON CONFLICT (run_key) DO NOTHING RETURNING * 数据库唯一约束保证只有一条记录
  返回 SchedulerJobRun 表示抢到锁；返回 None 表示已被其他 Worker 持有

设计说明：
- advisory_lock 是事务级锁，事务结束自动释放（不会跨事务泄漏）
- 唯一索引是数据库级强约束，即使 advisory_lock 失效也不会出现重复
- 未抢到锁的 Worker 应记录 SKIPPED_DUPLICATE 并立即返回，不执行业务
- SQLite 测试环境跳过 advisory lock（SQLite 无 pg_advisory_xact_lock），仅依赖唯一约束
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun

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
) -> SchedulerJobRun | None:
    """双保险获取任务执行权。

    流程：
    1. pg_advisory_xact_lock(hashtext(run_key)) 序列化并发（仅 PostgreSQL）
    2. 先 SELECT 查询 run_key 是否已存在（快速路径）
    3. 若不存在则 INSERT，flush 到数据库
    4. 返回新建记录（抢到锁）或 None（已被持有）

    Args:
        db: 异步会话
        run_key: 业务幂等键
        job_name: 任务名称
        business_date: 业务日期 YYYY-MM-DD
        lease_seconds: 租约时长（秒）
        scheduled_at: 计划执行时间
        metadata: 元数据 dict
        worker_instance_id: Worker 实例标识

    Returns:
        SchedulerJobRun 或 None
    """
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime.now(tz)

    # 1. advisory lock 序列化（仅 PostgreSQL，SQLite 测试环境跳过）
    dialect_name = db.bind.dialect.name if db.bind else "unknown"
    if dialect_name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:run_key))"),
            {"run_key": run_key},
        )

    # 2. 快速路径：查询已存在的记录（PostgreSQL 加 FOR UPDATE 行锁，SQLite 不支持）
    lock_suffix = " FOR UPDATE" if dialect_name == "postgresql" else ""
    existing = await db.execute(
        text(f"SELECT * FROM scheduler_job_runs WHERE run_key = :run_key{lock_suffix}"),
        {"run_key": run_key},
    )
    row = existing.first()
    if row is not None:
        # 已存在记录，如果是 running/succeeded 则跳过
        status = row.status
        if status in ("running", "succeeded"):
            logger.info("SKIPPED_DUPLICATE run_key=%s existing_status=%s", run_key, status)
            return None
        # 如果是 failed/interrupted，允许重新尝试（保留原 run_key 但更新记录）
        # 但为了简单起见，本版本直接跳过，重新触发需要管理员显式调用 after-close-runs
        logger.info(
            "SKIPPED_DUPLICATE run_key=%s existing_status=%s (allow retry via admin API)",
            run_key, status,
        )
        return None

    # 3. INSERT 新记录
    job_run = SchedulerJobRun(
        id=uuid4(),
        job_name=job_name,
        business_date=business_date,
        run_key=run_key,
        status="running",
        scheduled_at=scheduled_at if scheduled_at is not None else now,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        worker_instance_id=worker_instance_id,
        metadata_json=json.dumps(metadata) if metadata else None,
    )
    db.add(job_run)
    try:
        await db.flush()
    except Exception as e:
        # 唯一约束冲突，说明被并发抢先
        await db.rollback()
        logger.info("SKIPPED_DUPLICATE run_key=%s (integrity error: %s)", run_key, e)
        return None

    return job_run


if __name__ == "__main__":
    # 自测入口：验证函数签名和模块导入（不连接数据库）
    import inspect

    sig = inspect.signature(acquire_job_run_lock)
    expected_params = {
        "db", "run_key", "job_name", "business_date", "lease_seconds",
        "scheduled_at", "metadata", "worker_instance_id",
    }
    actual_params = set(sig.parameters.keys())
    assert expected_params == actual_params, f"参数不匹配: {actual_params}"
    print("acquire_job_run_lock 签名验证 ✓")
    print(f"参数列表: {sorted(actual_params)}")
    print("OK")
