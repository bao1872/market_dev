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

状态机（PR #77 收口：含 syncing_boards）：
queued → refreshing_daily → syncing_boards → checking_coverage → creating_dsa
  → waiting_dsa_worker → quality_gate → feature_snapshot → publishing → succeeded
任意步骤异常 → failed（syncing_boards 除外：软失败不阻断主流程）
syncing_boards 在 BOARD_SYNC_ENABLED=false / 非交易日时跳过

禁异常吞没：所有异常补充上下文后 re-raise 或写入 ERROR 事件后标记 failed。
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.repositories import strategy_result_repository
from app.services.after_close_chip_consensus_service import (
    create_after_close_chip_consensus_job,
)
from app.services.bars_scheduler_service import BarsSchedulerService
from app.services.feature_snapshot_service import (
    PublishedSnapshotRunExistsError,
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
_DEFAULT_STEP_TIMEOUT_SECONDS = 3600
_AUCTION_ANCHOR_TIMEOUT_SECONDS = 300
_HEARTBEAT_INTERVAL_SECONDS = 10

# [Step Contract 2026-08-03] 每个顶层步骤的硬性超时（秒）。
# watchdog 据此判断步骤 stale/超时；execute_orchestrator_step 据此 wait_for。
# 值需覆盖正常耗时 + 合理缓冲（refreshing_daily 约 13 分钟，computing_features 约 7 小时）。
_STEP_TIMEOUT_SECONDS: dict[str, float] = {
    "refreshing_daily": 3600,      # 约 13 分钟，留足缓冲
    "syncing_boards": 1800,
    "checking_coverage": 300,
    "computing_features": 28800,   # 约 7 小时主链
    "publishing": 3600,
    "computing_review": 1800,
    "auction_anchor": _AUCTION_ANCHOR_TIMEOUT_SECONDS,
    # [Phase0-Fix#8] chip 只做入队（不等计算），超时应短
    "enqueue_chip_job": 120,
}


def _step_timeout(step: str) -> float:
    """返回步骤超时（默认 _DEFAULT_STEP_TIMEOUT_SECONDS）。"""
    return _STEP_TIMEOUT_SECONDS.get(step, _DEFAULT_STEP_TIMEOUT_SECONDS)


class StepUnavailableError(RuntimeError):
    """可选步骤因上游无数据而合法不可用。"""


# [Step Contract 2026-08-03] 步骤级状态合同（唯一来源）：
#   pending / running / succeeded / skipped / unavailable / failed /
#   timed_out / cancelled / interrupted
# 注：原 "skipped_unavailable" 组合态已废弃——跳过与不可用是两个独立概念，
# 可选步骤无数据 → unavailable；显式跳过（断点恢复）→ skipped。
_STEP_STATUS_TERMINAL = {
    "succeeded", "skipped", "unavailable", "failed", "timed_out", "cancelled", "interrupted",
}


async def execute_orchestrator_step(
    step: str,
    operation: Callable[[], Awaitable[Any]],
    *,
    timeout_seconds: float = _DEFAULT_STEP_TIMEOUT_SECONDS,
    optional: bool = False,
    heartbeat: Callable[[], Awaitable[None]] | None = None,
    progress: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    cancellation_check: Callable[[], Awaitable[bool]] | None = None,
    attempt: int = 1,
    retry_count: int = 0,
    poll_interval: float | None = None,
) -> tuple[Any | None, dict[str, Any]]:
    """统一执行步骤；所有顶层盘后步骤都必须通过本执行器。

    责任（AC-02）：
    - 超时保护（非可选步骤超时 → timed_out 终态并抛出），超时会 cancel operation task
    - [Phase0] 执行器唯一周期循环：单次 heartbeat touch + 运行期进度刷新
      （elapsed_seconds / heartbeat_at / last_progress_at / timeout_seconds），
      使 watchdog 能在步骤运行期间判定 step_timed_out，而非仅事后诊断
    - finally 统一收尾并停止周期循环
    - 结构化 summary（started/finished/elapsed/error/progress/attempt）
    - [Phase0] 真正的运行中取消：周期轮询 cancellation_check，命中后 cancel
      operation task 并 await 其结束，确保业务写入停止
    - 可选步骤：普通异常与超时均降级为 skipped/unavailable，不抛出（调用方需检查 summary）

    Returns:
        (result, summary) —— summary 始终包含 step 级结构化状态，供写入 metadata.step_summary。
    """
    started_at = datetime.now(UTC)
    summary: dict[str, Any] = {
        "step": step,
        "status": "running",
        "started_at": started_at.isoformat(),
        "finished_at": None,
        "elapsed_seconds": 0.0,
        "processed": None,
        "total": None,
        "last_progress_at": started_at.isoformat(),
        "heartbeat_at": started_at.isoformat(),
        "timeout_seconds": timeout_seconds,
        "error_code": None,
        "error_message": None,
        "optional": optional,
        "attempt": attempt,
        "retry_count": retry_count,
    }
    if progress is not None:
        await progress(dict(summary))
    stop = asyncio.Event()

    async def _tick_loop() -> None:
        """[Phase0] 执行器唯一周期循环：心跳 touch + 运行期进度刷新。

        每 _HEARTBEAT_INTERVAL_SECONDS 执行一次：
        1. heartbeat() 单次 touch（不再传入无限循环）
        2. 刷新 elapsed_seconds / heartbeat_at / last_progress_at 并落库，
           使 watchdog 能在运行期间判定 step_timed_out。
        """
        while not stop.is_set():
            try:
                # 动态读取模块级间隔，便于测试注入更短周期
                await asyncio.wait_for(
                    stop.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS,
                )
                return
            except TimeoutError:
                pass
            now = datetime.now(UTC)
            if heartbeat is not None:
                try:
                    await heartbeat()
                except Exception as exc:  # 心跳失败不得中断业务步骤
                    logger.warning(
                        "[AfterClose] step heartbeat touch 失败: step=%s, error=%s", step, exc,
                    )
            summary["elapsed_seconds"] = max(0.0, (now - started_at).total_seconds())
            summary["heartbeat_at"] = now.isoformat()
            summary["last_progress_at"] = now.isoformat()
            if progress is not None:
                try:
                    await progress(dict(summary))
                except Exception as exc:
                    logger.warning(
                        "[AfterClose] step 运行期进度刷新失败: step=%s, error=%s", step, exc,
                    )

    tick_task = asyncio.create_task(_tick_loop())
    result: Any | None = None
    try:
        if cancellation_check is not None and await cancellation_check():
            summary.update(status="cancelled", error_code="STEP_CANCELLED_PRECHECK", error_message="cancelled before start")
            return None, summary
        # 协作取消：运行期周期轮询取消状态，命中后 cancel 并 await operation task
        if cancellation_check is not None:
            result = await _run_with_cancellation(
                operation,
                cancellation_check,
                timeout_seconds,
                poll_interval=poll_interval or _CANCEL_POLL_INTERVAL_SECONDS,
            )
        else:
            result = await asyncio.wait_for(operation(), timeout=timeout_seconds)
        if result is None and optional:
            raise StepUnavailableError(f"{step} returned no data")
        summary["status"] = "succeeded"
        if isinstance(result, dict):
            summary["processed"] = result.get("processed", result.get("count"))
            summary["total"] = result.get("total")
    except StepUnavailableError as exc:
        if not optional:
            summary.update(status="unavailable", error_code="STEP_UNAVAILABLE", error_message=str(exc))
            raise
        summary.update(status="unavailable", error_code="STEP_UNAVAILABLE", error_message=str(exc))
    except TimeoutError:
        summary.update(
            status="timed_out", error_code="STEP_TIMEOUT",
            error_message=f"{step} timed out after {timeout_seconds}s",
        )
        if not optional:
            raise
    except asyncio.CancelledError:
        summary.update(status="cancelled", error_code="STEP_CANCELLED", error_message="cancelled")
        raise
    except _StepCancelledError as exc:
        # [Phase0] 运行中取消：operation task 已被 cancel 并 await 结束，
        # 业务写入确定已停止。这是"受控取消"，返回 cancelled summary 由调用方收尾，
        # 不再转成 CancelledError 炸穿 Worker（区别于外部 CancelledError）。
        summary.update(
            status="cancelled", error_code="STEP_CANCELLED", error_message=str(exc),
        )
        result = None
    except Exception as exc:
        summary.update(status="failed", error_code=type(exc).__name__, error_message=str(exc))
        if not optional:
            raise
    finally:
        stop.set()
        tick_task.cancel()
        await asyncio.gather(tick_task, return_exceptions=True)
        finished_at = datetime.now(UTC)
        summary["finished_at"] = finished_at.isoformat()
        summary["elapsed_seconds"] = max(0.0, (finished_at - started_at).total_seconds())
        summary["last_progress_at"] = finished_at.isoformat()
        if progress is not None:
            await progress(dict(summary))
    return result, summary


class _StepCancelledError(Exception):
    """协作取消信号（由 _run_with_cancellation 抛出，外层转 CancelledError）。"""


# [Phase0] 运行中取消轮询间隔（秒）——独立于心跳间隔，便于测试注入
_CANCEL_POLL_INTERVAL_SECONDS = 5.0


async def _run_with_cancellation(
    operation: Callable[[], Awaitable[Any]],
    cancellation_check: Callable[[], Awaitable[bool]],
    timeout_seconds: float,
    poll_interval: float = _CANCEL_POLL_INTERVAL_SECONDS,
) -> Any:
    """运行 operation，并在运行期间周期性调用 cancellation_check。

    [Phase0] 真正的运行中取消：
    - operation 作为独立 task 运行；
    - 每 poll_interval 秒轮询一次 cancellation_check；
    - 命中取消 → cancel operation task 并 await 其真正结束（保证业务写入停止），
      随后抛出 _StepCancelledError；
    - 总耗时超过 timeout_seconds → cancel task 并抛 TimeoutError；
    - 保持原有超时语义。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    op_task = asyncio.ensure_future(operation())

    async def _finalize(exc: BaseException) -> None:
        """cancel operation task 并等待其真正结束，确保业务协程停止执行。"""
        op_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await op_task
        raise exc

    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _finalize(TimeoutError())
            wait_slice = min(poll_interval, remaining)
            done, _pending = await asyncio.wait({op_task}, timeout=wait_slice)
            if done:
                return await op_task
            # operation 仍在运行：轮询取消状态
            try:
                cancelled = await cancellation_check()
            except Exception:  # 取消检查失败不得误杀正在运行的步骤
                cancelled = False
            if cancelled:
                await _finalize(_StepCancelledError("cancelled during run"))
    except asyncio.CancelledError:
        op_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await op_task
        raise


def _make_step_progress_callback(
    job_run_id: uuid.UUID,
    worker_id: str | None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """构造统一执行器的 progress 回调：将 step_summary 合并写入 metadata.step_summary。

    每个步骤运行期间，execute_orchestrator_step 在 started/finished 时调用本回调，
    使 admin 页面能取到统一结构化的步骤状态（状态机唯一来源）。
    """

    async def _cb(summary: dict[str, Any]) -> None:
        step = summary.get("step")
        if not step:
            return
        try:
            async with AsyncSessionLocal() as db:
                job_run = await db.get(SchedulerJobRun, job_run_id)
                if job_run is None:
                    return
                meta = _parse_metadata(job_run)
                step_summary = dict(meta.get("step_summary") or {})
                step_summary[step] = summary
                meta["step_summary"] = step_summary
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                # [Fix 2026-08-03] 进度事件/metadata 必须 commit，避免 session 退出回滚
                await db.commit()
        except Exception as exc:  # 进度回调失败不得影响主流程
            logger.warning(
                "[AfterClose] step progress 回调写入失败: step=%s, error=%s",
                step, exc,
            )

    return _cb


async def _enqueue_chip_job_step(
    *,
    job_run_id: uuid.UUID,
    worker_id: str | None,
    lease_epoch: int | None,
    trade_date: date,
    snapshot_run_id: uuid.UUID | None,
    expected_count: int | None,
) -> tuple[str, uuid.UUID | None]:
    """[Phase0-Fix#8] 正式步骤 `enqueue_chip_job`：把 chip 入队纳入统一执行合同。

    语义：
    - 只负责"入队"，不 await chip 计算（chip 由独立 Worker 异步执行）；
    - 通过统一执行器产生 step summary（可选步骤，失败不炸主链）；
    - 返回 (status, chip_job_id) 供主任务终态计算 partial_success。

    [CHANGE-20260729-006 ID 合同统一] chip.core_run_id 必须指向 snapshot_run_id
    （StockFeatureSnapshotRun.id，数据版本），不再指向 job_run_id。

    Returns:
        (status, chip_job_id)，status ∈ {succeeded, skipped, failed}
    """
    if snapshot_run_id is None:
        summary = {
            "step": "enqueue_chip_job",
            "status": "skipped",
            "skip_reason": "SNAPSHOT_RUN_ID_MISSING",
            "optional": True,
            "error_code": None,
            "error_message": None,
        }
        await _persist_step_summary(job_run_id, summary)
        logger.warning(
            "[AfterClose] snapshot_run_id 为 None，跳过 chip consensus job 入队: "
            "trade_date=%s", trade_date,
        )
        return "skipped", None

    captured: dict[str, Any] = {}

    async def _enqueue() -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            chip_job, chip_is_new = await create_after_close_chip_consensus_job(
                db=db,
                trade_date=trade_date,
                core_run_id=snapshot_run_id,
                scope="all_a_share",
                expected_count=expected_count,
            )
            await db.commit()
        if chip_job is None:
            # 返回 None 视为业务软失败（下方统一转 failed）
            return {"status": "failed", "reason": "CHIP_JOB_CREATE_RETURNED_NONE"}
        captured["job_id"] = chip_job.id
        return {
            "status": "succeeded",
            "job_id": str(chip_job.id),
            "is_new": chip_is_new,
        }

    result, summary = await execute_orchestrator_step(
        "enqueue_chip_job",
        _enqueue,
        timeout_seconds=_step_timeout("enqueue_chip_job"),
        optional=True,
        heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
        progress=_make_step_progress_callback(job_run_id, worker_id),
        cancellation_check=_make_step_cancellation_check(job_run_id),
    )

    # [Mypy-fix 2026-08-04] 先窄化 result 为 dict，避免 union-attr（result 可能为 None）
    if isinstance(result, dict):
        business_status = result.get("status")
        business_reason = result.get("reason")
    else:
        business_status = None
        business_reason = None
    if summary["status"] == "succeeded" and business_status == "failed":
        # 业务软失败如实反映到 step summary（不得出现 business=failed/step=succeeded）
        summary["status"] = "failed"
        summary["error_code"] = "CHIP_ENQUEUE_FAILED"
        summary["error_message"] = str(business_reason)
        await _persist_step_summary(job_run_id, summary)

    chip_job_id = captured.get("job_id")
    if summary["status"] == "succeeded":
        logger.info(
            "[AfterClose] chip consensus job 已入队（独立 Worker 异步执行）: "
            "chip_run_id=%s, snapshot_run_id=%s, expected_count=%s",
            chip_job_id, snapshot_run_id, expected_count,
        )
        return "succeeded", chip_job_id

    logger.warning(
        "[AfterClose] chip consensus job 入队未成功（主 run 将记 partial_success）: "
        "step_status=%s, trade_date=%s, snapshot_run_id=%s, error=%s",
        summary["status"], trade_date, snapshot_run_id, summary.get("error_message"),
    )
    return "failed", chip_job_id


def _make_step_heartbeat(
    job_run_id: uuid.UUID,
    worker_id: str | None,
    lease_epoch: int | None,
) -> Callable[[], Awaitable[None]]:
    """[Phase0] 构造执行器的单次心跳回调。

    执行器自身拥有唯一周期循环，这里只做一次 touch；
    不再把 _job_run_heartbeat_loop（无限循环）当作回调传入。
    """

    async def _hb() -> None:
        await touch_job_run_heartbeat(
            job_run_id, worker_id=worker_id, lease_epoch=lease_epoch,
        )

    return _hb


async def _persist_step_summary(
    job_run_id: uuid.UUID,
    summary: dict[str, Any],
) -> None:
    """[Phase0] 将（可能被调用方修正过的）step summary 落库到 metadata.step_summary。"""
    step = summary.get("step")
    if not step:
        return
    try:
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            if job_run is None:
                return
            meta = _parse_metadata(job_run)
            step_summary = dict(meta.get("step_summary") or {})
            step_summary[step] = summary
            meta["step_summary"] = step_summary
            job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
            await db.commit()
    except Exception as exc:
        logger.warning(
            "[AfterClose] step summary 持久化失败: step=%s, error=%s", step, exc,
        )


def _make_step_cancellation_check(
    job_run_id: uuid.UUID,
) -> Callable[[], Awaitable[bool]]:
    """构造协作取消检查：查询 job_run.status == 'cancelled' 即视为已取消。

    管理员 cancel 写入 cancelled 后，长步骤在心跳周期后检查到即标记 cancelled 收尾。
    """

    async def _check() -> bool:
        try:
            async with AsyncSessionLocal() as db:
                job_run = await db.get(SchedulerJobRun, job_run_id)
                if job_run is None:
                    return True
                return job_run.status == "cancelled"
        except Exception:
            return False

    return _check


async def _execute_syncing_boards(
    *,
    job_run_id: uuid.UUID,
    trade_date: date,
    board_sync_disabled: bool,
    non_trading_day: bool,
) -> dict[str, Any]:
    """[AC-02] syncing_boards 业务体（软失败，不阻断主流程）。

    返回 dict（status=succeeded/failed/skipped），由统一执行器包装为 step_summary。
    内部保留全部现有语义：Redis 状态、job_run_event、metadata.board_sync_result。
    """
    from app.config import get_settings
    from app.services.board_sync_service import record_sync_status, sync_boards
    from app.services.wencai_board_provider import fetch_board_snapshot

    if non_trading_day:
        logger.info(
            "[AfterClose] 非交易日，跳过板块同步: job_run_id=%s", job_run_id,
        )
        return {"status": "skipped", "reason_code": "non_trading_day"}
    if board_sync_disabled:
        logger.info(
            "[AfterClose] BOARD_SYNC_ENABLED=false，跳过板块同步: job_run_id=%s", job_run_id,
        )
        await record_sync_status({
            "status": "skipped",
            "source": "wencai",
            "reused_previous_snapshot": True,
        })
        await _record_board_sync_outcome(
            job_run_id=job_run_id,
            outcome={
                "status": "skipped",
                "source": "wencai",
                "reused_previous_snapshot": True,
                "reason_code": "board_sync_disabled",
            },
            level="info",
            message="板块同步跳过（BOARD_SYNC_ENABLED=false）",
        )
        return {"status": "skipped", "reason_code": "board_sync_disabled"}

    settings = get_settings()
    if not settings.board_sync_enabled:
        return {"status": "skipped", "reason_code": "board_sync_disabled"}

    board_sync_start = time.monotonic()
    try:
        snapshot = await fetch_board_snapshot()
        async with AsyncSessionLocal() as db:
            async with db.begin():
                board_result = await sync_boards(
                    db,
                    snapshot,
                    instrument_resolver=_resolve_instruments_for_board_sync,
                    effective_date=trade_date,
                )

        await record_sync_status({
            "status": "succeeded",
            "source": "wencai",
            "raw_rows": board_result["raw_rows"],
            "resolved": board_result["resolved"],
            "unresolved": board_result["unresolved"],
            "industry_count": board_result["industry_count"],
            "concept_count": board_result["concept_count"],
            "membership_count": board_result["membership_count"],
            "duration_ms": int((time.monotonic() - board_sync_start) * 1000),
            "error_code": None,
            "reused_previous_snapshot": False,
        })

        board_sync_duration_ms = int((time.monotonic() - board_sync_start) * 1000)
        board_success_outcome = {
            "status": "succeeded",
            "source": "wencai",
            "raw_rows": board_result["raw_rows"],
            "resolved": board_result["resolved"],
            "unresolved": board_result["unresolved"],
            "industry_count": board_result["industry_count"],
            "concept_count": board_result["concept_count"],
            "membership_count": board_result["membership_count"],
            "duration_ms": board_sync_duration_ms,
            "error_code": None,
            "reused_previous_snapshot": False,
        }
        await _record_board_sync_outcome(
            job_run_id=job_run_id,
            outcome=board_success_outcome,
            level="info",
            message=(
                f"板块同步成功: 行业={board_result['industry_count']}, "
                f"概念={board_result['concept_count']}, "
                f"关系={board_result['membership_count']}, "
                f"耗时={board_sync_duration_ms}ms"
            ),
        )
        return {"status": "succeeded"}
    except Exception as board_exc:
        # 软失败：不覆盖旧数据、不阻断 DSA/快照/发布
        logger.exception(
            "[AfterClose] 板块同步失败（软失败，沿用上次数据）: %s", board_exc,
        )
        await record_sync_status({
            "status": "failed",
            "source": "wencai",
            "error_code": type(board_exc).__name__,
            "reused_previous_snapshot": True,
            "duration_ms": int((time.monotonic() - board_sync_start) * 1000),
        })
        board_fail_duration_ms = int((time.monotonic() - board_sync_start) * 1000)
        board_fail_outcome = {
            "status": "failed",
            "source": "wencai",
            "error_code": type(board_exc).__name__,
            "reused_previous_snapshot": True,
            "duration_ms": board_fail_duration_ms,
        }
        await _record_board_sync_outcome(
            job_run_id=job_run_id,
            outcome=board_fail_outcome,
            level="warn",
            message=(
                f"板块同步失败（软失败，沿用上次数据）: "
                f"error={type(board_exc).__name__}, "
                f"耗时={board_fail_duration_ms}ms"
            ),
        )
        return {"status": "failed", "error_code": type(board_exc).__name__}



class LeaseEpochMismatchError(Exception):
    """[PRD §4.3 JOB-02] lease_epoch 不匹配，旧 Worker 写入被拒绝。

    触发场景：
    - Worker A 持有 lease_epoch=N，被 watchdog 标记 interrupted
    - auto_resume 转换为 resume_queued，Worker B 领取并递增 lease_epoch=N+1
    - Worker A 复活后调用 _update_heartbeat_and_step（fenced UPDATE）
    - WHERE lease_epoch = N 不匹配（DB 已为 N+1），rowcount=0
    - 抛出本异常，Worker A 不再写状态（避免覆盖 Worker B 的进度）

    异常处理：
    - execute_after_close_run 接到此异常时，不再标记 failed（任务已由其他 Worker 接管）
    - 仅记录日志后 re-raise，调用方（Worker）也仅记录不崩溃
    """

    pass


# [PRD §4.3 JOB-02] - 当前 Worker 的 lease_epoch（None 表示 legacy 模式，跳过 fencing）
# 通过 contextvars.ContextVar 在 execute_after_close_run 入口设置，
# asyncio.create_task 自动继承到 _job_run_heartbeat_loop 等子任务。
_current_lease_epoch: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_current_lease_epoch", default=None,
)


class AfterCloseRunStatus(StrEnum):
    """盘后编排流水线状态枚举。

    状态流转：
    queued → refreshing_daily → syncing_boards → checking_coverage
      → computing_features → publishing → computing_review → succeeded
    任意步骤异常 → failed（syncing_boards 除外：软失败不阻断主流程）

    [Step Contract 2026-08-03] 总任务级终态补充：
    - PARTIAL_SUCCESS：核心已发布（stock_core/board）但可选阶段（auction/review/chip）失败/跳过
    - INTERRUPTED：Worker 崩溃/租约失效，由 watchdog 标记（区别于主动 failed）
    - CANCELLED：管理员协作式取消
    步骤级状态（succeeded/skipped/unavailable/failed/timed_out/cancelled/interrupted）
    由 metadata.step_summary 表达，不在此重复定义。
    """

    QUEUED = "queued"
    REFRESHING_DAILY = "refreshing_daily"
    SYNCING_BOARDS = "syncing_boards"
    CHECKING_COVERAGE = "checking_coverage"
    # [CHANGE-20260724-002 Phase 5] 4 步收敛为 computing_features
    # 旧 enum 保留用于历史 run 兼容读取（admin 页面不报错）
    CREATING_DSA = "creating_dsa"
    WAITING_DSA_WORKER = "waiting_dsa_worker"
    QUALITY_GATE = "quality_gate"
    FEATURE_SNAPSHOT = "feature_snapshot"
    COMPUTING_FEATURES = "computing_features"
    PUBLISHING = "publishing"
    # [CHANGE-20260801-REVIEW-CLOSURE] 新增复盘计算与发布阶段
    COMPUTING_REVIEW = "computing_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL_SUCCESS = "partial_success"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


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


async def _record_board_sync_outcome(
    job_run_id: uuid.UUID,
    outcome: dict[str, Any],
    level: str,
    message: str,
) -> None:
    """[AfterClose] - 记录板块同步结果到 job_run_events + metadata_json。

    PR #77 收口 §三.3：成功/失败/跳过均写入持久事件和 metadata，
    使管理后台盘后流水线时间线可看到完整结果（不只 Redis 和 logger）。

    Args:
        job_run_id: SchedulerJobRun ID
        outcome: 同步结果 dict（status/source/raw_rows/resolved/unresolved/...）
        level: 事件级别 info/warn/error
        message: 事件消息
    """
    async with AsyncSessionLocal() as db:
        job_run = await _get_job_run_or_raise(db, job_run_id)
        existing_meta = _parse_metadata(job_run)
        new_meta = dict(existing_meta)
        new_meta["board_sync_result"] = outcome
        job_run.metadata_json = json.dumps(new_meta, ensure_ascii=False)
        await append_event(
            db=db,
            job_run_id=job_run.id,
            step=AfterCloseRunStatus.SYNCING_BOARDS.value,
            level=level,
            message=message,
            payload=outcome,
        )
        await db.commit()


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
    last_completed_step: str | None,
    worker_id: str | None = None,
) -> None:
    """[Phase5] - 更新 heartbeat + lease + metadata.last_completed_step（flush 不 commit）。

    每个阶段完成后调用，用于：
    - 断点恢复：下次重启时根据 last_completed_step 跳过已成功阶段
    - 心跳租约：防止 Admin 页面误判任务卡死或租约过期

    [PRD §4.3 JOB-02] lease_epoch fencing：
    - 当 _current_lease_epoch ContextVar 不为 None 时，使用 raw SQL UPDATE
      WHERE lease_epoch = :expected_epoch，旧 Worker（lease 已被新 Worker 递增）写入被拒绝
    - ContextVar 为 None（legacy 模式）时，保持 ORM 属性更新（向后兼容）
    - 写入被拒绝时抛 LeaseEpochMismatchError，调用方应停止处理（任务已被新 Worker 接管）

    Args:
        db: 异步会话
        job_run: SchedulerJobRun 记录（已在 session 中）
        last_completed_step: 刚完成的阶段名（AfterCloseRunStatus.value）；
            [Phase0] 传 None 表示仅刷新心跳/租约，不推进检查点
            （用于 Review 等失败步骤，避免下次 resume 误跳过）
        worker_id: Worker 实例标识（非 None 时同步更新 worker_instance_id）

    Raises:
        LeaseEpochMismatchError: lease_epoch 不匹配（ContextVar 设置且 fenced UPDATE rowcount=0）
    """
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)
    # 保留已有 metadata（含 feature_snapshot_progress / feature_snapshot_run_id 等），
    # 仅更新 last_completed_step。
    meta = _parse_metadata(job_run)
    # [Phase0] None = 仅心跳，不推进检查点（保留原有 last_completed_step）
    if last_completed_step is not None:
        meta["last_completed_step"] = last_completed_step
    metadata_json_str = json.dumps(meta, ensure_ascii=False)

    expected_epoch = _current_lease_epoch.get()
    if expected_epoch is None:
        # Legacy 模式：ORM 属性更新（向后兼容无 lease_epoch 的调用）
        job_run.heartbeat_at = now
        job_run.lease_expires_at = lease_expires_at
        if worker_id is not None:
            job_run.worker_instance_id = worker_id
        job_run.metadata_json = metadata_json_str
        await db.flush()
        return

    # [JOB-02] fenced UPDATE：raw SQL WHERE lease_epoch = :expected_epoch
    # 同步更新 heartbeat / lease / worker_id / metadata_json，单事务原子操作
    # 使用 RETURNING + fetchall() 计数（与 recover_stale_scheduler_job_runs / worker.py 模式一致），
    # 避免 mypy 对 Result.rowcount 的 attr-defined 误报
    update_sql = text(
        """
        UPDATE scheduler_job_runs
        SET heartbeat_at = :now,
            lease_expires_at = :lease_expires,
            worker_instance_id = COALESCE(:worker_id, worker_instance_id),
            metadata_json = :metadata
        WHERE id = :id AND lease_epoch = :expected_epoch
        RETURNING id
        """
    )
    result = await db.execute(update_sql, {
        "now": now,
        "lease_expires": lease_expires_at,
        "worker_id": worker_id,
        "metadata": metadata_json_str,
        "id": job_run.id,
        "expected_epoch": expected_epoch,
    })
    if not result.fetchall():
        raise LeaseEpochMismatchError(
            f"lease_epoch 不匹配，旧 Worker 写入被拒绝: "
            f"job_run_id={job_run.id}, expected_epoch={expected_epoch}"
        )
    # 同步 ORM 对象属性（避免后续读取脏数据）
    job_run.heartbeat_at = now
    job_run.lease_expires_at = lease_expires_at
    if worker_id is not None:
        job_run.worker_instance_id = worker_id
    job_run.metadata_json = metadata_json_str
    await db.flush()


async def _job_run_heartbeat_loop(
    job_run_id: uuid.UUID,
    worker_id: str | None = None,
    interval: int = 30,
    lease_epoch: int | None = None,
) -> None:
    """[AfterClose] - 后台心跳任务：定期更新 heartbeat_at + lease_expires_at。

    用于长阶段（如 refresh_all_instruments 约13分钟）期间防止 watchdog 误判 stale。
    被取消时安静退出（CancelledError 不传播）。

    [PRD §4.3 JOB-02] lease_epoch fencing：
    - lease_epoch 非 None 时，使用 raw SQL UPDATE WHERE lease_epoch = :expected AND status='running'
    - rowcount=0 表示 Worker 已被中断或被新 Worker 接管，心跳安静退出（不抛异常）
    - lease_epoch 为 None（legacy 模式）时，保持 ORM 属性更新（向后兼容）

    Args:
        job_run_id: 编排任务 ID
        worker_id: Worker 实例标识（非 None 时同步更新 worker_instance_id）
        interval: 心跳间隔（秒，默认 30）
        lease_epoch: 当前 Worker 持有的 lease_epoch（None 表示 legacy 模式）
    """
    while True:
        try:
            await asyncio.sleep(interval)
            alive = await touch_job_run_heartbeat(
                job_run_id, worker_id=worker_id, lease_epoch=lease_epoch,
            )
            if not alive:
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "[AfterClose] 心跳更新失败 job_run_id=%s: %s",
                job_run_id, exc,
            )


async def touch_job_run_heartbeat(
    job_run_id: uuid.UUID,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """[Phase0] 单次心跳 touch：更新 heartbeat_at + lease_expires_at。

    从 _job_run_heartbeat_loop 中抽出，供统一执行器的单一周期循环调用，
    避免"把一个无限循环当作 heartbeat 回调传进执行器"。

    Returns:
        True  — 心跳写入成功，任务仍持有 lease；
        False — lease_epoch 不匹配或任务已非 running（调用方应停止心跳）。
    """
    async with AsyncSessionLocal() as db:
        now = datetime.now(ZoneInfo("Asia/Shanghai"))
        lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)

        if lease_epoch is None:
            # Legacy 模式：ORM 属性更新（向后兼容）
            job_run = await _get_job_run_or_raise(db, job_run_id)
            if job_run is None or job_run.status != "running":
                return False
            job_run.heartbeat_at = now
            job_run.lease_expires_at = lease_expires_at
            if worker_id is not None:
                job_run.worker_instance_id = worker_id
            await db.commit()
            return True

        # [JOB-02] fenced UPDATE：检查 lease_epoch + status='running'
        # 失败说明 Worker 已被 watchdog 标记 interrupted 或其他 Worker 接管
        update_sql = text(
            """
            UPDATE scheduler_job_runs
            SET heartbeat_at = :now,
                lease_expires_at = :lease_expires,
                worker_instance_id = COALESCE(:worker_id, worker_instance_id)
            WHERE id = :id
                AND lease_epoch = :expected_epoch
                AND status = 'running'
            RETURNING id
            """
        )
        result = await db.execute(update_sql, {
            "now": now,
            "lease_expires": lease_expires_at,
            "worker_id": worker_id,
            "id": job_run_id,
            "expected_epoch": lease_epoch,
        })
        if not result.fetchall():
            logger.warning(
                "[AfterClose] 心跳 lease_epoch 不匹配或任务非 running，"
                "退出心跳（任务已被中断或被新 Worker 接管）: "
                "job_run_id=%s, expected_epoch=%s",
                job_run_id, lease_epoch,
            )
            return False
        await db.commit()
        return True


# [Heartbeat] - feature_snapshot 进度事件采样间隔（instrument 数）
_FEATURE_SNAPSHOT_PROGRESS_EVENT_INTERVAL = 500


async def _resolve_instruments_for_board_sync(
    symbols: list[str],
    session: AsyncSession | None = None,
) -> dict[str, uuid.UUID]:
    """[BoardSync] - 按 symbol 批量查询现有 Instrument.id（供 board_sync_service 使用）。

    与 worker.py 的 _resolve_instruments 逻辑一致，独立定义为模块级函数避免循环依赖。
    session 参数仅供测试注入；生产调用不传，内部新建 AsyncSessionLocal。
    """
    from sqlalchemy import select

    from app.models.instrument import Instrument

    if not symbols:
        return {}

    async def _do_resolve(s: AsyncSession) -> dict[str, uuid.UUID]:
        stmt = select(Instrument.id, Instrument.symbol).where(
            Instrument.symbol.in_(symbols)
        )
        result = await s.execute(stmt)
        return {row.symbol: row.id for row in result}

    if session is not None:
        return await _do_resolve(session)
    async with AsyncSessionLocal() as session:
        return await _do_resolve(session)


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
                    # [Fix 2026-08-03] append_event 后必须 commit，否则事件随 session 退出回滚
                    await db.commit()
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

    [P0-1] 修复策略 - 统计限定 source_run_id：
    - 统计 stock_feature_snapshots WHERE source_run_id == snapshot_run.id（禁止按 trade_date 聚合）

    [P0-2] 修复策略 - DSA publish 前置检查 + tracked run 恢复：
    - 若 snapshot_run.id 匹配 metadata.feature_snapshot_run_id 且 job_run 仍可恢复
      （interrupted/failed），返回 action='resume_pending'，保持 run 为 running
    - 否则检查 DSA StrategyRun.published_at：
      - DSA 未 publish → 标记 failed（不得在 DSA 未发布时标记 succeeded）
      - DSA 已 publish 且 success_rate >= threshold → 标记 succeeded 并写 published_at
      - DSA 已 publish 但 success_rate < threshold → 标记 failed

    返回：
        被修复的 snapshot run 列表，每项含 snapshot_run_id / trade_date / action / reason。
        action ∈ {'resume_pending', 'succeeded', 'failed'}
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

            # [P0-1] 统计实际 snapshot 行数 - 必须限定 source_run_id == snapshot_run.id
            # 禁止只按 trade_date 统计其他 run 的数据
            count_stmt = select(func.count()).select_from(StockFeatureSnapshot).where(
                StockFeatureSnapshot.source_run_id == snapshot_run.id,
            )
            actual_count = (await db.execute(count_stmt)).scalar() or 0
            expected_count = snapshot_run.expected_count or 0
            success_rate = actual_count / expected_count if expected_count > 0 else 0.0

            # [P0-2] 检查此 snapshot_run 是否为 metadata 中 tracked 的可恢复 run
            # 对于 feature_snapshot_run_id 匹配、仍可恢复的任务，返回 resume_pending
            # 并保持 run 为 running，不标记 succeeded/failed
            tracked_run_id_str = meta.get("feature_snapshot_run_id")
            is_tracked = (
                tracked_run_id_str is not None
                and str(snapshot_run.id) == tracked_run_id_str
            )
            if is_tracked and job_run.status in ("interrupted", "failed"):
                repaired.append({
                    "snapshot_run_id": str(snapshot_run.id),
                    "trade_date": trade_date.isoformat(),
                    "action": "resume_pending",
                    "reason": "tracked_run_awaiting_resume",
                    "actual_count": actual_count,
                    "expected_count": expected_count,
                    "success_rate": success_rate,
                })
                logger.info(
                    "[Repair] snapshot run 为 tracked 且可恢复，保持 running 等待恢复: "
                    "run_id=%s, actual=%s, expected=%s",
                    snapshot_run.id, actual_count, expected_count,
                )
                continue

            # [P0-2] 检查 DSA 是否已 publish - 未 publish 不得标记 snapshot succeeded
            dsa_run_id_str = meta.get("dsa_run_id")
            dsa_published = False
            if dsa_run_id_str:
                try:
                    dsa_run_id_uuid = uuid.UUID(dsa_run_id_str)
                    dsa_run = await db.get(StrategyRun, dsa_run_id_uuid)
                    dsa_published = (
                        dsa_run is not None
                        and dsa_run.published_at is not None
                    )
                except (ValueError, TypeError):
                    pass

            if not dsa_published:
                # [P0-2] DSA 未 publish - 不得标记 snapshot succeeded，标记 failed
                await finish_snapshot_run(
                    db, snapshot_run,
                    status=STATUS_FAILED,
                    snapshot_count=actual_count,
                    failed_count=expected_count - actual_count,
                    expected_count=expected_count,
                    metadata={
                        "source": "after_close_orchestrator",
                        "scope": "full",
                        "reason": "dsa_not_published_or_orchestrator_interrupted",
                        "repaired_at": now.isoformat(),
                    },
                )
                repaired.append({
                    "snapshot_run_id": str(snapshot_run.id),
                    "trade_date": trade_date.isoformat(),
                    "action": "failed",
                    "reason": "dsa_not_published_or_orchestrator_interrupted",
                    "actual_count": actual_count,
                    "expected_count": expected_count,
                    "success_rate": success_rate,
                })
                logger.info(
                    "[Repair] snapshot run 修复为 failed (DSA 未发布): run_id=%s, "
                    "actual=%s, expected=%s",
                    snapshot_run.id, actual_count, expected_count,
                )
            elif expected_count > 0 and success_rate >= success_rate_threshold:
                # DSA 已 publish 且快照足够 - 标记 succeeded
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
                # DSA 已 publish 但快照不足 - 标记 failed
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


async def _execute_review_step(
    *,
    job_run_id: uuid.UUID,
    trade_date: date,
    snapshot_run_id: uuid.UUID | None,
    worker_id: str | None,
    skip_review: bool,
    stock_core_published: bool,
    aggregation_status: str,
) -> dict[str, Any]:
    """[AC-02] computing_review 业务体（软失败，不阻断主流程）。

    原为 execute_after_close_run 内的内联块；Phase0 收口后抽为独立业务体，
    由统一执行器 execute_orchestrator_step("computing_review", ...) 包装，
    满足 AC-02「所有顶层步骤必须通过统一步骤执行器」的合同。

    返回 result dict（status / failed / reason / run_id / publication_id /
    scope_count / signal_count / coverage / blockers / prereq_missing /
    resume_skipped），调用方据此：
    - 将业务 failed/gate_blocked 如实映射到 step_summary（软失败）；
    - 计算主任务 partial_success；
    - 仅成功才推进 last_completed_step 检查点。

    内部保留全部既有语义：幂等 create_run / compute_run / resume_run、
    publication pointer 唯一事实源、gate_blocked 不切 pointer、
    metadata.review_* 与事件时间线写入、断点恢复 skip_review 复用。
    失败不得使主 run failed（core 已发布），仅标记 failed 收 partial_success。
    """
    from app.services.review_orchestrator_service import (
        compute_run,
        create_run,
        publish_run,
    )
    from app.services.review_publication_service import (
        ReviewPublishBlockError,
        evaluate_publish_gate,
        get_published_review_run_id,
        is_formally_published_review_run,
    )

    # ---- 状态初始化（原 execute_after_close_run 内联块开头）----
    _review_run_id: uuid.UUID | None = None
    _review_status: str = "skipped"
    _review_reason: str | None = None
    _review_publication_id: uuid.UUID | None = None
    _review_scope_count: int = 0
    _review_signal_count: int = 0
    _review_coverage: float = 0.0
    _review_blockers: list[str] = []
    # [P0-1 2026-08-03] Review 失败（gate_blocked/计算失败）不再使整个 run failed，
    # 仅标记 _review_failed，主 run 收尾为 partial_success（core 已发布）。
    _review_failed: bool = False
    prereq_missing: bool = False

    if not skip_review:
        # 仅在 stock_core + board_analysis 均已正式发布时执行 review
        if stock_core_published and aggregation_status == "succeeded" and snapshot_run_id is not None:
            # 断点恢复：先从 metadata 读取已有 review_run_id
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                existing_meta = _parse_metadata(job_run)
                existing_review_run_id_str = existing_meta.get("review_run_id")
                if existing_review_run_id_str:
                    try:
                        _review_run_id = uuid.UUID(existing_review_run_id_str)
                        logger.info(
                            "[AfterClose] [Review] 断点恢复: 复用已有 review run: %s",
                            _review_run_id,
                        )
                    except (ValueError, TypeError):
                        _review_run_id = None

            # 写状态切换事件
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_REVIEW,
                    message=(
                        f"开始复盘计算与发布: trade_date={trade_date}, "
                        f"source_core_run_id={snapshot_run_id}"
                    ),
                    extra={
                        "review_run_id": str(_review_run_id) if _review_run_id else None,
                    },
                )
                await db.commit()

            try:
                # 1) 创建/复用 review run（幂等）
                async with AsyncSessionLocal() as review_db:
                    review_run = await create_run(
                        review_db,
                        trade_date=trade_date,
                        canary=False,
                        dry_run=False,
                        idempotency_key=f"after_close_orchestrator:{job_run_id}",
                    )
                    _review_run_id = review_run.id
                    logger.info(
                        "[AfterClose] [Review] create_run 完成: run_id=%s, "
                        "source_core=%s, source_board=%s, algo=%s, filter=%s",
                        review_run.id,
                        review_run.source_core_run_id,
                        review_run.source_board_run_id,
                        review_run.algorithm_version,
                        review_run.filter_version,
                    )

                    published_review_run_id = await get_published_review_run_id(
                        review_db, trade_date,
                    )
                    is_formally_published = is_formally_published_review_run(
                        review_run, published_review_run_id,
                    )
                    if is_formally_published:
                        _review_status = "published_already"
                        _review_reason = "idempotent_reuse_published_run"
                        _review_scope_count = review_run.expected_scope_count or 0
                        _review_signal_count = review_run.signal_count or 0
                        _review_coverage = float(review_run.coverage_ratio or 0)
                        logger.info(
                            "[AfterClose] [Review] 正式 pointer 仍指向 run，跳过计算与发布: %s",
                            review_run.id,
                        )
                    elif review_run.status == "published":
                        # Withdrawal 只撤销 pointer，历史发布状态不可篡改。
                        # 同算法唯一键仍可能返回旧 run；等待新算法版本创建新 run，
                        # 此处明确阻断复用，也不原地重算历史 run。
                        _review_status = "withdrawn_publication"
                        _review_reason = "published_run_not_referenced_by_live_pointer"
                        _review_scope_count = review_run.expected_scope_count or 0
                        _review_signal_count = review_run.signal_count or 0
                        _review_coverage = float(review_run.coverage_ratio or 0)
                        _review_blockers = [
                            "历史 published run 已无正式 Review pointer，禁止复用或原地重算",
                        ]
                        logger.warning(
                            "[AfterClose] [Review] run 保留历史 published 状态但 pointer 已撤销，"
                            "禁止复用: run_id=%s, live_pointer_run_id=%s",
                            review_run.id, published_review_run_id,
                        )
                    else:
                        # 2) 计算 review（metrics → signals → attribution → tracking）
                        # resume_run 语义：pending/failed/过期running自动重处理；
                        # succeeded item 不重算，保证输入不变则输出不变。
                        if (
                            review_run.status in ("signals_ready", "partial", "failed")
                            or (review_run.status == "computing" and review_run.started_at is not None)
                        ):
                            logger.info(
                                "[AfterClose] [Review] run 非 created 终态，调用 resume_run: "
                                "run_id=%s, status=%s",
                                review_run.id, review_run.status,
                            )
                            compute_result = await __import__(
                                "app.services.review_orchestrator_service",
                                fromlist=["resume_run"],
                            ).resume_run(review_db, review_run)
                        else:
                            compute_result = await compute_run(review_db, review_run)

                        _review_status = compute_result.get("status", "unknown")
                        _review_scope_count = compute_result.get("expected_scope_count", 0)
                        _review_signal_count = compute_result.get("signal_count", 0)
                        _review_coverage = compute_result.get("coverage_ratio", 0.0)
                        logger.info(
                            "[AfterClose] [Review] compute_run 完成: run_id=%s, "
                            "status=%s, scopes=%d, signals=%d, coverage=%.4f",
                            review_run.id, _review_status,
                            _review_scope_count, _review_signal_count, _review_coverage,
                        )

                    await review_db.commit()

                # 3) 发布 review（切 publication pointer）
                if _review_status != "published_already":
                    async with AsyncSessionLocal() as review_db2:
                        from app.services.review_orchestrator_service import get_run
                        review_run2 = await get_run(review_db2, _review_run_id)
                        if review_run2 is None:
                            raise RuntimeError(
                                f"review run 计算后读不到: run_id={_review_run_id}"
                            )

                        # 先评估门禁（不 force），记录 blockers 便于排查
                        publishable, blockers = await evaluate_publish_gate(
                            review_db2, review_run2,
                        )
                        _review_blockers = blockers
                        logger.info(
                            "[AfterClose] [Review] publish gate: publishable=%s, blockers=%s",
                            publishable, blockers,
                        )

                        if publishable:
                            publication, _ = await publish_run(review_db2, review_run2, force=False)
                            if publication is None:
                                # force=False 且门禁已通过时理论不可达；
                                # 防御性收口，避免 None 解引用静默通过
                                raise RuntimeError(
                                    "review publish 门禁通过但未返回 pointer: "
                                    f"run_id={_review_run_id}"
                                )
                            _review_status = "published"
                            _review_publication_id = publication.id
                            _review_reason = None
                            logger.info(
                                "[AfterClose] [Review] publish 成功: publication_id=%s, "
                                "review_run_id=%s",
                                publication.id, _review_run_id,
                            )
                        else:
                            # 门禁不通过但 run 已计算完成：视为 partial，不抛异常阻断主流程
                            # 但 review_status=gate_blocked，metadata 明确记录 blockers
                            _review_status = "gate_blocked"
                            _review_reason = (
                                f"publish_gate_blocked: {'; '.join(blockers)}"
                            )
                            logger.warning(
                                "[AfterClose] [Review] publish gate 不通过，不切 pointer: "
                                "run_id=%s, blockers=%s",
                                _review_run_id, blockers,
                            )
                            # [P0-1 2026-08-03 partial_success] Review gate_blocked 不再
                            # 让整个 run failed：core（stock_core/board）已成功发布，
                            # 仅标记 review 阶段失败，主 run 收尾为 partial_success。
                            _review_failed = True
                            logger.error(
                                "[AfterClose] [Review] publish gate 不通过，"
                                "主 run 将标记 partial_success: blockers=%s",
                                blockers,
                            )
                        await review_db2.commit()

            except ReviewPublishBlockError as pub_block_exc:
                _review_status = "gate_blocked"
                _review_blockers = list(pub_block_exc.blockers or [])
                _review_reason = f"publish_gate_blocked: {'; '.join(_review_blockers)}"
                _review_failed = True
                logger.error(
                    "[AfterClose] [Review] publish gate 阻塞（partial_success）: %s", pub_block_exc,
                )
            except Exception as review_exc:
                _review_status = "failed"
                _review_reason = f"{type(review_exc).__name__}: {review_exc}"[:500]
                _review_failed = True
                logger.error(
                    "[AfterClose] [Review] 复盘计算或发布失败（partial_success，core 已发布）: "
                    "trade_date=%s, error=%s",
                    trade_date, review_exc, exc_info=True,
                )
                # 写 review_failed 事件（供 admin 时间线展示）
                try:
                    async with AsyncSessionLocal() as db:
                        job_run = await _get_job_run_or_raise(db, job_run_id)
                        await append_event(
                            db=db,
                            job_run_id=job_run_id,
                            step=AfterCloseRunStatus.COMPUTING_REVIEW.value,
                            level="error",
                            message=(
                                f"复盘阶段失败: status={_review_status}, "
                                f"reason={_review_reason}"
                            ),
                            payload={
                                "review_run_id": str(_review_run_id) if _review_run_id else None,
                                "review_status": _review_status,
                                "review_reason": _review_reason,
                                "review_blockers": _review_blockers,
                            },
                        )
                        await db.commit()
                except Exception as inner_exc:
                    logger.warning(
                        "[AfterClose] [Review] 写入 review_failed 事件失败: %s",
                        inner_exc,
                    )

            finally:
                # 无论成功/失败，更新 metadata 记录 review_run_id/status/reason
                try:
                    async with AsyncSessionLocal() as db:
                        job_run = await _get_job_run_or_raise(db, job_run_id)
                        meta = _parse_metadata(job_run)
                        meta["review_run_id"] = (
                            str(_review_run_id) if _review_run_id else None
                        )
                        meta["review_status"] = _review_status
                        meta["review_reason"] = _review_reason
                        meta["review_publication_id"] = (
                            str(_review_publication_id)
                            if _review_publication_id
                            else None
                        )
                        meta["review_scope_count"] = _review_scope_count
                        meta["review_signal_count"] = _review_signal_count
                        meta["review_coverage"] = _review_coverage
                        meta["review_blockers"] = _review_blockers
                        job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                        await db.commit()
                except Exception as meta_exc:
                    logger.warning(
                        "[AfterClose] [Review] 更新 review metadata 失败: %s",
                        meta_exc,
                    )

            # [Phase0-Fix#7] review 阶段收尾：
            # 只有 review 真正成功才推进 last_completed_step=computing_review。
            # 失败/gate_blocked 时若仍推进检查点，下次 restart_from/resume 会
            # 直接跳过失败的 Review，破坏断点恢复语义。
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_REVIEW,
                    message=(
                        f"复盘完成: status={_review_status}, "
                        f"run_id={_review_run_id}, "
                        f"scopes={_review_scope_count}, signals={_review_signal_count}, "
                        f"coverage={_review_coverage:.4f}"
                    ),
                    payload={
                        "review_run_id": str(_review_run_id) if _review_run_id else None,
                        "review_status": _review_status,
                        "review_reason": _review_reason,
                        "review_publication_id": (
                            str(_review_publication_id)
                            if _review_publication_id
                            else None
                        ),
                        "review_scope_count": _review_scope_count,
                        "review_signal_count": _review_signal_count,
                        "review_coverage": _review_coverage,
                        "review_blockers": _review_blockers,
                    },
                    extra={
                        "review_run_id": str(_review_run_id) if _review_run_id else None,
                        "review_status": _review_status,
                    },
                )
                if _review_failed:
                    # 仅更新心跳，不推进 last_completed_step
                    await _update_heartbeat_and_step(
                        db, job_run, None, worker_id,
                    )
                    logger.warning(
                        "[AfterClose] [Review] 阶段失败，不推进 last_completed_step："
                        "status=%s, 下次 resume 将重新执行 computing_review",
                        _review_status,
                    )
                else:
                    await _update_heartbeat_and_step(
                        db, job_run, AfterCloseRunStatus.COMPUTING_REVIEW.value, worker_id,
                    )
                await db.commit()
        else:
            # 前置条件不满足：stock_core 或 board_analysis 未正式发布
            _review_status = "skipped"
            _review_reason = (
                f"prerequisite_missing: stock_core_published={stock_core_published}, "
                f"aggregation_status={aggregation_status}, "
                f"snapshot_run_id={'present' if snapshot_run_id else 'None'}"
            )
            prereq_missing = True
            logger.info(
                "[AfterClose] [Review] 跳过复盘阶段（前置条件不满足）: %s",
                _review_reason,
            )
            try:
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    meta = _parse_metadata(job_run)
                    meta["review_run_id"] = None
                    meta["review_status"] = _review_status
                    meta["review_reason"] = _review_reason
                    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                    await append_event(
                        db=db,
                        job_run_id=job_run_id,
                        step=AfterCloseRunStatus.COMPUTING_REVIEW.value,
                        level="warn",
                        message=f"复盘跳过: {_review_reason}",
                        payload={
                            "review_status": _review_status,
                            "review_reason": _review_reason,
                            "stock_core_published": stock_core_published,
                            "aggregation_status": aggregation_status,
                        },
                    )
                    await _update_heartbeat_and_step(
                        db, job_run, AfterCloseRunStatus.COMPUTING_REVIEW.value, worker_id,
                    )
                    await db.commit()
            except Exception as meta_exc2:
                logger.warning(
                    "[AfterClose] [Review] 更新 skipped review metadata 失败: %s",
                    meta_exc2,
                )
    else:
        # 断点恢复 skip_review=True：从 metadata 读取 review 信息
        _review_status = "skipped_by_resume"
        try:
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                meta = _parse_metadata(job_run)
                if meta.get("review_run_id"):
                    _review_run_id = uuid.UUID(meta["review_run_id"])
                _review_status = meta.get("review_status", "skipped_by_resume")
                _review_reason = meta.get("review_reason")
                if meta.get("review_publication_id"):
                    _review_publication_id = uuid.UUID(meta["review_publication_id"])
                _review_scope_count = int(meta.get("review_scope_count", 0) or 0)
                _review_signal_count = int(meta.get("review_signal_count", 0) or 0)
                _review_coverage = float(meta.get("review_coverage", 0.0) or 0.0)
                _review_blockers = list(meta.get("review_blockers", []) or [])
            logger.info(
                "[AfterClose] [Review] 断点恢复跳过复盘: status=%s, run_id=%s",
                _review_status, _review_run_id,
            )
        except Exception as resume_exc:
            logger.warning(
                "[AfterClose] [Review] 断点恢复读取 review metadata 失败: %s",
                resume_exc,
            )

    return {
        "status": _review_status,
        "failed": _review_failed,
        "reason": _review_reason,
        "run_id": _review_run_id,
        "publication_id": _review_publication_id,
        "scope_count": _review_scope_count,
        "signal_count": _review_signal_count,
        "coverage": _review_coverage,
        "blockers": _review_blockers,
        "prereq_missing": prereq_missing,
        "resume_skipped": skip_review,
    }


async def execute_after_close_run(
    job_run_id: uuid.UUID,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    dsa_poll_interval: int = _DSA_POLL_INTERVAL_SECONDS,
    dsa_poll_timeout: int = _DSA_POLL_TIMEOUT_SECONDS,
) -> None:
    """执行盘后编排流水线（后台异步，使用独立 AsyncSession）。

    [Phase5] 支持断点恢复 + 心跳租约：
    - 函数开头读取 metadata.last_completed_step，跳过已成功阶段
    - 每阶段完成后调用 _update_heartbeat_and_step 更新心跳 + lease + 检查点
    - worker_id 非 None 时同步更新 worker_instance_id

    [PRD §4.3 JOB-02] lease_epoch fencing：
    - lease_epoch 非 None 时设置 ContextVar，后续所有写操作校验 lease_epoch
    - 旧 Worker（lease 已被新 Worker 递增）的写操作被拒绝（LeaseEpochMismatchError）
    - 接到 LeaseEpochMismatchError 时不标记 failed（任务已由新 Worker 接管），仅 re-raise

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
    例外：LeaseEpochMismatchError 不标记 failed（任务已由新 Worker 接管）

    Args:
        job_run_id: 编排任务 ID
        trade_date: 交易日期
        worker_id: Worker 实例标识（非 None 时更新 worker_instance_id + 心跳）
        lease_epoch: 当前 Worker 持有的 lease_epoch（None 表示 legacy 模式，跳过 fencing）
        dsa_poll_interval: DSA 轮询间隔（秒，测试时可缩短）
        dsa_poll_timeout: DSA 轮询超时（秒，测试时可缩短）

    Raises:
        LeaseEpochMismatchError: lease_epoch 不匹配（任务已被新 Worker 接管）
        异常向上传播（调用方应捕获并记录日志）
    """
    # [JOB-02] 设置 lease_epoch ContextVar，子任务（asyncio.create_task）自动继承
    # _update_heartbeat_and_step 读取此 ContextVar 决定是否使用 fenced UPDATE
    _current_lease_epoch.set(lease_epoch)

    logger.info(
        "[AfterClose] 开始执行盘后编排: job_run_id=%s, trade_date=%s, worker_id=%s, "
        "lease_epoch=%s",
        job_run_id, trade_date, worker_id, lease_epoch,
    )

    bars_service = BarsSchedulerService()
    batch_service = StrategyBatchService()
    dsa_run_id: uuid.UUID | None = None
    published_run: Any = None
    # [P0 Atomicity] - snapshot_run_id / snapshot_error 在 try 块顶部初始化，
    # 保证断点恢复（skip_snapshot=True）时变量已定义，避免 NameError。
    snapshot_run_id: uuid.UUID | None = None
    snapshot_error: Exception | None = None
    snapshot_result: dict[str, Any] | None = None
    # [P0-7 修复 2026-07-29] cached_instrument_ids 初始化为 None，
    # 断点恢复 skip_computing=True 时仍可安全访问（chip job 创建时用于 expected_count）
    cached_instrument_ids: list[uuid.UUID] | None = None

    try:
        # [Phase5] - 读取断点恢复信息：last_completed_step + dsa_run_id + snapshot_run_id
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
            # [P0 Atomicity] - 断点恢复时从 metadata 读取 snapshot_run_id
            snapshot_run_id_str = meta.get("feature_snapshot_run_id")
            if snapshot_run_id_str:
                try:
                    snapshot_run_id = uuid.UUID(snapshot_run_id_str)
                except (ValueError, TypeError):
                    snapshot_run_id = None

        # [Repair] - 启动前修复上一次中断留下的 stuck running snapshot run，
        # 避免同 trade_date 的 running run 触发 partial unique index 冲突。
        # [P0-fix] repair 内部 finish_snapshot_run 只 flush 不 commit，
        # 调用方必须 commit 否则修复会随 session 关闭而回滚。
        try:
            async with AsyncSessionLocal() as db:
                repaired = await repair_stale_after_close_snapshot_runs(db)
                if repaired:
                    logger.info(
                        "[AfterClose] 启动前修复 %s 个 stuck snapshot run: %s",
                        len(repaired), repaired,
                    )
                await db.commit()
        except Exception as exc:
            logger.warning(
                "[AfterClose] 启动前 repair 失败，继续执行: %s", exc,
            )

        # [Phase5] - 根据last_completed_step 计算各阶段跳过标志
        # 阶段顺序（Phase 5 + Review Closure）：
        #   refreshing_daily → syncing_boards → computing_features
        #   → publishing → computing_review → succeeded
        # 旧步骤名（waiting_dsa_worker/quality_gate/feature_snapshot）兼容读取历史 run
        _completed_steps: dict[str | None, set[str]] = {
            None: set(),
            "queued": set(),
            "refreshing_daily": {"refreshing_daily"},
            "syncing_boards": {"refreshing_daily", "syncing_boards"},
            # [Phase 5] 4 步收敛为 computing_features
            "computing_features": {
                "refreshing_daily", "syncing_boards", "computing_features",
            },
            "publishing": {
                "refreshing_daily", "syncing_boards", "computing_features",
                "publishing",
            },
            # [CHANGE-20260801-REVIEW-CLOSURE] computing_review 断点恢复
            "computing_review": {
                "refreshing_daily", "syncing_boards", "computing_features",
                "publishing", "computing_review",
            },
            "succeeded": {
                "refreshing_daily", "syncing_boards", "computing_features",
                "publishing", "computing_review", "succeeded",
            },
            # [Phase 5] 旧步骤名兼容：历史 run 读取时映射到 computing_features 已完成
            "waiting_dsa_worker": {
                "refreshing_daily", "syncing_boards", "computing_features",
            },
            "quality_gate": {
                "refreshing_daily", "syncing_boards", "computing_features",
            },
            "feature_snapshot": {
                "refreshing_daily", "syncing_boards", "computing_features",
            },
        }
        completed: set[str] = _completed_steps.get(last_completed_step, set())
        if "succeeded" in completed:
            logger.info(
                "[AfterClose] 断点恢复: 已完成 succeeded，直接返回: job_run_id=%s",
                job_run_id,
            )
            return

        # [CHANGE-20260728-008] 原 dsa_only 模式已删除，
        # 改为通过 force?restart_from=daily_ready 设置 last_completed_step="refreshing_daily"，
        # 由 _completed_steps 自然跳过 refreshing_daily，后续步骤正常执行。

        skip_refresh = "refreshing_daily" in completed
        skip_board_sync = "syncing_boards" in completed
        # [Phase 5] 3 个旧 skip 标志收敛为 skip_computing
        skip_computing = "computing_features" in completed
        skip_publish = "publishing" in completed
        # [CHANGE-20260801-REVIEW-CLOSURE] review 阶段跳过标志
        skip_review = "computing_review" in completed

        logger.info(
            "[AfterClose] 断点恢复: last_completed_step=%s, "
            "skip_refresh=%s, skip_board_sync=%s, skip_computing=%s, "
            "skip_publish=%s, skip_review=%s",
            last_completed_step, skip_refresh, skip_board_sync, skip_computing,
            skip_publish, skip_review,
        )

        # ---- 步骤 1: refreshing_daily（统一执行器）----
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

            # [AC-02] 通过统一执行器运行：heartbeat（fenced）+ progress（写 step_summary）
            # + cancellation_check（协作式取消）+ 超时保护。
            # [JOB-02] lease_epoch 随 ContextVar 自动继承到 heartbeat 循环。
            async def _refresh_operation() -> dict[str, Any]:
                return await bars_service.refresh_all_instruments(
                    trade_date=trade_date,
                    db_session=None,
                    job_run_id=job_run_id,
                    trigger_dsa=False,
                )

            refresh_result, refresh_summary = await execute_orchestrator_step(
                "refreshing_daily",
                _refresh_operation,
                timeout_seconds=_step_timeout("refreshing_daily"),
                heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
                progress=_make_step_progress_callback(job_run_id, worker_id),
                cancellation_check=_make_step_cancellation_check(job_run_id),
            )
            if refresh_summary["status"] != "succeeded":
                raise RuntimeError(
                    f"refreshing_daily 未成功: status={refresh_summary['status']}, "
                    f"error={refresh_summary.get('error_message')}"
                )
            batch_result = refresh_result
            dsa_run_id = batch_result.dsa_run_id

            # ---- 步骤 2: syncing_boards（软失败，不阻断主流程，统一执行器）----
            # [AC-02] 通过统一执行器运行：产出 step_summary，软失败（optional）不抛出。
            if not skip_board_sync:
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.SYNCING_BOARDS,
                        message="开始同步问财板块数据",
                    )
                    await db.commit()

                # [Phase0-Fix#5] 正确区分 result 与 summary：
                # 之前写成 `board_summary, _ =`，把业务 result 当成执行器 summary，
                # 导致 result={"status":"failed"} 时 step summary 仍为 succeeded，
                # 且超时 result=None 时下方取下标会把可选失败升级为主链失败。
                board_result, board_step_summary = await execute_orchestrator_step(
                    "syncing_boards",
                    lambda: _execute_syncing_boards(
                        job_run_id=job_run_id,
                        trade_date=trade_date,
                        board_sync_disabled=False,
                        non_trading_day=(batch_result.skip_reason == "NON_TRADING_DAY"),
                    ),
                    timeout_seconds=_step_timeout("syncing_boards"),
                    optional=True,
                    heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
                    progress=_make_step_progress_callback(job_run_id, worker_id),
                    cancellation_check=_make_step_cancellation_check(job_run_id),
                )
                # [Phase0-Fix#5] 业务软失败必须如实反映到 step summary，
                # 否则会出现「业务 failed / 步骤 succeeded」的矛盾状态。
                # [Mypy-fix 2026-08-04] 先窄化 board_result 为 dict，避免 union-attr
                if isinstance(board_result, dict):
                    board_business_status = board_result.get("status")
                    board_error_code = board_result.get("error_code")
                    board_reason_code = board_result.get("reason_code")
                else:
                    board_business_status = None
                    board_error_code = None
                    board_reason_code = None
                if board_step_summary["status"] == "succeeded" and board_business_status:
                    if board_business_status == "failed":
                        board_step_summary["status"] = "failed"
                        board_step_summary["error_code"] = (
                            board_error_code or "BOARD_SYNC_SOFT_FAILURE"
                        )
                        board_step_summary["error_message"] = "板块同步软失败（沿用上次数据）"
                    elif board_business_status == "skipped":
                        board_step_summary["status"] = "skipped"
                        board_step_summary["skip_reason"] = board_reason_code
                    await _persist_step_summary(job_run_id, board_step_summary)
                logger.info(
                    "[AfterClose] syncing_boards 完成: step_status=%s, business_status=%s",
                    board_step_summary["status"], board_business_status,
                )

            # [Phase5] - syncing_boards 完成（或跳过），更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.SYNCING_BOARDS.value, worker_id,
                )
                await db.commit()

            if dsa_run_id is None:
                # [AfterClose] - 区分跳过原因：NON_TRADING_DAY（非交易日）vs 覆盖率不足 vs trigger_dsa=False
                skip_reason = batch_result.skip_reason
                if skip_reason == "NON_TRADING_DAY":
                    success_message = (
                        f"因非交易日跳过，未执行行情更新和选股: trade_date={trade_date}"
                    )
                    success_payload: dict[str, Any] = {"skip_reason": "NON_TRADING_DAY"}
                    success_extra: dict[str, Any] | None = {"skip_reason": "NON_TRADING_DAY"}
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
                        "[AfterClose] 非交易日，编排成功结束: job_run_id=%s", job_run_id,
                    )
                    return

                # [AC-04 / Phase 5A] checking_coverage 步骤：仅验证日线覆盖率就绪
                # PRD30 AC-04：盘后编排 readiness 只依赖目标交易日日线数据，
                # 15m 缺失不得阻塞 after-close run。15m intraday readiness 工具
                # 保留在 BarsCoverageService 供其他链路使用，after-close 不再调用。
                # [AC-02] 通过统一执行器运行（non-optional）：覆盖率不足时闭包抛错，
                # 执行器标记 failed 并重新抛出，由外围 except 标记整个 run failed。
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.CHECKING_COVERAGE,
                        message=f"开始检查日线覆盖率: trade_date={trade_date}",
                    )
                    await db.commit()

                async def _check_coverage_op() -> dict[str, Any]:
                    ok = (
                        batch_result.daily_coverage is not None
                        and batch_result.daily_coverage >= 0.9
                    )
                    if not ok:
                        raise RuntimeError(
                            f"日线覆盖率检查未通过: daily_coverage="
                            f"{batch_result.daily_coverage} < 0.9"
                        )
                    return {"daily_coverage": batch_result.daily_coverage, "ok": True}

                # [AC-02] 通过统一执行器运行（non-optional）：覆盖率不足时闭包抛错，
                # 执行器标记 step_summary=failed。覆盖率不足是预期内的"准入失败"，
                # 不应作为未处理异常向上传播（与 HEAD 行为一致：标记 failed 后 return），
                # 故在此捕获并转入优雅终态处理（下方 if not daily_coverage_ok 分支）。
                try:
                    _, coverage_summary = await execute_orchestrator_step(
                        "checking_coverage",
                        _check_coverage_op,
                        timeout_seconds=_step_timeout("checking_coverage"),
                        progress=_make_step_progress_callback(job_run_id, worker_id),
                        cancellation_check=_make_step_cancellation_check(job_run_id),
                    )
                    daily_coverage_ok = coverage_summary["status"] == "succeeded"
                except Exception as _cov_exc:
                    logger.warning(
                        "[AfterClose] [AC-04] 日线覆盖率检查未通过（准入失败）: "
                        "job_run_id=%s, error=%s",
                        job_run_id, _cov_exc,
                    )
                    daily_coverage_ok = False

                if not daily_coverage_ok:
                    # [AC-04] 日线覆盖率不足 → 标记 failed（不是 succeeded），不创建 DSA
                    fail_reasons: list[str] = [
                        f"daily_coverage={batch_result.daily_coverage} < 0.9"
                    ]
                    fail_message = (
                        f"日线覆盖率检查未通过，不创建 DSA: {', '.join(fail_reasons)}"
                    )
                    async with AsyncSessionLocal() as db:
                        job_run = await _get_job_run_or_raise(db, job_run_id)
                        await _update_orchestrator_status(
                            db=db,
                            job_run=job_run,
                            status=AfterCloseRunStatus.FAILED,
                            message=fail_message,
                            payload={
                                "daily_coverage": batch_result.daily_coverage,
                                "fail_reasons": fail_reasons,
                            },
                        )
                        job_run.status = "failed"
                        job_run.error_message = fail_message[:500]
                        job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                        await _update_heartbeat_and_step(
                            db, job_run, "failed", worker_id,
                        )
                        await db.commit()
                    logger.warning(
                        "[AfterClose] [AC-04] 日线覆盖率检查未通过，编排失败: "
                        "job_run_id=%s, %s",
                        job_run_id, fail_message,
                    )
                    return

                logger.info(
                    "[AfterClose] [AC-04] 日线覆盖率检查通过: daily=%.1f%%",
                    (batch_result.daily_coverage or 0) * 100,
                )

                # [Phase8A] trigger_dsa=False 且覆盖率达标：在 computing_features 前创建 DSA run
                # DSA 由 orchestrator 创建并原子 inline claim，避免 generic worker 抢先领取
                logger.info(
                    "[AfterClose] [Phase8A] 覆盖率达标，orchestrator 创建 DSA run: "
                    "trade_date=%s, coverage=%.1f%%",
                    trade_date, (batch_result.daily_coverage or 0) * 100,
                )
                from app.constants.strategy_keys import DSA_SELECTOR
                async with AsyncSessionLocal() as db:
                    dsa_run = await batch_service.create_batch_run(
                        db=db,
                        strategy_key=DSA_SELECTOR,
                        trade_date=trade_date,
                        run_type="scheduled",
                        claim_for_worker=f"orchestrator:{worker_id}",
                    )
                    await db.commit()
                    dsa_run_id = dsa_run.id
                    # 更新 metadata 记录 dsa_run_id
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.REFRESHING_DAILY,
                        message=f"orchestrator 已创建 DSA run: dsa_run_id={dsa_run_id}",
                        dsa_run_id=dsa_run_id,
                        payload={"dsa_run_id": str(dsa_run_id)},
                    )
                    await _update_heartbeat_and_step(
                        db, job_run, AfterCloseRunStatus.REFRESHING_DAILY.value, worker_id,
                    )
                    await db.commit()

        else:
            # [Phase5] - 断点恢复跳过日线刷新，dsa_run_id 从 metadata 读取
            # [CHANGE-20260728-008] 原 dsa_only 模式已删除。
            # 当 skip_refresh=True 且 dsa_run_id=None 时（如 force?restart_from=daily_ready），
            # 直接创建 DSA run（覆盖率已由 API 层校验）。
            if dsa_run_id is None:
                from app.constants.strategy_keys import DSA_SELECTOR
                logger.info(
                    "[AfterClose] 跳过日线刷新，直接创建 DSA run: "
                    "job_run_id=%s, trade_date=%s, last_completed_step=%s",
                    job_run_id, trade_date, last_completed_step,
                )
                async with AsyncSessionLocal() as db:
                    dsa_run = await batch_service.create_batch_run(
                        db=db,
                        strategy_key=DSA_SELECTOR,
                        trade_date=trade_date,
                        run_type="scheduled",
                        claim_for_worker=f"orchestrator:{worker_id}",
                    )
                    await db.commit()
                    dsa_run_id = dsa_run.id
                    # 更新 metadata 记录 dsa_run_id
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.REFRESHING_DAILY,
                        message=f"已创建 DSA run（跳过日线刷新）: dsa_run_id={dsa_run_id}",
                        dsa_run_id=dsa_run_id,
                        payload={"dsa_run_id": str(dsa_run_id)},
                    )
                    await _update_heartbeat_and_step(
                        db, job_run, AfterCloseRunStatus.REFRESHING_DAILY.value, worker_id,
                    )
                    await db.commit()

        # ---- 步骤 2: computing_features (Phase 5: 收敛 waiting_dsa_worker + quality_gate + feature_snapshot) ----
        # [CHANGE-20260724-002 Phase 5] scheduled after-close DSA 接入 MFCS 统一计算服务：
        # - DSA run 创建后 inline claim（status=running），防止 DSA worker 领取
        # - MFCS compute-once: DSA/SMC/Node 各 1 次，同一结果供 StrategyResult + snapshot
        # - 批次级事件预取（SQL=1）
        # - 组合质量门禁: DSA + continuous + event freshness 任一失败不 publish
        # manual DSA 和非 scheduled StrategyRun 继续走原 worker 路径，不受影响。
        if not skip_computing:
            # 2.1 设置 computing_features 状态
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_FEATURES,
                    message=f"开始统一特征计算: dsa_run_id={dsa_run_id}",
                    dsa_run_id=dsa_run_id,
                    payload={"dsa_run_id": str(dsa_run_id)},
                )
                await db.commit()

            # 2.2 inline claim DSA run（防止 worker 领取）
            # [Phase8A] DSA run 通过 claim_for_worker 创建时已是 status=running + worker_id，
            # 无需再次 claim；仅在 status=queued 时执行 legacy inline claim（断点恢复场景）
            dsa_already_completed = False
            async with AsyncSessionLocal() as db:
                dsa_run = await _get_strategy_run_or_raise(db, dsa_run_id)
                if dsa_run.status == "queued":
                    # Legacy/断点恢复：inline claim，防止 DSA worker 领取
                    dsa_run.status = "running"
                    dsa_run.started_at = datetime.now(UTC)
                    dsa_run.heartbeat_at = datetime.now(UTC)
                    dsa_run.worker_id = f"orchestrator:{worker_id}"
                    await db.commit()
                    logger.info(
                        "[AfterClose] legacy inline claim DSA run: run_id=%s, worker_id=%s",
                        dsa_run_id, dsa_run.worker_id,
                    )
                elif dsa_run.status == "running":
                    # [Phase8A-correction] cross-worker recovery with real fencing
                    # worker A 崩溃后 worker B 重新认领 child DSA
                    # 使用条件原子 UPDATE：基于 attempt_count（租约恢复计数）作为 fencing token
                    # 防止 worker A 恢复后使用旧 token 继续写入
                    expected_worker = f"orchestrator:{worker_id}"
                    if dsa_run.worker_id != expected_worker:
                        old_worker_id = dsa_run.worker_id
                        old_attempt_count = dsa_run.attempt_count or 0
                        now_utc = datetime.now(UTC)
                        new_lease_expires = now_utc + timedelta(minutes=30)

                        # 条件 UPDATE：status=running AND attempt_count=old（fencing token 匹配）
                        fence_stmt = (
                            update(StrategyRun)
                            .where(StrategyRun.id == dsa_run_id)
                            .where(StrategyRun.status == "running")
                            .where(StrategyRun.attempt_count == old_attempt_count)
                            .values(
                                worker_id=expected_worker,
                                attempt_count=old_attempt_count + 1,
                                heartbeat_at=now_utc,
                                lease_expires_at=new_lease_expires,
                            )
                        )
                        fence_result = await db.execute(fence_stmt)

                        # CursorResult.rowcount 在 Result 基类 typing 中缺失（SQLAlchemy 2.0 async 限制）
                        if fence_result.rowcount == 1:  # type: ignore[attr-defined]
                            await db.commit()
                            await db.refresh(dsa_run)
                            logger.info(
                                "[AfterClose] [Phase8A] 跨 worker fencing 成功: "
                                "run_id=%s, old_worker=%s, new_worker=%s, "
                                "attempt_count %d→%d, lease_expires=%s",
                                dsa_run_id, old_worker_id, expected_worker,
                                old_attempt_count, old_attempt_count + 1,
                                new_lease_expires.isoformat(),
                            )
                        else:
                            # 条件更新失败：重新读取当前状态
                            await db.rollback()
                            dsa_run = await _get_strategy_run_or_raise(db, dsa_run_id)
                            if dsa_run.status in ("completed", "published"):
                                dsa_already_completed = True
                                logger.info(
                                    "[AfterClose] 跨 worker fencing 失败（DSA 已完成）: "
                                    "run_id=%s, status=%s",
                                    dsa_run_id, dsa_run.status,
                                )
                            elif dsa_run.worker_id == expected_worker:
                                logger.info(
                                    "[AfterClose] 跨 worker fencing 跳过（已是当前 worker）: "
                                    "run_id=%s, worker_id=%s",
                                    dsa_run_id, dsa_run.worker_id,
                                )
                            else:
                                raise RuntimeError(
                                    f"跨 worker fencing 失败: run_id={dsa_run_id}, "
                                    f"current_worker={dsa_run.worker_id}, "
                                    f"current_attempt_count={dsa_run.attempt_count}, "
                                    f"expected_old_attempt_count={old_attempt_count}"
                                )
                    else:
                        logger.info(
                            "[AfterClose] DSA run 已原子 claim（Phase8A）: run_id=%s, worker_id=%s",
                            dsa_run_id, dsa_run.worker_id,
                        )
                elif dsa_run.status in ("completed", "published"):
                    # Worker 已完成（race condition），跳过 DSA 写入避免 DSA=2
                    dsa_already_completed = True
                    logger.info(
                        "[AfterClose] DSA run 已完成（worker 已处理），跳过 DSA 写入: run_id=%s status=%s",
                        dsa_run_id, dsa_run.status,
                    )
                elif dsa_run.status in ("failed", "partial_failed", "max_retries_exceeded"):
                    # [P0-2 2026-07-30] DSA failed → 调用正式 recovery service 创建新 run
                    # 禁止把失败 run 改回 queued；原失败 run 保留审计
                    from app.services.dsa_recovery_service import (
                        DSARecoveryError,
                        recover_failed_dsa_run,
                    )

                    logger.warning(
                        "[AfterClose] DSA run 失败（%s），调用 recovery service: run_id=%s",
                        dsa_run.status, dsa_run_id,
                    )
                    try:
                        new_dsa_run, is_new = await recover_failed_dsa_run(
                            db, job_run_id=job_run_id,
                            worker_id=worker_id,
                            lease_epoch=lease_epoch,
                        )
                        await db.commit()
                        dsa_run_id = new_dsa_run.id
                        logger.info(
                            "[AfterClose] DSA recovery 成功: old=%s, new=%s, "
                            "attempt_no=%s, is_new=%s",
                            dsa_run.id, new_dsa_run.id,
                            new_dsa_run.attempt_no, is_new,
                        )
                    except DSARecoveryError as recovery_exc:
                        logger.error(
                            "[AfterClose] DSA recovery 失败: %s", recovery_exc,
                        )
                        raise

            # 2.3 创建 snapshot run（复用原 feature_snapshot 步骤的 run 生命周期逻辑）
            snapshot_already_published = False
            try:
                async with AsyncSessionLocal() as db:
                    instrument_ids = await get_active_a_share_instruments(db)
                    cached_instrument_ids = instrument_ids

                    # [P0-4] 断点恢复：检查是否已有 tracked running snapshot run 可复用
                    _create_new_run = True
                    if snapshot_run_id is not None:
                        from app.models.stock_feature_snapshot_run import (
                            StockFeatureSnapshotRun as _SnapshotRun,
                        )
                        existing_run = await db.get(_SnapshotRun, snapshot_run_id)
                        if existing_run is not None and existing_run.status == "running":
                            logger.info(
                                "[AfterClose] 断点恢复: 复用已有 running snapshot run: "
                                "run_id=%s, trade_date=%s",
                                snapshot_run_id, trade_date,
                            )
                            _create_new_run = False

                    if _create_new_run:
                        snapshot_run = await create_snapshot_run(
                            db, trade_date, "after_close",
                            expected_count=len(instrument_ids),
                            metadata={"source": "after_close_orchestrator"},
                            scope="full",
                        )
                        await db.commit()
                        snapshot_run_id = snapshot_run.id
            except PublishedSnapshotRunExistsError as exc:
                logger.warning(
                    "[AfterClose] computing_features 已存在 published full run，"
                    "跳过 snapshot 计算，复用已有 run: trade_date=%s "
                    "existing_run_id=%s published_at=%s",
                    trade_date, exc.existing_run.id, exc.existing_run.published_at,
                )
                snapshot_run_id = exc.existing_run.id
                snapshot_result = {
                    "snapshot_count": 0, "failed_count": 0,
                    "schema_version": 1, "trade_date": trade_date.isoformat(),
                    "skipped_already_published": True,
                }
                snapshot_already_published = True

            # 2.4 写入 run_id 与 last_started_step（UI 不显示待执行，中断后知道从哪步恢复）
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_FEATURES,
                    message=f"开始统一特征计算: trade_date={trade_date}, run_id={snapshot_run_id}",
                    extra={
                        "feature_snapshot_run_id": str(snapshot_run_id),
                        "last_started_step": AfterCloseRunStatus.COMPUTING_FEATURES.value,
                    },
                )
                await db.commit()

            # 2.5 执行统一计算（MFCS compute-once + 批量事件预取 + snapshot 写入 + StrategyResult 写入）
            # [P0 Atomicity] snapshot 计算完成后不立即 finalize succeeded，
            # 等 DSA publish_run 成功后才标记 succeeded/published_at。
            # [AC-02] 通过统一执行器运行：heartbeat（fenced）+ progress + cancellation_check + 超时。
            if not snapshot_already_published:
                async def _compute_features_op() -> dict[str, Any]:
                    progress_callback = _build_feature_snapshot_progress_callback(
                        job_run_id, worker_id
                    )
                    from app.services.feature_snapshot_service import (
                        compute_review_core_with_run_items,
                    )

                    if snapshot_run_id is None:
                        raise RuntimeError("FEATURE_SNAPSHOT_RUN_ID_MISSING")
                    local_result = await compute_review_core_with_run_items(
                        trade_date=trade_date,
                        instrument_ids=cached_instrument_ids or [],
                        snapshot_run_id=snapshot_run_id,
                        worker_id=worker_id or "orchestrator",
                        lease_epoch=lease_epoch,
                        progress_callback=progress_callback,
                    )

                    # [CHANGE-20260728-007] 修复 running→completed 闭环：
                    # MFCS 只写入 snapshot，不写入 StrategyResult，也不推进 DSA run 状态。
                    # 在 MFCS 完成后显式调用 execute_run 写入 StrategyResult 并完成状态机。
                    # 不得在 publish 前伪造 completed；异常路径必须写 failed。
                    if not dsa_already_completed and dsa_run_id is not None:
                        logger.info(
                            "[AfterClose] 开始执行 DSA 策略（写入 StrategyResult + 推进状态）: "
                            "dsa_run_id=%s", dsa_run_id,
                        )
                        try:
                            async with AsyncSessionLocal() as db:
                                await batch_service.execute_run(
                                    db, dsa_run_id, job_run_id=job_run_id,
                                )
                                await db.commit()
                        except Exception as dsa_exec_exc:
                            logger.error(
                                "[AfterClose] DSA execute_run 失败: dsa_run_id=%s, error=%s",
                                dsa_run_id, dsa_exec_exc, exc_info=True,
                            )
                            # 异常路径必须写 failed（不得在 publish 前伪造 completed）
                            async with AsyncSessionLocal() as db:
                                dsa_run_on_failure = await db.get(
                                    StrategyRun, dsa_run_id,
                                )
                                if dsa_run_on_failure is not None and dsa_run_on_failure.status not in (
                                    "completed", "failed", "partial_failed", "published"
                                ):
                                    dsa_run_on_failure.status = "failed"
                                    dsa_run_on_failure.error_message = (
                                        f"execute_run 失败: {dsa_exec_exc}"[:500]
                                    )
                                    dsa_run_on_failure.finished_at = datetime.now(UTC)
                                    await db.commit()
                            raise
                    return local_result

                try:
                    snapshot_result, features_summary = await execute_orchestrator_step(
                        "computing_features",
                        _compute_features_op,
                        timeout_seconds=_step_timeout("computing_features"),
                        heartbeat=_make_step_heartbeat(
                            job_run_id, worker_id, lease_epoch,
                        ),
                        progress=_make_step_progress_callback(job_run_id, worker_id),
                        cancellation_check=_make_step_cancellation_check(job_run_id),
                    )
                except Exception as step_exc:
                    # 执行器已标记 step_summary=failed/timed_out，这里把 snapshot run 标 failed 后抛出
                    snapshot_error = step_exc
                    if snapshot_run_id is not None:
                        async with AsyncSessionLocal() as db:
                            from app.models.stock_feature_snapshot_run import (
                                StockFeatureSnapshotRun,
                            )
                            run_to_finish = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
                            if run_to_finish is not None:
                                await finish_snapshot_run(
                                    db, run_to_finish,
                                    status="failed",
                                    metadata={
                                        "source": "after_close_orchestrator",
                                        "error": str(snapshot_error),
                                        "scope": "full",
                                    },
                                )
                                await db.commit()
                    raise snapshot_error from None

            logger.info(
                "[AfterClose] 统一特征计算完成（待发布后 finalize）: trade_date=%s, "
                "snapshot_count=%s, failed_count=%s, dsa_succeeded=%s",
                trade_date,
                snapshot_result.get("snapshot_count") if snapshot_result else 0,
                snapshot_result.get("failed_count") if snapshot_result else 0,
                snapshot_result.get("dsa_succeeded") if snapshot_result else 0,
            )

            # 2.6 组合质量门禁（DSA + continuous + event freshness）
            # continuous + event freshness 已在 compute_for_trade_date 内部检查：
            # - failure_rate > threshold → RuntimeError（continuous 门禁）
            # - require_event_freshness=True → ValueError（event freshness 门禁）
            # 这里检查 DSA 质量门禁（仅在 DSA run 已 completed 且有成功结果时）
            # [CHANGE-20260728-007] 原条件 snapshot_result.get("dsa_succeeded") 永远为 0
            # （MFCS 不返回此字段），改为检查实际 DSA run 状态 + succeeded_count
            if not dsa_already_completed and dsa_run_id is not None:
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    dsa_run = await _get_strategy_run_or_raise(db, dsa_run_id)

                    if dsa_run.status == "completed" and (dsa_run.succeeded_count or 0) > 0:
                        result_count = await strategy_result_repository.count_by_run(
                            db, dsa_run_id
                        )
                        quality_passed = await batch_service._check_quality_gates(
                            dsa_run, result_count=result_count, db=db
                        )
                        await _update_orchestrator_status(
                            db=db,
                            job_run=job_run,
                            status=AfterCloseRunStatus.COMPUTING_FEATURES,
                            message=(
                                f"组合质量门禁{'通过' if quality_passed else '未通过'}: "
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
                                "snapshot_count": snapshot_result.get("snapshot_count", 0) if snapshot_result else 0,
                            },
                        )
                        await db.commit()

                        if not quality_passed:
                            raise RuntimeError(
                                f"组合质量门禁未通过: dsa_run_id={dsa_run_id}, "
                                f"status={dsa_run.status}"
                            )

            # 2.7 computing_features 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.COMPUTING_FEATURES.value, worker_id,
                )
                await db.commit()

        # ---- 步骤 4: publishing ----
        # [Phase8A 两阶段幂等发布] DSA publish_run（阶段1）与 snapshot run finalize（阶段2）
        # 在各自独立 session/事务中提交，非单一原子事务。故障恢复后通过 publish_run 幂等
        # 返回 + skip_publish 断点跳过达到最终一致。失败时 snapshot run 标记 failed。
        publish_failed = False
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

            # [AC-02] publishing 核心发布（phase1 publish_run + stock_core pointer）通过统一执行器：
            # 超时保护 + cancellation_check（协作取消）。核心失败 → 执行器标记 failed 并重新抛出。
            async def _run_core_publish_op() -> dict[str, Any]:
                _published_run = None
                _publish_failed = False
                try:
                    async with AsyncSessionLocal() as db:
                        _published_run = await batch_service.publish_run(db, dsa_run_id)
                        await db.commit()
                except Exception as publish_exc:
                    _publish_failed = True
                    logger.error(
                        "[AfterClose] DSA publish_run 失败，snapshot run 将标记 failed: "
                        "dsa_run_id=%s, error=%s",
                        dsa_run_id, publish_exc, exc_info=True,
                    )
                    if snapshot_run_id is not None:
                        async with AsyncSessionLocal() as db:
                            from app.models.stock_feature_snapshot_run import (
                                StockFeatureSnapshotRun,
                            )
                            run_to_fail = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
                            if run_to_fail is not None:
                                await finish_snapshot_run(
                                    db, run_to_fail,
                                    status="failed",
                                    metadata={
                                        "source": "after_close_orchestrator",
                                        "error": f"DSA publish_run failed: {publish_exc}",
                                        "scope": "full",
                                    },
                                )
                                await db.commit()
                    raise

                # [P0-2 2026-07-30 visibility window fix] Publish stock_core pointer FIRST,
                # then mark snapshot succeeded. If pointer fails or points to different run,
                # snapshot stays running (no published_at, not visible to consumers).
                _stock_core_published_local = False
                _stock_core_superseded_local = False
                if snapshot_run_id is not None and snapshot_error is None:
                    async with AsyncSessionLocal() as pub_db:
                        from app.models.factor_publication import PUBLICATION_KIND_STOCK_CORE
                        from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION
                        from app.services.factor_publication_service import (
                            CoverageBelowThresholdError,
                            compute_coverage,
                            get_publication,
                            publish_stock_core,
                        )

                        existing_pub = await get_publication(
                            pub_db,
                            scope_type="market",
                            scope_key="market",
                            trade_date=trade_date,
                            publication_kind=PUBLICATION_KIND_STOCK_CORE,
                        )

                        if existing_pub is not None and existing_pub.data_run_id == snapshot_run_id:
                            _stock_core_published_local = True
                        elif existing_pub is not None and existing_pub.data_run_id != snapshot_run_id:
                            logger.warning(
                                "[AfterClose] stock_core pointer exists for different run: "
                                "existing=%s, current=%s — NOT overwriting",
                                existing_pub.data_run_id, snapshot_run_id,
                            )
                            await append_event(
                                db=pub_db,
                                job_run_id=job_run_id,
                                step="publishing",
                                level="warning",
                                message=(
                                    f"stock_core pointer exists for different run: "
                                    f"existing={existing_pub.data_run_id}, "
                                    f"current={snapshot_run_id} — current run is SUPERSEDED, "
                                    f"will NOT be marked published or aggregated"
                                ),
                                payload={
                                    "existing_data_run_id": str(existing_pub.data_run_id),
                                    "current_snapshot_run_id": str(snapshot_run_id),
                                    "superseded": True,
                                    "superseded_by_run_id": str(existing_pub.data_run_id),
                                },
                            )
                            await pub_db.commit()
                            _stock_core_published_local = False
                            _stock_core_superseded_local = True
                        else:
                            cov_data = await compute_coverage(pub_db, snapshot_run_id)
                            try:
                                pub = await publish_stock_core(
                                    session=pub_db,
                                    trade_date=trade_date,
                                    snapshot_run_id=snapshot_run_id,
                                    algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                                    coverage=cov_data["coverage"],
                                    metadata={
                                        "source": "after_close_orchestrator",
                                        "data_run_id": str(snapshot_run_id),
                                        "coverage": cov_data["coverage"],
                                    },
                                )
                                await pub_db.commit()
                                logger.info(
                                    "[AfterClose] stock_core pointer published: "
                                    "trade_date=%s, publication_id=%s, coverage=%.4f",
                                    trade_date, pub.id, cov_data["coverage"],
                                )
                                _stock_core_published_local = True
                            except CoverageBelowThresholdError as cov_exc:
                                await pub_db.rollback()
                                logger.error(
                                    "[AfterClose] stock_core coverage below threshold: %s",
                                    cov_exc,
                                )
                                raise RuntimeError(
                                    f"stock_core publication failed: "
                                    f"coverage below threshold: {cov_exc}"
                                ) from cov_exc

                return {
                    "published_run": _published_run,
                    "publish_failed": _publish_failed,
                    "stock_core_published": _stock_core_published_local,
                    "stock_core_superseded": _stock_core_superseded_local,
                }

            try:
                _core_pub_out, _core_pub_summary = await execute_orchestrator_step(
                    "publishing",
                    _run_core_publish_op,
                    timeout_seconds=_step_timeout("publishing"),
                    progress=_make_step_progress_callback(job_run_id, worker_id),
                    cancellation_check=_make_step_cancellation_check(job_run_id),
                )
            except Exception as pub_exc:
                # 执行器已标记 step_summary=failed，核心发布失败 → 整个 run 失败
                logger.error(
                    "[AfterClose] publishing 核心发布失败: job_run_id=%s, error=%s",
                    job_run_id, pub_exc, exc_info=True,
                )
                raise

            publish_failed = _core_pub_out["publish_failed"]
            published_run = _core_pub_out["published_run"]
            _stock_core_published = _core_pub_out["stock_core_published"]
            _stock_core_superseded = _core_pub_out["stock_core_superseded"]

            # [Phase8A two-phase idempotent publish] Only mark snapshot succeeded
            # AFTER pointer is confirmed. Superseded runs are NOT marked succeeded
            # (no published_at → not visible to API consumers via fallback).
            if _stock_core_published and snapshot_run_id is not None and snapshot_error is None:
                async with AsyncSessionLocal() as db:
                    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
                    run_to_finish = await db.get(StockFeatureSnapshotRun, snapshot_run_id)
                    if run_to_finish is not None and run_to_finish.status != "succeeded":
                        # [P0-3] 断点从 last_completed_step='feature_snapshot' 恢复发布时，
                        # snapshot_result 为 None（feature_snapshot 阶段已 skip）。
                        # 此时必须从数据库读取该 run 实际 snapshot 数量，
                        # 保留 run.expected_count/已有 failed_count，禁止写成 0 或 None。
                        if snapshot_result is not None:
                            _snapshot_count = snapshot_result.get("snapshot_count", 0)
                            _failed_count = snapshot_result.get("failed_count", 0)
                        else:
                            from app.models.stock_feature_snapshot import StockFeatureSnapshot
                            _count_stmt = select(func.count()).select_from(
                                StockFeatureSnapshot
                            ).where(
                                StockFeatureSnapshot.source_run_id == snapshot_run_id,
                            )
                            _snapshot_count = (await db.execute(_count_stmt)).scalar() or 0
                            _failed_count = (run_to_finish.expected_count or 0) - _snapshot_count
                        _source_bar_hash = snapshot_result.get("source_bar_hash") if snapshot_result else None
                        _adj_factor_hash = snapshot_result.get("adj_factor_hash") if snapshot_result else None
                        _mdc_version = snapshot_result.get("market_data_contract_version") if snapshot_result else None
                        _completed_through = snapshot_result.get("completed_through") if snapshot_result else None
                        _adjustment_as_of = snapshot_result.get("adjustment_as_of") if snapshot_result else None
                        await finish_snapshot_run(
                            db, run_to_finish,
                            status="succeeded",
                            snapshot_count=_snapshot_count,
                            failed_count=_failed_count,
                            expected_count=run_to_finish.expected_count,
                            metadata={
                                "source": "after_close_orchestrator",
                                "scope": "full",
                            },
                            source_bar_hash=_source_bar_hash,
                            adj_factor_hash=_adj_factor_hash,
                            market_data_contract_version=_mdc_version,
                            completed_through=_completed_through,
                            adjustment_as_of=_adjustment_as_of,
                        )
                        await db.commit()
                        logger.info(
                            "[AfterClose] snapshot run 已标记 succeeded（pointer 确认后）: "
                            "run_id=%s, snapshot_count=%s",
                            snapshot_run_id, _snapshot_count,
                        )

            # [P0-1] Superseded: snapshot NOT marked succeeded, aggregation skipped
            if _stock_core_superseded and snapshot_run_id is not None:
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await append_event(
                        db=db,
                        job_run_id=job_run_id,
                        step="publishing",
                        level="warning",
                        message=(
                            f"Snapshot run {snapshot_run_id} SUPERSEDED by existing pointer. "
                            f"Not marking succeeded. Aggregation skipped."
                        ),
                        payload={
                            "snapshot_run_id": str(snapshot_run_id),
                            "superseded": True,
                        },
                    )
                    await db.commit()

            # [P0-4 aggregation dependency closure] After stock_core pointer is
            # published, trigger auction anchor generation then board analysis.
            # 接入顺序: stock_core → chip_consensus → auction_anchor → market_aggregation → review
            # [P0-1/P0-2 修复 2026-07-31] 主流程通过统一入口 generate_and_publish_auction_anchors
            # 完成生成+发布。chip 未完成时生成 structure_only 并发布；chip 完成后由
            # chip_consensus_service 回调重新生成完整锚点并原子切换 publication。
            _auction_anchor_status = "skipped"
            _auction_publication_id: uuid.UUID | None = None
            if _stock_core_published and snapshot_run_id is not None:
                try:
                    from app.services.auction_anchor_service import (
                        generate_and_publish_auction_anchors,
                    )

                    async with AsyncSessionLocal() as anchor_db:
                        async def _generate_anchor() -> dict[str, Any]:
                            result = await generate_and_publish_auction_anchors(
                                anchor_db,
                                trade_date=trade_date,
                                worker_id=worker_id,
                                lease_epoch=lease_epoch,
                            )
                            if not result or (
                                result.get("structure_count", 0) == 0
                                and result.get("chip_count", 0) == 0
                                and result.get("composite_count", 0) == 0
                            ):
                                raise StepUnavailableError("auction anchor source data unavailable")
                            await anchor_db.commit()
                            return result

                        anchor_result, anchor_summary = await execute_orchestrator_step(
                            "auction_anchor",
                            _generate_anchor,
                            timeout_seconds=_AUCTION_ANCHOR_TIMEOUT_SECONDS,
                            optional=True,
                        )
                    _auction_anchor_status = anchor_summary["status"]
                    _auction_publication_id = (
                        anchor_result.get("publication_id") if anchor_result else None
                    )
                    logger.info(
                        "[AfterClose] auction anchor 生成+发布完成: trade_date=%s, "
                        "status=%s, publication_id=%s, structure=%s, chip=%s, composite=%s",
                        trade_date,
                        _auction_anchor_status,
                        _auction_publication_id,
                        anchor_result.get("structure_count", 0) if anchor_result else 0,
                        anchor_result.get("chip_count", 0) if anchor_result else 0,
                        anchor_result.get("composite_count", 0) if anchor_result else 0,
                    )
                except Exception as anchor_exc:
                    _auction_anchor_status = "failed"
                    logger.warning(
                        "[AfterClose] auction anchor 生成+发布失败（optional，不影响 core）: "
                        "trade_date=%s, error=%s",
                        trade_date, anchor_exc,
                        exc_info=True,
                    )

            # Aggregation binds same source_core_run_id; failure only reruns
            # aggregation, does NOT reverse core. Main run status distinguishes
            # core_published vs optional_failure.
            _aggregation_status = "skipped"
            if _stock_core_published and snapshot_run_id is not None:
                try:
                    from app.services.board_analysis_service import (
                        compute_all_boards,
                    )

                    async with AsyncSessionLocal() as agg_db:
                        agg_result = await compute_all_boards(
                            agg_db,
                            trade_date=trade_date,
                            publish=True,
                        )
                        await agg_db.commit()
                    _aggregation_status = "succeeded"
                    logger.info(
                        "[AfterClose] board aggregation 完成: trade_date=%s, "
                        "published=%s",
                        trade_date,
                        agg_result.get("published", 0),
                    )
                except Exception as agg_exc:
                    _aggregation_status = "failed"
                    logger.warning(
                        "[AfterClose] board aggregation 失败（optional，不影响 core）: "
                        "trade_date=%s, error=%s",
                        trade_date, agg_exc,
                        exc_info=True,
                    )

            # [Phase5] - publishing 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                meta = _parse_metadata(job_run)
                step_summary = dict(meta.get("step_summary") or {})
                if "anchor_summary" in locals():
                    step_summary["auction_anchor"] = anchor_summary
                meta["step_summary"] = step_summary
                optional_failures = [
                    name for name, item in step_summary.items()
                    if item.get("optional")
                    and item.get("status") in {"failed", "unavailable", "timed_out", "interrupted"}
                ]
                meta["partial_success"] = bool(optional_failures)
                meta["optional_failures"] = optional_failures
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.PUBLISHING.value, worker_id,
                )
                await db.commit()

        # C5: 事件生成在 publishing 成功之后（或 skip_publish 断点恢复时已发布）
        # publishing 失败会抛异常跳过此处 → 不生成事件
        # 独立 session + try/except：事件生成失败不影响 orchestrator 主流程
        if snapshot_error is None and snapshot_run_id is not None and not publish_failed:
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

        # ---- 步骤 4.5: computing_review（复盘计算 + 发布） ----
        # [AC-02] 复盘业务体抽为 _execute_review_step，由统一执行器包装。
        # 软失败（gate_blocked/计算失败）不阻断主流程，仅标记 _review_failed
        # 收 partial_success；step summary 如实反映业务状态，失败不推进检查点。
        _review_result, _review_step_summary = await execute_orchestrator_step(
            "computing_review",
            lambda: _execute_review_step(
                job_run_id=job_run_id,
                trade_date=trade_date,
                snapshot_run_id=snapshot_run_id,
                worker_id=worker_id,
                skip_review=skip_review,
                stock_core_published=_stock_core_published,
                aggregation_status=_aggregation_status,
            ),
            timeout_seconds=_step_timeout("computing_review"),
            optional=True,
            heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
            progress=_make_step_progress_callback(job_run_id, worker_id),
            cancellation_check=_make_step_cancellation_check(job_run_id),
        )
        # 解包 review 业务状态（供主任务 partial_success 判定与 metadata 写入）
        _review_status = (
            _review_result.get("status") if isinstance(_review_result, dict) else "skipped"
        )
        _review_failed = (
            bool(_review_result.get("failed"))
            if isinstance(_review_result, dict)
            else False
        )
        _review_reason = (
            _review_result.get("reason") if isinstance(_review_result, dict) else None
        )
        _review_run_id = (
            _review_result.get("run_id") if isinstance(_review_result, dict) else None
        )
        _review_publication_id = (
            _review_result.get("publication_id") if isinstance(_review_result, dict) else None
        )
        _review_scope_count = (
            _review_result.get("scope_count", 0) if isinstance(_review_result, dict) else 0
        )
        _review_signal_count = (
            _review_result.get("signal_count", 0) if isinstance(_review_result, dict) else 0
        )
        _review_coverage = (
            float(_review_result.get("coverage", 0.0) or 0.0)
            if isinstance(_review_result, dict)
            else 0.0
        )
        _review_blockers = (
            list(_review_result.get("blockers", []))
            if isinstance(_review_result, dict)
            else []
        )
        # 业务软失败如实反映到 step summary（不伪装步骤 succeeded）
        if _review_step_summary.get("status") == "succeeded" and _review_failed:
            _review_step_summary["status"] = "failed"
            _review_step_summary["error_code"] = "REVIEW_SOFT_FAILURE"
            _review_step_summary["error_message"] = (
                f"复盘阶段软失败（core 已发布）: {_review_status}, reason={_review_reason}"
            )
            await _persist_step_summary(job_run_id, _review_step_summary)
        logger.info(
            "[AfterClose] computing_review 完成: step_status=%s, review_status=%s, "
            "run_id=%s, failed=%s",
            _review_step_summary.get("status"), _review_status,
            _review_run_id, _review_failed,
        )

        # ---- 步骤 4.9: enqueue_chip_job（正式步骤，必须在主任务终态之前）----
        # [Phase0-Fix#8] chip 原先在主任务终态提交之后创建，导致：
        # 1) chip job 创建失败无法进入 partial_success；
        # 2) chip 没有统一 step summary；
        # 3) metadata 缺稳定的 chip job id / enqueue 状态。
        # chip 本身仍是异步任务（不 await 其计算），这里只把"入队"做成正式步骤。
        _chip_enqueue_status, _chip_job_id = await _enqueue_chip_job_step(
            job_run_id=job_run_id,
            worker_id=worker_id,
            lease_epoch=lease_epoch,
            trade_date=trade_date,
            snapshot_run_id=snapshot_run_id,
            expected_count=(
                len(cached_instrument_ids)
                if cached_instrument_ids is not None
                else None
            ),
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
            # [P0-1 2026-08-03] 核心已发布但可选阶段（auction/review/aggregation）失败时，
            # 主任务状态为 PARTIAL_SUCCESS（而非 succeeded），明确表达"核心成功、后置降级"。
            # stock_core 被 superseded（pointer 指向其他 run）也视为部分成功。
            _optional_failed = (
                _review_failed
                or _auction_anchor_status == "failed"
                or _aggregation_status == "failed"
                or _stock_core_superseded
                # [Phase0-Fix#8] chip 入队失败纳入 partial_success 判定
                or _chip_enqueue_status == "failed"
            )
            final_status = (
                AfterCloseRunStatus.PARTIAL_SUCCESS
                if _optional_failed
                else AfterCloseRunStatus.SUCCEEDED
            )
            success_message = (
                f"盘后编排{'部分成功' if _optional_failed else '成功完成'}: "
                f"dsa_run_id={dsa_run_id}"
                + (f", published_at={published_run.published_at}"
                   if published_run is not None else "")
                + f", stock_core_published={_stock_core_published}"
                + f", auction_anchor_status={_auction_anchor_status}"
                + f", aggregation_status={_aggregation_status}"
                + f", review_status={_review_status}"
                + (f", review_run_id={_review_run_id}" if _review_run_id else "")
            )
            await _update_orchestrator_status(
                db=db,
                job_run=job_run,
                status=final_status,
                message=success_message,
                dsa_run_id=dsa_run_id,
                payload={
                    "published_at": published_at_str,
                    "stock_core_published": _stock_core_published,
                    "stock_core_superseded": _stock_core_superseded,
                    "auction_anchor_status": _auction_anchor_status,
                    "aggregation_status": _aggregation_status,
                    "partial_success": _optional_failed,
                    # [Phase0-Fix#8] chip 入队结果进入主任务 metadata（稳定 job id + 状态）
                    "chip_enqueue_status": _chip_enqueue_status,
                    "chip_job_id": str(_chip_job_id) if _chip_job_id else None,
                    # [CHANGE-20260801-REVIEW-CLOSURE] review 闭环字段
                    "review_run_id": str(_review_run_id) if _review_run_id else None,
                    "review_status": _review_status,
                    "review_reason": _review_reason,
                    "review_publication_id": (
                        str(_review_publication_id)
                        if _review_publication_id
                        else None
                    ),
                    "review_scope_count": _review_scope_count,
                    "review_signal_count": _review_signal_count,
                    "review_coverage": _review_coverage,
                    "review_blockers": _review_blockers,
                },
            )
            job_run.status = final_status.value
            job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            await _update_heartbeat_and_step(
                db, job_run, final_status.value, worker_id,
            )
            await db.commit()

        logger.info(
            "[AfterClose] 盘后编排结束: job_run_id=%s, dsa_run_id=%s, status=%s, "
            "chip_enqueue_status=%s",
            job_run_id, dsa_run_id, final_status.value, _chip_enqueue_status,
        )

    except Exception as exc:
        # [JOB-02] LeaseEpochMismatchError：任务已被新 Worker 接管，不标记 failed
        # 仅记录日志后 re-raise（新 Worker 负责后续状态推进）
        if isinstance(exc, LeaseEpochMismatchError):
            logger.warning(
                "[AfterClose] lease_epoch 不匹配，停止处理（任务已由新 Worker 接管）: "
                "job_run_id=%s, lease_epoch=%s, error=%s",
                job_run_id, _current_lease_epoch.get(), exc,
            )
            raise

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

    # [Watchdog 2026-08-03] 步骤级超时 + stale 判定（基于统一执行器写入的 step_summary）：
    # - 任何 running 步骤的 elapsed_seconds 超过 _step_timeout 阈值 → step_timed_out=True
    # - heartbeat_stale（整体）或任一 running 步骤 timed_out → stale=True
    # 这是 watchdog 展示闭环：API 计算 → 管理页告警 → 运维触发 restart_from/repair。
    step_summary = dict(meta.get("step_summary") or {})
    step_timed_out = False
    running_steps: list[str] = []
    for _step_name, _summary in step_summary.items():
        if isinstance(_summary, dict) and _summary.get("status") == "running":
            running_steps.append(_step_name)
            _elapsed = _summary.get("elapsed_seconds")
            _limit = _STEP_TIMEOUT_SECONDS.get(_step_name)
            if _elapsed is not None and _limit is not None and _elapsed > _limit:
                step_timed_out = True
    stale = heartbeat_stale or step_timed_out

    # 透传 partial_success（核心成功、可选阶段降级）
    partial_success_flag = bool(meta.get("partial_success")) or job_run.status == "partial_success"

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
        # [Watchdog 2026-08-03] 步骤级超时 + stale 闭环
        "step_summary": step_summary,
        "running_steps": running_steps,
        "step_timed_out": step_timed_out,
        "stale": stale,
        "partial_success": partial_success_flag,
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


async def cancel_after_close_run(
    db: AsyncSession,
    *,
    job_run_id: str,
    reason: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
) -> SchedulerJobRun:
    """协作式取消；终态重复调用仅返回当前事实。

    [P0-1 2026-08-03 cancel 真实语义] 不止改 DB 状态：
    - 记录 actor / request_id（审计谁在何时发起）
    - 写 cancel 事件（时间线可溯）
    - 递增 lease_epoch 立即 fence 旧 Worker（旧 Worker 后续心跳/写入被拒）
    - 长步骤通过 cancellation_check 在下一个心跳周期感知 cancelled 并收尾
    """
    job_run = await _get_job_run_or_raise(db, uuid.UUID(job_run_id))
    if job_run.status in {"succeeded", "failed", "cancelled", "interrupted"}:
        return job_run
    now = datetime.now(UTC)
    meta = _parse_metadata(job_run)
    # [P0-1] 递增 lease_epoch：使正在执行的旧 Worker 的 fenced 写入立即失效
    _current_epoch = job_run.lease_epoch if hasattr(job_run, "lease_epoch") and job_run.lease_epoch else 0
    _new_epoch = _current_epoch + 1
    meta.update(
        orchestrator_status="cancelled",
        cancel_reason=reason or "admin_cancelled",
        cancel_actor=actor or "unknown_admin",
        cancel_request_id=request_id,
        cancelled_at=now.isoformat(),
        cancelled_lease_epoch=_new_epoch,
    )
    job_run.status = "cancelled"
    job_run.error_code = "ADMIN_CANCELLED"
    job_run.error_message = reason or "管理员取消盘后任务"
    job_run.finished_at = now
    job_run.lease_expires_at = None
    if hasattr(job_run, "lease_epoch"):
        job_run.lease_epoch = _new_epoch
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    # 写 cancel 事件（时间线可溯，独立 session 提交）
    try:
        await append_event(
            db=db,
            job_run_id=uuid.UUID(job_run_id),
            step="cancelled",
            level="warn",
            message=f"管理员取消盘后任务: actor={actor}, reason={reason}",
            payload={
                "actor": actor,
                "request_id": request_id,
                "cancelled_lease_epoch": _new_epoch,
            },
        )
    except Exception as evt_exc:
        logger.warning("[AfterClose] 写 cancel 事件失败: %s", evt_exc)
    await db.flush()
    return job_run


async def _inspect_run_artifacts(
    db: AsyncSession,
    job_run: SchedulerJobRun,
) -> dict[str, Any]:
    """[Phase0-Fix#6] 只读核验任务对应交易日的真实产物 pointer。

    reconcile 若只对齐状态字段，无法发现"任务 failed 但 stock_core 已发布"
    或"任务 succeeded 但没有任何 publication"这类矛盾。

    只做只读查询，不修改任何业务 pointer。
    """
    artifacts: dict[str, Any] = {
        "stock_core_published": False,
        "stock_core_data_run_id": None,
        "market_aggregation_published": False,
        "checked_trade_date": None,
    }
    meta = _parse_metadata(job_run)
    trade_date_raw = meta.get("trade_date")
    if not trade_date_raw:
        return artifacts
    artifacts["checked_trade_date"] = trade_date_raw
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT publication_kind, data_run_id
                    FROM factor_publications
                    WHERE trade_date = CAST(:trade_date AS date)
                      AND scope_type = 'market'
                      AND publication_kind IN ('stock_core', 'market_aggregation')
                    """
                ),
                {"trade_date": trade_date_raw},
            )
        ).fetchall()
        for kind, data_run_id in rows:
            if kind == "stock_core":
                artifacts["stock_core_published"] = True
                artifacts["stock_core_data_run_id"] = str(data_run_id)
            elif kind == "market_aggregation":
                artifacts["market_aggregation_published"] = True
    except Exception as exc:
        logger.warning("[AfterClose] reconcile 产物核验失败（不阻断）: %s", exc)
        artifacts["inspect_error"] = str(exc)[:200]
    return artifacts


