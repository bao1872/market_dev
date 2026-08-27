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
import os
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
    finalize_snapshot_run_compute_complete,
    finish_snapshot_run,
    get_active_a_share_instruments,
)
from app.models.stock_feature_snapshot_run import STATUS_SUCCEEDED
from app.services.first_pyramid_history_service import (
    advance_history_to_trade_date,
    ensure_current_first_pyramid_history_run,
    materialize_history_v3_from_core,
)
from app.services.first_pyramid_service import (
    HISTORY_CONTRACT_VERSION,
    REVIEW_HISTORY_V3_CONTRACT_VERSION,
)
from app.services.idempotency_service import acquire_job_run_lock
from app.services.job_run_event_service import append_event, list_events
from app.services.review_history_readiness_service import (
    validate_canonical_history_run_readiness,
)
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
# 值需覆盖正常耗时 + 合理缓冲（computing_features 约 7 小时）。
# [Phase 4D.4] refreshing_daily 是 workload-variant long-running business step：
# 耗时随 instrument count / backfill window / provider throughput 变化，不由 fixed
# generic absolute wall-clock 上限决定成败（PRD 31 PC-43 / rules/80 §13.1）。
# 其值为 None —— 无 absolute timeout，由 stale watchdog（lease 过期 + heartbeat 不健康）
# 依据真实无进展判定 stalled，而非总耗时过长。
_STEP_TIMEOUT_SECONDS: dict[str, float | None] = {
    "refreshing_daily": None,      # workload-variant long-running：无 absolute 上限
    "syncing_boards": 1800,
    "checking_coverage": 300,
    "computing_features": 28800,   # 约 7 小时主链
    "publishing": 3600,
    # [SLICE-01-CORRECTION-02] computing_history 是全市场 canonical History exact-T
    # advancement（workload-variant long-running business batch）：耗时随 instrument
    # count 变化，不由 fixed generic absolute wall-clock 上限决定成败（rules/80 §13.1）。
    # 值为 None —— 无 absolute timeout，由 stale watchdog（lease 过期 + heartbeat 不健康）
    # 依据真实无进展判定 stalled，而非总耗时过长。
    "computing_history": None,
    "computing_review": 1800,
    "auction_anchor": _AUCTION_ANCHOR_TIMEOUT_SECONDS,
    # [Phase0-Fix#8] chip 只做入队（不等计算），超时应短
    "enqueue_chip_job": 120,
}


def _step_timeout(step: str) -> float | None:
    """返回步骤超时（默认 _DEFAULT_STEP_TIMEOUT_SECONDS）。

    [Phase 4D.4] 返回 None 表示该步骤无 absolute timeout（long-running business step，
    由 stale watchdog 依据真实无进展判定，而非总耗时）。
    """
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
            # [Phase 4D.4] last_progress_at 不得由 heartbeat 时间冒充：
            # heartbeat 在 CPU-bound / blocking provider call 时仍会刷新，不能证明业务有进展。
            # last_progress_at 只在业务真正推进时由 progress callback 更新；当前 step 无业务
            # progress 注入时保持 started_at，finally 时更新为 finished_at，避免 false-liveness。
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
            error_message=f"{step} timed out after {timeout_seconds if timeout_seconds is not None else 'no-limit'}s",
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
        # [SLICE-01-CORRECTION-04] last_progress_at 是 History 业务拥有字段；对 computing_history
        # 不在此覆盖（executor 不应冒充业务进度所有权），由 business callback 维护。
        if summary.get("step") != "computing_history":
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
    timeout_seconds: float | None,
    poll_interval: float = _CANCEL_POLL_INTERVAL_SECONDS,
) -> Any:
    """运行 operation，并在运行期间周期性调用 cancellation_check。

    [Phase0] 真正的运行中取消：
    - operation 作为独立 task 运行；
    - 每 poll_interval 秒轮询一次 cancellation_check；
    - 命中取消 → cancel operation task 并 await 其真正结束（保证业务写入停止），
      随后抛出 _StepCancelledError；
    - 总耗时超过 timeout_seconds → cancel task 并抛 TimeoutError；
    - [Phase 4D.4] timeout_seconds=None 时**不设 absolute deadline**：仅保留协作取消，
      由 stale watchdog（lease 过期 + heartbeat 不健康）防护无进展卡死，不在意总耗时。
    """
    loop = asyncio.get_running_loop()
    has_deadline = timeout_seconds is not None
    deadline = (loop.time() + timeout_seconds) if has_deadline else None
    op_task = asyncio.ensure_future(operation())

    async def _finalize(exc: BaseException) -> None:
        """cancel operation task 并等待其真正结束，确保业务协程停止执行。"""
        op_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await op_task
        raise exc

    try:
        while True:
            if has_deadline:
                assert deadline is not None
                remaining = deadline - loop.time()
                if remaining <= 0:
                    await _finalize(TimeoutError())
                wait_slice = min(poll_interval, remaining)
            else:
                # 无 absolute 上限：仅按 poll_interval 轮询取消状态
                wait_slice = poll_interval
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


