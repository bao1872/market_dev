"""盘后筹码共识独立任务服务（[CHANGE-20260729-003] 核心与筹码解耦）。

本模块定义独立 `after_close_chip_consensus` job 的接口与状态合同。

设计目标（PRD20 盘后核心/筹码解耦）：
1. **核心发布成功即标记主 run succeeded**：after_close_orchestrator 关键路径
   日线 → core 个股状态/事件 → 质量门禁 → 发布，core 发布成功即可复盘
2. **chip 任务后置非阻塞**：发布后创建独立 `after_close_chip_consensus` job，
   不 await、不加入主 run 成功门禁
3. **chip 可独立失败/重试**：chip 任务失败/部分成功/单独重试，绝不反改主 run 或重算 core
4. **chip 使用独立 version/hash/run 关联**：chip 计算边界由
   `first_pyramid_service.compute_chip_consensus_snapshot` 提供

[下一阶段唯一 blocker] chip 持久化 migration：
- 本轮**仅完成计算边界、独立 job 接口/状态合同和文档**
- 禁止修改已发布 core snapshot
- 禁止用 Redis 冒充持久化
- 禁止未经验证新增 migration
- chip 结果持久化表/migration 列为下一阶段唯一 blocker

状态合同（status）：
    queued → running → succeeded（全部 instrument chip 计算成功）
                     → partial（部分成功，可单独重试失败项）
                     → failed（全部失败或不可恢复错误）
    running → interrupted（watchdog 检测 lease 过期）
    interrupted → resume_queued（auto-resume，仅重试未成功项）

幂等键：
    run_key = "after_close_chip_consensus:{trade_date}"

模块自测：
    python -m app.services.after_close_chip_consensus_service
"""
from __future__ import annotations

import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.idempotency_service import acquire_job_run_lock

logger = logging.getLogger(__name__)

# =============================================================================
# 常量与状态合同
# =============================================================================

# 独立 job 名称（与 after_close_orchestrator 区分）
CHIP_CONSENSUS_JOB_NAME = "after_close_chip_consensus"

# 租约时长（chip 计算可能较慢，给予更长时间）
_CHIP_LEASE_SECONDS = 3600  # 1 小时

# chip 任务状态合同（扩展 SchedulerJobRun.status）
# queued/running/succeeded/failed/interrupted/resume_queued 复用现有状态机
# "partial" 为 chip 专属状态：部分 instrument 成功，可单独重试失败项
CHIP_STATUS_QUEUED = "queued"
CHIP_STATUS_RUNNING = "running"
CHIP_STATUS_SUCCEEDED = "succeeded"
CHIP_STATUS_PARTIAL = "partial"  # chip 专属：部分成功
CHIP_STATUS_FAILED = "failed"
CHIP_STATUS_INTERRUPTED = "interrupted"
CHIP_STATUS_RESUME_QUEUED = "resume_queued"


# =============================================================================
# Job 创建（幂等）
# =============================================================================


async def create_after_close_chip_consensus_job(
    db: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
    *,
    instrument_ids: list[uuid.UUID] | None = None,
) -> tuple[SchedulerJobRun, bool]:
    """[CHANGE-20260729-003] 创建盘后筹码共识独立任务（幂等）。

    在 after_close_orchestrator 主 run 标记 succeeded 后调用。
    本函数只创建任务记录，不 await 执行（执行由独立 Worker 领取）。

    幂等：同 trade_date 已有 queued/running/resume_queued 任务则返回已有。

    Args:
        db: 异步会话
        trade_date: 交易日期
        core_run_id: 关联的 after_close 主 run id（用于追溯 core 发布）
        instrument_ids: 待计算 chip 的 instrument 列表（None 表示全部 A 股）

    Returns:
        (SchedulerJobRun, is_new)：
        - is_new=True 表示本次新建任务
        - is_new=False 表示同日已有活跃任务，返回已有记录

    Raises:
        RuntimeError: 幂等锁获取失败且未返回已有记录
    """
    run_key = f"{CHIP_CONSENSUS_JOB_NAME}:{trade_date.isoformat()}"
    metadata: dict[str, Any] = {
        "chip_status": CHIP_STATUS_QUEUED,
        "trade_date": trade_date.isoformat(),
        "core_run_id": str(core_run_id),
        "instrument_count": len(instrument_ids) if instrument_ids else None,
    }
    if instrument_ids is not None:
        metadata["instrument_ids"] = [str(i) for i in instrument_ids]

    job_run, is_new = await acquire_job_run_lock(
        db=db,
        run_key=run_key,
        job_name=CHIP_CONSENSUS_JOB_NAME,
        business_date=trade_date.isoformat(),
        lease_seconds=_CHIP_LEASE_SECONDS,
        metadata=metadata,
        initial_status="queued",
    )
    if not is_new:
        if job_run is not None:
            logger.info(
                "[ChipConsensus] 同日已有 chip 任务，返回已有: run_id=%s, status=%s",
                job_run.id, job_run.status,
            )
            return job_run, False
        raise RuntimeError(
            f"acquire_job_run_lock 抢锁失败且未返回已有记录: run_key={run_key}"
        )

    if job_run is None:
        raise RuntimeError(
            f"acquire_job_run_lock 返回 is_new=True 但 job_run=None: run_key={run_key}"
        )

    await db.commit()
    logger.info(
        "[ChipConsensus] 创建 chip 任务: run_id=%s, trade_date=%s, core_run_id=%s",
        job_run.id, trade_date, core_run_id,
    )
    return job_run, is_new


