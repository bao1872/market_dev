"""盘后编排服务 - 串联日线刷新 → DSA 选股 → 质量门禁 → 发布的全流水线。

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
  StrategyBatchService._check_quality_gates / StrategyBatchService.publish_run
- DSA Worker 异步执行，编排层轮询 StrategyRun.status 直到 completed/failed/超时

状态机：
queued → refreshing_daily → checking_coverage → creating_dsa
  → waiting_dsa_worker → quality_gate → publishing → succeeded
任意步骤异常 → failed

禁异常吞没：所有异常补充上下文后 re-raise 或写入 ERROR 事件后标记 failed。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.services.bars_scheduler_service import BarsSchedulerService
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


class AfterCloseRunStatus(str, Enum):
    """盘后编排流水线状态枚举。

    状态流转：
    queued → refreshing_daily → checking_coverage → creating_dsa
      → waiting_dsa_worker → quality_gate → publishing → succeeded
    任意步骤异常 → failed
    """

    QUEUED = "queued"
    REFRESHING_DAILY = "refreshing_daily"
    CHECKING_COVERAGE = "checking_coverage"
    CREATING_DSA = "creating_dsa"
    WAITING_DSA_WORKER = "waiting_dsa_worker"
    QUALITY_GATE = "quality_gate"
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

    # 构造新 metadata
    new_meta: dict[str, Any] = {
        "orchestrator_status": status.value,
    }
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
) -> SchedulerJobRun:
    """创建盘后编排任务（幂等：同 trade_date 已有 running/succeeded 则返回已有）。

    流程：
    1. 构造 run_key = after_close_orchestrator:{trade_date}
    2. acquire_job_run_lock 获取任务执行权（幂等）
    3. 写入 metadata_json（orchestrator_status=queued）
    4. 写入 START 事件
    5. commit 并返回 SchedulerJobRun

    Args:
        db: 异步会话
        trade_date: 交易日期

    Returns:
        SchedulerJobRun 记录（status=running, orchestrator_status=queued）

    Raises:
        RuntimeError: 幂等锁获取失败（同日已有运行中任务）
    """
    run_key = f"{_AFTER_CLOSE_JOB_NAME}:{trade_date.isoformat()}"
    job_run = await acquire_job_run_lock(
        db=db,
        run_key=run_key,
        job_name=_AFTER_CLOSE_JOB_NAME,
        business_date=trade_date.isoformat(),
        lease_seconds=_ORCHESTRATOR_LEASE_SECONDS,
        metadata={
            "orchestrator_status": AfterCloseRunStatus.QUEUED.value,
            "trade_date": trade_date.isoformat(),
        },
    )
    if job_run is None:
        # [AfterClose] - 同日已有运行中/已成功的编排任务，查询已有记录返回
        stmt = select(SchedulerJobRun).where(
            SchedulerJobRun.run_key == run_key,
        )
        result = await db.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing is not None:
            logger.info(
                "[AfterClose] 同日已有编排任务，返回已有: run_id=%s, status=%s",
                existing.id, existing.status,
            )
            return existing
        raise RuntimeError(
            f"acquire_job_run_lock 返回 None 但未找到已有记录: run_key={run_key}"
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
    return job_run


async def execute_after_close_run(
    job_run_id: uuid.UUID,
    trade_date: date,
    *,
    dsa_poll_interval: int = _DSA_POLL_INTERVAL_SECONDS,
    dsa_poll_timeout: int = _DSA_POLL_TIMEOUT_SECONDS,
) -> None:
    """执行盘后编排流水线（后台异步，使用独立 AsyncSession）。

    流程：
    1. refreshing_daily: 调用 BarsSchedulerService.refresh_all_instruments
       - 内部完成日线刷新 + 覆盖率检查 + DSA 触发（写 DAILY_DONE/DSA_CREATED 事件）
       - 返回 BatchResult（含 dsa_run_id）
    2. waiting_dsa_worker: 轮询 DSA StrategyRun.status 直到 completed/failed/超时
    3. quality_gate: 调用 StrategyBatchService._check_quality_gates
    4. publishing: 调用 StrategyBatchService.publish_run
    5. succeeded: 标记整体任务成功

    任意步骤异常 → 写 ERROR 事件 + 标记 failed + 更新 SchedulerJobRun.status=failed

    Args:
        job_run_id: 编排任务 ID
        trade_date: 交易日期
        dsa_poll_interval: DSA 轮询间隔（秒，测试时可缩短）
        dsa_poll_timeout: DSA 轮询超时（秒，测试时可缩短）

    Raises:
        异常向上传播（调用方应捕获并记录日志）
    """
    logger.info(
        "[AfterClose] 开始执行盘后编排: job_run_id=%s, trade_date=%s",
        job_run_id, trade_date,
    )

    bars_service = BarsSchedulerService()
    batch_service = StrategyBatchService()
    dsa_run_id: uuid.UUID | None = None

    try:
        # ---- 步骤 1: refreshing_daily ----
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            if job_run is None:
                raise ValueError(f"编排任务不存在: job_run_id={job_run_id}")
            if job_run.status == "succeeded":
                logger.info("[AfterClose] 任务已成功，跳过: job_run_id=%s", job_run_id)
                return

            await _update_orchestrator_status(
                db=db,
                job_run=job_run,
                status=AfterCloseRunStatus.REFRESHING_DAILY,
                message=f"开始刷新日线: trade_date={trade_date}",
            )
            await db.commit()

        # 调用 bars_scheduler（使用独立 session，内部会传 job_run_id 写事件）
        batch_result = await bars_service.refresh_all_instruments(
            trade_date=trade_date,
            db_session=None,  # 服务内部创建 session
            job_run_id=job_run_id,
        )
        dsa_run_id = batch_result.dsa_run_id

        if dsa_run_id is None:
            # [AfterClose] - 覆盖率不足或 DSA 未触发，标记成功结束（非错误）
            async with AsyncSessionLocal() as db:
                job_run = await db.get(SchedulerJobRun, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.SUCCEEDED,
                    message=(
                        f"日线覆盖率不足未触发 DSA，编排结束: "
                        f"covered={batch_result.daily_covered}, "
                        f"total={batch_result.daily_total}, "
                        f"coverage={batch_result.daily_coverage}"
                    ),
                    payload={
                        "daily_covered": batch_result.daily_covered,
                        "daily_total": batch_result.daily_total,
                        "daily_coverage": batch_result.daily_coverage,
                    },
                )
                job_run.status = "succeeded"
                job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                await db.commit()

            logger.info(
                "[AfterClose] DSA 未触发，编排成功结束: job_run_id=%s", job_run_id,
            )
            return

        # ---- 步骤 2: waiting_dsa_worker ----
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            await _update_orchestrator_status(
                db=db,
                job_run=job_run,
                status=AfterCloseRunStatus.WAITING_DSA_WORKER,
                message=f"等待 DSA Worker 执行完成: dsa_run_id={dsa_run_id}",
                dsa_run_id=dsa_run_id,
                payload={"dsa_run_id": str(dsa_run_id)},
            )
            await db.commit()

        # 轮询 DSA run 状态
        dsa_final_status = await _poll_dsa_run_status(
            dsa_run_id=dsa_run_id,
            poll_interval=dsa_poll_interval,
            timeout=dsa_poll_timeout,
        )

        if dsa_final_status != "completed":
            raise RuntimeError(
                f"DSA 运行未完成: dsa_run_id={dsa_run_id}, "
                f"final_status={dsa_final_status}"
            )

        # ---- 步骤 3: quality_gate ----
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            dsa_run = await db.get(StrategyRun, dsa_run_id)
            if dsa_run is None:
                raise ValueError(f"DSA 运行记录不存在: dsa_run_id={dsa_run_id}")

            quality_passed = await batch_service._check_quality_gates(dsa_run)
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

        # ---- 步骤 4: publishing ----
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
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

        # ---- 步骤 5: succeeded ----
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            await _update_orchestrator_status(
                db=db,
                job_run=job_run,
                status=AfterCloseRunStatus.SUCCEEDED,
                message=(
                    f"盘后编排成功完成: dsa_run_id={dsa_run_id}, "
                    f"published_at={published_run.published_at}"
                ),
                dsa_run_id=dsa_run_id,
                payload={
                    "published_at": published_run.published_at.isoformat()
                    if published_run.published_at
                    else None,
                },
            )
            job_run.status = "succeeded"
            job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
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
                job_run = await db.get(SchedulerJobRun, job_run_id)
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
) -> str:
    """[AfterClose] - 轮询 DSA StrategyRun.status 直到终态或超时。

    Args:
        dsa_run_id: DSA StrategyRun id
        poll_interval: 轮询间隔（秒）
        timeout: 超时（秒）

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

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    raise TimeoutError(
        f"DSA 运行等待超时: dsa_run_id={dsa_run_id}, "
        f"elapsed={elapsed}s, timeout={timeout}s"
    )