async def reconcile_after_close_run(
    db: AsyncSession,
    *,
    job_run_id: str,
    reason: str | None = None,
    actor: str | None = None,
    request_id: str | None = None,
) -> SchedulerJobRun:
    """对账校准：根据 heartbeat、step_summary、产物 pointer 修正状态。

    [P0-1 2026-08-03 reconcile 真实语义]
    - 检查 running 但 heartbeat_stale → 标记为 interrupted（Worker 已失联）
    - 检查 running 但某步骤 step_timed_out → 标记 interrupted
    - 根据 job_run.status 修正 orchestrator_status 派生字段
    - 写 reconcile 事件记录审计

    [Phase0-Fix#6] 补齐：
    - 接入 request_id（端点已生成，此前未传入，审计断链）
    - interrupted 时写 finished_at、释放 lease_expires_at、递增 lease_epoch
      fence 旧 Worker（否则旧 Worker 仍可继续写入）
    - 核验真实产物 pointer（stock_core publication / review pointer），
      记录"产物已存在但任务未成功"的矛盾，供管理员判断
    """
    job_run = await _get_job_run_or_raise(db, uuid.UUID(job_run_id))
    meta = _parse_metadata(job_run)
    now = datetime.now(UTC)

    # 1) running 但心跳 stale / 步骤超时 → interrupted（Worker 失联，非业务失败）
    _reconciled_to_interrupted = False
    if job_run.status == "running":
        _hb_stale = False
        if job_run.heartbeat_at is not None:
            _now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
            _hb = job_run.heartbeat_at
            if _hb.tzinfo is None:
                _hb = _hb.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            _hb_stale = (_now_sh - _hb) > timedelta(seconds=_HEARTBEAT_STALE_SECONDS)
        _step_sum = dict(meta.get("step_summary") or {})
        _step_timeout_hit = False
        for _sn, _s in _step_sum.items():
            if isinstance(_s, dict) and _s.get("status") == "running":
                _lim = _STEP_TIMEOUT_SECONDS.get(_sn)
                if _s.get("elapsed_seconds") is not None and _lim and _s["elapsed_seconds"] > _lim:
                    _step_timeout_hit = True
        if _hb_stale or _step_timeout_hit:
            job_run.status = "interrupted"
            job_run.error_code = "RECONCILED_INTERRUPTED"
            job_run.error_message = (
                f"reconcile: heartbeat_stale={_hb_stale}, step_timed_out={_step_timeout_hit}"
            )
            _reconciled_to_interrupted = True
            # [Phase0-Fix#6] 终态收尾 + fence 旧 Worker：
            # 不写 finished_at / 不释放 lease / 不递增 epoch 时，
            # 旧 Worker 心跳仍可能匹配成功并继续写入业务数据。
            job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            job_run.lease_expires_at = None
            job_run.lease_epoch = (job_run.lease_epoch or 0) + 1
            # 把仍处于 running 的步骤如实收敛为 interrupted，避免永远挂 running
            for _sn, _s in _step_sum.items():
                if isinstance(_s, dict) and _s.get("status") == "running":
                    _s["status"] = "interrupted"
                    _s["error_code"] = "RECONCILED_INTERRUPTED"
                    _s["finished_at"] = now.isoformat()
            meta["step_summary"] = _step_sum
            logger.warning(
                "[AfterClose] reconcile: running→interrupted (stale): "
                "job_run_id=%s, hb_stale=%s, step_timeout=%s, new_lease_epoch=%s",
                job_run_id, _hb_stale, _step_timeout_hit, job_run.lease_epoch,
            )

    # 2) 修正 orchestrator_status 派生字段
    expected = {
        "succeeded": AfterCloseRunStatus.SUCCEEDED.value,
        "failed": AfterCloseRunStatus.FAILED.value,
        "cancelled": AfterCloseRunStatus.CANCELLED.value,
        "interrupted": AfterCloseRunStatus.INTERRUPTED.value,
        "partial_success": AfterCloseRunStatus.PARTIAL_SUCCESS.value,
    }.get(job_run.status)
    _changed = False
    if expected and meta.get("orchestrator_status") != expected:
        meta["orchestrator_status"] = expected
        _changed = True
    # 3) [Phase0-Fix#6] 核验真实产物 pointer，暴露"产物与任务状态矛盾"
    artifacts = await _inspect_run_artifacts(db, job_run)
    contradictions: list[str] = []
    if job_run.status in {"failed", "interrupted"} and artifacts.get("stock_core_published"):
        contradictions.append("STOCK_CORE_PUBLISHED_BUT_RUN_NOT_SUCCEEDED")
    if job_run.status == "succeeded" and not artifacts.get("stock_core_published"):
        contradictions.append("RUN_SUCCEEDED_BUT_NO_STOCK_CORE_PUBLICATION")
    meta["reconcile_artifacts"] = artifacts
    meta["reconcile_contradictions"] = contradictions
    if contradictions:
        logger.warning(
            "[AfterClose] reconcile 发现产物/状态矛盾: job_run_id=%s, status=%s, items=%s",
            job_run_id, job_run.status, contradictions,
        )

    meta["reconciled_at"] = now.isoformat()
    meta["reconcile_reason"] = reason or "admin_reconcile"
    meta["reconcile_actor"] = actor
    meta["reconcile_request_id"] = request_id
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    try:
        await append_event(
            db=db,
            job_run_id=uuid.UUID(job_run_id),
            step="reconciled",
            level="info",
            message=(
                f"对账校准: status={job_run.status}, "
                f"interrupted={_reconciled_to_interrupted}, meta_changed={_changed}"
            ),
            payload={
                "actor": actor,
                "request_id": request_id,
                "reconciled_to_interrupted": _reconciled_to_interrupted,
                "meta_changed": _changed,
                "artifacts": artifacts,
                "contradictions": contradictions,
                "new_lease_epoch": job_run.lease_epoch,
            },
        )
    except Exception as evt_exc:
        logger.warning("[AfterClose] 写 reconcile 事件失败: %s", evt_exc)
    await db.flush()
    return job_run


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
    # [PRD §4.3 JOB-01] 允许 failed 和 interrupted 状态重试（interrupted 可自动或手动恢复）
    if job_run.status not in ("failed", "interrupted"):
        raise ValueError(
            f"仅 failed/interrupted 状态可重试（当前 {job_run.status}）: job_run_id={job_run_id}"
        )

    # [Phase5] - 重置为 queued（不是 running），由独立 Worker 领取执行
    # [JOB-01] 手动重试也递增 attempt_no（与 auto_resume 一致）
    job_run.status = "queued"
    job_run.error_message = None
    job_run.error_code = None
    job_run.finished_at = None
    job_run.attempt_no = (job_run.attempt_no or 0) + 1
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    job_run.started_at = now
    job_run.heartbeat_at = now
    job_run.lease_expires_at = now + timedelta(seconds=_ORCHESTRATOR_LEASE_SECONDS)

    await _update_orchestrator_status(
        db=db,
        job_run=job_run,
        status=AfterCloseRunStatus.QUEUED,
        message=f"管理员手动重试: job_run_id={job_run_id}, attempt_no={job_run.attempt_no}",
    )
    await db.commit()

    logger.info("[AfterClose] 重试盘后编排: job_run_id=%s, attempt_no=%s", job_run_id, job_run.attempt_no)
    return job_run


