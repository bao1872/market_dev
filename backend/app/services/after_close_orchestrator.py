"""盘后编排服务 - 串联日线刷新 → DSA 选股 → 质量门禁 → 特征快照 → 发布的全流水线。

核心函数：
- create_after_close_run(db, trade_date): 创建盘后编排任务（幂等）
- execute_after_close_run(job_run_id, trade_date, ...): 执行盘后流水线（后台异步）
- get_after_close_run_status(db, job_run_id): 查询编排状态 + 事件时间线

设计说明：
- 编排任务以 SchedulerJobRun 记录（job_name="after_close_orchestrator"），
  orchestrator_status 存储在 metadata_json（JSON 字符串），与 SchedulerJobRun.status
  （running/succeeded/failed 表示整体任务状态）区分
- 每个步骤切换时写 job_run_event（step=状态名），便于前端时间线展示
- execute_after_close_run 使用独立 AsyncSessionLocal，不依赖 HTTP 请求 session
- 调用现有服务不重新实现：BarsSchedulerService.refresh_all_instruments /
  StrategyBatchService._check_quality_gates / StrategyBatchService.publish_run /
  feature_snapshot_service.compute_for_trade_date
- DSA Worker 异步执行，编排层轮询 StrategyRun.status 直到 completed/failed/超时

状态机：
queued → refreshing_daily → checking_coverage → creating_dsa
  → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → succeeded
任意步骤异常 → failed

禁异常吞没：所有异常补充上下文后 re-raise 或写入 ERROR 事件后标记 failed。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.repositories import strategy_result_repository
from app.services.bars_scheduler_service import BarsSchedulerService
from app.services.feature_snapshot_service import (
    compute_for_trade_date,
    create_snapshot_run,
    finish_snapshot_run,
    get_active_a_share_instruments,
)
from app.services.idempotency_service import acquire_job_run_lock
from app.services.job_run_event_service import append_event, list_events
from app.services.strategy_batch_service import StrategyBatchService

logger = logging.getLogger("after_close_orchestrator")

# [AfterClose] - 编排任务名称（区别于 bars_scheduler / strategy_batch_worker）
_AFTER_CLOSE_JOB_NAME = "after_close_orchestrator"

# [AfterClose] - DSA Worker 完成等待轮询间隔（秒）
_DSA_POLL_INTERVAL_SECONDS = 30

# [AfterClose] - DSA Worker 完成等待超时（秒，默认 2 小时）
_DSA_POLL_TIMEOUT_SECONDS = 7200

# [AfterClose] - 编排任务租约时长（秒，需覆盖全流水线 2h+）
_ORCHESTRATOR_LEASE_SECONDS = 14400


class AfterCloseRunStatus(StrEnum):
    """盘后编排流水线状态枚举。

    状态流转：
    queued → refreshing_daily → checking_coverage → creating_dsa
      → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → succeeded
    任意步骤异常 → failed
    """

    QUEUED = "queued"
    REFRESHING_DAILY = "refreshing_daily"
    CHECKING_COVERAGE = "checking_coverage"
    CREATING_DSA = "creating_dsa"
    WAITING_DSA_WORKER = "waiting_dsa_worker"
    QUALITY_GATE = "quality_gate"
    FEATURE_SNAPSHOT = "feature_snapshot"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


def _build_metadata(
    trade_date: date,
    orchestrator_status: AfterCloseRunStatus,
    dsa_run_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """[AfterClose] - 构造 metadata_json 字符串。"""
    payload: dict[str, Any] = {
        "orchestrator_status": orchestrator_status.value,
        "trade_date": trade_date.isoformat(),
    }
    if dsa_run_id is not None:
        payload["dsa_run_id"] = str(dsa_run_id)
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _parse_metadata(job_run: SchedulerJobRun) -> dict[str, Any]:
    """[AfterClose] - 解析 metadata_json 为 dict（空/异常时返回空 dict）。"""
    if not job_run.metadata_json:
        return {}
    try:
        return json.loads(job_run.metadata_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "[AfterClose] metadata_json 解析失败 job_run_id=%s: %s",
            job_run.id, exc,
        )
        return {}


async def _get_job_run_or_raise(
    db: AsyncSession,
    job_run_id: uuid.UUID,
) -> SchedulerJobRun:
    """获取 SchedulerJobRun，不存在则抛 RuntimeError（类型收窄 helper）。"""
    job_run = await db.get(SchedulerJobRun, job_run_id)
    if job_run is None:
        raise RuntimeError(f"SchedulerJobRun not found: {job_run_id}")
    return job_run


async def _get_strategy_run_or_raise(
    db: AsyncSession,
    run_id: uuid.UUID,
) -> StrategyRun:
    """获取 StrategyRun，不存在则抛 ValueError（类型收窄 helper）。"""
    run = await db.get(StrategyRun, run_id)
    if run is None:
        raise ValueError(f"StrategyRun not found: {run_id}")
    return run


async def _update_orchestrator_status(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    status: AfterCloseRunStatus,
    message: str = "",
    payload: dict[str, Any] | None = None,
    dsa_run_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """[AfterClose] - 更新编排状态：写 metadata_json + 写 job_run_event（flush 不 commit）。

    Args:
        db: 异步会话
        job_run: SchedulerJobRun 记录（已在 session 中）
        status: 目标编排状态
        message: 事件消息
        payload: 事件 payload
        dsa_run_id: 可选的 DSA run_id（写入 metadata_json）
        extra: 额外 metadata 字段
    """
    # 保留已有 metadata 中的字段（如 trade_date），仅更新 orchestrator_status
    existing_meta = _parse_metadata(job_run)
    trade_date_str = existing_meta.get("trade_date")
    if dsa_run_id is None:
        dsa_run_id_str = existing_meta.get("dsa_run_id")
        dsa_run_id = uuid.UUID(dsa_run_id_str) if dsa_run_id_str else None

    # trade_date 优先用已有 metadata，其次用 extra
    if trade_date_str is None and extra and "trade_date" in extra:
        trade_date_str = extra["trade_date"]

    # 构造新 metadata：保留已有字段，只更新本次涉及的字段
    new_meta: dict[str, Any] = dict(existing_meta)
    new_meta["orchestrator_status"] = status.value
    if trade_date_str is not None:
        new_meta["trade_date"] = trade_date_str
    if dsa_run_id is not None:
        new_meta["dsa_run_id"] = str(dsa_run_id)
    if extra:
        for k, v in extra.items():
            if k not in ("orchestrator_status", "trade_date", "dsa_run_id"):
                new_meta[k] = v

    job_run.metadata_json = json.dumps(new_meta, ensure_ascii=False)
    await db.flush()

    # 写事件（step=状态名，便于前端按步骤展示）
    event_payload = dict(payload) if payload else {}
    event_payload["orchestrator_status"] = status.value
    await append_event(
        db=db,
        job_run_id=job_run.id,
        step=status.value,
        level="info" if status != AfterCloseRunStatus.FAILED else "error",
        message=message or f"编排状态切换: {status.value}",
        payload=event_payload,
    )
    await db.flush()


async def create_after_close_run(
    db: AsyncSession,
    trade_date: date,
) -> tuple[SchedulerJobRun, bool]:
    """创建盘后编排任务（幂等：同 trade_date 已有 running/succeeded 则返回已有）。

    流程：
    1. 构造 run_key = after_close_orchestrator:{trade_date}
    2. acquire_job_run_lock 获取任务执行权（幂等）
    3. 写入 metadata_json（orchestrator_status=queued）
    4. 写入 START 事件
    5. commit 并返回 SchedulerJobRun + is_new

    Args:
        db: 异步会话
        trade_date: 交易日期

    Returns:
        (SchedulerJobRun, is_new)：
        - is_new=True 表示本次新建任务（status=queued, orchestrator_status=queued），
          由独立 Worker 领取执行
        - is_new=False 表示同日已有任务，返回已有记录（调用方应返回 409 Conflict）

    Raises:
        RuntimeError: 幂等锁获取失败（同日已有运行中任务）且未找到已有记录
    """
    run_key = f"{_AFTER_CLOSE_JOB_NAME}:{trade_date.isoformat()}"
    # [AfterClose] - acquire_job_run_lock 返回 (job_run, is_new)：
    # - is_new=True：新建任务（status=queued），由独立 Worker 领取执行
    # - is_new=False：已有活跃任务(existing)或抢锁失败(None)，返回 (existing, False) 或抛异常
    # [Phase5] - initial_status=queued：API 仅创建 queued 任务，不直接执行，
    # 由 run_after_close_orchestrator_worker 领取后改为 running
    job_run, is_new = await acquire_job_run_lock(
        db=db,
        run_key=run_key,
        job_name=_AFTER_CLOSE_JOB_NAME,
        business_date=trade_date.isoformat(),
        lease_seconds=_ORCHESTRATOR_LEASE_SECONDS,
        metadata={
            "orchestrator_status": AfterCloseRunStatus.QUEUED.value,
            "trade_date": trade_date.isoformat(),
        },
        initial_status="queued",
    )
    if not is_new:
        # acquire_job_run_lock 已返回 existing（或 None 表示抢锁失败）
        if job_run is not None:
            logger.info(
                "[AfterClose] 同日已有编排任务，返回已有: run_id=%s, status=%s",
                job_run.id, job_run.status,
            )
            return job_run, False
        # 抢锁失败（IntegrityError）且未返回已有记录
        raise RuntimeError(
            f"acquire_job_run_lock 抢锁失败且未返回已有记录: run_key={run_key}"
        )

    # is_new=True 时 job_run 必须存在，显式收窄类型
    if job_run is None:
        raise RuntimeError(
            f"acquire_job_run_lock 返回 is_new=True 但 job_run=None: run_key={run_key}"
        )

    # 写入初始 metadata + START 事件
    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=f"盘后编排已创建: trade_date={trade_date}",
        extra={"trade_date": trade_date.isoformat()},
    )
    await db.commit()

    logger.info(
        "[AfterClose] 创建盘后编排任务: run_id=%s, trade_date=%s",
        job_run.id, trade_date,
    )
    return job_run, True


async def compute_daily_coverage(
    db: AsyncSession,
    trade_date: date,
) -> tuple[int, int, float]:
    """[AfterClose] - 计算当日日线覆盖率（纯查询，无 DSA 触发副作用）。

    口径与 BarsSchedulerService._check_daily_coverage_and_trigger_dsa 对齐：
    - 覆盖数：bars_daily 表中 trade_date 当日不同 instrument_id 数
    - 总数：instruments 表中 status='active' 且为 A 股股票代码的标的数
      （排除指数/基金/ETF，因为这些标的不写入 bars_daily，计入分母会导致覆盖率虚低）
    - 覆盖率 = covered / total（total=0 时返 0.0）

    [Bugfix] - 描述: 本函数作为历史兼容 wrapper，内部复用 BarsCoverageService 统一 SQL，
    禁止复制覆盖率查询。

    Args:
        db: 异步会话
        trade_date: 交易日期

    Returns:
        (covered, total, coverage)：覆盖数、活跃股票总数、覆盖率（0.0-1.0）
    """
    from app.services.bars_coverage_service import BarsCoverageService

    result = await BarsCoverageService.compute_daily_coverage(db, trade_date)
    return result["covered"], result["total"], result["coverage"]


async def _update_heartbeat_and_step(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    last_completed_step: str,
    worker_id: str | None = None,
) -> None:
    """[Phase5] - 更新 heartbeat + lease + metadata.last_completed_step（flush 不 commit）。

    每个阶段完成后调用，用于：
    - 断点恢复：下次重启时根据 last_completed_step 跳过已成功阶段
    - 心跳租约：防止 Admin 页面误判任务卡死或租约过期

    Args:
        db: 异步会话
        job_run: SchedulerJobRun 记录（已在 session 中）
        last_completed_step: 刚完成的阶段名（AfterCloseRunStatus.value）
        worker_id: Worker 实例标识（非 None 时同步更新 worker_instance_id）
    """
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.heartbeat_at = now
    job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)
    if worker_id is not None:
        job_run.worker_instance_id = worker_id
    meta = _parse_metadata(job_run)
    # 保留已有 metadata（含 feature_snapshot_progress / feature_snapshot_run_id 等），
    # 仅更新 last_completed_step。
    meta["last_completed_step"] = last_completed_step
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db.flush()


async def _job_run_heartbeat_loop(
    job_run_id: uuid.UUID,
    worker_id: str | None = None,
    interval: int = 30,
) -> None:
    """[AfterClose] - 后台心跳任务：定期更新 heartbeat_at + lease_expires_at。

    用于长阶段（如 refresh_all_instruments 约13分钟）期间防止 watchdog 误判 stale。
    被取消时安静退出（CancelledError 不传播）。

    Args:
        job_run_id: 编排任务 ID
        worker_id: Worker 实例标识（非 None 时同步更新 worker_instance_id）
        interval: 心跳间隔（秒，默认 30）
    """
    while True:
        try:
            await asyncio.sleep(interval)
            async with AsyncSessionLocal() as db:
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is None or job_run.status != "running":
                    return
                job_run.heartbeat_at = now
                job_run.lease_expires_at = now + timedelta(
                    seconds=_ORCHESTRATOR_LEASE_SECONDS,
                )
                if worker_id is not None:
                    job_run.worker_instance_id = worker_id
                await db.commit()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "[AfterClose] 心跳更新失败 job_run_id=%s: %s",
                job_run_id, exc,
            )


# [Heartbeat] - feature_snapshot 进度事件采样间隔（instrument 数）
_FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL = 500


def _build_feature_snapshot_progress_callback(
    job_run_id: uuid.UUID,
    worker_id: str | None = None,
) -> Callable[..., Awaitable[None]]:
    """[Heartbeat] - 构造 feature_snapshot 阶段进度回调。

    每处理完一个 batch 调用，更新 orchestrator job_run 的心跳、lease 与 metadata 进度。
    每 _FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL 只股票写一次 info 事件，避免事件表膨胀。
    """
    last_event_processed = 0

    async def _callback(*, processed: int, total: int, snapshot_count: int, failed_count: int) -> None:
        nonlocal last_event_processed
        try:
            async with AsyncSessionLocal() as db:
                now = datetime.now(ZoneInfo("Asia/Shanghai"))
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is None or job_run.status != "running":
                    return
                job_run.heartbeat_at = now
                job_run.lease_expires_at = now + timedelta(
                    seconds=_ORCHESTRATOR_LEASE_SECONDS,
                )
                if worker_id is not None:
                    job_run.worker_instance_id = worker_id

                # 更新 metadata 中的进度（保留其他字段）
                meta = _parse_metadata(job_run)
                meta["feature_snapshot_progress"] = {
                    "processed": processed,
                    "total": total,
                    "snapshot_count": snapshot_count,
                    "failed_count": failed_count,
                    "updated_at": now.isoformat(),
                }
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                await db.commit()

                # 每阈值只股票写一次事件，避免每只股票都写事件
                if processed - last_event_processed >= _FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL:
                    await append_event(
                        db=db,
                        job_run_id=job_run_id,
                        step=AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
                        level="info",
                        message=(
                            f"feature_snapshot 进度: processed={processed}/{total}, "
                            f"snapshot_count={snapshot_count}, failed_count={failed_count}"
                        ),
                        payload={
                            "processed": processed,
                            "total": total,
                            "snapshot_count": snapshot_count,
                            "failed_count": failed_count,
                        },
                    )
                    last_event_processed = processed
        except Exception as exc:
            logger.warning(
                "[AfterClose] feature_snapshot 进度回调失败 job_run_id=%s: %s",
                job_run_id, exc,
            )

    return _callback


# [Repair] - 修复因 orchestrator 中断/失败而 stuck 的 running snapshot run
_REPAIR_STALE_THRESHOLD_SECONDS = 300
_REPAIR_SUCCESS_RATE_THRESHOLD = 0.95


async def repair_stale_after_close_snapshot_runs(
    db: AsyncSession,
    *,
    stale_threshold_seconds: int = _REPAIR_STALE_THRESHOLD_SECONDS,
    success_rate_threshold: float = _REPAIR_SUCCESS_RATE_THRESHOLD,
) -> list[dict[str, Any]]:
    """[Repair] 修复因 after_close_orchestrator 中断或失败而 stuck 的 running snapshot run。

    触发条件：
    - 存在 status='interrupted' 或 'failed' 的 after_close_orchestrator job_run
    - 同 trade_date 存在 run_type='after_close' 且 status='running' 的 snapshot run
    - 该 snapshot run 的 started_at 距离 now 超过 stale_threshold_seconds

    修复策略：
    - 若 stock_feature_snapshots 中该 trade_date 的实际行数 >= expected_count * success_rate_threshold，
      则标记 snapshot run 为 succeeded 并写入 published_at（允许 watchlist 读取）。
    - 否则标记为 failed，metadata 写入 reason='orchestrator_interrupted_or_lease_expired'。

    返回：
        被修复的 snapshot run 列表，每项含 snapshot_run_id / trade_date / action / reason。
    """
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import (
        RUN_TYPE_AFTER_CLOSE,
        STATUS_FAILED,
        STATUS_RUNNING,
        STATUS_SUCCEEDED,
        StockFeatureSnapshotRun,
    )

    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    repaired: list[dict[str, Any]] = []

    # 1. 找出近期中断/失败的 after_close_orchestrator job_run
    job_run_stmt = select(SchedulerJobRun).where(
        SchedulerJobRun.job_name == _AFTER_CLOSE_JOB_NAME,
        SchedulerJobRun.status.in_(("interrupted", "failed")),
    )
    job_runs_result = await db.execute(job_run_stmt)
    broken_job_runs = job_runs_result.scalars().all()

    for job_run in broken_job_runs:
        meta = _parse_metadata(job_run)
        trade_date_str = meta.get("trade_date")
        if not trade_date_str:
            continue
        try:
            trade_date = date.fromisoformat(trade_date_str)
        except ValueError:
            logger.warning(
                "[Repair] metadata 中 trade_date 格式非法: job_run_id=%s, value=%r",
                job_run.id, trade_date_str,
            )
            continue

        # 2. 查找同 trade_date 的 running after_close snapshot run
        snapshot_stmt = select(StockFeatureSnapshotRun).where(
            StockFeatureSnapshotRun.trade_date == trade_date,
            StockFeatureSnapshotRun.run_type == RUN_TYPE_AFTER_CLOSE,
            StockFeatureSnapshotRun.status == STATUS_RUNNING,
        )
        snapshot_result = await db.execute(snapshot_stmt)
        snapshot_runs = snapshot_result.scalars().all()

        for snapshot_run in snapshot_runs:
            started_at = snapshot_run.started_at or snapshot_run.created_at
            if started_at is None:
                continue
            # 统一时区后再比较（created_at 可能为 tz-aware）
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            stale_seconds = (now - started_at).total_seconds()
            if stale_seconds < stale_threshold_seconds:
                logger.info(
                    "[Repair] snapshot run 未超时，跳过: run_id=%s, stale_seconds=%s",
                    snapshot_run.id, stale_seconds,
                )
                continue

            # 3. 统计实际 snapshot 行数
            count_stmt = select(func.count()).select_from(StockFeatureSnapshot).where(
                StockFeatureSnapshot.trade_date == trade_date,
            )
            actual_count = (await db.execute(count_stmt)).scalar() or 0
            expected_count = snapshot_run.expected_count or 0
            success_rate = actual_count / expected_count if expected_count > 0 else 0.0

            if expected_count > 0 and success_rate >= success_rate_threshold:
                # 足够多的 snapshots：标记 succeeded 并发布
                await finish_snapshot_run(
                    db, snapshot_run,
                    status=STATUS_SUCCEEDED,
                    snapshot_count=actual_count,
                    failed_count=expected_count - actual_count,
                    expected_count=expected_count,
                    metadata={
                        "source": "after_close_orchestrator",
                        "scope": "full",
                        "repair_reason": "orchestrator_interrupted_or_lease_expired",
                        "repaired_at": now.isoformat(),
                    },
                )
                repaired.append({
                    "snapshot_run_id": str(snapshot_run.id),
                    "trade_date": trade_date.isoformat(),
                    "action": "succeeded",
                    "reason": "orchestrator_interrupted_or_lease_expired",
                    "actual_count": actual_count,
                    "expected_count": expected_count,
                    "success_rate": success_rate,
                })
                logger.info(
                    "[Repair] snapshot run 修复为 succeeded: run_id=%s, "
                    "actual=%s, expected=%s, rate=%.2f",
                    snapshot_run.id, actual_count, expected_count, success_rate,
                )
            else:
                # snapshots 不足：标记 failed
                await finish_snapshot_run(
                    db, snapshot_run,
                    status=STATUS_FAILED,
                    snapshot_count=actual_count,
                    failed_count=expected_count - actual_count,
                    expected_count=expected_count,
                    metadata={
                        "source": "after_close_orchestrator",
                        "scope": "full",
                        "reason": "orchestrator_interrupted_or_lease_expired",
                        "repaired_at": now.isoformat(),
                    },
                )
                repaired.append({
                    "snapshot_run_id": str(snapshot_run.id),
                    "trade_date": trade_date.isoformat(),
                    "action": "failed",
                    "reason": "orchestrator_interrupted_or_lease_expired",
                    "actual_count": actual_count,
                    "expected_count": expected_count,
                    "success_rate": success_rate,
                })
                logger.info(
                    "[Repair] snapshot run 修复为 failed: run_id=%s, "
                    "actual=%s, expected=%s, rate=%.2f",
                    snapshot_run.id, actual_count, expected_count, success_rate,
                )

    return repaired


async def execute_after_close_run(
    job_run_id: uuid.UUID,
    trade_date: date,
    *,
    worker_id: str | None = None,
    dsa_poll_interval: int = _DSA_POLL_INTERVAL_SECONDS,
    dsa_poll_timeout: int = _DSA_POLL_TIMEOUT_SECONDS,
) -> None:
    """执行盘后编排流水线（后台异步，使用独立 AsyncSession）。

    [Phase5] 支持断点恢复 + 心跳租约：
    - 函数开头读取 metadata.last_completed_step，跳过已成功阶段
    - 每阶段完成后调用 _update_heartbeat_and_step 更新心跳 + lease + 检查点
    - worker_id 非 None 时同步更新 worker_instance_id

    流程：
    1. refreshing_daily: 调用 BarsSchedulerService.refresh_all_instruments
       - 内部完成日线刷新 + 覆盖率检查 + DSA 触发（写 DAILY_DONE/DSA_CREATED 事件）
       - 返回 BatchResult（含 dsa_run_id）
    2. waiting_dsa_worker: 轮询 DSA StrategyRun.status 直到 completed/failed/超时
    3. quality_gate: 调用 StrategyBatchService._check_quality_gates
    4. publishing: 调用 StrategyBatchService.publish_run
    5. succeeded: 标记整体任务成功

    断点恢复（按 last_completed_step 跳过）：
    - None/queued → 从 refreshing_daily 开始
    - refreshing_daily → 跳过日线刷新，dsa_run_id 从 metadata 读取
    - waiting_dsa_worker → 跳过等待，直接质量门禁
    - quality_gate → 跳过质量门禁，直接发布
    - publishing/succeeded → 任务已完成，直接返回

    任意步骤异常 → 写 ERROR 事件 + 标记 failed + 更新 SchedulerJobRun.status=failed

    Args:
        job_run_id: 编排任务 ID
        trade_date: 交易日期
        worker_id: Worker 实例标识（非 None 时更新 worker_instance_id + 心跳）
        dsa_poll_interval: DSA 轮询间隔（秒，测试时可缩短）
        dsa_poll_timeout: DSA 轮询超时（秒，测试时可缩短）

    Raises:
        异常向上传播（调用方应捕获并记录日志）
    """
    logger.info(
        "[AfterClose] 开始执行盘后编排: job_run_id=%s, trade_date=%s, worker_id=%s",
        job_run_id, trade_date, worker_id,
    )

    bars_service = BarsSchedulerService()
    batch_service = StrategyBatchService()
    dsa_run_id: uuid.UUID | None = None
    published_run: Any = None

    try:
        # [Phase5] - 读取断点恢复信息：last_completed_step + dsa_run_id
        async with AsyncSessionLocal() as db:
            job_run = await _get_job_run_or_raise(db, job_run_id)
            if job_run is None:
                raise ValueError(f"编排任务不存在: job_run_id={job_run_id}")
            if job_run.status == "succeeded":
                logger.info("[AfterClose] 任务已成功，跳过: job_run_id=%s", job_run_id)
                return

            meta = _parse_metadata(job_run)
            last_completed_step = meta.get("last_completed_step")
            dsa_run_id_str = meta.get("dsa_run_id")
            if dsa_run_id_str:
                dsa_run_id = uuid.UUID(dsa_run_id_str)

        # [Repair] - 启动前修复上一次中断留下的 stuck running snapshot run，
        # 避免同 trade_date 的 running run 触发 partial unique index 冲突。
        try:
            async with AsyncSessionLocal() as db:
                repaired = await repair_stale_after_close_snapshot_runs(db)
                if repaired:
                    logger.info(
                        "[AfterClose] 启动前修复 %s 个 stuck snapshot run: %s",
                        len(repaired), repaired,
                    )
        except Exception as exc:
            logger.warning(
                "[AfterClose] 启动前 repair 失败，继续执行: %s", exc,
            )

        # [Phase5] - 根据last_completed_step 计算各阶段跳过标志
        # 阶段顺序：refreshing_daily → waiting_dsa_worker → quality_gate
        #   → feature_snapshot → publishing → succeeded
        _completed_steps: dict[str | None, set[str]] = {
            None: set(),
            "queued": set(),
            "refreshing_daily": {"refreshing_daily"},
            "waiting_dsa_worker": {"refreshing_daily", "waiting_dsa_worker"},
            "quality_gate": {"refreshing_daily", "waiting_dsa_worker", "quality_gate"},
            "feature_snapshot": {
                "refreshing_daily", "waiting_dsa_worker", "quality_gate",
                "feature_snapshot",
            },
            "publishing": {
                "refreshing_daily", "waiting_dsa_worker", "quality_gate",
                "feature_snapshot", "publishing",
            },
            "succeeded": {
                "refreshing_daily", "waiting_dsa_worker", "quality_gate",
                "feature_snapshot", "publishing", "succeeded",
            },
        }
        completed: set[str] = _completed_steps.get(last_completed_step, set())
        if "succeeded" in completed:
            logger.info(
                "[AfterClose] 断点恢复: 已完成 succeeded，直接返回: job_run_id=%s",
                job_run_id,
            )
            return

        # [Phase6] - dsa_only 模式：跳过日线刷新（覆盖率已由 API 层校验）
        mode = meta.get("mode")
        if mode == "dsa_only":
            completed = completed | {"refreshing_daily"}
            logger.info(
                "[AfterClose] dsa_only 模式: 强制跳过 refreshing_daily: job_run_id=%s",
                job_run_id,
            )

        skip_refresh = "refreshing_daily" in completed
        skip_wait = "waiting_dsa_worker" in completed
        skip_quality = "quality_gate" in completed
        skip_snapshot = "feature_snapshot" in completed
        skip_publish = "publishing" in completed

        logger.info(
            "[AfterClose] 断点恢复: last_completed_step=%s, "
            "skip_refresh=%s, skip_wait=%s, skip_quality=%s, "
            "skip_snapshot=%s, skip_publish=%s",
            last_completed_step, skip_refresh, skip_wait, skip_quality,
            skip_snapshot, skip_publish,
        )

        # ---- 步骤 1: refreshing_daily ----
        if not skip_refresh:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.REFRESHING_DAILY,
                    message=f"开始刷新日线: trade_date={trade_date}",
                )
                await db.commit()

            # [心跳保活] - 日线刷新耗时长（约 13 分钟），启动后台心跳任务防止 watchdog 60s 误判 stale
            # 完成后取消心跳任务（CancelledError 安静处理）
            heartbeat_task = asyncio.create_task(
                _job_run_heartbeat_loop(job_run_id, worker_id, interval=30)
            )
            try:
                # 调用 bars_scheduler（使用独立 session，内部会传 job_run_id 写事件）
                batch_result = await bars_service.refresh_all_instruments(
                    trade_date=trade_date,
                    db_session=None,  # 服务内部创建 session
                    job_run_id=job_run_id,
                )
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
            dsa_run_id = batch_result.dsa_run_id

            if dsa_run_id is None:
                # [AfterClose] - 区分跳过原因：NON_TRADING_DAY（非交易日）vs None（覆盖率不足）
                skip_reason = batch_result.skip_reason
                if skip_reason == "NON_TRADING_DAY":
                    success_message = (
                        f"因非交易日跳过，未执行行情更新和选股: trade_date={trade_date}"
                    )
                    success_payload: dict[str, Any] = {"skip_reason": "NON_TRADING_DAY"}
                    success_extra: dict[str, Any] | None = {"skip_reason": "NON_TRADING_DAY"}
                else:
                    success_message = (
                        f"日线覆盖率不足未触发 DSA，编排结束: "
                        f"covered={batch_result.daily_covered}, "
                        f"total={batch_result.daily_total}, "
                        f"coverage={batch_result.daily_coverage}"
                    )
                    success_payload = {
                        "daily_covered": batch_result.daily_covered,
                        "daily_total": batch_result.daily_total,
                        "daily_coverage": batch_result.daily_coverage,
                    }
                    success_extra = None

                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.SUCCEEDED,
                        message=success_message,
                        payload=success_payload,
                        extra=success_extra,
                    )
                    job_run.status = "succeeded"
                    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                    await _update_heartbeat_and_step(
                        db, job_run, "succeeded", worker_id,
                    )
                    await db.commit()

                logger.info(
                    "[AfterClose] DSA 未触发，编排成功结束: job_run_id=%s skip_reason=%s",
                    job_run_id, skip_reason,
                )
                return

            # [Phase5] - refreshing_daily 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.REFRESHING_DAILY.value, worker_id,
                )
                await db.commit()
        else:
            # [Phase5] - 断点恢复跳过日线刷新，dsa_run_id 从 metadata 读取
            # [Phase6] - dsa_only 模式：跳过日线刷新，直接创建 DSA run（覆盖率已由 API 层校验）
            mode = meta.get("mode")
            if dsa_run_id is None:
                if mode == "dsa_only":
                    # [Phase6] - dsa_only 模式：直接调用 create_batch_run 创建 DSA run
                    from app.constants.strategy_keys import DSA_SELECTOR
                    logger.info(
                        "[AfterClose] dsa_only 模式: 跳过日线刷新，直接创建 DSA run: "
                        "job_run_id=%s, trade_date=%s",
                        job_run_id, trade_date,
                    )
                    async with AsyncSessionLocal() as db:
                        dsa_run = await batch_service.create_batch_run(
                            db=db,
                            strategy_key=DSA_SELECTOR,
                            trade_date=trade_date,
                            run_type="scheduled",
                        )
                        await db.commit()
                        dsa_run_id = dsa_run.id
                        # 更新 metadata 记录 dsa_run_id
                        job_run = await _get_job_run_or_raise(db, job_run_id)
                        await _update_orchestrator_status(
                            db=db,
                            job_run=job_run,
                            status=AfterCloseRunStatus.REFRESHING_DAILY,
                            message=f"dsa_only 模式: 已创建 DSA run: dsa_run_id={dsa_run_id}",
                            dsa_run_id=dsa_run_id,
                            payload={"mode": "dsa_only", "dsa_run_id": str(dsa_run_id)},
                        )
                        await _update_heartbeat_and_step(
                            db, job_run, AfterCloseRunStatus.REFRESHING_DAILY.value, worker_id,
                        )
                        await db.commit()
                else:
                    raise ValueError(
                        f"断点恢复: last_completed_step={last_completed_step} "
                        f"但 metadata 缺少 dsa_run_id: job_run_id={job_run_id}"
                    )

        # ---- 步骤 2: waiting_dsa_worker ----
        if not skip_wait:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.WAITING_DSA_WORKER,
                    message=f"等待 DSA Worker 执行完成: dsa_run_id={dsa_run_id}",
                    dsa_run_id=dsa_run_id,
                    payload={"dsa_run_id": str(dsa_run_id)},
                )
                await db.commit()

            # 轮询 DSA run 状态（每轮更新心跳，防止 waiting_dsa_worker 阶段被误判为 stale）
            dsa_final_status = await _poll_dsa_run_status(
                dsa_run_id=dsa_run_id,
                poll_interval=dsa_poll_interval,
                timeout=dsa_poll_timeout,
                job_run_id=job_run_id,
                worker_id=worker_id,
            )

            # [AfterClose] - 描述: 接受 completed 和 published 都为成功终态
            # dsa_only 模式下 worker 会自动执行 quality_gate + publish，
            # DSA run 最终状态为 published（与 _poll_dsa_run_status 的 terminal_statuses 对齐）
            if dsa_final_status not in ("completed", "published"):
                raise RuntimeError(
                    f"DSA 运行未完成: dsa_run_id={dsa_run_id}, "
                    f"final_status={dsa_final_status}"
                )

            # [Phase5] - waiting_dsa_worker 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.WAITING_DSA_WORKER.value, worker_id,
                )
                await db.commit()

        # ---- 步骤 3: quality_gate ----
        if not skip_quality:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                dsa_run = await _get_strategy_run_or_raise(db, dsa_run_id)

                result_count = await strategy_result_repository.count_by_run(
                    db, dsa_run_id
                )
                quality_passed = await batch_service._check_quality_gates(
                    dsa_run, result_count=result_count, db=db
                )
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.QUALITY_GATE,
                    message=(
                        f"质量门禁{'通过' if quality_passed else '未通过'}: "
                        f"dsa_run_id={dsa_run_id}, "
                        f"succeeded={dsa_run.succeeded_count}, "
                        f"total={dsa_run.total_instruments}, "
                        f"failed={dsa_run.failed_count}"
                    ),
                    dsa_run_id=dsa_run_id,
                    payload={
                        "quality_passed": quality_passed,
                        "succeeded_count": dsa_run.succeeded_count,
                        "total_instruments": dsa_run.total_instruments,
                        "failed_count": dsa_run.failed_count,
                    },
                )
                await db.commit()

                if not quality_passed:
                    raise RuntimeError(
                        f"质量门禁未通过: dsa_run_id={dsa_run_id}, "
                        f"status={dsa_run.status}"
                    )

            # [Phase5] - quality_gate 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.QUALITY_GATE.value, worker_id,
                )
                await db.commit()

        # ---- 步骤 3.5: feature_snapshot ----
        # 生成特征快照供 /watchlist/monitor-status 读取，不再走实时 fallback。
        # snapshot 失败比例超过阈值时抛 RuntimeError，编排标记 failed。
        # 单股失败由 compute_for_trade_date 内部记录到 degraded_reasons，不阻塞其他股票。
        #
        # [Phase7] Run lifecycle：
        # - 开始时创建 status='running' 的 StockFeatureSnapshotRun（独立 session + commit）
        # - 成功时 finish_snapshot_run(status='succeeded') + 写 published_at
        # - 失败时 finish_snapshot_run(status='failed') + 不写 published_at
        # - watchlist 通过 _has_succeeded_snapshot_run 判断是否可读 snapshot
        # - run 记录在独立 session 中提交，保证失败时 run.status='failed' 持久化
        if not skip_snapshot:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is None:
                    raise RuntimeError(
                        f"SchedulerJobRun not found: job_run_id={job_run_id}"
                    )
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.FEATURE_SNAPSHOT,
                    message=f"开始生成特征快照: trade_date={trade_date}",
                )
                await db.commit()

            # [Phase7] 创建 running run + commit（独立 session，避免 snapshot rollback 影响）
            async with AsyncSessionLocal() as db:
                instrument_ids = await get_active_a_share_instruments(db)
                # [Blocker Fix] after_close 处理全市场 A 股，scope='full'（watchlist 可读）
                snapshot_run = await create_snapshot_run(
                    db, trade_date, "after_close",
                    expected_count=len(instrument_ids),
                    metadata={"source": "after_close_orchestrator"},
                    scope="full",
                )
                await db.commit()
                snapshot_run_id = snapshot_run.id
                # instrument_ids 复用，避免下个 session 重复查询
                cached_instrument_ids = instrument_ids

            # [Heartbeat] feature_snapshot 开始后立即写入 run_id 与 last_started_step，
            # 这样 UI 不会显示 feature_snapshot 待执行，且中断后知道从哪一步恢复。
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is None:
                    raise RuntimeError(
                        f"SchedulerJobRun not found: job_run_id={job_run_id}"
                    )
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.FEATURE_SNAPSHOT,
                    message=f"开始生成特征快照: trade_date={trade_date}, run_id={snapshot_run_id}",
                    extra={
                        "feature_snapshot_run_id": str(snapshot_run_id),
                        "last_started_step": AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
                    },
                )
                await db.commit()

            # 计算 snapshots（独立 session + 后台心跳保活 + 进度回调）
            snapshot_result: dict[str, Any] | None = None
            snapshot_error: Exception | None = None
            heartbeat_task = asyncio.create_task(
                _job_run_heartbeat_loop(job_run_id, worker_id, interval=30)
            )
            try:
                progress_callback = _build_feature_snapshot_progress_callback(
                    job_run_id, worker_id
                )
                async with AsyncSessionLocal() as db:
                    snapshot_result = await compute_for_trade_date(
                        db, trade_date, cached_instrument_ids,
                        progress_callback=progress_callback,
                        source_run_id=snapshot_run_id,
                    )
                    await db.commit()
            except RuntimeError as snapshot_exc:
                # [Blocker2] 失败比例超阈值：snapshot session 已自动 rollback 半成品行。
                # 异常暂存，先 finalize run 为 failed，再向上传播触发 orchestrator FAILED。
                snapshot_error = snapshot_exc
                logger.error(
                    "[AfterClose] feature_snapshot 失败比例超阈值，"
                    "snapshot session 已 rollback: trade_date=%s, error=%s",
                    trade_date, snapshot_exc,
                )
            except Exception as snapshot_exc:
                # 其他异常同样暂存，先 finalize run 为 failed
                snapshot_error = snapshot_exc
                logger.error(
                    "[AfterClose] feature_snapshot 异常: trade_date=%s, error=%s",
                    trade_date, snapshot_exc, exc_info=True,
                )
            finally:
                # [Heartbeat] 取消后台心跳任务，安静忽略 CancelledError
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

            # [Phase7] Finalize run（独立 session，保证 run 状态持久化）
            async with AsyncSessionLocal() as db:
                from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
                run_to_finish = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
                if run_to_finish is not None:
                    if snapshot_error is not None:
                        # [Blocker Fix] failed run 也写入 scope='full'（虽不发布，
                        # 但保持 metadata 一致性，便于追溯）
                        await finish_snapshot_run(
                            db, run_to_finish,
                            status="failed",
                            metadata={
                                "source": "after_close_orchestrator",
                                "error": str(snapshot_error),
                                "scope": "full",
                            },
                        )
                    else:
                        # [Blocker Fix] succeeded run 必须写入 scope='full'，
                        # watchlist gate 据此判断可读
                        await finish_snapshot_run(
                            db, run_to_finish,
                            status="succeeded",
                            snapshot_count=snapshot_result.get("snapshot_count", 0) if snapshot_result else 0,
                            failed_count=snapshot_result.get("failed_count", 0) if snapshot_result else 0,
                            expected_count=len(cached_instrument_ids),
                            metadata={
                                "source": "after_close_orchestrator",
                                "scope": "full",
                            },
                        )
                    await db.commit()

            # 失败时向上传播 RuntimeError，触发 orchestrator FAILED 状态写入，跳过 publishing
            # C5: 事件生成已移到 publishing 成功之后，此处失败不会生成事件
            if snapshot_error is not None:
                raise snapshot_error

            logger.info(
                "[AfterClose] 特征快照生成完成: trade_date=%s, "
                "snapshot_count=%s, failed_count=%s",
                trade_date,
                snapshot_result.get("snapshot_count") if snapshot_result else 0,
                snapshot_result.get("failed_count") if snapshot_result else 0,
            )

            # [Phase5] - feature_snapshot 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is None:
                    raise RuntimeError(
                        f"SchedulerJobRun not found: job_run_id={job_run_id}"
                    )
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.FEATURE_SNAPSHOT.value, worker_id,
                )
                await db.commit()

        # ---- 步骤 4: publishing ----
        if not skip_publish:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.PUBLISHING,
                    message=f"开始发布 DSA 结果: dsa_run_id={dsa_run_id}",
                    dsa_run_id=dsa_run_id,
                )
                await db.commit()

            # 调用 publish_run（使用独立 session）
            async with AsyncSessionLocal() as db:
                published_run = await batch_service.publish_run(db, dsa_run_id)
                await db.commit()

            # [Phase5] - publishing 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.PUBLISHING.value, worker_id,
                )
                await db.commit()

        # C5: 事件生成在 publishing 成功之后（或 skip_publish 断点恢复时已发布）
        # publishing 失败会抛异常跳过此处 → 不生成事件
        # 独立 session + try/except：事件生成失败不影响 orchestrator 主流程
        if snapshot_error is None and snapshot_run_id is not None:
            try:
                from app.services.state_event_service import (
                    cleanup_old_events,
                    generate_events_for_run,
                )
                async with AsyncSessionLocal() as event_db:
                    event_stats = await generate_events_for_run(event_db, snapshot_run_id)
                    # 90 天清理（P1-2）：事件生成后执行，失败不阻断主发布
                    cleanup_stats = await cleanup_old_events(event_db)
                    await event_db.commit()
                logger.info(
                    "[AfterClose] 状态事件生成完成: run_id=%s, "
                    "event_count=%s, skipped=%s, failed=%s, "
                    "cleanup_deleted=%s, cleanup_duration_ms=%s",
                    snapshot_run_id,
                    event_stats.get("event_count", 0),
                    event_stats.get("skipped_count", 0),
                    event_stats.get("failed_count", 0),
                    cleanup_stats.get("deleted_count", 0),
                    cleanup_stats.get("duration_ms", 0),
                )
            except Exception as event_exc:
                logger.warning(
                    "[AfterClose] 状态事件生成失败（不影响主流程）: "
                    "run_id=%s, error=%s",
                    snapshot_run_id, event_exc, exc_info=True,
                )

        # ---- 步骤 5: succeeded ----
        async with AsyncSessionLocal() as db:
            job_run = await _get_job_run_or_raise(db, job_run_id)
            # published_run 可能为 None（断点恢复跳过 publishing 时）
            published_at_str = (
                published_run.published_at.isoformat()
                if published_run is not None and published_run.published_at
                else None
            )
            success_message = (
                f"盘后编排成功完成: dsa_run_id={dsa_run_id}"
                + (f", published_at={published_run.published_at}"
                   if published_run is not None else "")
            )
            await _update_orchestrator_status(
                db=db,
                job_run=job_run,
                status=AfterCloseRunStatus.SUCCEEDED,
                message=success_message,
                dsa_run_id=dsa_run_id,
                payload={"published_at": published_at_str},
            )
            job_run.status = "succeeded"
            job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            await _update_heartbeat_and_step(
                db, job_run, "succeeded", worker_id,
            )
            await db.commit()

        logger.info(
            "[AfterClose] 盘后编排成功完成: job_run_id=%s, dsa_run_id=%s",
            job_run_id, dsa_run_id,
        )

    except Exception as exc:
        # [AfterClose] - 任意步骤异常：写 ERROR 事件 + 标记 failed
        logger.error(
            "[AfterClose] 盘后编排失败: job_run_id=%s, dsa_run_id=%s, error=%s",
            job_run_id, dsa_run_id, exc,
            exc_info=True,
        )
        import traceback as tb_mod
        try:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                if job_run is not None:
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.FAILED,
                        message=f"盘后编排失败: {exc}",
                        dsa_run_id=dsa_run_id,
                        payload={
                            "error_type": type(exc).__name__,
                            "traceback": tb_mod.format_exc()[:4000],
                        },
                    )
                    job_run.status = "failed"
                    job_run.error_message = str(exc)[:500]
                    job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                    if worker_id is not None:
                        job_run.worker_instance_id = worker_id
                    await db.commit()
        except Exception as inner_exc:
            # [AfterClose] - 写 ERROR 事件本身失败，记录日志但不吞没原异常
            logger.error(
                "[AfterClose] 写入 failed 状态失败: job_run_id=%s, inner_error=%s",
                job_run_id, inner_exc,
            )
        raise


async def _poll_dsa_run_status(
    dsa_run_id: uuid.UUID,
    poll_interval: int,
    timeout: int,
    *,
    job_run_id: uuid.UUID | None = None,
    worker_id: str | None = None,
) -> str:
    """[AfterClose] - 轮询 DSA StrategyRun.status 直到终态或超时。

    每个轮询周期更新 job_run 心跳，防止长时间等待被误判为 stale。

    Args:
        dsa_run_id: DSA StrategyRun id
        poll_interval: 轮询间隔（秒）
        timeout: 超时（秒）
        job_run_id: 编排任务 ID（非 None 时每轮更新心跳）
        worker_id: Worker 实例标识

    Returns:
        DSA run 最终状态（completed/failed/partial_failed/...）

    Raises:
        TimeoutError: 超过 timeout 仍未达到终态
    """
    terminal_statuses = {"completed", "failed", "partial_failed", "published", "interrupted"}
    elapsed = 0

    while elapsed < timeout:
        async with AsyncSessionLocal() as db:
            dsa_run = await db.get(StrategyRun, dsa_run_id)
            if dsa_run is None:
                raise ValueError(f"DSA 运行记录不存在: dsa_run_id={dsa_run_id}")

            status = dsa_run.status
            if status in terminal_statuses:
                logger.info(
                    "[AfterClose] DSA 运行达到终态: dsa_run_id=%s, status=%s",
                    dsa_run_id, status,
                )
                return status

        # [Phase7] - 每轮更新心跳，防止 waiting_dsa_worker 阶段被误判为 stale
        if job_run_id is not None:
            try:
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    if job_run is not None:
                        await _update_heartbeat_and_step(
                            db, job_run,
                            AfterCloseRunStatus.WAITING_DSA_WORKER.value,
                            worker_id,
                        )
                        await db.commit()
            except Exception as exc:
                logger.warning(
                    "[AfterClose] DSA 轮询期间更新心跳失败: job_run_id=%s, error=%s",
                    job_run_id, exc,
                )

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"DSA 运行等待超时: dsa_run_id={dsa_run_id}, "
        f"elapsed={elapsed}s, timeout={timeout}s"
    )


# [Phase7] - 心跳超时阈值：running 状态下 heartbeat_at 落后 now 超过 60s 视为 stale
_HEARTBEAT_STALE_SECONDS = 60


async def get_after_close_run_status(
    db: AsyncSession,
    job_run_id: uuid.UUID,
    event_limit: int = 50,
) -> dict[str, Any]:
    """查询盘后编排状态（orchestrator_status + 事件时间线 + DSA run 状态 + [Phase7] 详情字段）。

    [Phase7] 新增返回字段（供 Admin 后台展示）：
    - worker_instance_id: Worker 实例标识
    - heartbeat_at / lease_expires_at: ISO 格式心跳与租约时间
    - last_completed_step: 最后成功步骤（从 metadata_json 解析）
    - interrupt_reason: failed/interrupted 时拼接 "error_code: error_message"
    - is_retryable: status in ('failed','interrupted')
    - heartbeat_stale: running 且 heartbeat_at < now - 60s

    Args:
        db: 异步会话
        job_run_id: 编排任务 ID
        event_limit: 最多返回事件数

    Returns:
        dict:
        - job_run_id / job_name / business_date / status / orchestrator_status
        - trade_date / dsa_run_id / dsa_run_status
        - started_at / finished_at / error_message
        - [Phase7] worker_instance_id / heartbeat_at / lease_expires_at
        - [Phase7] last_completed_step / interrupt_reason / is_retryable / heartbeat_stale
        - events: 事件时间线列表

    Raises:
        ValueError: job_run_id 不存在或非编排任务
    """
    job_run = await _get_job_run_or_raise(db, job_run_id)
    if job_run is None:
        raise ValueError(f"编排任务不存在: job_run_id={job_run_id}")
    if job_run.job_name != _AFTER_CLOSE_JOB_NAME:
        raise ValueError(
            f"任务非盘后编排: job_name={job_run.job_name}, 期望={_AFTER_CLOSE_JOB_NAME}"
        )

    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status", "unknown")
    trade_date_str = meta.get("trade_date")
    dsa_run_id_str = meta.get("dsa_run_id")
    last_completed_step = meta.get("last_completed_step")
    # [AfterClose] - 透传非交易日等跳过原因到前端展示
    skip_reason = meta.get("skip_reason")

    dsa_run_status: str | None = None
    if dsa_run_id_str:
        try:
            dsa_run_id = uuid.UUID(dsa_run_id_str)
            dsa_run = await db.get(StrategyRun, dsa_run_id)
            if dsa_run is not None:
                dsa_run_status = dsa_run.status
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[AfterClose] dsa_run_id 解析失败: %s, error=%s",
                dsa_run_id_str, exc,
            )

    events = await list_events(db, job_run_id, limit=event_limit)

    # [Phase7] - 中断原因：failed/interrupted 时拼接 error_code + error_message
    interrupt_reason: str | None = None
    if job_run.status in ("failed", "interrupted"):
        code = job_run.error_code or "UNKNOWN"
        msg = job_run.error_message or ""
        interrupt_reason = f"{code}: {msg}" if msg else code

    # [Phase7] - 是否允许重试：与 _RESUMABLE_STATUSES 对齐（failed/interrupted）
    is_retryable = job_run.status in ("failed", "interrupted")

    # [Phase7] - 心跳超时：仅 running 状态判断，heartbeat_at 落后 now 超过阈值视为 stale
    heartbeat_stale = False
    if job_run.status == "running" and job_run.heartbeat_at is not None:
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
        # heartbeat_at 可能是 naive datetime（旧数据），统一附加 tz 后比较
        hb = job_run.heartbeat_at
        if hb.tzinfo is None:
            hb = hb.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        heartbeat_stale = (now_sh - hb) > timedelta(seconds=_HEARTBEAT_STALE_SECONDS)

    return {
        "job_run_id": str(job_run_id),
        "job_name": job_run.job_name,
        "business_date": job_run.business_date,
        "status": job_run.status,
        "orchestrator_status": orchestrator_status,
        "trade_date": trade_date_str,
        "dsa_run_id": dsa_run_id_str,
        "dsa_run_status": dsa_run_status,
        "started_at": job_run.started_at.isoformat() if job_run.started_at else None,
        "finished_at": job_run.finished_at.isoformat() if job_run.finished_at else None,
        "error_message": job_run.error_message,
        # [Phase7] - 详情字段
        "worker_instance_id": job_run.worker_instance_id,
        "heartbeat_at": job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None,
        "lease_expires_at": job_run.lease_expires_at.isoformat() if job_run.lease_expires_at else None,
        "last_completed_step": last_completed_step,
        "skip_reason": skip_reason,
        "interrupt_reason": interrupt_reason,
        "is_retryable": is_retryable,
        "heartbeat_stale": heartbeat_stale,
        "events": [
            {
                "id": str(e.id),
                "step": e.step,
                "level": e.level,
                "message": e.message,
                "payload": e.payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


async def retry_after_close_run(
    db: AsyncSession,
    job_run_id: uuid.UUID,
) -> SchedulerJobRun:
    """重试失败的盘后编排任务（重置状态为 queued，允许重新执行）。

    流程：
    1. 加载 job_run，校验为编排任务且 status=failed
    2. 重置 status=queued, error_message=None, finished_at=None（由 Worker 领取）
    3. 更新 orchestrator_status=queued + 写 retry 事件
    4. commit

    Args:
        db: 异步会话
        job_run_id: 编排任务 ID

    Returns:
        更新后的 SchedulerJobRun

    Raises:
        ValueError: 任务不存在/非编排任务/状态非 failed
    """
    job_run = await _get_job_run_or_raise(db, job_run_id)
    if job_run is None:
        raise ValueError(f"编排任务不存在: job_run_id={job_run_id}")
    if job_run.job_name != _AFTER_CLOSE_JOB_NAME:
        raise ValueError(
            f"任务非盘后编排: job_name={job_run.job_name}"
        )
    if job_run.status != "failed":
        raise ValueError(
            f"仅 failed 状态可重试（当前 {job_run.status}）: job_run_id={job_run_id}"
        )

    # [Phase5] - 重置为 queued（不是 running），由独立 Worker 领取执行
    job_run.status = "queued"
    job_run.error_message = None
    job_run.error_code = None
    job_run.finished_at = None
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.started_at = now
    job_run.heartbeat_at = now
    job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)

    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=f"管理员手动重试: job_run_id={job_run_id}",
    )
    await db.commit()

    logger.info("[AfterClose] 重试盘后编排: job_run_id=%s", job_run_id)
    return job_run


if __name__ == "__main__":
    # 自测入口：验证枚举、函数签名与模块导入（不连接数据库）
    import inspect

    # 验证 AfterCloseRunStatus 枚举
    expected_statuses = {
        "queued", "refreshing_daily", "checking_coverage", "creating_dsa",
        "waiting_dsa_worker", "quality_gate", "feature_snapshot",
        "publishing", "succeeded", "failed",
    }
    actual_statuses = {s.value for s in AfterCloseRunStatus}
    assert actual_statuses == expected_statuses, (
        f"AfterCloseRunStatus 枚举值不匹配: {actual_statuses}"
    )
    print(f"AfterCloseRunStatus 枚举验证 ✓: {sorted(actual_statuses)}")

    # 验证 create_after_close_run 签名
    sig = inspect.signature(create_after_close_run)
    params = set(sig.parameters.keys())
    assert params == {"db", "trade_date"}, f"create_after_close_run 参数不匹配: {params}"
    print(f"create_after_close_run 签名 ✓: {sorted(params)}")

    # 验证 execute_after_close_run 签名
    sig = inspect.signature(execute_after_close_run)
    params = set(sig.parameters.keys())
    assert "job_run_id" in params and "trade_date" in params, (
        f"execute_after_close_run 缺少必要参数: {params}"
    )
    # [Phase5] - worker_id 参数支持断点恢复 + 心跳
    assert "worker_id" in params, f"execute_after_close_run 缺少 worker_id 参数: {params}"
    assert sig.parameters["worker_id"].default is None, (
        "worker_id 默认值应为 None"
    )
    assert sig.parameters["dsa_poll_interval"].default == _DSA_POLL_INTERVAL_SECONDS
    assert sig.parameters["dsa_poll_timeout"].default == _DSA_POLL_TIMEOUT_SECONDS
    print(f"execute_after_close_run 签名 ✓: {sorted(params)}")

    # [Phase5] - 验证 _update_heartbeat_and_step 签名
    sig = inspect.signature(_update_heartbeat_and_step)
    params = set(sig.parameters.keys())
    assert params == {"db", "job_run", "last_completed_step", "worker_id"}, (
        f"_update_heartbeat_and_step 参数不匹配: {params}"
    )
    assert sig.parameters["worker_id"].default is None
    print(f"_update_heartbeat_and_step 签名 ✓: {sorted(params)}")

    # 验证 get_after_close_run_status 签名
    sig = inspect.signature(get_after_close_run_status)
    params = set(sig.parameters.keys())
    assert params == {"db", "job_run_id", "event_limit"}, (
        f"get_after_close_run_status 参数不匹配: {params}"
    )
    assert sig.parameters["event_limit"].default == 50
    print(f"get_after_close_run_status 签名 ✓: {sorted(params)}")

    # 验证 retry_after_close_run 签名
    sig = inspect.signature(retry_after_close_run)
    params = set(sig.parameters.keys())
    assert params == {"db", "job_run_id"}, (
        f"retry_after_close_run 参数不匹配: {params}"
    )
    print(f"retry_after_close_run 签名 ✓: {sorted(params)}")

    # 验证 _build_metadata / _parse_metadata 互逆
    td = date(2026, 6, 25)
    drid = uuid.uuid4()
    meta_str = _build_metadata(td, AfterCloseRunStatus.QUEUED, dsa_run_id=drid)
    parsed = json.loads(meta_str)
    assert parsed["orchestrator_status"] == "queued"
    assert parsed["trade_date"] == "2026-06-25"
    assert parsed["dsa_run_id"] == str(drid)
    print("_build_metadata / _parse_metadata 互逆 ✓")

    print("OK")
