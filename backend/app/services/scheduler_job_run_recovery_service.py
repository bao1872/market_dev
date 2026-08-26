"""SchedulerJobRun 僵尸任务统一恢复服务。

提供两个核心函数：
1. recover_stale_scheduler_job_runs(db, now): 原子更新 status='running' 且
   (lease 过期 OR heartbeat 超时 90s) 的任务为 interrupted，并写唯一一条 recovery 事件。
2. auto_resume_interrupted_after_close_runs(db, now): [PRD §4.3 JOB-01] 自动将
   interrupted 的 after_close_orchestrator 任务转换为 resume_queued，递增 attempt_no，
   允许 Worker 领取重试（断点恢复 via last_completed_step）。

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
- [JOB-01] auto_resume 仅处理 after_close_orchestrator 任务（其他 job 不含断点恢复）。
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
    """恢复僵尸任务（lease 已过期且 heartbeat 不健康）。

    原子 UPDATE 将符合条件的 running 任务标记为 interrupted，
    并为每条任务写入唯一一条 recovery 事件（幂等）。
    after_close_orchestrator 任务的 metadata.orchestrator_status 同步改为 interrupted。

    恢复条件（WHERE 子句）：
        status='running'
        AND lease_expires_at < now
        AND heartbeat_at < now - interval '90 seconds'

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
            error_message = '后台进程在任务执行期间失联，任务租约过期且心跳超时，系统自动中断',
            finished_at = :now
        WHERE status = 'running'
            AND lease_expires_at < :now
            AND (heartbeat_at IS NULL OR heartbeat_at < :heartbeat_cutoff)
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
                message="后台进程在任务执行期间失联，任务租约过期且心跳超时，系统自动中断",
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


def _parse_worker_slot(instance_id: str) -> str:
    """返回 worker slot（hostname:pid）。

    兼容 legacy 格式 "hostname:pid"（无 incarnation nonce）与新格式
    "hostname:pid:nonce"。slot 相同意味着「同一机器同一 PID 槽位」，是判断
    incarnation 是否被替换的依据。
    """
    if not instance_id:
        return instance_id
    parts = instance_id.split(":")
    if len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return instance_id


def _parse_worker_incarnation(instance_id: str) -> str | None:
    """返回 incarnation nonce；legacy "hostname:pid" 返回 None。"""
    if not instance_id:
        return None
    parts = instance_id.split(":")
    if len(parts) == 3:
        return parts[2]
    return None


