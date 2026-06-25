"""任务事件服务 - 写入和查询 job_run_events 时间线。

核心函数：
- append_event(db, job_run_id, step, level, message, payload): 同步写入事件（flush 不 commit）
- list_events(db, job_run_id, limit): 按时间倒序查询事件列表

设计说明：
- append_event 只 flush 不 commit，事务由调用方控制（worker/orchestrator 在适当时机 commit）
- list_events 按 created_at 倒序返回，便于任务详情抽屉展示最新事件在前
- 外键 ON DELETE CASCADE 保证任务删除时事件自动清除
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_run_event import JobRunEvent

logger = logging.getLogger("job_run_event_service")


async def append_event(
    db: AsyncSession,
    job_run_id: uuid.UUID,
    step: str,
    level: str = "info",
    message: str = "",
    payload: dict[str, Any] | None = None,
) -> JobRunEvent:
    """写入一条任务执行事件（不 commit，由调用方控制事务）。

    Args:
        db: 异步会话
        job_run_id: 关联的 SchedulerJobRun id
        step: 步骤名（START/DAILY_DONE/DSA_CREATED/ERROR 等）
        level: 级别 info/warn/error
        message: 人类可读消息
        payload: 详细数据 JSON（覆盖率、run_id 等）

    Returns:
        创建的 JobRunEvent 记录（已 flush，未 commit）

    Raises:
        IntegrityError: job_run_id 不存在时 DB 外键约束抛出（由调用方处理）
    """
    event = JobRunEvent(
        job_run_id=job_run_id,
        step=step,
        level=level,
        message=message,
        payload=payload,
    )
    db.add(event)
    # [JobRunEvent] - 直接 flush 不包装异常，让 DB 外键约束等异常自然传播
    # 调用方按异常类型处理（IntegrityError 表示 job_run_id 不存在等）
    await db.flush()
    logger.debug(
        "写入任务事件: job_run_id=%s, step=%s, level=%s",
        job_run_id, step, level,
    )
    return event


async def list_events(
    db: AsyncSession,
    job_run_id: uuid.UUID,
    limit: int = 100,
) -> list[JobRunEvent]:
    """按 created_at 倒序查询任务事件列表。

    Args:
        db: 异步会话
        job_run_id: 关联的 SchedulerJobRun id
        limit: 最多返回条数（默认 100）

    Returns:
        JobRunEvent 列表（最新事件在前）
    """
    stmt = (
        select(JobRunEvent)
        .where(JobRunEvent.job_run_id == job_run_id)
        .order_by(JobRunEvent.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


if __name__ == "__main__":
    # 自测入口：验证函数签名与模块导入（不连接数据库）
    import inspect

    # 验证 append_event 签名
    sig = inspect.signature(append_event)
    expected_params = {"db", "job_run_id", "step", "level", "message", "payload"}
    actual_params = set(sig.parameters.keys())
    assert expected_params == actual_params, f"append_event 参数不匹配: {actual_params}"
    assert sig.parameters["level"].default == "info"
    assert sig.parameters["message"].default == ""
    assert sig.parameters["payload"].default is None
    print(f"append_event 签名验证 ✓")
    print(f"参数列表: {sorted(actual_params)}")

    # 验证 list_events 签名
    sig = inspect.signature(list_events)
    expected_params = {"db", "job_run_id", "limit"}
    actual_params = set(sig.parameters.keys())
    assert expected_params == actual_params, f"list_events 参数不匹配: {actual_params}"
    assert sig.parameters["limit"].default == 100
    print(f"list_events 签名验证 ✓")
    print(f"参数列表: {sorted(actual_params)}")

    # 验证 JobRunEvent 导入
    assert JobRunEvent is not None
    assert JobRunEvent.__tablename__ == "job_run_events"
    print(f"JobRunEvent.__tablename__={JobRunEvent.__tablename__} ✓")

    print("OK")
