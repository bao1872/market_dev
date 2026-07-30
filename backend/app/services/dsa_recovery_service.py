"""DSA 恢复服务 - 失败 DSA run 的正式恢复入口（P0-2）。

设计目标（ref/instruction.md §二.2）：
1. 断点恢复读取到 child DSA 为 failed/partial_failed/max_retries_exceeded 时，
   禁止把失败 run 直接改回 queued
2. 原失败 run 保留审计，创建新 DSA run
3. 原子更新 orchestrator metadata 中的 dsa_run_id 并递增恢复次数
4. running 且 lease 过期继续用现有 fencing
5. completed/published 直接复用

约束：
- 管理 API/CLI 只能调用该 service，禁止裸 SQL
- 禁止 /tmp Python、docker cp
- 新 DSA run 使用 create_batch_run 创建（自动 attempt_no 递增）

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.dsa_recovery_service
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.after_close_orchestrator import (
    _AFTER_CLOSE_JOB_NAME,
    AfterCloseRunStatus,
    _parse_metadata,
    _update_orchestrator_status,
    append_event,
)

logger = logging.getLogger("dsa_recovery_service")

# DSA run 失败状态（需要创建新 run 恢复）
_DSA_FAILED_STATUSES = frozenset({"failed", "partial_failed"})

# DSA run 成功状态（直接复用，不需要恢复）
_DSA_COMPLETED_STATUSES = frozenset({"completed", "published"})

# 最大恢复次数（防止无限恢复）
_MAX_DSA_RECOVERY_COUNT = 5


class DSARecoveryError(Exception):
    """DSA 恢复失败。"""

    pass


async def recover_failed_dsa_run(
    db: AsyncSession,
    job_run_id: uuid.UUID,
    *,
    strategy_key: str = "dsa_selector",
    run_type: str = "scheduled",
) -> tuple[StrategyRun, bool]:
    """恢复失败的 DSA run（创建新 run，不修改原 run）。

    流程：
    1. 读取 orchestrator job_run 和当前 dsa_run_id
    2. 读取 DSA run 状态
    3. 若 DSA run 为 completed/published → 直接复用，返回 (run, False)
    4. 若 DSA run 为 running 且 lease 未过期 → 拒绝恢复（正在执行）
    5. 若 DSA run 为 failed/partial_failed → 创建新 run，更新 metadata
    6. 若恢复次数超过上限 → 抛 DSARecoveryError

    Args:
        db: 异步会话（caller 控制 commit）
        job_run_id: SchedulerJobRun.id（orchestrator job）
        strategy_key: 策略 key（默认 dsa_selector）
        run_type: 运行类型（默认 scheduled）

    Returns:
        (new_dsa_run, is_new)：
        - (run, True) 表示创建了新 DSA run
        - (run, False) 表示复用已有 completed/published run

    Raises:
        DSARecoveryError: DSA run 正在执行 / 恢复次数超限 / job_run 不存在
    """
    # 1. 读取 orchestrator job_run
    job_run = await db.get(SchedulerJobRun, job_run_id)
    if job_run is None:
        raise DSARecoveryError(f"job_run 不存在: {job_run_id}")
    if job_run.job_name != _AFTER_CLOSE_JOB_NAME:
        raise DSARecoveryError(
            f"job_run 不是 after_close_orchestrator: job_name={job_run.job_name}"
        )

    meta = _parse_metadata(job_run)
    trade_date_str = meta.get("trade_date")
    if not trade_date_str:
        raise DSARecoveryError("metadata 缺少 trade_date")
    from datetime import date

    trade_date = date.fromisoformat(trade_date_str)

    old_dsa_run_id_str = meta.get("dsa_run_id")
    if not old_dsa_run_id_str:
        raise DSARecoveryError("metadata 缺少 dsa_run_id")
    old_dsa_run_id = uuid.UUID(old_dsa_run_id_str)

    recovery_count = meta.get("dsa_recovery_count", 0)

    # 2. 读取 DSA run 状态
    old_dsa_run = await db.get(StrategyRun, old_dsa_run_id)
    if old_dsa_run is None:
        raise DSARecoveryError(f"DSA run 不存在: {old_dsa_run_id}")

    logger.info(
        "[DSA-Recovery] job_run_id=%s, old_dsa_run_id=%s, status=%s, "
        "recovery_count=%d",
        job_run_id, old_dsa_run_id, old_dsa_run.status, recovery_count,
    )

    # 3. completed/published → 直接复用
    if old_dsa_run.status in _DSA_COMPLETED_STATUSES:
        logger.info(
            "[DSA-Recovery] DSA run 已完成（%s），直接复用: run_id=%s",
            old_dsa_run.status, old_dsa_run_id,
        )
        return old_dsa_run, False

    # 4. running 且 lease 未过期 → 拒绝恢复
    if old_dsa_run.status == "running":
        lease_expires = old_dsa_run.lease_expires_at
        if lease_expires is not None and lease_expires > datetime.now(UTC):
            raise DSARecoveryError(
                f"DSA run 正在执行且 lease 未过期: run_id={old_dsa_run_id}, "
                f"lease_expires_at={lease_expires}"
            )
        # lease 过期 → 继续用现有 fencing，不创建新 run
        logger.info(
            "[DSA-Recovery] DSA running 但 lease 过期，使用现有 fencing 恢复: "
            "run_id=%s",
            old_dsa_run_id,
        )
        return old_dsa_run, False

    # 5. failed/partial_failed → 创建新 run
    if old_dsa_run.status not in _DSA_FAILED_STATUSES:
        raise DSARecoveryError(
            f"DSA run 状态不支持恢复: status={old_dsa_run.status} "
            f"(仅支持 {_DSA_FAILED_STATUSES})"
        )

    # 6. 检查恢复次数
    if recovery_count >= _MAX_DSA_RECOVERY_COUNT:
        raise DSARecoveryError(
            f"DSA 恢复次数超限: recovery_count={recovery_count}, "
            f"max={_MAX_DSA_RECOVERY_COUNT}"
        )

    # 7. 创建新 DSA run（create_batch_run 自动处理 attempt_no 递增）
    from app.services.strategy_batch_service import StrategyBatchService

    batch_service = StrategyBatchService()
    new_dsa_run = await batch_service.create_batch_run(
        db,
        strategy_key=strategy_key,
        trade_date=trade_date,
        run_type=run_type,
        claim_for_worker=f"orchestrator:recovery:{job_run_id}",
    )

    # 8. 原子更新 orchestrator metadata
    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=(
            f"[DSA-Recovery] 创建新 DSA run: old={old_dsa_run_id} (failed), "
            f"new={new_dsa_run.id}, recovery_count={recovery_count + 1}"
        ),
        dsa_run_id=new_dsa_run.id,
        extra={
            "dsa_recovery_count": recovery_count + 1,
            "dsa_recovery_old_run_id": str(old_dsa_run_id),
            "dsa_recovery_new_run_id": str(new_dsa_run.id),
            "dsa_recovery_previous_status": old_dsa_run.status,
            "dsa_recovery_timestamp": datetime.now(UTC).isoformat(),
        },
    )

    await append_event(
        db=db,
        job_run_id=job_run_id,
        step="dsa_recovery",
        level="info",
        message=(
            f"DSA 恢复: old_run={old_dsa_run_id} ({old_dsa_run.status}→保留审计), "
            f"new_run={new_dsa_run.id} (queued), recovery_count={recovery_count + 1}"
        ),
        payload={
            "old_dsa_run_id": str(old_dsa_run_id),
            "new_dsa_run_id": str(new_dsa_run.id),
            "old_dsa_run_status": old_dsa_run.status,
            "recovery_count": recovery_count + 1,
            "previous_recovery_count": recovery_count,
        },
    )

    logger.info(
        "[DSA-Recovery] 新 DSA run 已创建: old=%s (%s), new=%s (queued), "
        "recovery_count=%d→%d",
        old_dsa_run_id, old_dsa_run.status, new_dsa_run.id,
        recovery_count, recovery_count + 1,
    )

    return new_dsa_run, True


async def get_dsa_recovery_status(
    db: AsyncSession,
    job_run_id: uuid.UUID,
) -> dict[str, Any]:
    """查询 DSA 恢复状态（只读）。

    Returns:
        {
            "job_run_id": str,
            "dsa_run_id": str (current),
            "dsa_run_status": str,
            "dsa_recovery_count": int,
            "dsa_recovery_old_run_ids": list[str],
            "can_recover": bool,
            "reason": str | None,
        }
    """
    job_run = await db.get(SchedulerJobRun, job_run_id)
    if job_run is None:
        raise DSARecoveryError(f"job_run 不存在: {job_run_id}")

    meta = _parse_metadata(job_run)
    dsa_run_id_str = meta.get("dsa_run_id")
    recovery_count = meta.get("dsa_recovery_count", 0)

    dsa_run_status = None
    can_recover = False
    reason = None

    if dsa_run_id_str:
        dsa_run = await db.get(StrategyRun, uuid.UUID(dsa_run_id_str))
        if dsa_run is not None:
            dsa_run_status = dsa_run.status
            if dsa_run.status in _DSA_FAILED_STATUSES:
                if recovery_count >= _MAX_DSA_RECOVERY_COUNT:
                    can_recover = False
                    reason = f"恢复次数超限: {recovery_count}/{_MAX_DSA_RECOVERY_COUNT}"
                else:
                    can_recover = True
                    reason = None
            elif dsa_run.status == "running":
                lease_expires = dsa_run.lease_expires_at
                if lease_expires is not None and lease_expires > datetime.now(UTC):
                    can_recover = False
                    reason = f"正在执行，lease 未过期: {lease_expires}"
                else:
                    can_recover = False
                    reason = "running 但 lease 过期，使用现有 fencing 恢复"
            else:
                can_recover = False
                reason = f"DSA run 状态为 {dsa_run.status}，无需恢复"

    # 收集所有历史恢复记录
    old_run_ids: list[str] = []
    recovery_prefix = "dsa_recovery_old_run_id"
    for key, value in meta.items():
        if key.startswith(recovery_prefix):
            old_run_ids.append(str(value))

    return {
        "job_run_id": str(job_run_id),
        "dsa_run_id": dsa_run_id_str,
        "dsa_run_status": dsa_run_status,
        "dsa_recovery_count": recovery_count,
        "dsa_recovery_old_run_ids": old_run_ids,
        "can_recover": can_recover,
        "reason": reason,
    }


if __name__ == "__main__":
    # 模块自测
    assert _DSA_FAILED_STATUSES == frozenset({"failed", "partial_failed"})
    assert _DSA_COMPLETED_STATUSES == frozenset({"completed", "published"})
    assert _MAX_DSA_RECOVERY_COUNT == 5
    print("OK: dsa_recovery_service interface verified")
    print(f"Failed statuses: {_DSA_FAILED_STATUSES}")
    print(f"Completed statuses: {_DSA_COMPLETED_STATUSES}")
    print(f"Max recovery count: {_MAX_DSA_RECOVERY_COUNT}")