async def recover_replaced_incarnation_runs(
    db: AsyncSession,
    current_worker_instance_id: str,
    now: datetime | None = None,
) -> int:
    """[CRASH-RESUME-SLICE / P0-C] 同 slot incarnation 替换快速恢复。

    安全前提：当前 worker 进程已真实启动（current_worker_instance_id 含全新
    startup nonce）。若存在同一 PID slot（hostname:pid）上的「上一代」running 任务，
    则可证明该 slot 上的旧进程已被替换（进程不复活），无需等待 4h lease 自然过期即可
    立即 interrupted → 触发 auto-resume 断点恢复。

    判定（Python 端，安全且可解释）：
        owner_slot == current_slot
        AND (owner_incarnation is None  # legacy 无 nonce，视为可替换
             OR owner_incarnation != current_incarnation)

    该分支与 recover_stale_scheduler_job_runs 的保守 AND 分支互补、互不重叠：
    - 保守分支要求 lease 过期 AND heartbeat 超时，用于「owner 死亡无法证明」的模糊场景；
    - 本分支仅用于「同 slot 已出现新 incarnation」这类 owner 被明确替换的场景，
      不会因「存在另一个不同 slot 的 worker」而误抢任务。

    注意：本函数不处理不同 slot 的 worker（即使其 heartbeat stale 且 lease 仍有效），
    那属于保守分支的责任范围，本分支故意不触碰，保持 fail-safe。
    """
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

    current_slot = _parse_worker_slot(current_worker_instance_id)
    current_incarnation = _parse_worker_incarnation(current_worker_instance_id)
    if not current_slot or current_incarnation is None:
        # 当前 worker 自身是 legacy 格式（无 nonce），无法证明替换关系，跳过。
        return 0

    # 取出本 slot 上所有 running 任务（含 legacy 精确匹配与新格式前缀匹配）
    select_sql = text(
        """
        SELECT id, job_name, worker_instance_id, heartbeat_at, metadata_json
        FROM scheduler_job_runs
        WHERE status = 'running'
          AND (
            worker_instance_id = :slot
            OR worker_instance_id LIKE :slot_prefix
          )
        """
    )
    result = await db.execute(
        select_sql,
        {"slot": current_slot, "slot_prefix": f"{current_slot}:%"},
    )
    candidate_rows = result.fetchall()

    to_interrupt: list = []
    for row in candidate_rows:
        owner_id = row.worker_instance_id
        if not owner_id:
            continue
        owner_slot = _parse_worker_slot(owner_id)
        if owner_slot != current_slot:
            # 不同 slot：不抢（保守路径负责）
            continue
        owner_incarnation = _parse_worker_incarnation(owner_id)
        if owner_incarnation == current_incarnation:
            # 同一 incarnation（同一进程）：不抢，避免重复中断自身正在跑的任务
            continue
        # owner 为 legacy（None）或不同 nonce → 上一代进程已被替换，可快速恢复
        to_interrupt.append(row)

    if not to_interrupt:
        return 0

    update_sql = text(
        """
        UPDATE scheduler_job_runs
        SET status = 'interrupted',
            error_code = 'WORKER_INCARNATION_REPLACED',
            error_message = '同 slot 上已启动新 incarnation，旧进程被替换，任务立即中断并断点恢复',
            finished_at = :now
        WHERE id = :id
        """
    )
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
    recovered = 0
    for row in to_interrupt:
        job_run_id = row.id
        job_name = row.job_name
        last_heartbeat = row.heartbeat_at
        await db.execute(update_sql, {"now": now, "id": job_run_id})
        existing = await db.execute(
            check_event_sql, {"job_run_id": job_run_id},
        )
        if existing.first() is None:
            event = JobRunEvent(
                job_run_id=job_run_id,
                step="recovery",
                level="error",
                message=(
                    "同 slot 已启动新 incarnation，旧进程被替换，任务立即中断并断点恢复"
                ),
                payload={
                    "original_status": "running",
                    "owner_instance_id": row.worker_instance_id,
                    "current_instance_id": current_worker_instance_id,
                    "last_heartbeat": (
                        last_heartbeat.isoformat() if last_heartbeat else None
                    ),
                    "recovered_at": now.isoformat(),
                },
            )
            db.add(event)
        if job_name == "after_close_orchestrator":
            await db.execute(update_metadata_sql, {"id": job_run_id})
        recovered += 1

    await db.flush()
    logger.info(
        "[Recovery] incarnation 替换快速恢复: %d 个同 slot 旧进程任务",
        recovered,
    )
    return recovered


# [PRD §4.3 JOB-01] - after_close_orchestrator 任务名（支持 auto-resume）
_AFTER_CLOSE_JOB_NAME = "after_close_orchestrator"

# [P0-3 ref/instruction.md §二.3] - chip_consensus 任务名（同样支持 auto-resume + 断点续算）
_CHIP_CONSENSUS_JOB_NAME = "after_close_chip_consensus"

# [PRD §4.3 JOB-01] - 最大自动重试次数（超过则不再 auto-resume，需人工介入）
_MAX_AUTO_RESUME_ATTEMPTS = 3