def _make_history_business_progress(
    job_run_id: uuid.UUID,
    worker_id: str | None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """[SLICE-01-CORRECTION-03] 专属业务进度回调（仅给 advance_history_to_trade_date 用）。

    advance_history_to_trade_date 的 callback payload 形如：
        {"processed": N, "total": M, "target_state_count": K}
    这是唯一合法的「真实业务 progress」来源。本回调：
    1) 仅当 payload 含 processed/total 业务键时才视为真实进度（与 executor 的
       heartbeat/elapsed tick 区分，后者不含这些键，绝不在此被当成业务 progress）；
    2) MERGE 进既有 step_summary：保留 status / started_at / heartbeat_at /
       last_progress_at 等已有字段，只覆盖业务进度字段；
    3) 写入 last_progress_at = now（只有真实业务推进才更新，heartbeat 不冒充）；
    4) 注入 orchestrator 侧 step 名 "computing_history"（ownership 留在 orchestrator，
       producer 不知道该 UI 名）。

    注意：本回调与外层 executor 的 progress= 回调（_make_step_progress_callback）
    严格分离 —— executor 的 tick/finally 调用只持久化 executor summary，不碰
    last_progress_at（其 last_progress_at 由 executor 自身语义控制），绝不产生 false-liveness。
    """

    async def _cb(payload: dict[str, Any]) -> None:
        if not payload or "processed" not in payload or "total" not in payload:
            # 非业务进度（如 executor heartbeat tick），忽略，避免 false-liveness
            return
        try:
            async with AsyncSessionLocal() as db:
                # [CORRECTION-05] FOR UPDATE：锁定 exact 行，避免与 executor writer 并发
                # lost update（双方各拿旧快照先后 commit）。
                stmt = (
                    select(SchedulerJobRun)
                    .where(SchedulerJobRun.id == job_run_id)
                    .with_for_update()
                )
                job_run = (await db.execute(stmt)).scalar_one_or_none()
                if job_run is None:
                    return
                meta = _parse_metadata(job_run)
                step_summary = dict(meta.get("step_summary") or {})
                existing = dict(step_summary.get("computing_history") or {})
                # MERGE：保留既有 executor 字段，只更新业务进度（业务拥有字段白名单）
                merged = dict(existing)
                merged["step"] = "computing_history"
                merged["processed"] = payload.get("processed")
                merged["total"] = payload.get("total")
                if "target_state_count" in payload:
                    merged["target_state_count"] = payload["target_state_count"]
                merged["last_progress_at"] = datetime.now(UTC).isoformat()
                step_summary["computing_history"] = merged
                meta["step_summary"] = step_summary
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                await db.commit()
        except Exception as exc:  # 进度回调失败不得影响主流程
            logger.warning(
                "[AfterClose] history business progress 写入失败: error=%s", exc,
            )

    return _cb


# [SLICE-01-CORRECTION-04/05] progress 字段所有权划分 + 并发事务所有权：
# computing_history.step_summary 由两类 writer 共同更新。
# - History 业务拥有（advance 真实推进）：processed/total/target_state_count/last_progress_at
# - 其余字段（status/finished_at/elapsed_seconds/heartbeat_at/timeout_seconds/attempt/
#   retry_count/error_code/error_message/step/started_at/optional 等）均属 executor 拥有。
# 原则：executor 可写 summary 中「除业务拥有字段外」的所有字段（白名单反转，避免漏同步
# 新增 executor 字段，例如 CORRECTION-05 补的 started_at/optional）；business 只写业务字段。
# [CORRECTION-05] 两个 writer 各自独立 AsyncSession，若只做「读 metadata → Python merge →
# 写回」而无行锁，真实 asyncio 并发下会 lost update（双方各拿旧快照先后 commit）。因此两个
# callback 都必须用 SELECT ... FOR UPDATE 锁定 exact SchedulerJobRun 行，锁后读最新已提交
# metadata → 按字段 owner merge → commit；第二个 writer 阻塞直到第一个 commit，杜绝丢更新。
_HISTORY_BUSINESS_OWNED_FIELDS = (
    "processed", "total", "target_state_count", "last_progress_at",
)


def _make_history_executor_progress(
    job_run_id: uuid.UUID,
    worker_id: str | None,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    """[SLICE-01-CORRECTION-04/05] computing_history 专属 executor progress 持久化回调。

    与通用 _make_step_progress_callback（整块覆盖）不同，本回调采用 MERGE 语义：
    把 executor summary 中「除业务拥有字段外」的所有字段写回（白名单反转，自动覆盖
    started_at/optional 等新增 executor 字段），并**保留** DB 中已由
    _make_history_business_progress 写入的「业务拥有字段」
    （processed/total/target_state_count/last_progress_at）。

    [CORRECTION-05] 使用 SELECT ... FOR UPDATE 锁定 exact SchedulerJobRun 行，确保与
    business writer 在真实 asyncio 并发下串行化（read-modify-write 全程持锁），杜绝
    lost update。

    这样形成：
        executor heartbeat ─┐
                            ├─► 同一条 computing_history summary（不互相覆盖）
        History business  ─┘
    """

    async def _cb(summary: dict[str, Any]) -> None:
        step = summary.get("step")
        if step != "computing_history":
            # 非本步骤仍走通用整块覆盖语义（其它步骤没有分离的业务 writer）
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(SchedulerJobRun)
                    .where(SchedulerJobRun.id == job_run_id)
                    .with_for_update()
                )
                job_run = (await db.execute(stmt)).scalar_one_or_none()
                if job_run is None:
                    return
                meta = _parse_metadata(job_run)
                step_summary = dict(meta.get("step_summary") or {})
                step_summary[step] = summary
                meta["step_summary"] = step_summary
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                await db.commit()
            return
        try:
            async with AsyncSessionLocal() as db:
                # [CORRECTION-05] FOR UPDATE：锁定 exact 行，避免与 business writer 并发
                # lost update。
                stmt = (
                    select(SchedulerJobRun)
                    .where(SchedulerJobRun.id == job_run_id)
                    .with_for_update()
                )
                job_run = (await db.execute(stmt)).scalar_one_or_none()
                if job_run is None:
                    return
                meta = _parse_metadata(job_run)
                step_summary = dict(meta.get("step_summary") or {})
                existing = dict(step_summary.get("computing_history") or {})
                # executor 可写：summary 中除业务拥有字段外的所有字段（反转白名单，
                # 自动保留 started_at/optional 等）
                merged = dict(existing)
                for f, v in summary.items():
                    if f in _HISTORY_BUSINESS_OWNED_FIELDS:
                        continue
                    merged[f] = v
                merged["step"] = "computing_history"
                step_summary["computing_history"] = merged
                meta["step_summary"] = step_summary
                job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
                await db.commit()
        except Exception as exc:  # 进度回调失败不得影响主流程
            logger.warning(
                "[AfterClose] history executor progress 写入失败: error=%s", exc,
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


async def _run_dsa_compatibility_projection(
    *,
    job_run_id: uuid.UUID,
    worker_id: str | None,
    lease_epoch: int | None,
    trade_date: date,
    snapshot_run_id: uuid.UUID,
    dsa_run_id: uuid.UUID | None,
    instrument_ids: list[str],
) -> dict[str, Any]:
    """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] post-core OPTIONAL DSA 兼容性投影。

    状态 owner 是统一执行器 execute_orchestrator_step("dsa_compatibility",
    optional=True) 的 summary —— 本函数不再自持 catch-all return 状态机：
    执行异常时先如实把 DSA run 标记为 failed，然后把异常向 optional executor
    re-raise（由 executor 吸收为 step_summary["dsa_compatibility"].status="failed"
    → optional_failures → parent partial_success）。成功返回业务结果 dict。

    内部合同（全部不重算 DSA kernel，仅消费已持久化 Core artifact）：

    1. 创建/复用 required_compatibility StrategyRun（source_core_run_id=X）；
       恢复路径对已有 run 做 lineage fail-closed 核验
       （input_overrides.source_core_run_id / requirement / trade_date）
    2. already-published 幂等 resume：status==published 且 published_at 非空 →
       直接返回 succeeded（不投影/不质检/不新建）；published_at 缺失 fail-closed
    3. project_dsa_batch + persist_precomputed_dsa_results（从 snapshot 重建 artifact）
    4. 质量门禁（run 必须 completed 且 succeeded>0 → _check_quality_gates）
    5. publish_run 真实签名调用：await StrategyBatchService().publish_run(db, run_id)
       （内部只 flush 不 commit）→ 由本函数显式 commit
    6. 提交后复核真实行：status == "published" 且 published_at 非空，
       否则视为兼容性输出未达成 → RuntimeError（optional failed，绝不伪造成功）

    状态所有权：仅执行中非终态异常把 run 标 failed；completed 的门禁失败由
    step_summary 表达（run 保持 completed），不得改写 completed 领域语义。
    """
    from app.constants.strategy_keys import DSA_SELECTOR
    from app.services.core_artifact_repository import CoreArtifactRepository
    from app.services.strategy_batch_service import (
        StrategyBatchService,
        persist_precomputed_dsa_results,
    )

    # 局部实例（原 mandatory 路径亦如此），不污染模块级符号。
    batch_service = StrategyBatchService()

    try:
        async with AsyncSessionLocal() as db:
            job_run = await _get_job_run_or_raise(db, job_run_id)

            # 1) 兼容性 StrategyRun 创建（首次运行 dsa_run_id 为 None）
            if dsa_run_id is None:
                dsa_run = await batch_service.create_batch_run(
                    db=db,
                    strategy_key=DSA_SELECTOR,
                    trade_date=trade_date,
                    run_type="scheduled",
                    instrument_ids=instrument_ids,
                    claim_for_worker=f"orchestrator:{worker_id}",
                    source_core_run_id=snapshot_run_id,
                    requirement="required_compatibility",
                )
                await db.commit()
                dsa_run_id = dsa_run.id
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_FEATURES,
                    message=(
                        f"[DSA compat] 已创建 compatibility projection run: "
                        f"dsa_run_id={dsa_run_id}"
                    ),
                    dsa_run_id=dsa_run_id,
                    payload={
                        "dsa_run_id": str(dsa_run_id),
                        "source_core_run_id": str(snapshot_run_id),
                    },
                )
                await db.commit()

            dsa_run_row = await db.get(StrategyRun, dsa_run_id)
            if dsa_run_row is None:
                raise RuntimeError(f"DSA run 不存在 dsa_run_id={dsa_run_id}")

            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04-PG-GATE]
            # lineage fail-closed：恢复到已有 compatibility run 时必须核验真实身份
            # （StrategyRun 无直列 source_core_run_id —— create_batch_run 把
            # source_core_run_id / requirement 写入 input_overrides JSONB，
            # existing-run 幂等查询亦以该键匹配）。lineage 不匹配 → fail-closed，
            # 禁止把其他 CoreRun 的 DSA compatibility run 当作本 Core 的兼容输出。
            _row_source_core = ((dsa_run_row.input_overrides or {}).get(
                "source_core_run_id"))
            _row_requirement = (dsa_run_row.input_overrides or {}).get("requirement")
            if (_row_source_core != str(snapshot_run_id)) or (
                _row_requirement != "required_compatibility"
            ) or (getattr(dsa_run_row, "trade_date", None) != trade_date):
                raise RuntimeError(
                    f"[DSA compat] lineage 不匹配，禁止复用: dsa_run_id={dsa_run_id}, "
                    f"expected(source_core={snapshot_run_id}, "
                    f"requirement=required_compatibility, trade_date={trade_date}), "
                    f"actual(source_core={_row_source_core}, "
                    f"requirement={_row_requirement}, "
                    f"trade_date={getattr(dsa_run_row, 'trade_date', None)})"
                )

            # [CORRECTION-04-PG-GATE] already-published 幂等 resume：
            # crash 发生在 publish commit 之后时，恢复路径会带着既有 dsa_run_id 重入。
            # status == published 且 published_at 非空 → 兼容输出已达成，
            # 直接幂等返回 succeeded；不得重新投影/持久化/质检/创建新 run，
            # 也不得将 parent 标 partial_success（这不是失败）。
            if getattr(dsa_run_row, "status", None) == "published":
                if dsa_run_row.published_at is None:
                    # fail-closed：published 但缺时间戳属损坏事实，禁止冒充成功。
                    raise RuntimeError(
                        f"[DSA compat] run 已 published 但 published_at 缺失，"
                        f"拒绝幂等复用: dsa_run_id={dsa_run_id}"
                    )
                logger.info(
                    "[AfterClose][DSA compat] 已发布兼容 run 幂等复用（resume）: "
                    "dsa_run_id=%s, published_at=%s",
                    dsa_run_id, dsa_run_row.published_at,
                )
                return {
                    "status": "succeeded",
                    "dsa_run_id": str(dsa_run_id),
                    "published_at": dsa_run_row.published_at.isoformat()
                    if hasattr(dsa_run_row.published_at, "isoformat") else None,
                    "resumed": True,
                }

            strategy_version_id = dsa_run_row.strategy_version_id

            # 2) DSA 预计算投影（不重算算法，仅从持久化 snapshot 重建 artifact）
            if getattr(dsa_run_row, "status", None) not in (
                "completed", "failed", "partial_failed", "published"
            ):
                repo = CoreArtifactRepository(db, batch_size=200)
                await repo.project_dsa_batch(
                    source_core_run_id=snapshot_run_id,
                    dsa_run_id=dsa_run_id,
                    trade_date=trade_date,
                    strategy_version_id=strategy_version_id,
                    persist_fn=persist_precomputed_dsa_results,
                    job_run_id=job_run_id,
                )
                await db.commit()

            # 3) 质量门禁前提：run 必须已达 completed 且有成功结果。
            refreshed = await db.get(StrategyRun, dsa_run_id)
            if refreshed.status != "completed":
                raise RuntimeError(
                    f"[DSA compat] 投影后 run 未达 completed: "
                    f"status={refreshed.status}, dsa_run_id={dsa_run_id}"
                )
            if (refreshed.succeeded_count or 0) <= 0:
                raise RuntimeError(
                    f"[DSA compat] 无成功结果禁止发布: dsa_run_id={dsa_run_id}"
                )
            result_count = await strategy_result_repository.count_by_run(
                db, dsa_run_id
            )
            quality_passed = await batch_service._check_quality_gates(
                refreshed, result_count=result_count, db=db
            )
            if not quality_passed:
                raise RuntimeError(
                    f"[DSA compat] 组合质量门禁未通过: dsa_run_id={dsa_run_id}, "
                    f"succeeded={refreshed.succeeded_count}, total={refreshed.total_instruments}"
                )

            # 4) 兼容性发布 —— 真实签名合同（KPI-7/8）：
            #    publish_run(db, run_id)；publish_run 内部只 flush 不自行 commit，
            #    因此本函数随后显式 commit。
            published_row = await StrategyBatchService().publish_run(
                db, dsa_run_id
            )
            await db.commit()

            # 5) 提交后复核真实行状态（不得以内存对象冒充持久化事实）
            verify_row = await db.get(StrategyRun, dsa_run_id)
            if verify_row is None or verify_row.status != "published":
                raise RuntimeError(
                    f"[DSA compat] 发布后复核失败: status="
                    f"{getattr(verify_row, 'status', None)}, dsa_run_id={dsa_run_id}"
                )
            if verify_row.published_at is None:
                raise RuntimeError(
                    f"[DSA compat] 发布后 published_at 缺失: dsa_run_id={dsa_run_id}"
                )

        logger.info(
            "[AfterClose][DSA compat] 兼容性投影完成并发布: dsa_run_id=%s, "
            "published_at=%s",
            dsa_run_id, verify_row.published_at,
        )
        return {
            "status": "succeeded",
            "dsa_run_id": str(dsa_run_id),
            "published_at": verify_row.published_at.isoformat()
            if hasattr(verify_row.published_at, "isoformat") else None,
        }
    except Exception as dsa_exc:
        # [CORRECTION-04-PG-GATE] 状态所有权准确描述（修正过宽的"所有异常都会把
        # run 标 failed"表述）：
        # - 仅当 run 仍处于执行中非终态（queued/running 等执行阶段异常）时，
        #   才按既有状态机把 StrategyRun 标记 failed —— StrategyRun.status==completed
        #   表示「DSA projection 计算完成」，该领域语义不得被改写；
        # - completed 之后的质量门禁失败 / 未发布，由统一执行器的
        #   step_summary["dsa_compatibility"].status=failed 表达（run 保持 completed），
        #   不回写 completed→failed；
        # - lineage fail-closed / published-at 缺失等校验异常发生在已终态行上，
        #   同样不触碰 row 状态。
        # 异常本身继续向 optional executor re-raise：由 execute_orchestrator_step
        # 吸收为 step_summary["dsa_compatibility"] failed → parent partial_success。
        # 绝不在 helper 内吞掉异常伪造状态；Core/Review 不受影响（post-core 位置保证）。
        logger.error(
            "[AfterClose][DSA compat] 兼容性投影失败（OPTIONAL）: "
            "snapshot_run_id=%s, error=%s",
            snapshot_run_id, dsa_exc, exc_info=True,
        )
        try:
            async with AsyncSessionLocal() as fail_db:
                failed_row = (
                    await fail_db.get(StrategyRun, dsa_run_id) if dsa_run_id else None
                )
                if failed_row is not None and failed_row.status not in (
                    "completed", "failed", "partial_failed", "published"
                ):
                    failed_row.status = "failed"
                    failed_row.error_message = f"DSA 兼容性投影失败: {dsa_exc}"[:500]
                    failed_row.finished_at = datetime.now(UTC)
                    await fail_db.commit()
        except Exception as persist_exc:
            logger.error(
                "[AfterClose][DSA compat] 写 DSA 失败状态异常: %s", persist_exc,
                exc_info=True,
            )
        raise


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


