"""盘后流水线可视化聚合服务。

为 /admin/after-close/pipeline/* 端点提供只读聚合：
- 按 trade_date 聚合 after_close_orchestrator 状态 + 8 步骤时间线
- 复用 system_overview_service._compute_data_freshness 计算数据新鲜度
- 复用 feature_snapshot_service.has_succeeded_snapshot_run 判定 watchlist_ready
- 复用 after_close_orchestrator 状态机与 job_run_event_service.list_events

设计原则：
- 不新建大表，不复制 SQL。
- 不对 after_close_orchestrator 状态机做语义扩展。
- overall_status 与系统概览的 PIPELINE_STATUS_* 是两套枚举，不可混用。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import now_shanghai
from app.models.job_run_event import JobRunEvent
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_feature_snapshot_run import (
    RUN_TYPE_BACKFILL,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.after_close_orchestrator import (
    AfterCloseRunStatus,
    create_after_close_run,
)
from app.services.calendar_service import is_trading_day_async
from app.services.feature_snapshot_service import has_succeeded_snapshot_run
from app.services.job_run_event_service import list_events
from app.services.market_status_service import (
    MARKET_SESSION_CLOSED,
    MARKET_SESSION_NON_TRADING_DAY,
    compute_market_session,
)
from app.services.system_overview_service import _compute_data_freshness

logger = logging.getLogger("after_close_pipeline_service")

_AFTER_CLOSE_JOB_NAME = "after_close_orchestrator"

# 收盘后超过该阈值（分钟）仍无 after_close run，视为 blocked
_BLOCKED_AFTER_CLOSE_MINUTES = 30

# feature_snapshot 阶段“疑似停滞”判定阈值（秒）：
# 编排处于 feature_snapshot 且心跳新鲜，但 progress.updated_at 超过该值未更新。
_FEATURE_SNAPSHOT_STALL_SECONDS = 300

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")

# 5 个面向用户的真实业务阶段（合并内部细粒度状态）：
#   行情准备(market_prep)      = refreshing_daily + checking_coverage + creating_dsa
#   DSA 计算(dsa_compute)      = waiting_dsa_worker
#   质量校验(quality_gate)      = quality_gate
#   特征快照(feature_snapshot) = feature_snapshot
#   发布结果(publishing)        = publishing
# “自选可用”(watchlist_ready) 作为最终发布门禁(gate)，不作为执行步骤。
# 内部原始细事件仍保留在 events 抽屉中，向后兼容，不删数据库历史。
_PHASE_KEYS = [
    "market_prep",
    "dsa_compute",
    "quality_gate",
    "feature_snapshot",
    "publishing",
]

_PHASE_LABELS = {
    "market_prep": "行情准备",
    "dsa_compute": "DSA计算",
    "quality_gate": "质量校验",
    "feature_snapshot": "特征快照",
    "publishing": "发布结果",
}

# 内部 orchestrator_status → 5 阶段下标
_STATUS_TO_PHASE = {
    AfterCloseRunStatus.REFRESHING_DAILY.value: 0,
    AfterCloseRunStatus.CHECKING_COVERAGE.value: 0,
    AfterCloseRunStatus.CREATING_DSA.value: 0,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: 1,
    AfterCloseRunStatus.QUALITY_GATE.value: 2,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: 3,
    AfterCloseRunStatus.PUBLISHING.value: 4,
    AfterCloseRunStatus.SUCCEEDED.value: 4,
}

# 内部状态 → 其所属 5 阶段的代表状态（虚拟状态 checking_coverage/creating_dsa 归并到 market_prep 代表 refreshing_daily）
_PHASE_REP_FOR_STATUS = {
    AfterCloseRunStatus.REFRESHING_DAILY.value: AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.CHECKING_COVERAGE.value: AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.CREATING_DSA.value: AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: AfterCloseRunStatus.WAITING_DSA_WORKER.value,
    AfterCloseRunStatus.QUALITY_GATE.value: AfterCloseRunStatus.QUALITY_GATE.value,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
    AfterCloseRunStatus.PUBLISHING.value: AfterCloseRunStatus.PUBLISHING.value,
    AfterCloseRunStatus.SUCCEEDED.value: AfterCloseRunStatus.SUCCEEDED.value,
}

# 每个阶段的主代表内部状态（用于从 events 取该阶段 started 时间）
_PHASE_REPRESENTATIVE_STATUS = [
    AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value,
    AfterCloseRunStatus.QUALITY_GATE.value,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
    AfterCloseRunStatus.PUBLISHING.value,
]

# last_completed_step（内部状态）→ 已完成阶段数（含该阶段）。
# 注意 checking_coverage/creating_dsa 仅是 refresh_all_instruments 内部虚拟步骤，
# 不会作为 orchestrator_status 出现，因此这里不单列。
_COMPLETED_PHASE_INDEX = {
    None: -1,
    AfterCloseRunStatus.QUEUED.value: -1,
    AfterCloseRunStatus.REFRESHING_DAILY.value: 0,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: 1,
    AfterCloseRunStatus.QUALITY_GATE.value: 2,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: 3,
    AfterCloseRunStatus.PUBLISHING.value: 4,
    AfterCloseRunStatus.SUCCEEDED.value: 5,
}


def _parse_metadata(job_run: SchedulerJobRun) -> dict[str, Any]:
    """解析 scheduler_job_run.metadata_json。"""
    if not job_run.metadata_json:
        return {}
    try:
        return json.loads(job_run.metadata_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "[AfterClosePipeline] metadata_json 解析失败 job_run_id=%s: %s",
            job_run.id, exc,
        )
        return {}


def _format_dt(value: datetime | None) -> str | None:
    """时区感知 datetime 转 ISO 字符串。"""
    if value is None:
        return None
    return value.isoformat()


async def _get_after_close_run_for_trade_date(
    db: AsyncSession,
    trade_date: date,
) -> SchedulerJobRun | None:
    """查询指定交易日最新的 after_close_orchestrator job_run。"""
    stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == _AFTER_CLOSE_JOB_NAME,
            SchedulerJobRun.business_date == trade_date.isoformat(),
        )
        .order_by(desc(SchedulerJobRun.created_at))
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _get_snapshot_run_summary(
    db: AsyncSession,
    trade_date: date,
) -> dict[str, Any] | None:
    """查询指定交易日的 feature_snapshot_run 摘要。

    优先返回可读的 full/published/succeeded run（即 watchlist_ready 的实际数据源）；
    若不存在，fallback 到最新任意 run（用于展示 sample/running/failed 等参考信息）。
    避免出现 watchlist_ready=true 但页面展示 sample run 的误导。
    """
    # 优先：succeeded + published + scope=full（与 has_succeeded_snapshot_run 筛选条件一致）
    preferred_stmt = (
        select(StockFeatureSnapshotRun)
        .where(StockFeatureSnapshotRun.trade_date == trade_date)
        .where(StockFeatureSnapshotRun.status == STATUS_SUCCEEDED)
        .where(StockFeatureSnapshotRun.published_at.is_not(None))
        .where(StockFeatureSnapshotRun.metadata_["scope"].astext == "full")
        .order_by(desc(StockFeatureSnapshotRun.created_at))
        .limit(1)
    )
    preferred_result = await db.execute(preferred_stmt)
    run = preferred_result.scalar_one_or_none()

    if run is None:
        # fallback：最新任意 run
        fallback_stmt = (
            select(StockFeatureSnapshotRun)
            .where(StockFeatureSnapshotRun.trade_date == trade_date)
            .order_by(desc(StockFeatureSnapshotRun.created_at))
            .limit(1)
        )
        fallback_result = await db.execute(fallback_stmt)
        run = fallback_result.scalar_one_or_none()

    if run is None:
        return None
    meta = run.metadata_ or {}
    return {
        "run_id": str(run.id),
        "run_type": run.run_type,
        "status": run.status,
        "scope": meta.get("scope") or "full",
        "snapshot_count": run.snapshot_count,
        "failed_count": run.failed_count,
        "skipped_count": run.skipped_count,
        "expected_count": run.expected_count,
        "published_at": _format_dt(run.published_at),
        "started_at": _format_dt(run.started_at),
        "finished_at": _format_dt(run.finished_at),
    }


def _collect_phase_started_at(
    events: list[JobRunEvent],
) -> tuple[dict[str, datetime], datetime | None]:
    """从事件收集每个内部代表状态的首个事件时间，以及 succeeded 事件时间。

    返回 (status_started_at, succeeded_at)：
    - status_started_at[status] = 该内部状态最早事件时间（即该阶段开始时间）；
    - succeeded_at = step=="succeeded" 的最早事件时间（用于推导最后一阶段结束）。

    兼容历史数据：旧事件无 event_type 时，首事件时间即开始时间（推导结束用下一阶段开始）。
    """
    status_started_at: dict[str, datetime] = {}
    succeeded_at: datetime | None = None
    for event in events:
        step = event.step
        # ERROR/START 事件从 payload.step 归并到真实阶段
        if step in ("ERROR", "START"):
            payload_step = (
                event.payload.get("step")
                if isinstance(event.payload, dict)
                else None
            )
            if isinstance(payload_step, str):
                step = payload_step
        if step in _PHASE_REPRESENTATIVE_STATUS:
            cur = status_started_at.get(step)
            if cur is None or event.created_at < cur:
                status_started_at[step] = event.created_at
        elif step == AfterCloseRunStatus.SUCCEEDED.value:
            if succeeded_at is None or event.created_at < succeeded_at:
                succeeded_at = event.created_at
    return status_started_at, succeeded_at


def _phase_counts_error(
    events: list[JobRunEvent],
    status: str,
) -> tuple[dict[str, Any], str | None]:
    """从某内部状态的事件中提取计数与错误信息（用于阶段详情）。

    status 为 5 阶段代表状态（如 refreshing_daily）；内部虚拟状态
    checking_coverage/creating_dsa 的事件会被归一化到 refreshing_daily 阶段。
    """
    counts: dict[str, Any] = {}
    error_message: str | None = None
    for event in events:
        step = event.step
        if step in ("ERROR", "START"):
            payload_step = (
                event.payload.get("step")
                if isinstance(event.payload, dict)
                else None
            )
            if isinstance(payload_step, str):
                step = payload_step
        # 归一化虚拟状态到阶段代表状态
        norm_step = _PHASE_REP_FOR_STATUS.get(step, step)
        if norm_step != status:
            continue
        if event.level == "error":
            error_message = event.message or error_message
        if isinstance(event.payload, dict):
            for key in (
                "coverage", "covered", "total", "succeeded_count", "failed_count",
                "snapshot_count", "partial_failed_count", "expected_count",
                "processed", "failed",
            ):
                if key in event.payload and key not in counts:
                    counts[key] = event.payload[key]
    return counts, error_message


def _infer_failed_phase(
    orchestrator_status: str | None,
    events: list[JobRunEvent],
    last_completed_step: str | None,
) -> int:
    """推断失败所处的 5 阶段下标。"""
    failed_step: str | None = None
    if orchestrator_status in _PHASE_REPRESENTATIVE_STATUS:
        failed_step = orchestrator_status
    else:
        for event in events:
            if event.level == "error" and isinstance(event.payload, dict) and event.payload.get("step"):
                failed_step = event.payload["step"]
                break
    if failed_step is None and last_completed_step is not None:
        # 失败阶段 = 最后完成阶段之后的那一阶段
        completed_idx = _COMPLETED_PHASE_INDEX.get(last_completed_step, -1)
        if 0 <= completed_idx < len(_PHASE_KEYS):
            failed_step = _PHASE_REPRESENTATIVE_STATUS[completed_idx]
    if failed_step is not None:
        return _STATUS_TO_PHASE.get(failed_step, -1)
    return -1


def _compute_step_states(
    job_run: SchedulerJobRun | None,
    events: list[JobRunEvent],
    watchlist_ready: bool,
    snapshot_summary: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """根据 job_run 状态、事件、watchlist_ready、snapshot_summary 计算 5 阶段状态。

    now 用于运行中阶段的耗时计算：运行中阶段 finished_at 必须为 None，
    耗时按 now - started_at 计算，不得用事件最大时间冒充结束时间。

    阶段结束时间推导（兼容历史数据）：
    - 已完成阶段 i 的结束时间 = 阶段 i+1 的开始时间；
    - 最后一阶段（publishing）结束时间 = succeeded 事件时间；
    - 若无法推导（如运行中的最后阶段），finished_at=None，耗时按 now 计算；
    - 任何耗时不得为负（duration<0 归零）。
    """
    if job_run is None:
        return [
            {
                "step": phase,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "counts": {},
                "error_message": None,
            }
            for phase in _PHASE_KEYS
        ]

    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status")
    last_completed_step = meta.get("last_completed_step")
    completed_phase_idx = _COMPLETED_PHASE_INDEX.get(last_completed_step, -1)

    status_started_at, succeeded_at = _collect_phase_started_at(events)

    def _phase_start(idx: int) -> datetime | None:
        return status_started_at.get(_PHASE_REPRESENTATIVE_STATUS[idx])

    def _phase_finish(idx: int) -> datetime | None:
        if idx + 1 < len(_PHASE_KEYS):
            return _phase_start(idx + 1)
        return succeeded_at

    # 当前运行阶段（SUCCEEDED 表示全部完成，无当前运行阶段）
    current_phase = -1
    if orchestrator_status and orchestrator_status != AfterCloseRunStatus.SUCCEEDED.value:
        current_phase = _STATUS_TO_PHASE.get(orchestrator_status, -1)

    failed_phase = (
        _infer_failed_phase(orchestrator_status, events, last_completed_step)
        if job_run.status == "failed"
        else -1
    )

    steps: list[dict[str, Any]] = []
    for idx, phase in enumerate(_PHASE_KEYS):
        started_at = _phase_start(idx)
        finished_at = _phase_finish(idx)
        counts, error_message = _phase_counts_error(
            events, _PHASE_REPRESENTATIVE_STATUS[idx]
        )

        if job_run.status == "succeeded":
            step_status = "completed"
        elif job_run.status == "failed":
            if failed_phase >= 0 and idx == failed_phase:
                step_status = "failed"
            elif idx <= completed_phase_idx:
                step_status = "completed"
            else:
                step_status = "pending"
        elif job_run.status == "running":
            if current_phase >= 0 and idx == current_phase:
                step_status = "running"
            elif idx < current_phase:
                step_status = "completed"
            else:
                step_status = "pending"
        elif job_run.status == "interrupted":
            # [Repair] orchestrator 已中断但 snapshot 仍在 running，
            # 特征快照阶段显示 running，提示“快照计算失联/待修复”。
            if (
                idx == 3
                and snapshot_summary is not None
                and snapshot_summary.get("status") == "running"
            ):
                step_status = "running"
            elif idx <= completed_phase_idx:
                step_status = "completed"
            else:
                step_status = "pending"
        else:
            # queued 或其他：已完成阶段显示 completed，当前及之后 pending
            step_status = "completed" if idx <= completed_phase_idx else "pending"

        # 耗时计算：运行中 finished_at 必须为 None，按 now - started_at；
        # 已完成阶段 finished>=started，任何负耗时归零。
        duration: float | None = None
        if step_status == "running":
            finished_at = None
            if now is not None and started_at is not None:
                duration = (now - started_at).total_seconds()
        elif started_at is not None and finished_at is not None:
            duration = (finished_at - started_at).total_seconds()
            if duration < 0:
                duration = 0.0

        steps.append({
            "step": phase,
            "status": step_status,
            "started_at": _format_dt(started_at),
            "finished_at": _format_dt(finished_at),
            "duration_seconds": duration,
            "counts": counts,
            "error_message": error_message,
        })
    return steps


def _compute_feature_snapshot_stalled(
    job_run: SchedulerJobRun | None,
    meta: dict[str, Any],
    now: datetime,
) -> bool:
    """判断 feature_snapshot 阶段是否“疑似停滞”。

    条件：编排处于 feature_snapshot 且心跳新鲜（未触发 blocked），
    但 metadata.feature_snapshot_progress.updated_at 距今超过阈值
    （默认 _FEATURE_SNAPSHOT_STALL_SECONDS=300s）。

    返回 True 仅表示“心跳新鲜但进度长时间未推进”，供前端提示“疑似停滞”，
    不替代 blocked（心跳已超时由 _compute_overall_status 判定）。
    """
    if job_run is None:
        return False
    if meta.get("orchestrator_status") != AfterCloseRunStatus.FEATURE_SNAPSHOT.value:
        return False
    progress = meta.get("feature_snapshot_progress")
    if not isinstance(progress, dict):
        return False
    updated_at = progress.get("updated_at")
    if not isinstance(updated_at, str):
        return False
    try:
        prog_time = datetime.fromisoformat(updated_at)
    except ValueError:
        return False
    if prog_time.tzinfo is None:
        prog_time = prog_time.replace(tzinfo=_SHANGHAI_TZ)
    return (now - prog_time).total_seconds() > _FEATURE_SNAPSHOT_STALL_SECONDS


def _compute_overall_status(
    job_run: SchedulerJobRun | None,
    market_session: str,
    now: datetime,
    watchlist_ready: bool,
    has_backfill_full: bool,
) -> str:
    """overall_status: not_started/running/succeeded/failed/blocked/skipped。"""
    if market_session == MARKET_SESSION_NON_TRADING_DAY:
        if job_run is None and not has_backfill_full:
            return "skipped"
    if job_run is None:
        # 收盘后超过阈值仍无 run -> blocked
        if market_session == MARKET_SESSION_CLOSED:
            market_close = now.replace(hour=15, minute=0, second=0, microsecond=0)
            if now >= market_close + timedelta(minutes=_BLOCKED_AFTER_CLOSE_MINUTES):
                return "blocked"
        return "not_started"
    if job_run.status == "running":
        # 心跳/租约超时判定为 blocked（简化：超过 10 分钟无心跳）
        if job_run.heartbeat_at is not None:
            if now - job_run.heartbeat_at > timedelta(minutes=10):
                return "blocked"
        return "running"
    if job_run.status == "failed":
        return "failed"
    if job_run.status == "succeeded":
        return "succeeded" if watchlist_ready else "failed"
    # queued 视为 running
    if job_run.status == "queued":
        return "running"
    return "not_started"


def _compute_watchlist_reason(
    watchlist_ready: bool,
    job_run: SchedulerJobRun | None,
    snapshot_summary: dict[str, Any] | None,
    has_backfill_full: bool = False,
) -> str:
    """watchlist_ready 的人类可读原因。"""
    if watchlist_ready:
        return "after_close 已 succeeded，feature_snapshot full/published，自选股可读"
    if job_run is None:
        if has_backfill_full:
            return "存在 backfill full 快照，但无 after_close run，属于手动补齐数据"
        return "尚未有 after_close run，无法判定 snapshot 可用性"
    if job_run.status != "succeeded":
        return f"after_close 状态为 {job_run.status}，未进入 publish"
    if snapshot_summary is None:
        return "after_close 已完成，但未找到 feature_snapshot_run 记录"
    if snapshot_summary.get("status") != "succeeded":
        return f"feature_snapshot_run 状态为 {snapshot_summary['status']}，未发布"
    if snapshot_summary.get("published_at") is None:
        return "feature_snapshot_run 未写入 published_at"
    if snapshot_summary.get("scope") != "full":
        return f"feature_snapshot_run scope={snapshot_summary['scope']}，非 full，不可读"
    return "未知原因导致不可读"


async def _build_pipeline_response(
    db: AsyncSession,
    trade_date: date,
    now: datetime,
) -> dict[str, Any]:
    """构建单个交易日的 after_close pipeline 聚合响应。"""
    is_trading_day = await is_trading_day_async(db, trade_date)
    market_session = compute_market_session(now, is_trading_day)

    job_run = await _get_after_close_run_for_trade_date(db, trade_date)
    events: list[JobRunEvent] = []
    if job_run is not None:
        events = await list_events(db, job_run.id, limit=100)

    watchlist_ready = await has_succeeded_snapshot_run(db, trade_date)
    snapshot_summary = await _get_snapshot_run_summary(db, trade_date)

    # 是否存在 backfill full succeeded（用于“手动补齐”文案）
    has_backfill_full = False
    if not watchlist_ready and snapshot_summary is not None:
        if (
            snapshot_summary["run_type"] == RUN_TYPE_BACKFILL
            and snapshot_summary["status"] == "succeeded"
            and snapshot_summary["scope"] == "full"
        ):
            has_backfill_full = True

    overall_status = _compute_overall_status(
        job_run, market_session, now, watchlist_ready, has_backfill_full
    )
    # 如果有 backfill full 但无 after_close succeeded，且当前并非失败，则标记为 blocked/manual_fill
    if overall_status == "not_started" and has_backfill_full:
        overall_status = "blocked"

    watchlist_reason = _compute_watchlist_reason(
        watchlist_ready, job_run, snapshot_summary, has_backfill_full
    )

    # [Repair] 判断 orchestrator 中断但 snapshot 仍在 running 的失联状态
    feature_snapshot_lost_contact = (
        job_run is not None
        and job_run.status == "interrupted"
        and snapshot_summary is not None
        and snapshot_summary.get("status") == "running"
    )

    # [Fix] feature_snapshot 阶段疑似停滞判定（心跳新鲜但进度长时间未推进）
    feature_snapshot_stalled = False
    if job_run is not None:
        _meta = _parse_metadata(job_run)
        feature_snapshot_stalled = _compute_feature_snapshot_stalled(job_run, _meta, now)

    data_freshness = await _compute_data_freshness(db, now)
    steps = _compute_step_states(
        job_run, events, watchlist_ready, snapshot_summary, now=now,
    )

    after_close_run_summary: dict[str, Any] | None = None
    if job_run is not None:
        meta = _parse_metadata(job_run)
        after_close_run_summary = {
            "job_run_id": str(job_run.id),
            "status": job_run.status,
            "orchestrator_status": meta.get("orchestrator_status"),
            "started_at": _format_dt(job_run.started_at),
            "finished_at": _format_dt(job_run.finished_at),
            "heartbeat_at": _format_dt(job_run.heartbeat_at),
            "lease_expires_at": _format_dt(job_run.lease_expires_at),
            "last_completed_step": meta.get("last_completed_step"),
            "error_message": job_run.error_message,
            "worker_instance_id": job_run.worker_instance_id,
            "trade_date": meta.get("trade_date"),
            "feature_snapshot_run_id": meta.get("feature_snapshot_run_id"),
            "feature_snapshot_progress": meta.get("feature_snapshot_progress"),
            "feature_snapshot_stalled": feature_snapshot_stalled,
        }

    return {
        "trade_date": trade_date.isoformat(),
        "market_session": market_session,
        "overall_status": overall_status,
        "watchlist_ready": watchlist_ready,
        "watchlist_reason": watchlist_reason,
        "has_backfill_full": has_backfill_full,
        "after_close_run": after_close_run_summary,
        "steps": steps,
        "data_freshness": data_freshness,
        "feature_snapshot_run": snapshot_summary,
        "feature_snapshot_lost_contact": feature_snapshot_lost_contact,
        "feature_snapshot_stalled": feature_snapshot_stalled,
        "events": [
            {
                "id": str(e.id),
                "job_run_id": str(e.job_run_id),
                "step": e.step,
                "level": e.level,
                "message": e.message,
                "payload": e.payload,
                "created_at": _format_dt(e.created_at),
            }
            for e in events
        ],
    }


async def get_latest_pipeline(
    db: AsyncSession,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回最近交易日（含今日）的 after_close pipeline 聚合状态。

    策略：
    - 交易日（含今日）：始终以 today 为目标 trade_date，即使无 after_close run
      也返回 today 的 not_started/blocked，避免回退历史 run 掩盖"今天未执行"。
    - 非交易日：回退到最近一个有 after_close run 记录的交易日，展示历史状态。
    """
    if now is None:
        now = now_shanghai()
    today = now.date()
    is_trading_day = await is_trading_day_async(db, today)

    if is_trading_day:
        # 交易日：始终以 today 为目标，不回退历史
        return await _build_pipeline_response(db, today, now)

    # 非交易日：回退到最近一个有 after_close run 记录的交易日
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.job_name == _AFTER_CLOSE_JOB_NAME)
        .order_by(desc(SchedulerJobRun.business_date))
        .limit(1)
    )
    result = await db.execute(stmt)
    latest = result.scalar_one_or_none()
    if latest is not None and latest.business_date:
        try:
            trade_date = date.fromisoformat(latest.business_date)
        except ValueError:
            trade_date = today
    else:
        trade_date = today
    return await _build_pipeline_response(db, trade_date, now)