async def auto_resume_interrupted_after_close_runs(
    db: AsyncSession,
    now: datetime | None = None,
) -> int:
    """[PRD §4.3 JOB-01] 自动将 interrupted 的盘后任务转为 resume_queued。

    状态闭环：queued → running → interrupted → resume_queued → running → succeeded/failed

    [P0-3] 同时处理 after_close_orchestrator 和 after_close_chip_consensus 两类任务：
    - after_close_orchestrator：断点恢复 via metadata.last_completed_step
    - after_close_chip_consensus：断点续算 via get_pending_chip_instruments（跳过已 succeeded 的 instrument）

    此函数实现 interrupted → resume_queued 转换：
    1. 查找 status='interrupted' 且 job_name IN (after_close_orchestrator, after_close_chip_consensus) 的任务
    2. 过滤 attempt_no < _MAX_AUTO_RESUME_ATTEMPTS（超过上限不自动恢复，需人工介入）
    3. 原子 UPDATE：status → resume_queued, attempt_no + 1, error_code/message 清空
    4. 写 resume 事件（记录 attempt_no 和 last_completed_step）

    Worker 领取 resume_queued 任务时：
    - _after_close_poll_once：递增 lease_epoch（fencing）+ execute_after_close_run 读 last_completed_step 跳过已成功阶段
    - _chip_consensus_poll_once：递增 lease_epoch（fencing）+ get_pending_chip_instruments 跳过已成功 instrument

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        now: 当前时间（默认 Asia/Shanghai 当前时间）

    Returns:
        转换为 resume_queued 的任务数量

    Raises:
        Exception: 数据库执行异常向上传播（不吞异常）
    """
    if now is None:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))

    # [JOB-01 + P0-3] 原子 UPDATE：interrupted → resume_queued + attempt_no + 1
    # 同时处理 after_close_orchestrator 和 after_close_chip_consensus 两类任务
    # WHERE attempt_no < _MAX_AUTO_RESUME_ATTEMPTS 限制最大重试次数
    update_sql = text(
        """
        UPDATE scheduler_job_runs
        SET status = 'resume_queued',
            attempt_no = attempt_no + 1,
            error_code = NULL,
            error_message = NULL,
            finished_at = NULL,
            heartbeat_at = :now,
            updated_at = :now
        WHERE status = 'interrupted'
            AND job_name IN (:orchestrator_job, :chip_job)
            AND attempt_no < :max_attempts
        RETURNING id, attempt_no, metadata_json, job_name
        """
    )
    result = await db.execute(update_sql, {
        "now": now,
        "orchestrator_job": _AFTER_CLOSE_JOB_NAME,
        "chip_job": _CHIP_CONSENSUS_JOB_NAME,
        "max_attempts": _MAX_AUTO_RESUME_ATTEMPTS,
    })
    resumed_rows = result.fetchall()

    if not resumed_rows:
        return 0

    # [JOB-01] 每条恢复任务：写 resume 事件（记录 attempt_no + last_completed_step）
    for row in resumed_rows:
        job_run_id = row.id
        new_attempt_no = row.attempt_no
        job_name = row.job_name

        # 提取 last_completed_step（从 metadata_json，仅 after_close_orchestrator 有）
        last_completed_step = None
        if row.metadata_json:
            try:
                metadata = json.loads(row.metadata_json)
                last_completed_step = metadata.get("last_completed_step")
            except (json.JSONDecodeError, TypeError):
                pass

        event = JobRunEvent(
            job_run_id=job_run_id,
            step="auto_resume",
            level="info",
            message=f"自动恢复：interrupted → resume_queued (job={job_name}, attempt_no={new_attempt_no})",
            payload={
                "action": "interrupted_to_resume_queued",
                "job_name": job_name,
                "attempt_no": new_attempt_no,
                "last_completed_step": last_completed_step,
                "resumed_at": now.isoformat(),
            },
        )
        db.add(event)

    await db.flush()
    logger.info(
        "[Recovery] [JOB-01] 自动恢复 %d 个 interrupted 盘后任务 → resume_queued "
        "（含 after_close_orchestrator + after_close_chip_consensus）",
        len(resumed_rows),
    )
    return len(resumed_rows)


if __name__ == "__main__":
    # 自测入口：验证函数签名与模块导入（不连接数据库，无副作用）
    import inspect

    # 验证函数签名
    sig = inspect.signature(recover_stale_scheduler_job_runs)
    params = set(sig.parameters.keys())
    assert params == {"db", "now"}, f"参数不匹配: {params}"
    assert sig.parameters["now"].default is None, "now 默认值应为 None"
    print("recover_stale_scheduler_job_runs 签名验证 ✓")
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

    # [PRD §4.3 JOB-01] 验证 auto_resume_interrupted_after_close_runs
    sig_resume = inspect.signature(auto_resume_interrupted_after_close_runs)
    params_resume = set(sig_resume.parameters.keys())
    assert params_resume == {"db", "now"}, f"auto_resume 参数不匹配: {params_resume}"
    assert sig_resume.parameters["now"].default is None, "auto_resume now 默认值应为 None"
    assert sig_resume.return_annotation in (int, "int"), (
        f"auto_resume 返回类型应为 int, 实际: {sig_resume.return_annotation}"
    )
    print("auto_resume_interrupted_after_close_runs 签名验证 ✓")

    # 验证 JOB-01 常量
    assert _AFTER_CLOSE_JOB_NAME == "after_close_orchestrator"
    assert _CHIP_CONSENSUS_JOB_NAME == "after_close_chip_consensus"
    assert _MAX_AUTO_RESUME_ATTEMPTS == 3
    print(f"_AFTER_CLOSE_JOB_NAME={_AFTER_CLOSE_JOB_NAME} ✓")
    print(f"_CHIP_CONSENSUS_JOB_NAME={_CHIP_CONSENSUS_JOB_NAME} ✓")
    print(f"_MAX_AUTO_RESUME_ATTEMPTS={_MAX_AUTO_RESUME_ATTEMPTS} ✓")

    print("OK")