async def _persist_step_summary_status(
    *,
    job_run_id: uuid.UUID,
    step: str,
    status: str,
    error_code: str | None = None,
    processed: Any = None,
) -> None:
    """[SLICE-01-CORRECTION-02] 轻量便捷封装：直接以给定 status/error_code 写 step_summary。

    用于业务结果真实性与状态机收尾（如 History not_ready 必须标 failed，而非被
    execute_orchestrator_step 误写为 succeeded）。仅覆盖该 step 的 status/error_code/
    processed/last_progress_at，不破坏其他既有字段。
    """
    try:
        async with AsyncSessionLocal() as db:
            job_run = await db.get(SchedulerJobRun, job_run_id)
            if job_run is None:
                return
            meta = _parse_metadata(job_run)
            step_summary = dict(meta.get("step_summary") or {})
            existing = dict(step_summary.get(step) or {})
            existing["step"] = step
            existing["status"] = status
            if error_code is not None:
                existing["error_code"] = error_code
            if processed is not None:
                existing["processed"] = processed
            existing["last_progress_at"] = datetime.now(UTC).isoformat()
            step_summary[step] = existing
            meta["step_summary"] = step_summary
            job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
            await db.commit()
    except Exception as exc:
        logger.warning(
            "[AfterClose] step summary 状态覆盖失败: step=%s, error=%s", step, exc,
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
    - PARTIAL_SUCCESS：核心已发布（stock_core）但可选阶段（auction/review/chip）失败/跳过
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
    # [SLICE-01-CORRECTION-02] 新增 First Pyramid History 自动生产 + exact-T readiness 阶段
    COMPUTING_HISTORY = "computing_history"
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


async def _claim_or_recover_dsa_run(
    *,
    db_session_local: Any,
    dsa_run_id: uuid.UUID,
    worker_id: str,
    job_run_id: uuid.UUID,
    lease_epoch: Any = None,
) -> tuple[bool, uuid.UUID]:
    """[AfterClose 2.2] inline claim / cross-worker fencing / recovery DSA run。

    断点恢复已存在 DSA run 时，根据其状态：
    - queued：legacy inline claim（防 worker 领取）；
    - running：跨 worker fencing（条件原子 UPDATE，attempt_count 作 fencing token）；
    - completed/published：已处理完成，跳过 DSA 写入（dsa_already_completed=True）；
    - failed/partial_failed/max_retries_exceeded：调用正式 dsa_recovery_service 创建新 run。

    抽成独立函数以避免在 computing_features 中整块 re-indent（减少无意义 diff churn）。

    Returns:
        (dsa_already_completed, dsa_run_id)
    """
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    dsa_already_completed = False
    async with db_session_local() as db:
        dsa_run = await _get_strategy_run_or_raise(db, dsa_run_id)
        if dsa_run.status == "queued":
            # Legacy/断点恢复：inline claim，防止 DSA worker 领取
            dsa_run.status = "running"
            dsa_run.started_at = _dt.now(_UTC)
            dsa_run.heartbeat_at = _dt.now(_UTC)
            dsa_run.worker_id = f"orchestrator:{worker_id}"
            await db.commit()
            logger.info(
                "[AfterClose] legacy inline claim DSA run: run_id=%s, worker_id=%s",
                dsa_run_id, dsa_run.worker_id,
            )
        elif dsa_run.status == "running":
            # [Phase8A-correction] cross-worker recovery with real fencing
            expected_worker = f"orchestrator:{worker_id}"
            if dsa_run.worker_id != expected_worker:
                old_worker_id = dsa_run.worker_id
                old_attempt_count = dsa_run.attempt_count or 0
                now_utc = _dt.now(_UTC)
                new_lease_expires = now_utc + _td(minutes=30)

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

    return dsa_already_completed, dsa_run_id


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
    # [SLICE-01-CORRECTION-05] 重新从 DB 读取最新已提交 metadata_json（FOR UPDATE 持锁），
    # 不信任传入的 job_run 内存对象：computing_history 期间 business progress 由独立
    # AsyncSession 提交，producer 会话内的 job_run 可能是旧快照；若直接用旧快照会覆盖
    # 业务进度（lost update）。锁后读取保证看到最新已提交状态。
    refreshed = (
        await db.execute(
            select(SchedulerJobRun)
            .where(SchedulerJobRun.id == job_run.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # 保留已有 metadata（含 feature_snapshot_progress / feature_snapshot_run_id / 业务进度等），
    # 仅更新 last_completed_step。
    meta = _parse_metadata(refreshed) if refreshed is not None else _parse_metadata(job_run)
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


def _make_history_step(
    *,
    job_run_id: uuid.UUID,
    trade_date: date,
    worker_id: str | None,
    skip_history: bool,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """[SLICE-01 H2] 构造 computing_history 步骤业务体。

    exact-T First Pyramid History 自动生产 + readiness 门控步骤：
    - skip_history=True 时（显式跳过复盘）直接返回 ready=False（不阻断主流程）；
    - 否则：
        1) resolve-or-create 当前 canonical FirstPyramidHistoryRun（producer resolver，幂等）；
        2) 调用既有 canonical advancement owner ``advance_history_to_trade_date``
           把数据集推进到 trade_date（1x write amplification，幂等，不重写历史）；
        3) 用 ``validate_canonical_history_run_readiness`` 校验该 run 对 trade_date
           是否 exact-T ready（真实合同：返回 dict，status=='ok' 才是 ready）。
    返回 dict：{"history_run_id", "ready", "status", "reason", "advanced"}。
    history_run_id 仅作诊断 metadata；ready 才是 Review 是否允许的真实门控信号。
    """

    async def _run() -> dict[str, Any]:
        # [SLICE-01-CORRECTION-02/03] History 启动前写入 orchestrator 当前阶段，
        # 由主流程在拥有 db/job_run 的上下文中调用 _update_orchestrator_status
        # （真实合同需要 db + job_run，不在 operation 内部用 job_run_id= 误调用）。
        if skip_history:
            return {
                "history_run_id": None,
                "ready": False,
                "status": "skipped",
                "reason": "skip_history",
                "advanced": False,
            }
        async with AsyncSessionLocal() as db:
            run, _is_new = await ensure_current_first_pyramid_history_run(
                db,
                scheduler_job_run_id=job_run_id,
            )
            await db.commit()
            # [SLICE-01-CORRECTION] 先自动推进 canonical History 到 trade_date。
            # 这是当前 canonical exact-T advancement owner（非 250 天全量重跑），
            # 幂等（daily_state upsert + events on_conflict_do_nothing）。
            # [SLICE-01-CORRECTION-03] business progress 使用专属回调
            # _make_history_business_progress（只认 producer 的 {processed,total,
            # target_state_count}，MERGE 进既有 step_summary 并写 last_progress_at）；
            # 外层 executor 的 progress= 另用 _make_step_progress_callback（不碰
            # last_progress_at，heartbeat 不冒充业务 progress）。两者严格分离。
            advance_result = await advance_history_to_trade_date(
                db,
                run.id,
                trade_date,
                progress_callback=_make_history_business_progress(job_run_id, worker_id),
            )
            await db.commit()
            # [SLICE-01-CORRECTION] 真实 readiness 合同：返回 dict，
            # status == 'ok' 才是 exact-T ready；任何 predicate 不满足都返回
            # status='not_ready'（含 reason）。禁止 bool(dict)（空 dict 仍为 True）。
            readiness = await validate_canonical_history_run_readiness(
                db,
                run.id,
                HISTORY_CONTRACT_VERSION,
                required_trade_date=trade_date,
            )
            await db.commit()
            history_ready = (
                isinstance(readiness, dict) and readiness.get("status") == "ok"
            )
            logger.info(
                "[AfterClose][H2] history advance+readiness job=%s trade=%s run=%s "
                "advance=%s readiness_status=%s ready=%s",
                str(job_run_id), trade_date, run.id,
                advance_result.get("target_state_count"),
                readiness.get("status") if isinstance(readiness, dict) else readiness,
                history_ready,
            )
            # [SLICE-01-CORRECTION-03] 业务结果真实性（step_summary 标 failed）放在
            # execute_orchestrator_step 返回以后由主流程统一修正并持久化，避免被
            # executor finally 的 succeeded 覆盖（见主流程 after-history 块）。
            return {
                # 诊断 metadata（不用于 Review gate 绕过 readiness）
                "history_run_id": run.id,
                # 真实门控信号：仅 exact-T readiness 决定
                "ready": history_ready,
                "status": "succeeded" if history_ready else "not_ready",
                "reason": None if history_ready else "HISTORY_NOT_READY_T",
                "advanced": True,
                "readiness_detail": readiness if isinstance(readiness, dict) else {"status": "error"},
            }

    return _run


def _make_history_v3_step(
    *,
    job_run_id: uuid.UUID,
    trade_date: date,
    worker_id: str | None,
    skip_history: bool,
    core_run_id: uuid.UUID | None = None,
) -> Callable[[], Awaitable[dict[str, Any]]]:
    """[CHANGE-20260826-001 History-v3] Daily AfterClose 的 canonical History(T) owner。

    **投影语义，禁止重算**：从 durable Core artifact
    （``StockFeatureSnapshot.summary_payload["first_pyramid_flat"]``，Core 已计算一次）
    投影并物化 review-history-v3。不再调用 ``advance_history_to_trade_date`` /
    ``compute_first_pyramid_history`` / DSA / SMC / BB / SQZMOM / VolumeContext。

    旧 ``_make_history_step`` + ``advance_history_to_trade_date`` 保留给 legacy v2
    replay / backfill 专用，daily AfterClose 不可达。

    Returns:
        {"materialized": bool, "ready": bool, "processed", "target_state_count",
         "no_core_flat", "failed", "failed_instruments"}
    """

    async def _run() -> dict[str, Any]:
        if skip_history:
            return {
                "materialized": False,
                "ready": False,
                "status": "skipped",
                "reason": "skip_history",
                "processed": 0,
                "target_state_count": 0,
                "no_core_flat": 0,
                "failed": 0,
                "failed_instruments": [],
            }

        from sqlalchemy import and_

        from app.models.first_pyramid_history import FirstPyramidHistoryDailyState
        from app.models.stock_feature_snapshot import StockFeatureSnapshot
        from app.services.first_pyramid_history_service import (
            materialize_history_v3_from_core,
        )

        async with AsyncSessionLocal() as db:
            # 取当日所有已发布 Core 快照的 first_pyramid_flat（durable artifact）
            rows = await db.execute(
                select(
                    StockFeatureSnapshot.instrument_id,
                    StockFeatureSnapshot.source_run_id,
                    StockFeatureSnapshot.summary_payload,
                ).where(StockFeatureSnapshot.trade_date == trade_date)
            )
            snapshots = rows.all()

            processed = 0
            target_state_count = 0
            no_core_flat = 0
            failed = 0
            failed_instruments: list[dict[str, Any]] = []

            for instrument_id, snap_run_id, summary in snapshots:
                core_flat = (summary or {}).get("first_pyramid_flat")
                if not isinstance(core_flat, dict) or not core_flat:
                    no_core_flat += 1
                    continue
                try:
                    result = await materialize_history_v3_from_core(
                        db,
                        instrument_id,
                        trade_date,
                        core_flat,
                        core_run_id=core_run_id or snap_run_id,
                    )
                    processed += 1
                    target_state_count += result.get("daily_state_count", 0)
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    failed_instruments.append(
                        {"instrument_id": str(instrument_id), "error": str(exc)[:200]}
                    )
                    logger.warning(
                        "[AfterClose][H3] v3 materialize failed iid=%s err=%s",
                        instrument_id, exc,
                    )

            await db.commit()

            # v3 readiness：T 日已成功投影即为 ready（纯投影，无二次重算等待）
            ready = processed > 0 and failed == 0
            logger.info(
                "[AfterClose][H3] v3 materialize job=%s trade=%s processed=%s "
                "target_state=%s no_core_flat=%s failed=%s",
                str(job_run_id), trade_date, processed, target_state_count,
                no_core_flat, failed,
            )
            return {
                "materialized": True,
                "ready": ready,
                "status": "succeeded" if ready else "partial",
                "reason": None if ready else "HISTORY_V3_PARTIAL",
                "processed": processed,
                "target_state_count": target_state_count,
                "no_core_flat": no_core_flat,
                "failed": failed,
                "failed_instruments": failed_instruments,
            }

    return _run


async def _execute_review_step(
    *,
    job_run_id: uuid.UUID,
    trade_date: date,
    snapshot_run_id: uuid.UUID | None,
    worker_id: str | None,
    skip_review: bool,
    stock_core_published: bool,
    history_run_id: uuid.UUID | None = None,
    history_ready: bool = False,
) -> dict[str, Any]:
    """[AC-02] computing_review 业务体（软失败，不阻断主流程）。

    [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 / CORRECTION-04-PG-GATE] 当前合同：
    Review(T) = Core(T) + History(<T)。当前第一金字塔事实直接来自
    Core(T) StockFeatureSnapshot（由显式 source_core_run_id=snapshot_run_id=X 锁定）；
    历史 baseline 来自 FirstPyramidHistoryDailyState 的 <=T-1 状态。
    History(T) 在 Review 之后由 computing_history 生产，exact-T History(T)
    不是 Review 前置条件；stock_core publication 不是 Review readiness owner。
    （history_ready/history_run_id 参数保留为兼容签名与诊断 metadata，
    不再参与 gate 判定 —— 原 invariant H2 硬门控已移除。）


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
        # [CHANGE-20260826-001 Slice 1 REVIEW-CURRENT-OWNER-01]
        # Review(T) = Core(T) + History(<T)。exact-T History(T) 不再是 Review 前置条件：
        # Review 当前第一金字塔事实直接来自 Core(T)（StockFeatureSnapshot，
        # 由显式 source_core_run_id=snapshot_run_id 锁定）；历史 baseline 来自
        # FirstPyramidHistoryDailyState 的 <=T-1 状态（独立于 exact-T History(T)）。
        # 因此移除旧的「History(T) 未就绪即阻断 Review」硬门控（原 invariant H2）；
        # stock_core publication 不是 Review readiness owner，Review 不经任何 pointer 解析。
        # 注意：本 Slice 不引入任何 review-history-v3 DB write（v3 物化在 Slice 4 接回）。
        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] Core 计算完成后直接进入 Review。
        # Review 的 readiness 仅依赖本次 AfterCloseRun 产生的 CoreRun（StockFeatureSnapshotRun，
        # 由 snapshot_run_id 标识）compute-complete 合同，**不再依赖 stock_core publication
        # pointer / published_at / FactorPublication(kind=stock_core)**。因此 gate 只检查
        # snapshot_run_id 非空（Core 已产生可消费的快照 run），不再检查 stock_core_published。
        if snapshot_run_id is not None:
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
                        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] 显式绑定本次 CoreRun
                        # （snapshot_run_id），Review 直接消费，不再经 stock_core
                        # FactorPublication pointer 解析（KPI-4: 100% lineage lock）。
                        source_core_run_id=snapshot_run_id,
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
            # 前置条件不满足：stock_core 未正式发布或 snapshot 缺失
            _review_status = "skipped"
            _review_reason = (
                f"prerequisite_missing: stock_core_published={stock_core_published}, "
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
                        },
                    )
                    # 前置条件缺失：仅刷新心跳/租约，不推进 last_completed_step。
                    # 否则后续 resume 会误判 computing_review 已完成，永久跳过 Review。
                    await _update_heartbeat_and_step(
                        db, job_run, None, worker_id,
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


def _is_terminal_review_short_circuit(review_step_status: str | None) -> bool:
    """[AC-CANCEL-01 2026-08-04] 判定 Review 步骤终态是否必须短路收尾。

    Review step 为 cancelled / interrupted 时，主流程不得覆盖总任务终态：

    [CORRECTION-04 2026-08-26] 短路点位于 computing_review 之后、computing_history
    之前：Review cancelled/interrupted 时 History / DSA compatibility / state_events /
    chip 全部不再执行（KPI-11）。此前 History 终止短路仅覆盖 History 阶段的取消，
    Review 阶段取消会继续跑后续副作用 —— 本判定现提前到 Review 后立即生效。
    - cancelled：管理员主动取消，保持 cancelled；
    - interrupted：旧 Worker 被接管，保持 interrupted，交由 reconcile/restart。

    其余终态（succeeded / failed / timed_out / unavailable）走既有
    partial_success 判定，不在此短路。
    """
    return review_step_status in (
        AfterCloseRunStatus.CANCELLED.value,
        AfterCloseRunStatus.INTERRUPTED.value,
    )


def resolve_terminal_run_status(review_step_status: str | None) -> AfterCloseRunStatus:
    """[AC-TERMINAL-01 2026-08-04] 把 Review 终态字符串转为 AfterCloseRunStatus 枚举。

    P0 修复：`_update_orchestrator_status(status=...)` 的形参类型是
    AfterCloseRunStatus 枚举，此前短路块直接传入裸字符串
    ("cancelled"/"interrupted")，导致 `status.value` 在运行时抛
    AttributeError（str 无 .value），取消链路写状态失败。

    仅接受短路终态；其余输入视为编程错误直接抛 ValueError，
    避免把未知字符串静默映射成某个终态。
    """
    if review_step_status == AfterCloseRunStatus.CANCELLED.value:
        return AfterCloseRunStatus.CANCELLED
    if review_step_status == AfterCloseRunStatus.INTERRUPTED.value:
        return AfterCloseRunStatus.INTERRUPTED
    raise ValueError(
        f"非短路终态不得转换为总任务终态: review_step_status={review_step_status!r}"
    )


class AfterCloseCancelledError(Exception):
    """[AC-TERMINAL-01 2026-08-04] 盘后编排被取消/中断的信号异常。

    外层 except 捕获到本异常时，不得把 job_run 覆写成 failed —— 终态已由
    短路块写入（cancelled/interrupted）。
    """

    def __init__(self, terminal_status: AfterCloseRunStatus) -> None:
        self.terminal_status = terminal_status
        super().__init__(f"after-close run terminated as {terminal_status.value}")


class AfterCloseCoreNotReadyError(Exception):
    """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] Mandatory Core 未就绪。

    Core 计算完成（finalize）后，CoreRun 必须 status==succeeded 且
    trade_date 匹配，否则 mandatory 下游（Review / History(T) / state_events /
    chip / DSA compatibility）不得执行，且 Core 失败不得降级为 partial_success。
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"after-close Core not ready: {reason}")


async def _validate_core_ready(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID | None,
    trade_date: date,
) -> "object":
    """[AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] canonical Core readiness owner。

    唯一权威判定 CoreRun 是否可进入 mandatory 下游（Review / History(T) /
    state_events / chip / DSA compatibility）。直接校验真实 CoreRun 行：

    - run 存在且 id == snapshot_run_id
    - run.trade_date == T
    - run.status == succeeded（compute-complete contract 满足）

    任一不满足抛 AfterCloseCoreNotReadyError（fail-closed）。
    Core 是 mandatory foundation，其失败不得降级为 partial_success。

    不使用任何会偏离 DB 事实的派生布尔（如 snapshot_run_id 非空 /
    snapshot_error 为空 / publication 状态）。
    """
    if snapshot_run_id is None:
        raise AfterCloseCoreNotReadyError(
            f"CoreRun 缺失 snapshot_run_id={snapshot_run_id}"
        )
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun

    core_run = await session.get(StockFeatureSnapshotRun, snapshot_run_id)
    if core_run is None:
        raise AfterCloseCoreNotReadyError(
            f"CoreRun 不存在 snapshot_run_id={snapshot_run_id}"
        )
    if core_run.id != snapshot_run_id:
        raise AfterCloseCoreNotReadyError(
            f"CoreRun id 不匹配 expected={snapshot_run_id} actual={core_run.id}"
        )
    if core_run.trade_date != trade_date:
        raise AfterCloseCoreNotReadyError(
            f"CoreRun trade_date 不匹配 expected={trade_date} "
            f"actual={core_run.trade_date}"
        )
    if core_run.status != STATUS_SUCCEEDED:
        raise AfterCloseCoreNotReadyError(
            f"Core 计算未完成 status={core_run.status} "
            f"(running/failed/pending)，禁止进入下游 mandatory 路径"
        )
    return core_run


async def resolve_stock_core_published(
    session: AsyncSession,
    trade_date: date,
    snapshot_run_id: uuid.UUID | None,
) -> tuple[bool, bool]:
    """唯一权威判定：snapshot_run_id 是否为当前 stock_core publication pointer。

    返回 (published, superseded)：
    - published=True  : pointer 存在且 data_run_id == snapshot_run_id（本 run 是正式发布）
    - superseded=True : pointer 存在但指向别的 run（本 run 被抢占）
    - 两者皆 False    : 无 pointer（未发布）

    这是 Chip 入队、auction anchor 的唯一判据来源（[Slice 4A9] legacy board aggregation 已退役）。
    normal publish 路径与 skip_publish=True resume 路径共用同一判定，
    避免各自用局部布尔（publish_failed / snapshot_error）推断“已发布”而产生分叉。
    """
    if snapshot_run_id is None:
        return (False, False)
    from app.models.factor_publication import PUBLICATION_KIND_STOCK_CORE
    from app.services.factor_publication_service import get_publication

    existing_pub = await get_publication(
        session,
        scope_type="market",
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    if existing_pub is None:
        return (False, False)
    if existing_pub.data_run_id == snapshot_run_id:
        return (True, False)
    return (False, True)


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
    # [Phase4.2 corrective] 不再以顶部 "skipped" 默认值掩盖业务步骤未执行。
    # 这些状态变量由 normal publish 分支（执行 auction anchor / publishing checkpoint）
    # 或 skip_publish 分支（显式置 skipped）赋值，不得用统一的默认值假装“已处理”。
    # [Slice 4A9] legacy board aggregation 已退役，_aggregation_status 恒为 "skipped"。

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
    # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] core_ready 唯一生命周期：
    # 仅在此初始化一次为 False。正常路径由 mandatory Core gate（finalize 后经
    # canonical _validate_core_ready 校验真实 CoreRun 行）置 True；skip_publish /
    # resume 分支同样调用 _validate_core_ready 置位。此后任何代码路径不得重新
    # 初始化/覆盖为 False（CORRECTION-03 曾出现 gate PASS 后被二次声明清零的
    # false-green：DSA/state_events/chip 全部静默跳过）。
    core_ready: bool = False

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
        # 阶段顺序（PHASE-A Core→Review Source Closure）：
        #   refreshing_daily → syncing_boards → computing_features
        #   → computing_review → computing_history → post-core optional → succeeded
        # publishing / stock_core 发布已旁路，不再是真实步骤（KPI-A1）。
        # 旧步骤名（waiting_dsa_worker/quality_gate/feature_snapshot）兼容读取历史 run
        # [REPROCESS-OWNER-CLOSURE-01 P0-2] mainchain_stage 是「本次 execution 从哪里开始」
        # 的正式起点合同（PRD30 AC-16），与 last_completed_step（「已真实完成的检查点」，
        # 用于断点恢复）语义分离，不得混用，也不得伪造 last_completed_step。
        # 若 run metadata 含 mainchain_stage，则把 _CHECKPOINT_ORDER 中位于该 stage 之前
        # 的所有阶段标记为跳过（独立 skip 集合，不写 last_completed_step）。
        meta = _parse_metadata(job_run)
        mainchain_stage = meta.get("mainchain_stage")
        # [REPROCESS-OWNER-CLOSURE-01 CORRECTION-01] 单一真相源：stage-resolution owner。
        # 合并 checkpoint resume (last_completed_step) 与 restart 正式起点 (mainchain_stage)。
        # 非法 mainchain_stage 在此 fail-closed（corrupt/typo metadata 不得退化为 full run）。
        completed = _resolve_execution_completed_steps(last_completed_step, mainchain_stage)
        if mainchain_stage and mainchain_stage in _CHECKPOINT_ORDER:
            stage_rank = _CHECKPOINT_ORDER[mainchain_stage]
            pre_stages = {
                s for s, r in _CHECKPOINT_ORDER.items() if r < stage_rank
            }
            logger.info(
                "[AfterClose] mainchain_stage=%s，跳过其之前阶段=%s",
                mainchain_stage, sorted(pre_stages),
            )
        if "succeeded" in completed:
            logger.info(
                "[AfterClose] 断点恢复: 已完成 succeeded，直接返回: job_run_id=%s",
                job_run_id,
            )
            return

        # [CHANGE-20260728-008 kept-for-history] 原 dsa_only 模式已删除。
        # 当前合同（REPROCESS-OWNER-CLOSURE-01）：
        #   - restart 正式起点 = mainchain_stage（由 granular_restart_service 注入，
        #     经 _resolve_execution_completed_steps 合并其之前 pre-stage 为已完成，跳过 refreshing_daily）。
        #   - checkpoint resume = last_completed_step（旧合同，仍独立可用）。
        #   二者不得混用：daily_ready restart 不再伪造 last_completed_step="refreshing_daily"。

        skip_refresh = "refreshing_daily" in completed
        skip_board_sync = "syncing_boards" in completed
        # [Phase 5] 3 个旧 skip 标志收敛为 skip_computing
        skip_computing = "computing_features" in completed
        skip_publish = "publishing" in completed
        # [CHANGE-20260801-REVIEW-CLOSURE] review 阶段跳过标志
        skip_review = "computing_review" in completed
        # [SLICE-01-CORRECTION] history 阶段跳过标志：
        # History readiness 不能通过 checkpoint 名称恢复（run 存在 ≠ exact-T ready）。
        # 仅当 computing_review 也已完成（即整个 History+Review 后置链都完成）
        # 才可整体跳过 History 步骤；否则必须重新执行幂等 advance + revalidate，
        # 否则会出现「已验证 ready → crash → resume 反而判 not-ready」的回归。
        skip_history = "computing_history" in completed and "computing_review" in completed

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
            async def _refresh_operation() -> Any:
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
            # [mypy-clean] refreshing_daily 成功后 refresh_result 必非 None（上面已校验 status）
            assert batch_result is not None, "refreshing_daily 成功但结果为空"
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
                        heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
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
                # [required compatibility projection identity] DSA run 创建推迟到 computing_features
                # 步骤（snapshot_run_id 确定后），使 source_core_run_id=snapshot_run_id 可在创建时
                # 写入 input_overrides。此处仅更新状态说明"待 computing_features 创建 DSA run"，
                # 不提前创建（避免无 source_core identity 的 StrategyRun）。
                logger.info(
                    "[AfterClose] [Phase8A] 覆盖率达标，DSA run 将在 computing_features "
                    "（snapshot_run_id 确定后）创建: trade_date=%s, coverage=%.1f%%",
                    trade_date, (batch_result.daily_coverage or 0) * 100,
                )
                async with AsyncSessionLocal() as db:
                    job_run = await _get_job_run_or_raise(db, job_run_id)
                    await _update_orchestrator_status(
                        db=db,
                        job_run=job_run,
                        status=AfterCloseRunStatus.REFRESHING_DAILY,
                        message="DSA run 将在 computing_features 创建（待 snapshot_run_id）",
                    )
                    await _update_heartbeat_and_step(
                        db, job_run, AfterCloseRunStatus.REFRESHING_DAILY.value, worker_id,
                    )
                    await db.commit()

        else:
            # [Phase5] - 断点恢复跳过日线刷新，dsa_run_id 从 metadata 读取
            # [CHANGE-20260728-008] 原 dsa_only 模式已删除。
            # 当 skip_refresh=True 且 dsa_run_id=None 时（如 force?restart_from=daily_ready），
            # 不在 refreshing_daily 阶段创建 DSA run；若 snapshot_run_id 已在 metadata 恢复，
            # 直接在此用正确 source_core 创建；否则推迟到 computing_features 创建。
            # [required compatibility projection identity] source_core_run_id 必须 =
            # snapshot_run_id，故只在 snapshot_run_id 已知时才能创建 compatibility StrategyRun。
            if dsa_run_id is None and snapshot_run_id is not None:
                from app.constants.strategy_keys import DSA_SELECTOR
                logger.info(
                    "[AfterClose] 跳过日线刷新，snapshot_run_id 已恢复，直接创建 DSA run: "
                    "job_run_id=%s, trade_date=%s, snapshot_run_id=%s",
                    job_run_id, trade_date, snapshot_run_id,
                )
                async with AsyncSessionLocal() as db:
                    # [required compatibility projection identity] resume path 的 projection
                    # universe 必须来自 source Core **frozen-input facts source**：
                    # stock_feature_snapshot_run_items（phase='core'）。Core 计算开始时对
                    # 全部 eligible instrument 预创建 core run item（create_run_items 幂等，
                    # 无论单股 snapshot 是否完成），故该集合在任何 resume 状态（已创建 0
                    # snapshot / partial snapshots）都是完整 frozen universe——不得用
                    # StockFeatureSnapshot（只有已完成快照，partial 时会缩小）冒充，也不得
                    # 重新解析新的 active universe。
                    from app.models.stock_feature_snapshot_run_item import (
                        PHASE_CORE as _SNAP_PHASE_CORE,
                    )
                    from app.models.stock_feature_snapshot_run_item import (
                        StockFeatureSnapshotRunItem as _SnapshotRunItem,
                    )
                    frozen_rows = (
                        await db.execute(
                            select(_SnapshotRunItem.instrument_id)
                            .where(
                                _SnapshotRunItem.snapshot_run_id == snapshot_run_id,
                                _SnapshotRunItem.phase == _SNAP_PHASE_CORE,
                            )
                            .distinct()
                        )
                    ).scalars().all()
                    if not frozen_rows:
                        # 冻结 universe 尚未确立（computing_features 未创建 run items，
                        # 如 resume 自更早阶段）→ 不得用空集合冒充，推迟到 2.3b 创建
                        #（保持 dsa_run_id=None，2.3b 的 if dsa_run_id is None 会接管）。
                        logger.info(
                            "[AfterClose] resume: snapshot_run_id=%s 尚无 core run items，"
                            "冻结 universe 未确立，推迟 DSA run 到 computing_features",
                            snapshot_run_id,
                        )
                    else:
                        dsa_run = await batch_service.create_batch_run(
                            db=db,
                            strategy_key=DSA_SELECTOR,
                            trade_date=trade_date,
                            run_type="scheduled",
                            instrument_ids=list(frozen_rows),
                            claim_for_worker=f"orchestrator:{worker_id}",
                            source_core_run_id=snapshot_run_id,
                            requirement="required_compatibility",
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
            # 无需再次 claim；仅在 status=queued 时执行 legacy inline claim（断点恢复场景）。
            # [required compatibility projection identity] 首次运行 dsa_run_id 尚未创建
            # （推迟到 2.3 后以 snapshot_run_id 创建），此处仅在断点恢复已存在 DSA run 时
            # 执行 claim/fencing/recovery（逻辑抽到 _claim_or_recover_dsa_run，避免整块 re-indent）。
            dsa_already_completed = False
            if dsa_run_id is not None:
                dsa_already_completed, dsa_run_id = await _claim_or_recover_dsa_run(
                    db_session_local=AsyncSessionLocal,
                    dsa_run_id=dsa_run_id,
                    worker_id=worker_id,
                    job_run_id=job_run_id,
                    lease_epoch=lease_epoch,
                )
            else:
                # 首次运行：DSA run 在 2.3 snapshot_run_id 确定后创建，此处跳过 claim。
                logger.info(
                    "[AfterClose] computing_features: dsa_run_id 未创建，"
                    "将在 snapshot_run_id 确定后创建（source_core identity）",
                )

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

            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] DSA 兼容性投影的
            # StrategyRun 创建 + project_dsa_batch + persist + quality gate + publish_run
            # finalization 全部从 mandatory Core(computing_features) 路径移除，
            # 改为 post-core OPTIONAL compatibility 工作（见 _run_dsa_compatibility_projection）。
            # DSA 兼容性失败不得标记 Core failed、不得阻断 Review、仅产生 partial_success/degraded。
            # 此处仅在 dsa_run_id 已由更早阶段（refreshing_daily / resume）确立时沿用；
            # 否则留给 post-core 阶段按需创建。

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

                    # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03]
                    # DSA 兼容性投影（project_dsa_batch + persist + quality gate + publish_run）
                    # 已从 mandatory Core 计算路径移除，改由 post-core OPTIONAL compatibility
                    # 阶段 _run_dsa_compatibility_projection 执行。Core 计算只负责 Core 本身；
                    # DSA 兼容性失败不得标记 Core failed、不得阻断 Review。
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

            # [REVIEW-RUNTIME-BLOCKER / 2026-08-09] 计算终态与发布可见性分离。
            # 计算（run-items）已全 terminal 后，立即记录 compute truth（status=succeeded +
            # finished_at），不写 published_at / 不动 pointer。质量门禁失败只阻止发布，
            # 不得使本已完成的 compute run 继续显示 running（COMPUTATION TERMINALITY PRINCIPLE）。
            #
            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] MANDATORY CORE GATE（无条件）。
            # 不得用 `if snapshot_run_id is not None:` 包裹本门禁——那会使 snapshot_run_id=None
            # 绕过校验、Core missing 不 fail-closed（CORRECTION-03 P0-2 回归教训）。
            # snapshot_run_id 为 None → 立即抛 AfterCloseCoreNotReadyError；
            # snapshot_run_id 存在 → finalize 后经 canonical _validate_core_ready 校验真实
            # CoreRun 行（id / trade_date / status==succeeded），任一不满足 → fail-closed raise，
            # Review / History(T) / DSA 兼容性 / state_events / chip 一律不执行，
            # 且 mandatory 失败不得降级为 partial_success（executor 对非 optional 步骤 re-raise）。
            if snapshot_run_id is None:
                raise AfterCloseCoreNotReadyError(
                    f"CoreRun 缺失: snapshot_run_id=None (job={job_run_id}, "
                    f"trade_date={trade_date})，mandatory 下游禁止执行"
                )
            async with AsyncSessionLocal() as db:
                finalized_core_run = await finalize_snapshot_run_compute_complete(
                    db, snapshot_run_id
                )
                await db.commit()
                # canonical Core readiness owner：校验真实 DB 行（单一事实源）
                _validated_core_run = await _validate_core_ready(
                    db, snapshot_run_id, trade_date
                )
            core_ready = True
            logger.info(
                "[AfterClose] Core 已就绪（finalize,未发布）: snapshot_run_id=%s, "
                "trade_date=%s, status=%s",
                snapshot_run_id, trade_date, _validated_core_run.status,
            )

            # 2.6 [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] DSA 组合质量门禁已从
            # mandatory Core 路径移除。continuous + event freshness 门禁仍在 compute_for_trade_date
            # 内部生效（Core 计算本身受控）；DSA 兼容性质量门禁改由 post-core OPTIONAL
            # _run_dsa_compatibility_projection 执行，其失败不得 marking Core failed、不得阻断 Review。

            # 2.7 computing_features 完成，更新心跳 + 检查点
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, job_run, AfterCloseRunStatus.COMPUTING_FEATURES.value, worker_id,
                )
                await db.commit()

        # ---- 步骤 4: post-core readiness setup ----
        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-02] 正常 AfterClose DAG 不再进入
        # PUBLISHING 阶段（KPI-7）：Core 计算完成 → Review(T) 计算 → History(T) 推进 →
        # post-core enhancement（state_events / chip）。stock_core publication 已旁路，
        # 不再是 Review / state_events / chip 的 readiness owner。
        # chip / state_events 的 readiness owner 改为 CoreRun 显式绑定（snapshot_run_id X）；
        # 此处仅置发布相关布尔为安全默认值，供 diagnostics 使用，不再决定任何业务步骤是否执行。
        _stock_core_published = False
        # [P1-2 2026-08-07] chip 状态变量在此处统一声明默认值（位于 skip_publish
        # 分支判定之前），保证 normal publish 分支（下方 post-core 分叉点赋值）与
        # skip_publish 断点恢复分支（显式置 skipped）引用时均已定义，
        # 避免 skip_publish 路径 UnboundLocalError。
        _chip_enqueue_status: str = "skipped"
        _chip_job_id: uuid.UUID | None = None
        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] core_ready 的唯一置位点在
        # mandatory Core gate（fresh compute）与 skip_publish 恢复分支（统一 owner
        # _validate_core_ready）。此处禁止再初始化/清零（CORRECTION-03 P0-1 回归教训）：
        # DSA compatibility / state_events / chip 全部依赖该布尔保持为 True。
        if skip_publish:
            # =================================================================
            # LEGACY publishing-resume compatibility branch
            # -----------------------------------------------------------------
            # 仅当 last_completed_step == "publishing"（或正式兼容合同明确识别的
            # 旧 checkpoint）时进入。正常 Core→Review 主链（skip_publish=False）
            # 永不进入本分支，resolve_stock_core_published 的 stock_core pointer
            # 读取被严格隔离在 legacy 路径内（KPI-A1/A2/A4/A5/A6/A7）。
            # [Phase4.2 corrective] 断点恢复到 publishing 之后：局部布尔不可信，
            # 必须重新核验线上真实 publication pointer 的真实身份。
            # 只有当 pointer 确实存在且 data_run_id == snapshot_run_id 时，本 run 才是正式
            # stock_core publication；否则（pointer 指向别人 / 不存在）一律视为未发布/被抢占，
            # chip 不得入队。禁止用 publish_failed=False 等局部布尔推断“已发布”。
            _stock_core_superseded = False
            if snapshot_run_id is not None:
                # 断点恢复：局部布尔不可信，复用唯一权威判定 resolve_stock_core_published
                # （与 normal publish 路径共用），重新核验线上真实 publication pointer 身份。
                async with AsyncSessionLocal() as verify_db:
                    logger.info(
                        "[BOUNDARY-P1] before resolve job=%s trade=%s pid=%s",
                        str(job_run_id), trade_date, os.getpid(),
                    )
                    _stock_core_published, _stock_core_superseded = await resolve_stock_core_published(
                        verify_db, trade_date, snapshot_run_id
                    )
                    logger.info(
                        "[BOUNDARY-P2] after resolve job=%s published=%s superseded=%s snap=%s pid=%s",
                        str(job_run_id), _stock_core_published, _stock_core_superseded,
                        snapshot_run_id, os.getpid(),
                    )
            else:
                _stock_core_published = False
                _stock_core_superseded = False
            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] skip_publish 恢复路径
            # 必须复用唯一事实源 _validate_core_ready（禁止第二套手写 row exists/
            # trade_date/status 逻辑——CORRECTION-03 曾出现双 owner 漂移）。
            # snapshot_run_id=None 同样 fail-closed；Core running/failed → raise。
            async with AsyncSessionLocal() as verify_db:
                await _validate_core_ready(verify_db, snapshot_run_id, trade_date)
            core_ready = True
            # skip_publish 路径不执行 normal publish 专属步骤，显式置 skipped 以如实反映未执行。
            _auction_anchor_status = "skipped"
            _auction_publication_id = None
            _aggregation_status = "skipped"
            # skip_publish 路径：DSA 兼容性投影由 post-core 统一执行器执行，
            # 此初始值仅在步骤被跳过时如实表达 not_run（实际状态以 executor summary 为准）。
            _dsa_compatibility_status = "not_run"
            _chip_enqueue_status = "skipped"
        else:
            # =================================================================
            # CURRENT Direct-Link path (skip_publish == False)
            # -----------------------------------------------------------------
            # 适用于 fresh / computing_features resume / computing_review resume /
            # computing_history resume。正常 AfterClose DAG 不再进入 PUBLISHING 阶段
            # （KPI-7）：Core 计算完成 → Review(T) → History(T) → post-core enhancement。
            # stock_core publication / FactorPublication(kind=stock_core) 的 read/write
            # 与 publish_stock_core_atomically 调用全部从 Core → Review 主链旁路
            # （KPI-2/3）；DSA 兼容性投影保留为 Review 后 optional enhancement。
            # 本分支严禁调用 resolve_stock_core_published（KPI-A1/A2/A4/A5/A6/A7）。
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=AfterCloseRunStatus.COMPUTING_FEATURES,
                    message=(
                        f"Core 计算完成，直接进入 Review（stock_core 发布已旁路）: "
                        f"dsa_run_id={dsa_run_id}"
                    ),
                    dsa_run_id=dsa_run_id,
                )
                await db.commit()

            # 发布相关布尔直接置安全默认值：无 stock_core publication 也能 Review。
            published_run = None
            _stock_core_published = False
            _stock_core_superseded = False
            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] DSA 兼容性已在 post-core
            # 由统一执行器执行；此初始值仅在步骤被跳过时如实表达 not_run。
            _dsa_compatibility_status = "not_run"
            _auction_anchor_status = "skipped"
            _auction_publication_id = None
            _aggregation_status = "skipped"

            # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 PHASE-A] resume readiness owner：
            # fresh compute 的 mandatory Core gate 在 skip_computing=True（断点恢复）时被跳过，
            # core_ready 不会经该门置位（core_ready 已在 mandatory Core gate 成功路径置 True，
            # 见 3682）。此处对 current resume 复用唯一事实源 _validate_core_ready 重新校验真实
            # CoreRun 行（id / trade_date / status==succeeded），置 core_ready=True，供下游
            # state_events / chip 等 CORE_READY-gated post-core enhancement 继续执行（§10）。
            # 严禁读取 stock_core FactorPublication / pointer（KPI-A7/A8：resume publication read=0）。
            # fresh（skip_computing=False）core_ready 已为 True，无需重复校验（KPI-A7=0 read）。
            if skip_computing:
                async with AsyncSessionLocal() as verify_db:
                    await _validate_core_ready(verify_db, snapshot_run_id, trade_date)
                core_ready = True

        # [CRASH-RESUME-SLICE / P0-B] state_events 与 chip 已下移到 computing_review 之后
        # 的 post-core enhancement 段执行，不再阻塞 History/Review 这一 mandatory 关键路径。
        # 详见下方 "# ---- 步骤 4.9: post-core enhancement（non-blocking）----"。

        # [Slice 4A9] Legacy board aggregation 已退役：AfterClose 不再运行任何
        # 板块聚合 / 发布 / pointer 确认，也不维护 legacy batch 状态。
        # Unified Review 是当前正式板块分析唯一 owner，且 Review 只依赖已发布的
        # stock_core（见下方 computing_review），因此此处不添加任何替代 Board 阶段。
        # 兼容性：保留 metadata 键 aggregation_status，如实置为 "skipped"（该阶段已退役/不再执行）。
        _aggregation_status = "skipped"

        # ---- 步骤 4.4: computing_history（First Pyramid History 自动生产 + exact-T readiness gate） ----
        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01] History(T) 在 Review 之后生产：
        # Review(T) = Core(T) + History(<T)，exact-T History(T) 不再是 Review 前置。
        # 此步骤负责：
        #  1) 解析/创建当前 canonical FirstPyramidHistoryRun（producer resolver，幂等）；
        #  2) 调用既有 canonical advancement owner「advance_history_to_trade_date」
        #     把数据集推进到 trade_date（1x write amplification，幂等）；
        #  3) 校验该 run 对 trade_date 是否 exact-T ready（validate_canonical_history_run_readiness）。
        # readiness 由既有的 Review readiness 服务判定，返回 dict，status=='ok' 才是 ready。
        # [CHANGE-20260826-001 Slice 1 CORRECTION] History(T) 不再挡在 Review 前面。
        # Review(T) = Core(T) + History(<T)。computing_history（_make_history_step →
        # advance_history_to_trade_date 的第二次计算）改到 computing_review 之后执行：
        # 从 stock_core published 到 Review compute started 之间不再有任何 History(T)
        # producer / recompute（KPI-4）。_history_run_id / _history_ready 先置空，
        # Review 不再依赖它们（H2 硬门控已移除）。
        _history_run_id: uuid.UUID | None = None
        _history_ready: bool = False

        # ---- 步骤 4.5: computing_review（复盘计算 + 发布） — 先于 History 执行 ----
        logger.info(
            "[BOUNDARY-P9] before computing_review job=%s trade=%s snap=%s pid=%s",
            str(job_run_id), trade_date, snapshot_run_id, os.getpid(),
        )
        # [AC-02] 复盘业务体抽为 _execute_review_step，由统一执行器包装。
        # 软失败（gate_blocked/计算失败）不阻断主流程，仅标记 _review_failed
        # 收 partial_success；step summary 如实反映业务状态，失败不推进检查点。
        # [SLICE-01 H2] 传入 history_run_id：若 computing_history 未就绪则为 None，
        # _execute_review_step 据此硬门控 gate_blocked（History 缺失 → Review 不运行）。
        _review_result, _review_step_summary = await execute_orchestrator_step(
            "computing_review",
            lambda: _execute_review_step(
                job_run_id=job_run_id,
                trade_date=trade_date,
                snapshot_run_id=snapshot_run_id,
                worker_id=worker_id,
                skip_review=skip_review,
                history_run_id=_history_run_id,
                history_ready=_history_ready,
                stock_core_published=_stock_core_published,
            ),
            timeout_seconds=_step_timeout("computing_review"),
            optional=True,
            heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
            progress=_make_step_progress_callback(job_run_id, worker_id),
            cancellation_check=_make_step_cancellation_check(job_run_id),
        )

        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] Review 终态短路（KPI-11）。
        # Review step summary 为 cancelled / interrupted 时，必须先终态收尾再停止：
        # 不得继续 History / DSA compatibility / state_events / chip 任何后续副作用写入。
        # 与 History 终止短路同构（resolve_terminal_run_status + 最终状态落库 +
        # AfterCloseCancelledError → 外层 as-terminal 返回，不覆写 failed）。
        _review_step_status_for_short_circuit = (
            _review_step_summary.get("status")
        ) if isinstance(_review_step_summary, dict) else None
        if _is_terminal_review_short_circuit(_review_step_status_for_short_circuit):
            _terminal_status = resolve_terminal_run_status(
                _review_step_status_for_short_circuit
            )
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=_terminal_status,
                    message=(
                        f"盘后编排在复盘计算阶段被{_terminal_status.value}，"
                        f"停止后续步骤"
                    ),
                    dsa_run_id=dsa_run_id,
                    payload={
                        "stock_core_published": _stock_core_published,
                        "review_step_status": _review_step_status_for_short_circuit,
                        "terminal_short_circuit": True,
                    },
                )
                job_run.status = _terminal_status.value
                job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                await _update_heartbeat_and_step(db, job_run, None, worker_id)
                await db.commit()
            logger.warning(
                "[AfterClose][CORRECTION-04] computing_review 终止短路: "
                "job=%s status=%s（History/DSA/events/chip 均不执行）",
                str(job_run_id), _terminal_status.value,
            )
            raise AfterCloseCancelledError(_terminal_status)

        # ---- 步骤 4.6: computing_history（历史状态推进）— Review 之后执行 ----
        # [CHANGE-20260826-001 Slice 1 CORRECTION] 仅在此处（Review 已 compute/publish 后）
        # 才运行 History(T) producer（_make_history_step → advance_history_to_trade_date）。
        # 从 stock_core published 到 Review compute started 之间 0 次 recompute（KPI-4）。
        # Legacy v2 函数保留给 backfill，不删除。
        logger.info(
            "[BOUNDARY-PH] before computing_history job=%s trade=%s pid=%s",
            str(job_run_id), trade_date, os.getpid(),
        )
        # History 启动前写入 orchestrator 当前阶段（真实合同：_update_orchestrator_status
        # 需要 db + job_run）。使 admin 页面「当前阶段」不再停留在 computing_review。
        async with AsyncSessionLocal() as db:
            _hr_job = await _get_job_run_or_raise(db, job_run_id)
            await _update_orchestrator_status(
                db=db,
                job_run=_hr_job,
                status=AfterCloseRunStatus.COMPUTING_HISTORY,
                message="开始历史状态推进（First Pyramid History 自动生产 + exact-T readiness）",
                dsa_run_id=dsa_run_id,
                payload={"stock_core_published": _stock_core_published},
            )
            await db.commit()

        _history_result, _history_step_summary = await execute_orchestrator_step(
            "computing_history",
            _make_history_step(
                job_run_id=job_run_id,
                trade_date=trade_date,
                worker_id=worker_id,
                skip_history=skip_history,
            ),
            timeout_seconds=_step_timeout("computing_history"),  # None: 无 absolute timeout
            optional=True,
            heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
            # computing_history 用专属 executor progress 回调（MERGE 语义）：heartbeat 只更新
            # executor 拥有字段，保留 advance 写入的业务进度，两者不再互相覆盖。
            progress=_make_history_executor_progress(job_run_id, worker_id),
            cancellation_check=_make_step_cancellation_check(job_run_id),
        )
        if isinstance(_history_result, dict):
            _history_run_id = _history_result.get("history_run_id")
            _history_ready = bool(_history_result.get("ready"))
        _history_status = _history_step_summary.get("status")
        logger.info(
            "[AfterClose] computing_history 完成: ready=%s, run_id=%s, step=%s",
            _history_ready, _history_run_id, _history_status,
        )

        # 业务结果真实性（修正 executor 的 succeeded 覆盖）：History not_ready 是业务失败，
        # 不是 step 成功。必须在 wrapper 返回后据此修正 _history_step_summary 并持久化。
        if (not skip_history) and (not _history_ready) and _history_status != "cancelled" \
                and _history_status != "interrupted":
            _history_step_summary["status"] = "failed"
            _history_step_summary["error_code"] = "HISTORY_NOT_READY_T"
            await _make_step_progress_callback(job_run_id, worker_id)(
                dict(_history_step_summary)
            )

        # History 终止短路：管理员 cancel / worker interrupted 时必须立即收尾。
        if _history_status in ("cancelled", "interrupted"):
            _terminal_status = resolve_terminal_run_status(_history_status)
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=_terminal_status,
                    message=(
                        f"盘后编排在历史状态推进阶段被{_terminal_status.value}，"
                        f"停止后续步骤"
                    ),
                    dsa_run_id=dsa_run_id,
                    payload={
                        "stock_core_published": _stock_core_published,
                        "history_ready": _history_ready,
                        "terminal_short_circuit": True,
                    },
                )
                job_run.status = _terminal_status.value
                job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                await _update_heartbeat_and_step(db, job_run, None, worker_id)
                await db.commit()
            logger.warning(
                "[AfterClose][H2] computing_history 终止短路: job=%s status=%s",
                str(job_run_id), _terminal_status.value,
            )
            raise AfterCloseCancelledError(_terminal_status)

        # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-04] post-core OPTIONAL DSA 兼容性投影
        # —— 统一执行器顶层步骤（与 refreshing_daily / computing_review / enqueue_chip_job 同级）。
        # Core 已就绪（core_ready）→ Review → History 后执行；经 execute_orchestrator_step(
        # optional=True) 由统一执行器拥有 status/timeout/heartbeat/cancel/step summary/
        # optional failure，不再维护第二套手写状态机。其失败不得标记 Core failed、不得阻断
        # Review；summary["dsa_compatibility"]（含 step 键的 executor 标准结构）落库后进入
        # optional_failures → parent partial_success。
        if core_ready and snapshot_run_id is not None:
            async def _dsa_compatibility_operation() -> dict[str, Any]:
                return await _run_dsa_compatibility_projection(
                    job_run_id=job_run_id,
                    worker_id=worker_id,
                    lease_epoch=lease_epoch,
                    trade_date=trade_date,
                    snapshot_run_id=snapshot_run_id,
                    dsa_run_id=dsa_run_id,
                    instrument_ids=cached_instrument_ids or [],
                )

            _dsa_result, _dsa_step_summary = await execute_orchestrator_step(
                "dsa_compatibility",
                _dsa_compatibility_operation,
                timeout_seconds=_step_timeout("dsa_compatibility"),
                optional=True,
                heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
                progress=_make_step_progress_callback(job_run_id, worker_id),
                cancellation_check=_make_step_cancellation_check(job_run_id),
            )
            # 规范化 summary 元数据（不改 status —— status owner 仍是统一执行器）：
            # step_summary["dsa_compatibility"] 必须存在且带 optional=True，
            # 失败时才能被 optional_failures 读取并使 parent 成为 partial_success。
            _dsa_step_summary.setdefault("step", "dsa_compatibility")
            _dsa_step_summary.setdefault("optional", True)
            _dsa_compatibility_status = _dsa_step_summary.get("status", "not_run")
            await _persist_step_summary(job_run_id, dict(_dsa_step_summary))
            logger.info(
                "[AfterClose][CORRECTION-04] dsa_compatibility 完成: "
                "step_status=%s, result=%s",
                _dsa_compatibility_status, _dsa_result,
            )

        # checkpoint：仅 history_ready=True 时才把 last_completed_step 推进为
        # computing_history（记录真实历史事实）。
        if _history_ready:
            async with AsyncSessionLocal() as db:
                _ck_job = await _get_job_run_or_raise(db, job_run_id)
                await _update_heartbeat_and_step(
                    db, _ck_job,
                    AfterCloseRunStatus.COMPUTING_HISTORY.value,
                    worker_id,
                )
                await db.commit()
        # 解包 review 业务状态（供主任务 partial_success 判定与 metadata 写入）
        _review_status = (
            _review_result.get("status") if isinstance(_review_result, dict) else "skipped"
        )
        # [P0-1 2026-08-04] 失败判定必须同时考虑 review 业务结果 _和_ 执行器 step summary：
        # 执行器若 timed_out/unavailable/interrupted/cancelled 会返回 result=None 或
        # failed=False，但 step_summary.status 已如实记录。仅看业务结果会把超时误判为成功。
        _review_step_status = _review_step_summary.get("status")
        _review_failed = bool(
            (isinstance(_review_result, dict) and _review_result.get("failed"))
            or _review_step_status
            in ("failed", "timed_out", "unavailable", "interrupted", "cancelled")
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

        # ---- 步骤 4.8.5: 取消/中断终态短路 ----
        # [AC-CANCEL-01 2026-08-04] Review step 为 cancelled/interrupted 时，
        # 不得覆盖总任务终态（[AUD-08] chip 已在步骤 4.6 入队，不受此短路影响）：
        # - cancelled：管理员主动取消，保持 cancelled，交由用户/调度不再恢复；
        # - interrupted：旧 Worker 被接管，保持 interrupted，交由 reconcile/restart。
        # 两者均不应降级为 partial_success 而继续执行后续步骤。
        _review_step_status = _review_step_summary.get("status")
        if _is_terminal_review_short_circuit(_review_step_status):
            # [AC-TERMINAL-01 P0#1] 裸字符串必须转为 AfterCloseRunStatus 枚举，
            # 否则 _update_orchestrator_status 内部 status.value 抛 AttributeError。
            _terminal_status = resolve_terminal_run_status(_review_step_status)
            async with AsyncSessionLocal() as db:
                job_run = await _get_job_run_or_raise(db, job_run_id)
                await _update_orchestrator_status(
                    db=db,
                    job_run=job_run,
                    status=_terminal_status,
                    message=(
                        f"盘后编排在复盘阶段被{_terminal_status.value}，"
                        f"停止后续步骤: review_reason={_review_reason}"
                    ),
                    dsa_run_id=dsa_run_id,
                    payload={
                        "stock_core_published": _stock_core_published,
                        "review_status": _review_status,
                        "review_run_id": (
                            str(_review_run_id) if _review_run_id else None
                        ),
                        "review_reason": _review_reason,
                        "terminal_short_circuit": True,
                    },
                )
                job_run.status = _terminal_status.value
                job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
                # [AC-TERMINAL-01 P0#2] last_completed_step 是"断点恢复检查点"，
                # 只能记录真正完成的流水线步骤。取消/中断不是步骤，写入会导致
                # _COMPLETED_STEP_INDEX 查不到（fallback -1）→ 已完成步骤全部
                # 回退成 pending，且断点恢复从头重跑。
                # 传 None：只刷心跳/租约，保留 publishing 等原检查点。
                await _update_heartbeat_and_step(db, job_run, None, worker_id)
                await db.commit()
            logger.warning(
                "[AfterClose] 复盘阶段终态短路: job_run_id=%s, status=%s, "
                "post-core enhancement（chip/state_events/auction）将于 Review 之后执行"
                "（status=%s）（保留原检查点）",
                job_run_id, _terminal_status.value, _chip_enqueue_status,
            )
            # [AC-TERMINAL-01 P0#3] 抛信号异常，让外层 except 明确区分
            # "取消/中断"与"真实失败"，避免被覆写成 failed。
            raise AfterCloseCancelledError(_terminal_status)

        # ---- 步骤 4.9: post-core enhancement（non-blocking）----
        # [CRASH-RESUME-SLICE / P0-B] 以下 enhancement 输出在 mandatory 关键路径
        # （Core X 计算完成 → Review(T) 计算 → History(T) 推进）
        # 全部完成之后执行。它们失败/超时/crash 都不得阻断 Review(T) 的形成，
        # 只能表达 partial_success / compatibility incomplete / degraded。
        # 这些 enhancement 的 readiness owner 是 CoreRun 显式绑定（snapshot_run_id X），
        # 而非 stock_core publication / published_at。
        # auction_anchor 原始为 normal-publish 专属步骤（skip_publish 恢复路径不重复执行）。

        # [P0-4][AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-02] auction anchor（RETIRED 产品）
        # 旧 AuctionAnchor 产品（structure/chip anchor 模型）已废止（PRD75 §23）；
        # 竞价分析现在是 observation 产品（PRD75），不再由 AfterClose 生成 AuctionAnchorRun。
        # 因此 auction_anchor 永远是显式 skipped，不依赖 stock_core publication 也不读它。
        if False:  # RETIRED: AuctionAnchor 产品已废止，不执行任何 anchor 生成/发布
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
                        heartbeat=_make_step_heartbeat(job_run_id, worker_id, lease_epoch),
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

        # [P1-2 2026-08-07][AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] state events（non-blocking post-core）
        # readiness owner 改为 canonical CORE_READY（CoreRun 真实 status==succeeded），
        # 不再依赖 snapshot_run_id 非空或 stock_core publication。
        # Core succeeded → Review（computing_review 已在上方完成）→ 此处生成 Core X 的事件。
        if core_ready:
            logger.info(
                "[BOUNDARY-P3] before state-events job=%s trade=%s pid=%s",
                str(job_run_id), trade_date, os.getpid(),
            )
            try:
                from app.services.state_event_service import (
                    cleanup_old_events,
                    generate_events_for_run,
                )
                async with AsyncSessionLocal() as event_db:
                    logger.info(
                        "[BOUNDARY-S1] before generate_events_for_run job=%s snap=%s pid=%s",
                        str(job_run_id), snapshot_run_id, os.getpid(),
                    )
                    event_stats = await generate_events_for_run(event_db, snapshot_run_id)
                    logger.info(
                        "[BOUNDARY-S2] after generate_events_for_run job=%s events=%s skipped=%s failed=%s pid=%s",
                        str(job_run_id),
                        event_stats.get("event_count", 0),
                        event_stats.get("skipped_count", 0),
                        event_stats.get("failed_count", 0),
                        os.getpid(),
                    )
                    # 90 天清理（P1-2）：事件生成后执行，失败不阻断主发布
                    logger.info(
                        "[BOUNDARY-S3] before cleanup_old_events job=%s pid=%s",
                        str(job_run_id), os.getpid(),
                    )
                    cleanup_stats = await cleanup_old_events(event_db)
                    logger.info(
                        "[BOUNDARY-S4] after cleanup_old_events job=%s deleted=%s pid=%s",
                        str(job_run_id), cleanup_stats.get("deleted_count", 0), os.getpid(),
                    )
                    logger.info(
                        "[BOUNDARY-S5] before commit job=%s pid=%s",
                        str(job_run_id), os.getpid(),
                    )
                    await event_db.commit()
                    logger.info(
                        "[BOUNDARY-S6] after commit job=%s pid=%s",
                        str(job_run_id), os.getpid(),
                    )
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
                logger.info(
                    "[BOUNDARY-P4] after state-events job=%s count=%s pid=%s",
                    str(job_run_id), event_stats.get("event_count", 0), os.getpid(),
                )
                # [AUDIT-CORRECTION-01 / Blocker 4] 记录成功态，使 enhancement 段
                # 的成功/失败均有 step_summary 条目，统一进入 optional_failures 判定。
                # 使用 _persist_step_summary 落库（避免闭包 free-var 在 except 分支的
                # UnboundLocalError，并确保 terminal 块与 get_after_close_run_status
                # 读取的是同一份 DB step_summary）。
                await _persist_step_summary(job_run_id, {
                    "step": "state_events",
                    "optional": True,
                    "status": "succeeded",
                    "event_count": event_stats.get("event_count", 0),
                    "failed_count": event_stats.get("failed_count", 0),
                })
            except Exception as event_exc:
                logger.warning(
                    "[AfterClose] 状态事件生成失败（不影响主流程）: "
                    "run_id=%s, error=%s",
                    snapshot_run_id, event_exc, exc_info=True,
                )
                # [AUDIT-CORRECTION-01 / Blocker 4] 与 auction/chip 一致：
                # enhancement 失败必须写入 step_summary（optional=True, status=failed），
                # 否则它既不会进入 optional_failures，也不会使 parent 成为 partial_success，
                # 造成「enhancement 失败却被伪装成全成功」的 false-green。
                # 使用 _persist_step_summary 落库（避免闭包 free-var 在 except 分支的
                # UnboundLocalError）。
                await _persist_step_summary(job_run_id, {
                    "step": "state_events",
                    "optional": True,
                    "status": "failed",
                    "error": str(event_exc),
                })

        # [P1-2][AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-03] chip 入队（non-blocking post-core）
        # readiness owner 改为 canonical CORE_READY（CoreRun 真实 status==succeeded），
        # 不再依赖 snapshot_run_id 非空或 stock_core publication。
        # 幂等依据：create_after_close_chip_consensus_job 以
        # (trade_date, core_run_id=snapshot_run_id) 幂等，重复调用返回既有 job（chip_is_new=False）。
        if core_ready:
            try:
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
                logger.info(
                    "[BOUNDARY-P6] after chip-enqueue job=%s chip_status=%s pid=%s",
                    str(job_run_id), _chip_enqueue_status, os.getpid(),
                )
            except Exception as chip_exc:
                _chip_enqueue_status = "failed"
                logger.warning(
                    "[AfterClose] chip 实时计算入队失败（optional，不影响 core）: "
                    "job=%s, error=%s",
                    str(job_run_id), chip_exc, exc_info=True,
                )

        # [AUD-08 2026-08-07][AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01-CORRECTION-02]
        # chip 入队已在上方 post-core enhancement 段执行（readiness owner = snapshot_run_id X，
        # 不再依赖 stock_core 发布），此处不再重复。chip 入队仍是正式步骤、失败仍纳入
        # partial_success 判定（见下方 _optional_failed）。

        # ---- 步骤 5: succeeded ----
        async with AsyncSessionLocal() as db:
            job_run = await _get_job_run_or_raise(db, job_run_id)
            # [AUDIT-CORRECTION-01 / Blocker 4] 单一事实源：terminal 块必须从「已落库」的
            # metadata.step_summary 重新加载，而不是用函数内早期那份 in-memory 副本
            # （review/auction_anchor/state_events/chip 等 enhancement 摘要只经
            # _persist_step_summary 写入 DB，不会回写 in-memory 副本）。否则 terminal
            # 计算的 optional_failures / partial_success 会与 DB 中 step_summary 不一致
            # （false-green）。
            step_summary = dict(_parse_metadata(job_run).get("step_summary") or {})
            # published_run 可能为 None（断点恢复跳过 publishing 时）
            published_at_str = (
                published_run.published_at.isoformat()
                if published_run is not None and published_run.published_at
                else None
            )
            # [AUDIT-CORRECTION-01 / Blocker 4] 单一 owner 原则：optional 汇总的唯一事实源
            # 是最终 step_summary（review / auction_anchor / state_events / chip 均已写入）。
            # 所有 enhancement 在 Review 之后才执行，因此必须在本终端块一次性从最终
            # step_summary 生成 optional_failures / partial_success / final_status，
            # 禁止再用早期（publishing 阶段）的 stale 值或多个分散的 _*_status 变量各算一遍。
            # 这样保证 job_run.status、meta.partial_success、meta.optional_failures 三者
            # 必然一致（同源同刻）。
            optional_failures = [
                name
                for name, item in step_summary.items()
                if isinstance(item, dict)
                and item.get("optional")
                and item.get("status") in {"failed", "unavailable", "timed_out", "interrupted"}
            ]
            # stock_core 被 superseded（pointer 指向其他 run）也视为部分成功。
            _optional_failed = bool(optional_failures) or _stock_core_superseded
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
                    # [SLICE-01 H2] First Pyramid History exact-T readiness 闭环字段
                    "history_run_id": str(_history_run_id) if _history_run_id else None,
                    "history_ready": _history_ready,
                },
                # [AUDIT-CORRECTION-01 / Blocker 4] 单一 owner：partial_success 与
                # optional_failures 由本终端块从最终 step_summary 一次性生成，并经 extra
                # 写回 metadata_json，确保与 job_run.status（同源）最终一致。
                # [AUDIT-CORRECTION-03 / Reviewer #2] 同时将最终完整 step_summary 一并
                # 落库，保证 metadata.step_summary / optional_failures / partial_success /
                # job_run.status 四者全部来自同一最终事实集合（step_summary 是后续
                # reconcile / debug / resume / 事故调查的正式运行证据，不得停留在早先版本）。
                extra={
                    "step_summary": step_summary,
                    "partial_success": bool(optional_failures),
                    "optional_failures": optional_failures,
                },
            )
            job_run.status = final_status.value
            job_run.finished_at = datetime.now(ZoneInfo("Asia/Shanghai"))
            # [CHECKPOINT-SEMANTICS-01] partial_success 是运行结果状态，
            # 不是 pipeline checkpoint。与 INTERRUPTED/CANCELLED 一致：
            # 传 None 保留当前 last_completed_step，仅刷新心跳/租约。
            _step = (
                None
                if final_status == AfterCloseRunStatus.PARTIAL_SUCCESS
                else final_status.value
            )
            await _update_heartbeat_and_step(
                db, job_run, _step, worker_id,
            )
            await db.commit()

        logger.info(
            "[AfterClose] 盘后编排结束: job_run_id=%s, dsa_run_id=%s, status=%s, "
            "chip_enqueue_status=%s",
            job_run_id, dsa_run_id, final_status.value, _chip_enqueue_status,
        )

    except AfterCloseCancelledError as cancel_exc:
        # [AC-TERMINAL-01 P0#3] 取消/中断不是失败：终态已由短路块写入并 commit，
        # 这里绝不能再覆写成 failed，否则管理员取消会显示为"任务失败"。
        logger.info(
            "[AfterClose] 盘后编排以终态结束（非失败）: job_run_id=%s, status=%s",
            job_run_id, cancel_exc.terminal_status.value,
        )
        return

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
        "checked_trade_date": None,
    }
    meta = _parse_metadata(job_run)
    trade_date_raw = meta.get("trade_date")
    if not trade_date_raw:
        return artifacts
    # [CRASH-RESUME-SLICE / P1-B] trade_date 必须显式解析为 datetime.date 再绑定，
    # 不能把 "2026-08-25" 字符串直接交给 asyncpg 的 date 占位符，
    # 否则会触发 asyncpg DataError（字符串无法按 date 合同绑定）。
    try:
        trade_date_obj = date.fromisoformat(str(trade_date_raw))
    except ValueError:
        artifacts["inspect_error"] = f"invalid trade_date: {trade_date_raw!r}"
        return artifacts
    artifacts["checked_trade_date"] = trade_date_obj.isoformat()
    try:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT publication_kind, data_run_id
                    FROM factor_publications
                    WHERE trade_date = :trade_date
                      AND scope_type = 'market'
                      AND publication_kind = 'stock_core'
                    """
                ),
                {"trade_date": trade_date_obj},
            )
        ).fetchall()
        for kind, data_run_id in rows:
            # [Slice 4A9] legacy board aggregation 已退役，AfterClose 不再核验
            # market_aggregation pointer；reconcile 只关注 stock_core 产物。
            if kind == "stock_core":
                artifacts["stock_core_published"] = True
                artifacts["stock_core_data_run_id"] = str(data_run_id)
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


# =============================================================================
# [RECOVERY-CHECKPOINT-01] Checkpoint Reconciliation from Durable Artifacts
# =============================================================================
_CHECKPOINT_ORDER: dict[str, int] = {
    "refreshing_daily": 0,
    "syncing_boards": 1,
    "computing_features": 2,
    # [AFTERCLOSE-DIRECT-CORE-TO-REVIEW-01 PHASE-A] 真实 DAG 已收敛为
    # features → review → history（publishing / stock_core 发布已旁路，不再是真实步骤）。
    "computing_review": 3,
    "computing_history": 4,
    "succeeded": 5,
    # legacy token：旧 DAG 中 last_completed_step 可能为 "publishing"（stock_core 发布步骤）。
    # 当前正文不再执行 publishing 步骤，保留 rank 仅供 reconcile 兼容历史 run 的
    # last_completed_step="publishing"（_CHECKPOINT_ORDER.get 必须非 None）；
    # rank 置于 succeeded 之后，确保其不落入任何真实 stage 的 pre_stages 区间，
    # 不会污染当前 DAG 的 restart/resume skip 计算（KPI-A1/A7/A8）。
    "publishing": 99,
}

# [REPROCESS-OWNER-CLOSURE-01 CORRECTION-01] 断点恢复映射：last_completed_step → 已完成 stage 集合。
# 与 _CHECKPOINT_ORDER 同为 module-level 单一真相源，被 execute_after_close_run 与
# _resolve_execution_completed_steps 共同使用；禁止在 test 中复制等价映射。
_COMPLETED_STEPS: dict[str | None, set[str]] = {
    None: set(),
    "queued": set(),
    "refreshing_daily": {"refreshing_daily"},
    "syncing_boards": {"refreshing_daily", "syncing_boards"},
    # [Phase 5] 4 步收敛为 computing_features
    "computing_features": {
        "refreshing_daily", "syncing_boards", "computing_features",
    },
    # [PHASE-A] legacy publishing token：历史 run 的 last_completed_step 可能为 publishing；
    # 当前正文不再执行 publishing 步骤，保留此映射供兼容（Contract D / reconcile）。
    # 不含 computing_review / computing_history（publishing token 不得污染当前语义）。
    "publishing": {
        "refreshing_daily", "syncing_boards", "computing_features",
        "publishing",
    },
    # [PHASE-A] computing_review 断点恢复：Review 成功、History 尚未执行。
    # 严禁包含 computing_history —— 否则 resume 会误判 skip_history=True，History 永不 retry
    # （违反 KPI-A2/A4）。也不包含 legacy publishing（token 不得污染当前语义）。
    "computing_review": {
        "refreshing_daily", "syncing_boards", "computing_features",
        "computing_review",
    },
    # [PHASE-A] computing_history 断点恢复：真实 DAG 为 features → review → history，
    # History 完成即 Review+History 后置链整体完成（review 必在 history 之前）。
    # 不含 legacy publishing。
    "computing_history": {
        "refreshing_daily", "syncing_boards", "computing_features",
        "computing_review", "computing_history",
    },
    "succeeded": {
        "refreshing_daily", "syncing_boards", "computing_features",
        "computing_review", "computing_history", "succeeded",
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


def _resolve_execution_completed_steps(
    last_completed_step: str | None,
    mainchain_stage: str | None,
) -> set[str]:
    """[REPROCESS-OWNER-CLOSURE-01 CORRECTION-01] 单一真相源：解析盘后 run 的已完成 stage 集合。

    两种启动合同的合并：
      - checkpoint resume：last_completed_step（旧合同，仍可独立使用）
      - restart 正式起点：mainchain_stage（P0-1/P0-2 唯一正式起点合同；取代伪造 last_completed_step）

    mainchain_stage 合法性：
      - mainchain_stage is None        → 不引入任何预完成 stage（正常 initial run / 普通 resume）
      - mainchain_stage in _CHECKPOINT_ORDER
                                     → 合并其之前所有 pre-stage 为已完成（跳过 refreshing_daily 等）
      - mainchain_stage NOT in _CHECKPOINT_ORDER
                                     → fail closed：corrupt/typo restart metadata 不得静默退化成 full run，
                                       显式抛出 ValueError，禁止重跑 refreshing_daily / 整条链。

    该函数被 execute_after_close_run 与契约测试共同调用，禁止在 test 中复制等价逻辑。
    """
    completed: set[str] = set(_COMPLETED_STEPS.get(last_completed_step, set()))
    if mainchain_stage is not None:
        if mainchain_stage not in _CHECKPOINT_ORDER:
            raise ValueError(
                f"invalid mainchain_stage={mainchain_stage!r}: "
                f"不在正式 _CHECKPOINT_ORDER，禁止作为 restart 起点（corrupt/typo metadata）。"
            )
        stage_rank = _CHECKPOINT_ORDER[mainchain_stage]
        pre_stages = {
            s for s, r in _CHECKPOINT_ORDER.items() if r < stage_rank
        }
        completed |= pre_stages
    return completed


async def reconcile_after_close_checkpoint_from_artifacts(
    db: AsyncSession,
    *,
    job_run_id: str,
    target_step: str = "computing_features",
) -> dict:
    """[RECOVERY-CHECKPOINT-01] 从 durable artifacts + step_summary 证据对账 checkpoint。

    仅用于 failed/interrupted 盘后 run：当 step_summary 记录 computing_features 已成功、
    DSA + snapshot durable artifacts 完整但 orchestrator 在写 checkpoint 前崩溃时，
    将 last_completed_step 从较早 checkpoint 推进到 computing_features。

    Required evidence（缺一 REFUSE）：
    - step_summary.computing_features.status == "succeeded"
    - snapshot run: published_at IS NULL, expected_count 正确
    - snapshot run-items: 全部 terminal acceptable, instrument set 完整
    - StockFeatureSnapshot: count == expected_count, instrument set 与 items 一致
    - DSA run-items: 全部 terminal acceptable, skip reasons ∈ allowlist
    - DSA succeeded instrument set ⊆ snapshot instrument set
    - formal stock_core pointer 尚未指向该 snapshot

    不执行: compute / publish / pointer switch / board / Review。
    """
    from app.models.stock_feature_snapshot import StockFeatureSnapshot
    from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
    from app.models.stock_feature_snapshot_run_item import StockFeatureSnapshotRunItem
    from app.models.strategy_run import StrategyResult, StrategyRun, StrategyRunItem

    # ————————————————————————————————————————
    # 1. job_run identity
    # ————————————————————————————————————————
    job_run = await _get_job_run_or_raise(db, uuid.UUID(job_run_id))
    meta = _parse_metadata(job_run)
    trade_date_raw = meta.get("trade_date")
    if not trade_date_raw:
        return {"ok": False, "refuse": "metadata 缺少 trade_date"}
    trade_date = date.fromisoformat(trade_date_raw)
    dsa_run_id = meta.get("dsa_run_id")
    snap_id = meta.get("feature_snapshot_run_id")

    # ————————————————————————————————————————
    # 2. current checkpoint 不得倒退
    # ————————————————————————————————————————
    current_step = meta.get("last_completed_step", "")
    if current_step == target_step:
        return {"ok": True, "action": "noop", "reason": f"checkpoint already {target_step}"}
    current_order = _CHECKPOINT_ORDER.get(current_step)
    target_order = _CHECKPOINT_ORDER.get(target_step)
    if current_order is None:
        return {"ok": False, "refuse": f"unknown current checkpoint: {current_step}"}
    if target_order is None:
        return {"ok": False, "refuse": f"unknown target checkpoint: {target_step}"}
    if current_order > target_order:
        return {"ok": False, "refuse": f"checkpoint {current_step} already later than {target_step}"}

    # ————————————————————————————————————————
    # 3. step_summary: computing_features succeeded
    # ————————————————————————————————————————
    step_summary = dict(meta.get("step_summary") or {})
    comp_step = step_summary.get("computing_features", {})
    if comp_step.get("status") != "succeeded":
        return {"ok": False, "refuse": "step_summary.computing_features != succeeded"}

    # ————————————————————————————————————————
    # 4. snapshot run + items + artifacts
    # ————————————————————————————————————————
    if not snap_id:
        return {"ok": False, "refuse": "metadata 缺少 feature_snapshot_run_id"}
    snap_run = await db.get(StockFeatureSnapshotRun, uuid.UUID(snap_id))
    if snap_run is None:
        return {"ok": False, "refuse": f"snapshot run not found: {snap_id}"}
    if snap_run.published_at is not None:
        return {"ok": False, "refuse": "snapshot run already published"}
    if snap_run.expected_count != 5293:
        return {"ok": False, "refuse": f"snapshot expected_count={snap_run.expected_count} != 5293"}

    # snapshot run-items
    items_stmt = select(
        StockFeatureSnapshotRunItem.status,
        func.count().label("cnt"),
    ).where(
        StockFeatureSnapshotRunItem.snapshot_run_id == uuid.UUID(snap_id),
    ).group_by(StockFeatureSnapshotRunItem.status)
    item_rows = (await db.execute(items_stmt)).all()
    item_map = {row.status: row.cnt for row in item_rows}
    if item_map.get("succeeded", 0) != 5293:
        return {"ok": False, "refuse": f"snapshot items succeeded={item_map.get('succeeded',0)} != 5293"}
    if item_map.get("failed", 0) != 0:
        return {"ok": False, "refuse": f"snapshot items failed={item_map['failed']} > 0"}
    if item_map.get("pending", 0) + item_map.get("running", 0) != 0:
        return {"ok": False, "refuse": "snapshot items still pending/running"}

    # StockFeatureSnapshot count
    artifact_count = (
        await db.execute(
            select(func.count()).where(
                StockFeatureSnapshot.source_run_id == uuid.UUID(snap_id),
            )
        )
    ).scalar_one()
    if artifact_count != 5293:
        return {"ok": False, "refuse": f"StockFeatureSnapshot count={artifact_count} != 5293"}

    # instrument set equality: snapshot items ↔ StockFeatureSnapshot
    snap_instrument_ids = {
        row[0]
        for row in (
            await db.execute(
                select(StockFeatureSnapshotRunItem.instrument_id).where(
                    StockFeatureSnapshotRunItem.snapshot_run_id == uuid.UUID(snap_id),
                    StockFeatureSnapshotRunItem.status == "succeeded",
                )
            )
        ).all()
    }
    artifact_instrument_ids = {
        row[0]
        for row in (
            await db.execute(
                select(StockFeatureSnapshot.instrument_id).where(
                    StockFeatureSnapshot.source_run_id == uuid.UUID(snap_id),
                )
            )
        ).all()
    }
    if snap_instrument_ids != artifact_instrument_ids:
        return {"ok": False, "refuse": "snapshot item instrument set != StockFeatureSnapshot instrument set"}

    # ————————————————————————————————————————
    # 5. DSA run-items + results
    # ————————————————————————————————————————
    if not dsa_run_id:
        return {"ok": False, "refuse": "metadata 缺少 dsa_run_id"}
    dsa_run = await db.get(StrategyRun, uuid.UUID(dsa_run_id))
    if dsa_run is None:
        return {"ok": False, "refuse": f"DSA run not found: {dsa_run_id}"}

    # DSA item aggregation
    dsa_items_stmt = select(
        StrategyRunItem.status,
        func.count().label("cnt"),
    ).where(
        StrategyRunItem.run_id == uuid.UUID(dsa_run_id),
    ).group_by(StrategyRunItem.status)
    dsa_item_rows = (await db.execute(dsa_items_stmt)).all()
    dsa_item_map = {row.status: row.cnt for row in dsa_item_rows}
    if dsa_item_map.get("succeeded", 0) != 5283:
        return {"ok": False, "refuse": f"DSA items succeeded={dsa_item_map.get('succeeded',0)} != 5283"}
    if dsa_item_map.get("failed", 0) != 0:
        return {"ok": False, "refuse": f"DSA items failed={dsa_item_map['failed']} > 0"}
    if dsa_item_map.get("pending", 0) + dsa_item_map.get("running", 0) != 0:
        return {"ok": False, "refuse": "DSA items still pending/running"}

    # skip reason allowlist — 复用 canonical StrategyBatchService._SKIPPED_REASON_ALLOWLIST
    from app.services.strategy_batch_service import StrategyBatchService
    canonical_allowlist = StrategyBatchService._SKIPPED_REASON_ALLOWLIST
    skip_stmt = select(StrategyRunItem.reason_code, func.count()).where(
        StrategyRunItem.run_id == uuid.UUID(dsa_run_id),
        StrategyRunItem.status == "skipped",
    ).group_by(StrategyRunItem.reason_code)
    skip_rows = (await db.execute(skip_stmt)).all()
    for reason, _ in skip_rows:
        if reason and reason not in canonical_allowlist:
            return {"ok": False, "refuse": f"DSA skip reason not in allowlist: {reason}"}

    # StrategyResult count
    result_count = (
        await db.execute(
            select(func.count()).where(
                StrategyResult.run_id == uuid.UUID(dsa_run_id),
            )
        )
    ).scalar_one()
    if result_count != 5283:
        return {"ok": False, "refuse": f"StrategyResult count={result_count} != 5283"}

    # DSA succeeded ⊆ snapshot succeeded
    dsa_succeeded_ids = {
        row[0]
        for row in (
            await db.execute(
                select(StrategyRunItem.instrument_id).where(
                    StrategyRunItem.run_id == uuid.UUID(dsa_run_id),
                    StrategyRunItem.status == "succeeded",
                )
            )
        ).all()
    }
    if not dsa_succeeded_ids.issubset(snap_instrument_ids):
        return {"ok": False, "refuse": "DSA succeeded instrument set not subset of snapshot set"}

    # ————————————————————————————————————————
    # 6. formal pointer not yet pointing to this snapshot
    # ————————————————————————————————————————
    from app.services.factor_publication_service import (
        PUBLICATION_KIND_STOCK_CORE,
        get_publication,
    )

    pointer = await get_publication(
        db,
        scope_type="market",
        scope_key="market",
        trade_date=trade_date,
        publication_kind=PUBLICATION_KIND_STOCK_CORE,
    )
    if pointer is not None and pointer.data_run_id == snap_run.id:
        return {"ok": False, "refuse": "formal pointer already points to this snapshot run"}

    # ————————————————————————————————————————
    # 7. ALL EVIDENCE PASS — advance checkpoint
    # ————————————————————————————————————————
    meta["last_completed_step"] = target_step
    job_run.metadata_json = json.dumps(meta, ensure_ascii=False)
    await db.flush()

    logger.info(
        "[AfterClose] reconcile checkpoint: job_run_id=%s, %s → %s",
        job_run_id, current_step, target_step,
    )
    return {
        "ok": True,
        "action": "advanced",
        "before": current_step,
        "after": target_step,
    }


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