async def get_after_close_run_status(
    db: AsyncSession,
    job_run_id: uuid.UUID,
    event_limit: int = 50,
) -> dict[str, Any]:
    """查询盘后编排状态（orchestrator_status + 事件时间线 + DSA run 状态）。

    Args:
        db: 异步会话
        job_run_id: 编排任务 ID
        event_limit: 最多返回事件数

    Returns:
        dict:
        - job_run: SchedulerJobRun 基本信息
        - orchestrator_status: 当前编排状态（从 metadata_json 解析）
        - trade_date: 交易日期
        - dsa_run_id: DSA StrategyRun id（如有）
        - dsa_run_status: DSA run 当前状态（如有）
        - events: 事件时间线列表

    Raises:
        ValueError: job_run_id 不存在或非编排任务
    """
    job_run = await db.get(SchedulerJobRun, job_run_id)
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
    2. 重置 status=running, error_message=None, finished_at=None
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
    job_run = await db.get(SchedulerJobRun, job_run_id)
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

    # 重置任务状态
    job_run.status = "running"
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
        "waiting_dsa_worker", "quality_gate", "publishing", "succeeded", "failed",
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
    assert sig.parameters["dsa_poll_interval"].default == _DSA_POLL_INTERVAL_SECONDS
    assert sig.parameters["dsa_poll_timeout"].default == _DSA_POLL_TIMEOUT_SECONDS
    print(f"execute_after_close_run 签名 ✓: {sorted(params)}")

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
    print(f"_build_metadata / _parse_metadata 互逆 ✓")

    print("OK")
