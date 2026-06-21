"""Job 队列 - 基于 Redis List 的任务队列 + 幂等键去重。

设计：
- 队列键：job:queue:{job_type}，使用 LPUSH/RPOP 模式
- 幂等键：job:idem:{idempotency_key} -> job_id，SET NX EX 保证唯一
- Job 元数据：job:meta:{job_id} -> JSON（payload、status 等）

入队流程（enqueue）：
1. 检查幂等键是否已存在（GET job:idem:{key}）
2. 若已存在，返回已入队的 job_id（幂等）
3. 若不存在，创建 JobRun 记录（pending），写入幂等键，LPUSH 到队列

出队流程（dequeue）：
1. BRPOP 多个 job:queue:{job_type} 键（阻塞等待）
2. 返回 job_id 与 payload

状态更新（update_job_status）：
1. 更新 JobRun 记录的 status/result/error/finished_at
2. 更新 job:meta:{job_id} 中的 status

幂等保证：
- 相同 idempotency_key 的任务不重复入队
- 幂等键 TTL 默认 24 小时，避免无限增长
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_redis
from app.models.job import JobRun

# Redis 键前缀
_QUEUE_PREFIX = "job:queue:"
_IDEM_PREFIX = "job:idem:"
_META_PREFIX = "job:meta:"

# 幂等键默认 TTL（秒）：24 小时
DEFAULT_IDEM_TTL = 24 * 3600


async def enqueue(
    db: AsyncSession,
    job_type: str,
    payload: dict[str, Any],
    idempotency_key: str,
    idem_ttl: int = DEFAULT_IDEM_TTL,
) -> JobRun:
    """入队任务（幂等）。

    幂等行为：
    - 若 idempotency_key 已存在，返回已入队的 JobRun（不重复入队）
    - 若不存在，创建 JobRun + 写入幂等键 + LPUSH 到队列

    Args:
        db: 异步会话
        job_type: 任务类型（如 strategy_run, selection_plan_run）
        payload: 任务输入参数
        idempotency_key: 幂等键（相同 key 不重复入队）
        idem_ttl: 幂等键 TTL（秒）

    Returns:
        JobRun 记录（新建或已存在的）

    Raises:
        ValueError: 参数非法
    """
    if not job_type:
        raise ValueError("job_type 不能为空")
    if not idempotency_key:
        raise ValueError("idempotency_key 不能为空")

    redis = get_redis()

    # 1. 检查幂等键是否已存在
    existing_job_id = await redis.get(f"{_IDEM_PREFIX}{idempotency_key}")
    if existing_job_id is not None:
        # 幂等：返回已存在的 JobRun
        stmt = select(JobRun).where(JobRun.id == UUID(existing_job_id))
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            return existing
        # 幂等键存在但 DB 记录不存在（异常情况）：清除幂等键后继续创建
        await redis.delete(f"{_IDEM_PREFIX}{idempotency_key}")

    # 2. 创建 JobRun 记录
    job_run = JobRun(
        id=uuid4(),
        job_type=job_type,
        status="pending",
        payload=payload,
    )
    db.add(job_run)
    await db.flush()  # 获取 id

    job_id_str = str(job_run.id)

    # 3. 写入幂等键（SET NX EX，原子操作）
    set_ok = await redis.set(
        f"{_IDEM_PREFIX}{idempotency_key}",
        job_id_str,
        nx=True,
        ex=idem_ttl,
    )
    if not set_ok:
        # 并发情况下被其他请求抢先：回滚并返回已存在的
        await db.rollback()
        existing_job_id = await redis.get(f"{_IDEM_PREFIX}{idempotency_key}")
        if existing_job_id is not None:
            stmt = select(JobRun).where(JobRun.id == UUID(existing_job_id))
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing
        raise RuntimeError(
            f"幂等键写入失败且无法恢复: idempotency_key={idempotency_key}"
        )

    # 4. 写入 job 元数据
    meta = {
        "job_id": job_id_str,
        "job_type": job_type,
        "status": "pending",
        "payload": payload,
        "idempotency_key": idempotency_key,
    }
    await redis.set(f"{_META_PREFIX}{job_id_str}", json.dumps(meta, ensure_ascii=False))

    # 5. LPUSH 到队列（生产者左侧入队）
    await redis.lpush(f"{_QUEUE_PREFIX}{job_type}", job_id_str)

    return job_run


async def dequeue(
    job_types: list[str],
    timeout: int = 0,
) -> tuple[str, str] | None:
    """出队任务（阻塞等待）。

    使用 BRPOP 从多个 job_type 队列右侧弹出（消费者右侧出队，FIFO）。

    Args:
        job_types: 要监听的 job_type 列表
        timeout: 阻塞超时秒数，0 表示无限等待

    Returns:
        (job_type, job_id) 元组，超时返回 None
    """
    if not job_types:
        raise ValueError("job_types 不能为空")

    redis = get_redis()
    keys = [f"{_QUEUE_PREFIX}{jt}" for jt in job_types]
    result = await redis.brpop(keys, timeout=timeout)
    if result is None:
        return None
    # result: (key, value)，key 形如 "job:queue:strategy_run"
    key_bytes, job_id = result
    job_type = key_bytes.removeprefix(_QUEUE_PREFIX) if isinstance(key_bytes, str) else key_bytes.decode().removeprefix(_QUEUE_PREFIX)
    return job_type, job_id


async def update_job_status(
    db: AsyncSession,
    job_id: UUID,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> JobRun:
    """更新任务状态。

    Args:
        db: 异步会话
        job_id: 任务 ID
        status: 新状态（running/succeeded/failed/cancelled）
        result: 成功结果（status=succeeded 时）
        error: 失败原因（status=failed 时）

    Returns:
        更新后的 JobRun

    Raises:
        ValueError: 任务不存在
    """
    stmt = select(JobRun).where(JobRun.id == job_id)
    result_exec = await db.execute(stmt)
    job_run = result_exec.scalar_one_or_none()
    if job_run is None:
        raise ValueError(f"任务不存在: job_id={job_id}")

    job_run.status = status
    if status == "running" and job_run.started_at is None:
        job_run.started_at = datetime.now(UTC)
    if status in ("succeeded", "failed", "cancelled"):
        job_run.finished_at = datetime.now(UTC)
    if result is not None:
        job_run.result = result
    if error is not None:
        job_run.error = error

    await db.flush()

    # 同步更新 Redis 元数据
    redis = get_redis()
    meta_str = await redis.get(f"{_META_PREFIX}{job_id}")
    if meta_str is not None:
        try:
            meta = json.loads(meta_str)
            meta["status"] = status
            if result is not None:
                meta["result"] = result
            if error is not None:
                meta["error"] = error
            await redis.set(
                f"{_META_PREFIX}{job_id}",
                json.dumps(meta, ensure_ascii=False),
            )
        except (json.JSONDecodeError, TypeError) as e:
            # 元数据更新失败不影响主流程，记录错误后继续
            # 不吞没异常上下文，但元数据非关键路径
            print(f"WARNING: 更新 job meta 失败: {e}")

    return job_run


if __name__ == "__main__":
    # 自测入口：验证函数可导入（不连接 Redis/DB）
    print(f"enqueue={enqueue}")
    print(f"dequeue={dequeue}")
    print(f"update_job_status={update_job_status}")
    print(f"queue prefix={_QUEUE_PREFIX}")
    print(f"idem prefix={_IDEM_PREFIX}")
    print("OK")