# =============================================================================
# Job 查询
# =============================================================================


async def get_chip_consensus_job_for_date(
    db: AsyncSession,
    trade_date: date,
) -> SchedulerJobRun | None:
    """查询指定 trade_date 的 chip consensus 任务（取最新一条）。"""
    run_key = f"{CHIP_CONSENSUS_JOB_NAME}:{trade_date.isoformat()}"
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# =============================================================================
# Job 执行（接口合同 - 下一阶段实现）
# =============================================================================


async def execute_after_close_chip_consensus(
    job_run_id: uuid.UUID,
    trade_date: date,
    core_run_id: uuid.UUID,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> None:
    """[CHANGE-20260729-003] 执行盘后筹码共识独立任务。

    **接口合同（本轮仅定义，执行实现为下一阶段 blocker）**。

    执行流程（目标设计，下一阶段实现）：
    1. 读取 metadata.instrument_ids（或查询全部 A 股）
    2. 对每个 instrument：
       a. 获取 daily + 15m bars（point-in-time <= trade_date）
       b. 调用 `first_pyramid_service.compute_chip_consensus_snapshot`
       c. 持久化 chip 结果（**下一阶段 blocker：chip 持久化 migration**）
    3. 统计 succeeded/failed/partial
    4. 标记 job 状态：全成功→succeeded，部分成功→partial，全失败→failed
    5. 写入 metadata（chip_results_summary）

    约束：
    - chip 失败不反改主 run（after_close_orchestrator）状态
    - chip 失败不重算 core snapshot
    - chip 可单独重试失败项（resume_queued 状态）
    - 禁止用 Redis 冒充持久化

    Raises:
        NotImplementedError: 本轮仅定义接口合同，执行实现为下一阶段 blocker
    """
    raise NotImplementedError(
        "[CHANGE-20260729-003] execute_after_close_chip_consensus 接口合同已定义，"
        "执行实现为下一阶段唯一 blocker：chip 持久化 migration。"
        "本轮仅完成计算边界（compute_chip_consensus_snapshot）、"
        "独立 job 接口/状态合同和文档。"
    )


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    # 验证常量与状态合同
    assert CHIP_CONSENSUS_JOB_NAME == "after_close_chip_consensus"
    assert CHIP_STATUS_PARTIAL == "partial"
    assert CHIP_STATUS_SUCCEEDED == "succeeded"
    assert CHIP_STATUS_FAILED == "failed"
    # 验证函数签名
    import inspect
    sig_create = inspect.signature(create_after_close_chip_consensus_job)
    assert "core_run_id" in sig_create.parameters
    assert "trade_date" in sig_create.parameters
    sig_exec = inspect.signature(execute_after_close_chip_consensus)
    assert "core_run_id" in sig_exec.parameters
    assert "job_run_id" in sig_exec.parameters
    print(f"OK: {CHIP_CONSENSUS_JOB_NAME} interface contract verified")
    print("Status contract: queued/running/succeeded/partial/failed/interrupted/resume_queued")
    print("Next stage blocker: chip 持久化 migration")