async def get_pipeline_by_trade_date(
    db: AsyncSession,
    trade_date: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回指定交易日的 after_close pipeline 聚合状态。"""
    if now is None:
        now = now_shanghai()
    return await _build_pipeline_response(db, trade_date, now)


async def list_pipeline_runs(
    db: AsyncSession,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """返回最近 N 次 after_close_orchestrator 与 snapshot run 摘要。"""
    # after_close_orchestrator runs
    ac_stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.job_name == _AFTER_CLOSE_JOB_NAME)
        .order_by(desc(SchedulerJobRun.created_at))
        .limit(limit)
    )
    ac_result = await db.execute(ac_stmt)
    ac_runs = list(ac_result.scalars().all())

    # snapshot runs（backfill full/sample、after_close、manual）
    snap_stmt = (
        select(StockFeatureSnapshotRun)
        .order_by(desc(StockFeatureSnapshotRun.created_at))
        .limit(limit)
    )
    snap_result = await db.execute(snap_stmt)
    snap_runs = list(snap_result.scalars().all())

    items: list[dict[str, Any]] = []
    for run in ac_runs:
        meta = _parse_metadata(run)
        items.append({
            "kind": "after_close_orchestrator",
            "job_run_id": str(run.id),
            "trade_date": run.business_date,
            "status": run.status,
            "orchestrator_status": meta.get("orchestrator_status"),
            "started_at": _format_dt(run.started_at),
            "finished_at": _format_dt(run.finished_at),
            "error_message": run.error_message,
            "worker_instance_id": run.worker_instance_id,
            "last_completed_step": meta.get("last_completed_step"),
        })
    snap_run: StockFeatureSnapshotRun
    for snap_run in snap_runs:
        meta = snap_run.metadata_ or {}
        items.append({
            "kind": "snapshot_run",
            "run_id": str(snap_run.id),
            "trade_date": snap_run.trade_date.isoformat(),
            "run_type": snap_run.run_type,
            "status": snap_run.status,
            "scope": meta.get("scope") or "full",
            "snapshot_count": snap_run.snapshot_count,
            "failed_count": snap_run.failed_count,
            "published_at": _format_dt(snap_run.published_at),
            "started_at": _format_dt(snap_run.started_at),
            "finished_at": _format_dt(snap_run.finished_at),
        })

    # 按 created_at 倒序合并并截断
    items.sort(key=lambda x: x.get("started_at") or x.get("published_at") or "", reverse=True)
    return items[:limit]


async def create_pipeline_run(
    db: AsyncSession,
    trade_date: date,
) -> tuple[SchedulerJobRun, bool]:
    """管理员触发指定交易日的 after_close 编排任务。

    Returns:
        (job_run, is_new)：is_new=True 表示新建；False 表示已有 queued/running/succeeded。
    """
    return await create_after_close_run(db, trade_date)


if __name__ == "__main__":
    # 自测入口：验证 5 阶段常量与映射一致性（不连 DB）
    assert "market_prep" in _PHASE_KEYS
    assert "publishing" in _PHASE_KEYS
    assert len(_PHASE_KEYS) == 5
    # SUCCEEDED 表示全部 5 阶段完成，completed index = 5
    assert _COMPLETED_PHASE_INDEX[AfterCloseRunStatus.SUCCEEDED.value] == 5
    assert _COMPLETED_PHASE_INDEX[AfterCloseRunStatus.FEATURE_SNAPSHOT.value] == 3
    # 内部状态 → 阶段下标 映射正确
    assert _STATUS_TO_PHASE[AfterCloseRunStatus.FEATURE_SNAPSHOT.value] == 3
    assert _STATUS_TO_PHASE[AfterCloseRunStatus.PUBLISHING.value] == 4
    # 5 阶段映射覆盖全部代表性内部状态
    assert all(
        s in _STATUS_TO_PHASE
        for s in _PHASE_REPRESENTATIVE_STATUS
    )
    print("after_close_pipeline_service 5 阶段常量与映射自测通过")
