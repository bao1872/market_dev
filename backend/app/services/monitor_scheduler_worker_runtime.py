"""Monitor scheduler worker main lifecycle, separate from the process composition root.

This module owns the Monitor Scheduler main polling lifecycle AND the Monitor-specific
session helpers (moved from the composition root in PANJI-GOV-W4A / W4B1):

* Trading-session classification (morning / afternoon / non-session) is owned by this
  module's :func:`_get_monitor_session`; the boundary rules are preserved verbatim.
* Monitor session job-run acquisition is an EXCLUSIVE-OWNER model (C2C): the runtime
  acquires a per-session ``SchedulerJobRun`` via the injected ``create_job_run``; if it
  returns ``None`` the session is already owned by another process, so this runtime
  SKIPS the cycle and does NOT ``SELECT``-reuse the existing row. No second owner, no
  cross-process ownership theft.
* Fenced session lifecycle (C2C): a ``FencedJobToken`` is built from the acquired
  job_run ownership fields; a single ``FencedJobHeartbeat(30s)`` keeps the lease alive
  for the whole session; per-cycle progress is written only through
  ``update_owned_job_run_progress`` (fenced: ``last_cycle_at`` + counts + metadata);
  the session terminal is written only through ``finalize_job_run`` carrying the token.
  A lost lease stops the session without writing any terminal; an unexpected runtime
  exception is swallowed and, if still owned, written as a fenced ``failed`` terminal;
  graceful shutdown only stops the heartbeat (the running row is left to lease-expiry
  recovery).
* Startup / error Feishu notification lives in the dedicated
  :mod:`app.services.monitor_status_notifier` module, imported at module top; the
  runtime only *calls* it and does NOT own the Redis idempotency / channel-query / DTO /
  adapter logic.
* The generic ``SchedulerJobRun`` state rules (finish / recover) are injected by the
  composition root (:mod:`app.worker`) and must not be duplicated here.
* Monitor cycle execution and stale-evaluation recovery live in
  :class:`app.services.monitor_batch_service.MonitorBatchService`.
* The monotonic clock used for cycle latency is injected (``monotonic_clock``) so the
  composition root keeps ownership of the time source.

The top-level :mod:`app.worker` module supplies the session factory, the legacy process
heartbeat loop, shared shutdown signal, the canonical helpers, and business owners.
This is a pure structural + fencing extraction (PANJI-GOV-W4A, W4B1, W4B2, C2C); no
trading-session boundary / reentrancy guard / transaction / exception / sleep-cadence /
notify timing semantics changed beyond the C2C exclusive-ownership + fenced lifecycle.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from datetime import datetime
from datetime import time as time_cls
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select

from app.config import get_settings
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.scheduler_job_run import SchedulerJobRun
from app.services.fenced_job_run_service import (
    FencedJobHeartbeat,
    FencedJobToken,
    JobLeaseLostError,
    finalize_job_run,
    update_owned_job_run_progress,
)
from app.services.monitor_batch_service import MonitorBatchService
from app.services.monitor_status_notifier import notify_monitor_status

SessionFactory = Callable[[], Any]
HeartbeatLoop = Callable[[str], Coroutine[Any, Any, None]]
ShutdownProbe = Callable[[], bool]
RecoverStaleJobRuns = Callable[[Any], Coroutine[Any, Any, int]]
CreateJobRun = Callable[..., Coroutine[Any, Any, SchedulerJobRun | None]]
FinishJobRun = Callable[..., Coroutine[Any, Any, None]]
MonotonicClock = Callable[[], float]


def _get_monitor_session(
    now_cst: Any,
) -> tuple[str, time_cls, time_cls] | None:
    """根据当前上海时间返回盘中交易时段标签与起止时间。

    Returns:
        (label, start_time, end_time) 或 None（非交易时段）

    边界（逐字保持）：
        - 09:30 包含，11:30 不包含
        - 13:00 包含，15:00 不包含
    """
    current_time = now_cst.time()
    morning_start = time_cls(9, 30)
    morning_end = time_cls(11, 30)
    afternoon_start = time_cls(13, 0)
    afternoon_end = time_cls(15, 0)

    if morning_start <= current_time < morning_end:
        return ("morning", morning_start, morning_end)
    if afternoon_start <= current_time < afternoon_end:
        return ("afternoon", afternoon_start, afternoon_end)
    return None


async def _find_or_create_monitor_session_job_run(
    db: Any,
    now_cst: Any,
    business_date: str,
    session_label: str,
    *,
    create_job_run: CreateJobRun,
) -> SchedulerJobRun | None:
    """Acquire the current trading-session's monitor_scheduler job_run (exclusive owner, C2C).

    Based on run_key=monitor_scheduler:{business_date}:{session_label} unique index.
    Returns the created ``SchedulerJobRun``, or ``None`` if the session is already owned
    by another process (the unique index / advisory lock prevented a second owner). The
    caller must then SKIP the cycle — it must NOT ``SELECT`` the existing row and write
    to it (that would be cross-process ownership theft).

    Thin wrapper delegating to the injected canonical ``create_job_run``; does not
    INSERT, commit, recover, or SELECT-reuse on its own.
    """
    run_key = f"monitor_scheduler:{business_date}:{session_label}"
    return await create_job_run(
        db,
        "monitor_scheduler",
        business_date,
        lease_seconds=120,
        metadata={"session_label": session_label},
        run_key=run_key,
    )


async def _finalize_monitor_session(
    token: FencedJobToken,
    heartbeat: FencedJobHeartbeat | None,
    succeeded: int,
    failed: int,
    *,
    logger: logging.Logger,
) -> None:
    """Write the session terminal (``succeeded``) through the fenced primitive.

    Monitor session semantics (C2C): a few failed cycles never upgrade the session to
    ``partial_failed`` — the session terminal stays ``succeeded`` (Do Not introduce
    ``partial_failed`` for Monitor). A lost lease before finalize is swallowed (no
    terminal). The heartbeat is always stopped.
    """
    try:
        await finalize_job_run(
            token,
            status="succeeded",
            metadata_updates={},
            total_count=succeeded + failed,
            succeeded_count=succeeded,
            failed_count=failed,
        )
    except JobLeaseLostError:
        logger.warning("monitor_scheduler lease lost before session finalize; skip terminal")
    if heartbeat is not None:
        await heartbeat.stop()


async def run_monitor_scheduler_worker_runtime(
    *,
    session_factory: SessionFactory,
    heartbeat_loop: HeartbeatLoop,
    should_shutdown: ShutdownProbe,
    recover_stale_job_runs: RecoverStaleJobRuns,
    create_job_run: CreateJobRun,
    finish_job_run: FinishJobRun,
    monotonic_clock: MonotonicClock,
    logger: logging.Logger,
) -> None:
    """Run the Monitor Scheduler main lifecycle.

    C2C fenced model: the runtime acquires a per-session ``SchedulerJobRun`` as its
    EXCLUSIVE owner (``create_job_run`` returning ``None`` means another process owns it
    → skip cycle, never SELECT-reuse). A ``FencedJobToken`` + a single ``FencedJobHeartbeat(30s)``
    keep the lease alive for the whole session; per-cycle progress is written through
    ``update_owned_job_run_progress``; the session terminal is written only through
    ``finalize_job_run`` carrying the token. A lost lease stops the session with no
    terminal; an unexpected runtime exception is swallowed and, if still owned, written
    as a fenced ``failed`` terminal; graceful shutdown only stops the heartbeat.
    """
    # 进程级心跳（liveness，与 session lease 正交）——保持既有行为。
    _hb_task = asyncio.create_task(heartbeat_loop("monitor_scheduler"))
    service = MonitorBatchService()
    cycle_interval = get_settings().intraday_monitor_poll_seconds

    # [盘中监控1秒] 防重入标志
    _cycle_running = False

    # C2C session 内存状态（exclusive owner，不查询复用别人创建的 row）
    active_session_key: str | None = None
    active_token: FencedJobToken | None = None
    active_heartbeat: FencedJobHeartbeat | None = None
    session_succeeded = 0
    session_failed = 0

    async def _clear_active_session() -> None:
        nonlocal active_session_key, active_token, active_heartbeat
        nonlocal session_succeeded, session_failed
        active_session_key = None
        active_token = None
        active_heartbeat = None
        session_succeeded = 0
        session_failed = 0

    # [eval_recovery] 启动时恢复过期租约的 PENDING 评估（无 try/except，异常向上传播）
    async with session_factory() as db:
        recovered = await service.recover_stale_evaluations(db)
        await db.commit()
        if recovered > 0:
            logger.info("Monitor Worker 启动恢复: %d 个过期评估", recovered)

    # 启动时恢复过期的 monitor_scheduler running 任务（异常吞掉并记录）
    try:
        async with session_factory() as db:
            recovered = await recover_stale_job_runs(db)
            await db.commit()
            if recovered > 0:
                logger.info("Monitor Scheduler 启动恢复: %d 个过期任务", recovered)
    except Exception as exc:
        logger.exception("Monitor Scheduler 启动恢复异常: %s", exc)

    logger.info(
        "Monitor Scheduler Worker 启动（交易时段 9:30-11:30 / 13:00-15:00, 间隔=%ds）",
        cycle_interval,
    )

    # 启动成功飞书通知
    await notify_monitor_status(
        "监控服务已启动",
        f"交易时段 9:30-11:30 / 13:00-15:00\n每 {cycle_interval} 秒执行一轮监控",
        session_factory=session_factory,
        logger=logger,
    )

    while not should_shutdown():
        try:
            now = datetime.now(ZoneInfo("Asia/Shanghai"))

            # 交易日检查（使用异步接口，避免在事件循环中降级到 weekday）
            from app.services.calendar_service import is_trading_day_async

            async with session_factory() as db:
                trading = await is_trading_day_async(db, now.date())
            if not trading:
                # 非交易日，等待到下一个工作日
                await asyncio.sleep(300)  # 5分钟检查一次
                continue

            session_info = _get_monitor_session(now)
            if session_info is None:
                # 非交易时段：若当前有活跃 session，说明已在边界处结束 → finalize succeeded
                _ended_token = active_token
                if _ended_token is not None:
                    await _finalize_monitor_session(
                        _ended_token,
                        active_heartbeat,
                        session_succeeded,
                        session_failed,
                        logger=logger,
                    )
                    await _clear_active_session()
                # 等待逻辑（开盘前 / 午休 / 收盘后）
                current_time = now.time()
                if current_time < time_cls(9, 30):
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("等待开盘，还需 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif time_cls(11, 30) <= current_time < time_cls(13, 0):
                    wait_seconds = (
                        datetime(now.year, now.month, now.day, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now
                    ).total_seconds()
                    if wait_seconds > 0:
                        logger.info("午休中，等待 %d 秒", int(wait_seconds))
                        await asyncio.sleep(min(wait_seconds, 60))
                elif current_time >= time_cls(15, 0):
                    await asyncio.sleep(300)
                continue

            session_label, _start_time, end_time = session_info
            business_date = str(now.date())
            session_key = f"{business_date}:{session_label}"

            # session 过渡（上午→下午，或 lease-lost 后重新 acquire）→ finalize 旧的
            if active_session_key is not None and active_session_key != session_key:
                _prev_token = active_token
                if _prev_token is not None:
                    await _finalize_monitor_session(
                        _prev_token,
                        active_heartbeat,
                        session_succeeded,
                        session_failed,
                        logger=logger,
                    )
                await _clear_active_session()

            # 首次 acquire（exclusive owner）
            if active_session_key is None:
                async with session_factory() as db:
                    job_run = await _find_or_create_monitor_session_job_run(
                        db, now, business_date, session_label,
                        create_job_run=create_job_run,
                    )
                if job_run is None:
                    # session 已被其他进程拥有 → 跳过本轮，不 SELECT 复用（禁止跨进程偷 ownership）
                    logger.info("monitor_scheduler SKIPPED_DUPLICATE session=%s", session_key)
                    await asyncio.sleep(cycle_interval)
                    continue
                if not job_run.worker_instance_id:
                    raise RuntimeError(
                        f"monitor_scheduler create_job_run 未设置 worker_instance_id: "
                        f"job_run_id={job_run.id}"
                    )
                active_session_key = session_key
                active_token = FencedJobToken(
                    job_run_id=job_run.id,
                    worker_instance_id=job_run.worker_instance_id,
                    lease_epoch=job_run.lease_epoch,
                    lease_seconds=120,
                )
                # C2C：整段 session 用一个 30s fenced heartbeat 覆盖 lease，避免 120s lease 误过期
                active_heartbeat = FencedJobHeartbeat(active_token, interval_seconds=30.0)
                await active_heartbeat.start()
                session_succeeded = 0
                session_failed = 0

            # 防重入：上一周期未完成则跳过
            if _cycle_running:
                logger.debug("monitor_scheduler 上一周期未完成，跳过本轮")
                await asyncio.sleep(cycle_interval)
                continue

            _cycle_running = True  # [盘中监控1秒] 设置防重入标志
            _cycle_start_ts = monotonic_clock()
            try:
                # 每 cycle 校验 ownership 仍有效（heartbeat 后台已置位则直接抛）
                if active_heartbeat is not None:
                    active_heartbeat.ensure_owned()
                async with session_factory() as db:
                    try:
                        result = await service.execute_monitor_cycle(db)
                        await db.commit()
                    except JobLeaseLostError:
                        raise
                    except Exception:
                        await db.rollback()
                        raise
                    cycle_succeeded = True
                    session_succeeded += 1
                    _cycle_latency = monotonic_clock() - _cycle_start_ts
                    if cycle_succeeded and result.total_events_written > 0:
                        logger.info(
                            "监控周期完成: session=%s instruments=%d events=%d "
                            "notifications=%d latency=%.3fs skip=0",
                            session_label,
                            result.total_instruments,
                            result.total_events_written,
                            result.total_notifications_created,
                            _cycle_latency,
                        )
                    else:
                        logger.debug(
                            "监控周期完成: session=%s instruments=%d events=0 "
                            "latency=%.3fs",
                            session_label,
                            result.total_instruments,
                            _cycle_latency,
                        )
            except JobLeaseLostError:
                # ownership 在 cycle 内丢失：停 heartbeat、清 session、不写 terminal，下轮再 acquire
                logger.warning("monitor_scheduler lease lost during cycle session=%s", session_key)
                if active_heartbeat is not None:
                    await active_heartbeat.stop()
                await _clear_active_session()
                continue
            except Exception as exc:
                logger.exception("Monitor Scheduler 周期异常: %s", exc)
                session_failed += 1
            finally:
                _cycle_running = False  # [盘中监控1秒] 清除防重入标志

            # fenced progress update（仅当仍持有 ownership）
            if (
                active_token is not None
                and active_heartbeat is not None
                and not active_heartbeat.lost
            ):
                meta_updates: dict[str, Any] = {}
                try:
                    async with session_factory() as db:
                        latest_bar_time = await db.scalar(
                            sa_select(sa_func.max(MonitorEvaluation.source_bar_time))
                        )
                    if latest_bar_time is not None:
                        meta_updates["last_bar_time"] = latest_bar_time.isoformat()
                except Exception as exc:
                    logger.warning("查询 latest source_bar_time 失败: %s", exc)
                try:
                    await update_owned_job_run_progress(
                        active_token,
                        last_cycle_at=now,
                        succeeded_count=session_succeeded,
                        failed_count=session_failed,
                        metadata_updates=meta_updates,
                    )
                except JobLeaseLostError:
                    logger.warning(
                        "monitor_scheduler lease lost before progress update session=%s", session_key,
                    )
                    if active_heartbeat is not None:
                        await active_heartbeat.stop()
                    await _clear_active_session()
        except Exception as exc:
            # 未预期运行时异常：swallow；若仍持有 ownership 则写 fenced failed terminal
            logger.exception("Monitor Scheduler 异常: %s", exc)
            if active_token is not None:
                try:
                    await finalize_job_run(
                        active_token,
                        status="failed",
                        metadata_updates={"error": str(exc)[:500]},
                        total_count=session_succeeded + session_failed,
                        succeeded_count=session_succeeded,
                        failed_count=session_failed,
                    )
                except JobLeaseLostError:
                    pass
            if active_heartbeat is not None:
                await active_heartbeat.stop()
            await _clear_active_session()
            # 异常退出飞书通知
            await notify_monitor_status(
                "监控服务异常", str(exc), is_error=True,
                session_factory=session_factory, logger=logger,
            )

        # 交易时段内每 cycle_interval 秒一轮
        await asyncio.sleep(cycle_interval)

    # 优雅退出：只停 heartbeat，不伪造 succeeded/failed terminal（未完成 running row 由 lease 到期 recovery 处理）
    if active_heartbeat is not None:
        await active_heartbeat.stop()
    logger.info("Monitor Scheduler Worker 已退出")


__all__ = [
    "run_monitor_scheduler_worker_runtime",
    "_get_monitor_session",
    "_find_or_create_monitor_session_job_run",
    "_finalize_monitor_session",
]
