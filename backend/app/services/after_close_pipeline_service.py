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

# 8 个展示步骤（前 7 个来自 after_close_orchestrator 状态机，最后一个是 watchlist gate）
_PIPELINE_STEPS = [
    AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.CHECKING_COVERAGE.value,
    AfterCloseRunStatus.CREATING_DSA.value,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value,
    AfterCloseRunStatus.QUALITY_GATE.value,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value,
    AfterCloseRunStatus.PUBLISHING.value,
    "watchlist_ready",
]

# last_completed_step -> 已完成步骤索引（含 checking_coverage/creating_dsa 的隐式完成）
_COMPLETED_STEP_INDEX = {
    None: -1,
    AfterCloseRunStatus.QUEUED.value: -1,
    AfterCloseRunStatus.REFRESHING_DAILY.value: 0,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: 3,
    AfterCloseRunStatus.QUALITY_GATE.value: 4,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: 5,
    AfterCloseRunStatus.PUBLISHING.value: 6,
    AfterCloseRunStatus.SUCCEEDED.value: 7,
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
    # 优先：succeeded + published + scope=full
    preferred_stmt = (
        select(StockFeatureSnapshotRun)
        .where(StockFeatureSnapshotRun.trade_date == trade_date)
        .where(StockFeatureSnapshotRun.status == STATUS_SUCCEEDED)
        .where(StockFeatureSnapshotRun.published_at.is_not(None))
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


def _aggregate_step_events(
    events: list[JobRunEvent],
) -> dict[str, dict[str, Any]]:
    """按 step 聚合事件，得到每个步骤的启停时间、count、错误信息。"""
    stats: dict[str, dict[str, Any]] = {}
    for event in events:
        step = event.step
        # 只关注状态机步骤或 ERROR 事件
        if step not in _PIPELINE_STEPS and step not in {
            AfterCloseRunStatus.QUEUED.value,
            AfterCloseRunStatus.FAILED.value,
            "ERROR",
            "START",
        }:
            continue
        # ERROR 事件没有固定 step，尝试从 payload 取 step；否则归为当前 orchestrator_status
        if step in ("ERROR", "START"):
            payload_step = (
                event.payload.get("step")
                if isinstance(event.payload, dict)
                else None
            )
            if not isinstance(payload_step, str):
                continue
            step = payload_step
        step_stats = stats.setdefault(step, {
            "started_at": None,
            "finished_at": None,
            "error_message": None,
            "counts": {},
            "event_count": 0,
        })
        if step_stats["started_at"] is None:
            step_stats["started_at"] = event.created_at
        step_stats["finished_at"] = event.created_at
        step_stats["event_count"] += 1
        if event.level == "error":
            step_stats["error_message"] = event.message or step_stats["error_message"]
        if isinstance(event.payload, dict):
            for key in (
                "coverage", "covered", "total", "succeeded_count", "failed_count",
                "snapshot_count", "partial_failed_count", "expected_count",
            ):
                if key in event.payload:
                    step_stats["counts"][key] = event.payload[key]
    return stats


def _compute_step_states(
    job_run: SchedulerJobRun | None,
    events: list[JobRunEvent],
    watchlist_ready: bool,
) -> list[dict[str, Any]]:
    """根据 job_run 状态、事件、watchlist_ready 计算 8 步骤状态。"""
    if job_run is None:
        return [
            {
                "step": step,
                "status": "pending",
                "started_at": None,
                "finished_at": None,
                "duration_seconds": None,
                "counts": {},
                "error_message": None,
            }
            for step in _PIPELINE_STEPS
        ]

    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status")
    last_completed_step = meta.get("last_completed_step")
    completed_idx = _COMPLETED_STEP_INDEX.get(last_completed_step, -1)
    step_events = _aggregate_step_events(events)

    # 失败时定位失败步骤
    failed_step: str | None = None
    if job_run.status == "failed":
        if orchestrator_status in _PIPELINE_STEPS:
            failed_step = orchestrator_status
        else:
            # 从 ERROR 事件 payload 或最近非 pending 步骤推断
            for event in events:
                if event.level == "error" and isinstance(event.payload, dict) and event.payload.get("step"):
                    failed_step = event.payload["step"]
                    break
            if failed_step is None and last_completed_step is not None:
                failed_step = _step_after(last_completed_step)

    # 当前运行步骤
    current_idx = -1
    if orchestrator_status in _PIPELINE_STEPS:
        current_idx = _PIPELINE_STEPS.index(orchestrator_status)

    steps: list[dict[str, Any]] = []
    for idx, step in enumerate(_PIPELINE_STEPS):
        stats = step_events.get(step, {})
        started_at = stats.get("started_at")
        finished_at = stats.get("finished_at")
        duration = None
        if started_at is not None and finished_at is not None:
            duration = (finished_at - started_at).total_seconds()

        if step == "watchlist_ready":
            if job_run.status == "succeeded":
                step_status = "completed" if watchlist_ready else "pending"
            elif current_idx == idx:
                step_status = "running"
            elif watchlist_ready:
                step_status = "completed"
            else:
                step_status = "pending"
        elif job_run.status == "failed":
            if step == failed_step:
                step_status = "failed"
            elif idx <= completed_idx or (current_idx >= 0 and idx < current_idx):
                step_status = "completed"
            else:
                step_status = "pending"
        elif job_run.status == "succeeded":
            step_status = "completed"
        elif job_run.status == "running":
            if idx == current_idx:
                step_status = "running"
            elif idx <= completed_idx:
                step_status = "completed"
            else:
                step_status = "pending"
        else:
            # queued 或其他：已完成步骤显示 completed，当前及之后 pending
            step_status = "completed" if idx <= completed_idx else "pending"

        steps.append({
            "step": step,
            "status": step_status,
            "started_at": _format_dt(started_at),
            "finished_at": _format_dt(finished_at),
            "duration_seconds": duration,
            "counts": stats.get("counts", {}),
            "error_message": stats.get("error_message"),
        })
    return steps


def _step_after(last_completed_step: str | None) -> str | None:
    """根据 last_completed_step 返回下一个可能步骤。"""
    idx = _COMPLETED_STEP_INDEX.get(last_completed_step, -1)
    if idx < 0:
        return AfterCloseRunStatus.REFRESHING_DAILY.value
    if idx + 1 < len(_PIPELINE_STEPS):
        return _PIPELINE_STEPS[idx + 1]
    return None


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

    data_freshness = await _compute_data_freshness(db, now)
    steps = _compute_step_states(job_run, events, watchlist_ready)

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
    # 自测入口：验证常量与映射一致性（不连 DB）
    assert "refreshing_daily" in _PIPELINE_STEPS
    assert "watchlist_ready" in _PIPELINE_STEPS
    assert len(_PIPELINE_STEPS) == 8
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.WAITING_DSA_WORKER.value] == 3
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.SUCCEEDED.value] == 7
    print("after_close_pipeline_service 常量与映射自测通过")
