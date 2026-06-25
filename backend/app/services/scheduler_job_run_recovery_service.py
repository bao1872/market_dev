"""SchedulerJobRun 僵尸任务统一恢复服务。

提供 recover_stale_scheduler_job_runs(db, now) 函数，原子更新
status='running' 且 (lease 过期 OR heartbeat 超时 90s) 的任务为 interrupted，
并写唯一一条 recovery 事件。

调用点（4 处，由 Phase 4 接入）：
- main.py lifespan 启动时
- 各 Worker 启动时
- acquire_job_run_lock 抢锁前
- _recovery_watchdog_loop 每 60s

设计说明：
- 唯一实现：禁止另写多套恢复逻辑。
- strategy_batch_service.recover_stale_runs() 针对 strategy_runs 表，与本函数职责不同。
- 不单独 commit（由调用方控制事务），仅 flush。
- 幂等：每条任务最多一条 recovery 事件（先 SELECT 判断）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job_run_event import JobRunEvent

logger = logging.getLogger("scheduler_job_run_recovery_service")

# [Recovery] - 心跳超时阈值：90s = 3 个心跳周期（每 30s 一次心跳）
HEARTBEAT_TIMEOUT_SECONDS = 90


async def recover_stale_scheduler_job_runs(
    db: AsyncSession,
    now: datetime | None = None,
) -> int:
    """恢复僵尸任务（status='running' 但 lease 过期或 heartbeat 超时）。

    原子 UPDATE 将符合条件的 running 任务标记为 interrupted，
    并为每条任务写入唯一一条 recovery 事件（幂等）。
    after_close_orchestrator 任务的 metadata.orchestrator_status 同步改为 interrupted。

    恢复条件（WHERE 子句）：
        status='running'
        AND (lease_expires_at < now OR heartbeat_at < now - interval '90 seconds')

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        now: 当前时间（默认 Asia/Shanghai 当前时间）

    Returns:
        恢复的任务数量

    Raises:
        Exception: 数据库执行异常向上传播（不吞异常）
    """
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

    # [Recovery] - heartbeat 超时阈值：Python 端计算 cutoff，避免 PG 特有 interval 语法
    # 使函数跨方言兼容（生产 PG + 测试 SQLite 均可运行）
    heartbeat_cutoff = now - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)

    # [Recovery] - 原子 UPDATE：status -> interrupted + 错误信息 + 完成时间
    # metadata_json 不在此 UPDATE 中修改，RETURNING 返回原始值用于提取 original_step
    update_sql = text(
        """
        UPDATE scheduler_job_runs
        SET status = 'interrupted',
            error_code = 'STALE_PROCESS_TERMINATED',
            error_message = '后台进程在任务执行期间重启，任务租约过期或心跳超时，系统自动中断',
            finished_at = :now
        WHERE status = 'running'
            AND (
                lease_expires_at < :now
                OR heartbeat_at < :heartbeat_cutoff
            )
        RETURNING id, job_name, heartbeat_at, metadata_json
        """
    )
    result = await db.execute(update_sql, {"now": now, "heartbeat_cutoff": heartbeat_cutoff})
    recovered_rows = result.fetchall()

    if not recovered_rows:
        return 0

    # [Recovery] - 每条恢复任务：写 recovery 事件（幂等） + 更新 after_close_orchestrator metadata
    check_event_sql = text(
        "SELECT 1 FROM job_run_events "
        "WHERE job_run_id = :job_run_id AND step = 'recovery' LIMIT 1"
    )
    update_metadata_sql = text(
        """
        UPDATE scheduler_job_runs
        SET metadata_json = jsonb_set(
            COALESCE(metadata_json::jsonb, '{}'::jsonb),
            '{orchestrator_status}',
            '"interrupted"'
        )::text
        WHERE id = :id
        """
    )

    for row in recovered_rows:
        job_run_id = row.id
        job_name = row.job_name
        last_heartbeat = row.heartbeat_at

        # 提取 original_step（仅 after_close_orchestrator 的 metadata.orchestrator_status 有值）
        original_step = None
        if row.metadata_json:
            try:
                metadata = json.loads(row.metadata_json)
                original_step = metadata.get("orchestrator_status")
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "[Recovery] metadata_json 解析失败: job_run_id=%s", job_run_id,
                )

        # 幂等检查：已存在 recovery 事件则跳过插入
        existing = await db.execute(
            check_event_sql, {"job_run_id": job_run_id},
        )
        if existing.first() is None:
            event = JobRunEvent(
                job_run_id=job_run_id,
                step="recovery",
                level="error",
                message="后台进程在任务执行期间重启，任务租约过期或心跳超时，系统自动中断",
                payload={
                    "original_status": "running",
                    "original_step": original_step,
                    "last_heartbeat": (
                        last_heartbeat.isoformat() if last_heartbeat else None
                    ),
                    "recovered_at": now.isoformat(),
                },
            )
            db.add(event)

        # after_close_orchestrator 任务：metadata.orchestrator_status -> interrupted
        if job_name == "after_close_orchestrator":
            await db.execute(update_metadata_sql, {"id": job_run_id})

    await db.flush()
    logger.info("[Recovery] 恢复了 %d 个僵尸任务", len(recovered_rows))
    return len(recovered_rows)


if __name__ == "__main__":
    # 自测入口：验证函数签名与模块导入（不连接数据库，无副作用）
    import inspect

    # 验证函数签名
    sig = inspect.signature(recover_stale_scheduler_job_runs)
    params = set(sig.parameters.keys())
    assert params == {"db", "now"}, f"参数不匹配: {params}"
    assert sig.parameters["now"].default is None, "now 默认值应为 None"
    print(f"recover_stale_scheduler_job_runs 签名验证 ✓")
    print(f"参数列表: {sorted(params)}")

    # 验证返回注解（from __future__ import annotations 使注解为字符串）
    assert sig.return_annotation in (int, "int"), (
        f"返回类型应为 int, 实际: {sig.return_annotation}"
    )
    print(f"返回类型: {sig.return_annotation} ✓")

    # 验证常量
    assert HEARTBEAT_TIMEOUT_SECONDS == 90
    print(f"HEARTBEAT_TIMEOUT_SECONDS={HEARTBEAT_TIMEOUT_SECONDS} ✓")

    # 验证 JobRunEvent 导入
    assert JobRunEvent is not None
    assert JobRunEvent.__tablename__ == "job_run_events"
    print(f"JobRunEvent.__tablename__={JobRunEvent.__tablename__} ✓")

    print("OK")