if __name__ == "__main__":
    # 自测入口：验证枚举、函数签名与模块导入（不连接数据库）
    import inspect

    # 验证 AfterCloseRunStatus 枚举
    # [PR #77] - syncing_boards 是 PR #77 收口后新增的软失败阶段，加入期望集合
    expected_statuses = {
        "queued", "refreshing_daily", "syncing_boards", "checking_coverage",
        "creating_dsa", "waiting_dsa_worker", "quality_gate", "feature_snapshot",
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
    # [JOB-02] - lease_epoch 参数支持 fenced UPDATE
    assert "lease_epoch" in params, (
        f"execute_after_close_run 缺少 lease_epoch 参数: {params}"
    )
    assert sig.parameters["lease_epoch"].default is None, (
        "lease_epoch 默认值应为 None"
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

    # [JOB-02] - 验证 _job_run_heartbeat_loop 签名包含 lease_epoch
    sig = inspect.signature(_job_run_heartbeat_loop)
    hb_params = set(sig.parameters.keys())
    assert "lease_epoch" in hb_params, (
        f"_job_run_heartbeat_loop 缺少 lease_epoch 参数: {hb_params}"
    )
    assert sig.parameters["lease_epoch"].default is None, (
        "lease_epoch 默认值应为 None"
    )
    print(f"_job_run_heartbeat_loop 签名 ✓: {sorted(hb_params)}")

    # [JOB-02] - 验证 LeaseEpochMismatchError 异常类 + _current_lease_epoch ContextVar
    assert issubclass(LeaseEpochMismatchError, Exception), (
        "LeaseEpochMismatchError 必须继承 Exception"
    )
    assert _current_lease_epoch.get() is None, (
        "_current_lease_epoch 默认值应为 None（legacy 模式）"
    )
    test_token = _current_lease_epoch.set(42)
    assert _current_lease_epoch.get() == 42, "ContextVar 设置失败"
    _current_lease_epoch.reset(test_token)
    assert _current_lease_epoch.get() is None, "ContextVar reset 失败"
    print("LeaseEpochMismatchError + _current_lease_epoch ContextVar 验证 ✓")

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
