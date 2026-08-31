"""盘后流水线可视化聚合服务。

为 /admin/after-close/pipeline/* 端点提供只读聚合：
- 按 trade_date 聚合 after_close_orchestrator 状态 + 6 步骤时间线
- 复用 system_overview_service._compute_data_freshness 计算数据新鲜度
- 复用 feature_snapshot_service.has_succeeded_snapshot_run 判定 watchlist_ready
- 复用 after_close_orchestrator 状态机与 job_run_event_service.list_events

设计原则：
- 不新建大表，不复制 SQL。
- 不对 after_close_orchestrator 状态机做语义扩展。
- overall_status 与系统概览的 PIPELINE_STATUS_* 是两套枚举，不可混用。
[Phase8A] 步骤序列：refreshing_daily → syncing_boards → checking_coverage
  → computing_features → computing_review → computing_history → watchlist_ready
  旧四状态（creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）
  映射到 computing_features，仅历史 run 兼容读取。
  [CHANGE-20260831-ADMIN-TIMELINE] publishing 已从 current canonical DAG 移除：
  不为当前 run 合成 publishing；仅当历史 run 真实存在 publishing 事件时如实呈现。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
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

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_UTC_TZ = UTC

# [CHANGE-20260801-REVIEW-CLOSURE] 7 个展示步骤：
#   refreshing_daily → syncing_boards → checking_coverage
#   → computing_features → publishing → computing_review → watchlist_ready
# 旧 4 步（creating_dsa/waiting_dsa_worker/quality_gate/feature_snapshot）
# 收敛为 computing_features，仅历史映射。
_PIPELINE_STEPS = [
    AfterCloseRunStatus.REFRESHING_DAILY.value,
    AfterCloseRunStatus.SYNCING_BOARDS.value,
    AfterCloseRunStatus.CHECKING_COVERAGE.value,
    AfterCloseRunStatus.COMPUTING_FEATURES.value,
    AfterCloseRunStatus.COMPUTING_REVIEW.value,
    AfterCloseRunStatus.COMPUTING_HISTORY.value,
    "watchlist_ready",
]

# [CHANGE-20260831-ADMIN-TIMELINE] 历史 run 中可能出现的真实 legacy 步骤。
# 仅用于事件识别（真实发生过的事件不得被吞掉），不进入 current canonical 默认序列。
_LEGACY_EVENT_STEPS = frozenset({
    AfterCloseRunStatus.PUBLISHING.value,
})

# [Phase8A] 旧四状态 → computing_features 的映射（历史 run 兼容读取）
_LEGACY_STATUS_MAP = {
    AfterCloseRunStatus.CREATING_DSA.value: AfterCloseRunStatus.COMPUTING_FEATURES.value,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: AfterCloseRunStatus.COMPUTING_FEATURES.value,
    AfterCloseRunStatus.QUALITY_GATE.value: AfterCloseRunStatus.COMPUTING_FEATURES.value,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: AfterCloseRunStatus.COMPUTING_FEATURES.value,
}

# 触发新 attempt 开始的边界 step（retry/resume/管理员手动恢复等）
_ATTEMPT_BOUNDARY_STEPS = {
    AfterCloseRunStatus.QUEUED.value,
    "manual_resume",
    "resume",
    "START",
}

# last_completed_step -> 已完成步骤索引（新状态机 + 旧四状态历史映射）
_COMPLETED_STEP_INDEX = {
    None: -1,
    AfterCloseRunStatus.QUEUED.value: -1,
    AfterCloseRunStatus.REFRESHING_DAILY.value: 0,
    AfterCloseRunStatus.SYNCING_BOARDS.value: 1,
    AfterCloseRunStatus.CHECKING_COVERAGE.value: 2,
    AfterCloseRunStatus.COMPUTING_FEATURES.value: 3,
    AfterCloseRunStatus.COMPUTING_REVIEW.value: 4,
    AfterCloseRunStatus.COMPUTING_HISTORY.value: 5,
    AfterCloseRunStatus.SUCCEEDED.value: 6,
    # legacy token：历史 run 的 last_completed_step 可能为 publishing（stock_core 发布步骤）。
    # 与 orchestrator._COMPLETED_STEPS["publishing"] 语义一致：核心（features）已完成，
    # 但 computing_review / computing_history 未完成，故映射回 computing_features 完成度（=3）；
    # 不得因 publishing 不在 current canonical 序列中而丢失历史 run 的真实进度。
    AfterCloseRunStatus.PUBLISHING.value: 3,
    # 旧四状态映射到 computing_features 的索引（历史 run 兼容）
    AfterCloseRunStatus.CREATING_DSA.value: 3,
    AfterCloseRunStatus.WAITING_DSA_WORKER.value: 3,
    AfterCloseRunStatus.QUALITY_GATE.value: 3,
    AfterCloseRunStatus.FEATURE_SNAPSHOT.value: 3,
}

# [AC-TERMINAL-01 2026-08-04] 注意：cancelled / interrupted / partial_success
# 刻意不在 _COMPLETED_STEP_INDEX 中——它们是 run 终态而非流水线步骤。
# 若被误写入 last_completed_step，此处查表 fallback -1，会让所有已完成步骤
# 回退成 pending 并使断点恢复从头重跑。orchestrator 短路块因此必须传 None
# 以保留原检查点。

# 步骤级终态：允许在 partial_success 聚合时如实透出，而不是笼统 pending。
_STEP_TERMINAL_STATUSES = frozenset({
    "succeeded",
    "completed",
    "failed",
    "timed_out",
    "unavailable",
    AfterCloseRunStatus.CANCELLED.value,
    AfterCloseRunStatus.INTERRUPTED.value,
})


def _step_summary_status(
    step_summaries: dict[str, Any],
    step: str,
) -> str | None:
    """从 metadata.step_summary 读取指定步骤的终态字符串。

    [AC-TERMINAL-01 2026-08-04] step_summary 由 orchestrator 落库，
    是 computing_review 等可选步骤 timed_out/unavailable/cancelled/interrupted
    的权威来源；事件聚合不提供该字段。
    """
    summary = step_summaries.get(step)
    if not isinstance(summary, dict):
        return None
    status = summary.get("status")
    if status is None:
        return None
    status_str = str(status)
    # step_summary 里 succeeded 与展示层 completed 等价
    return "completed" if status_str == "succeeded" else status_str


def _normalize_to_shanghai(dt: datetime | None) -> datetime | None:
    """将任意 datetime 统一为 Asia/Shanghai 时区感知 datetime。

    规则：
    - None → None
    - naive（无时区）：按 PostgreSQL 默认 UTC 语义解释，再转为上海时区。
      （理由：PostgreSQL TIMESTAMPTZ 驱动返回带时区，naive 极少出现，
       但遇到时采用最保守 UTC→上海 转换而非简单 attach tz。）
    - UTC aware → 转换到上海
    - 其他时区 → 转换到上海
    - 已是上海 → 直接返回
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # naive: 假设 UTC（DB 默认），再转上海
        utc_dt = dt.replace(tzinfo=_UTC_TZ)
        return utc_dt.astimezone(_SHANGHAI_TZ)
    return dt.astimezone(_SHANGHAI_TZ)


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
    """[TIMELINE-FIX] 时区统一归一化到 Asia/Shanghai 后再转 ISO 字符串。"""
    normalized = _normalize_to_shanghai(value)
    if normalized is None:
        return None
    return normalized.isoformat()


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


def _resolve_event_step(event: JobRunEvent) -> str | None:
    """从事件解析出真正归属的 pipeline step（归一化 + ERROR/START payload 提取）。

    返回 None 表示该事件不归入任何 pipeline step（忽略）。
    """
    step = event.step
    # [Phase8A] 旧四状态映射到 computing_features
    if step in _LEGACY_STATUS_MAP:
        step = _LEGACY_STATUS_MAP[step]
    # ERROR/START 没有固定 step，尝试从 payload 取 step；否则忽略
    if step in ("ERROR", "START"):
        payload_step = (
            event.payload.get("step")
            if isinstance(event.payload, dict)
            else None
        )
        if not isinstance(payload_step, str):
            return None
        step = payload_step
        # payload 里的 step 也要旧状态映射
        if step in _LEGACY_STATUS_MAP:
            step = _LEGACY_STATUS_MAP[step]
    # 只关注 pipeline step
    if (
        step not in _PIPELINE_STEPS
        and step not in _LEGACY_EVENT_STEPS
        and step not in _ATTEMPT_BOUNDARY_STEPS
    ):
        return None
    return step


def _aggregate_step_events(
    events: list[JobRunEvent],
) -> dict[str, dict[str, Any]]:
    """[TIMELINE-FIX v2] 按升序＋attempt 隔离＋步骤转移正确计算每个步骤的启停时间。

    核心规则：
    1. list_events 返回 新→旧；此处先按 created_at 升序（旧→新）后再处理。
    2. 所有 timestamp 统一归一化到 Asia/Shanghai 时区（UTC/naive 先转）。
    3. attempt 边界（_ATTEMPT_BOUNDARY_STEPS：queued/manual_resume/resume/START）
       触发新 attempt，之前未闭合的步骤不再与后续 attempt 混用。
    4. 每个 attempt 内部：步骤顺序是 A→B→C→...
       - A 的 entered_at = A 事件 created_at（该 attempt 下首次出现步骤 A）
       - A 的 finished_at = 下一个步骤事件的 created_at（即 B 事件时间 = A 结束 + B 开始）
       - 最后一个步骤的 finished_at = 之后出现的 succeeded/failed 事件时间
         或同一最后步骤的最新"更新事件"时间（有计数/数据才视为结束）
    5. 有 started_at 无 finished_at → 表示该步骤进行中，duration 保持 None。
    6. 同一 job_run_id 多 attempts：对每个 step，保留"最后一次 attempt 中完整或进行中的时间段"
       （最能代表真实耗时；早期失败 attempt 不覆盖后期成功 attempt）。
    7. ERROR 事件：归到对应 step，仅更新 error_message，不篡改 start/end 时间。
    8. duration 若计算得 ≤ 0（DB 写入偏差或边界事件）→ 置 None，前端显示"未知/进行中"
       而非用 0 或 max(0, x) 掩盖。
    9. counts：从 payload 中提取覆盖率/计数；同一 step 多事件保留最新值。
    """
    if not events:
        return {}

    # (1) 时区归一化 + 按 created_at 升序（旧→新）。用归一化时间排序保证跨时区不会乱序。
    normalized_events: list[tuple[datetime, JobRunEvent, str | None]] = []
    for e in events:
        t = _normalize_to_shanghai(e.created_at)
        if t is None:
            continue
        resolved_step = _resolve_event_step(e)
        normalized_events.append((t, e, resolved_step))
    normalized_events.sort(key=lambda item: item[0])

    # (2) 拆分为多个 attempt（按 boundary step 切开）
    attempts: list[list[tuple[datetime, JobRunEvent, str | None]]] = []
    current_attempt: list[tuple[datetime, JobRunEvent, str | None]] = []
    for item in normalized_events:
        t, e, resolved_step = item
        # 边界 step 开启新 attempt（边界事件本身归入新 attempt 作为启动事件）
        if e.step in _ATTEMPT_BOUNDARY_STEPS:
            if current_attempt:
                attempts.append(current_attempt)
            current_attempt = [item]
        else:
            current_attempt.append(item)
    if current_attempt:
        attempts.append(current_attempt)

    if not attempts:
        # fallback：没有 attempt 边界（老数据或极端情况），所有事件归为一个 attempt
        attempts = [normalized_events]

    # per-step 最终聚合结果：每个 step 取最后一次 attempt 的 segment
    per_step_final: dict[str, dict[str, Any]] = {}

    for attempt in attempts:
        # 收集该 attempt 中每个 pipeline step 的首个出现时间（entered_at）
        # 以及 succeeded/failed 事件（terminal 事件）
        pipeline_transitions: list[tuple[str, datetime]] = []
        terminal_at: datetime | None = None
        step_counts: dict[str, dict[str, Any]] = {}
        step_error: dict[str, str] = {}

        for t, e, resolved_step in attempt:
            # 收集 ERROR 信息（无论 step 是否为 pipeline step，只要有 payload.step）
            if e.level == "error" and isinstance(e.payload, dict):
                err_step = e.payload.get("step") if isinstance(e.payload, dict) else None
                if isinstance(err_step, str):
                    if err_step in _LEGACY_STATUS_MAP:
                        err_step = _LEGACY_STATUS_MAP[err_step]
                    if (
                        err_step in _PIPELINE_STEPS
                        or err_step in _LEGACY_EVENT_STEPS
                    ):
                        step_error[err_step] = e.message or step_error.get(err_step, "")

            # terminal 事件：succeeded/failed 视为结束
            if e.step == AfterCloseRunStatus.SUCCEEDED.value:
                terminal_at = t
                continue
            if e.step == AfterCloseRunStatus.FAILED.value:
                terminal_at = t
                continue

            if not resolved_step or (
                resolved_step not in _PIPELINE_STEPS
                and resolved_step not in _LEGACY_EVENT_STEPS
            ):
                continue

            # 同一 step 的后续事件更新 counts；不重复记录进入时间
            if resolved_step not in [s for s, _ in pipeline_transitions]:
                pipeline_transitions.append((resolved_step, t))

            # counts 从 payload 提取（用最新）
            if isinstance(e.payload, dict):
                counts: dict[str, Any] = {}
                for key in (
                    "coverage", "covered", "total", "succeeded_count", "failed_count",
                    "snapshot_count", "partial_failed_count", "expected_count",
                ):
                    if key in e.payload:
                        counts[key] = e.payload[key]
                if counts:
                    step_counts[resolved_step] = counts

        # 基于 pipeline_transitions + terminal_at 计算每个 step 的 start/end/duration
        n_trans = len(pipeline_transitions)
        for i, (step, entered_at) in enumerate(pipeline_transitions):
            finished_at: datetime | None = None
            if i + 1 < n_trans:
                # 下一步骤的进入时间 = 当前步骤结束时间
                finished_at = pipeline_transitions[i + 1][1]
            elif terminal_at is not None:
                # 最后一步且存在 terminal 事件
                finished_at = terminal_at
            # 没 finished_at 说明仍在运行中

            # 计算 duration：仅当两端都存在且 finished > entered 才写入
            duration_seconds: float | None = None
            if entered_at is not None and finished_at is not None:
                delta = (finished_at - entered_at).total_seconds()
                # [HARD RULE] 不用 max(0,x) 掩盖；偏差≤0 时置 None，前端显示"进行中/未知"
                if delta > 0:
                    duration_seconds = delta
                else:
                    # 非正耗时：尝试从同一 step 的多事件估算（最后事件 - 首个事件）
                    step_end_estimate: datetime | None = None
                    for t, _e, rs in attempt:
                        if rs == step:
                            step_end_estimate = t  # 升序，最后一次
                    if step_end_estimate is not None and step_end_estimate > entered_at:
                        delta2 = (step_end_estimate - entered_at).total_seconds()
                        if delta2 > 0:
                            duration_seconds = delta2
                            finished_at = step_end_estimate

            counts = step_counts.get(step, {})
            error_message = step_error.get(step)

            # [Attempt 覆盖规则] 总是保存当前 attempt 的 segment
            # （后面 attempt 会覆盖前面的；最后一次有效 attempt 保留）
            per_step_final[step] = {
                "started_at": entered_at,
                "finished_at": finished_at,
                "duration_seconds": duration_seconds,
                "error_message": error_message,
                "counts": counts,
            }

    return per_step_final


def _compute_step_states(
    job_run: SchedulerJobRun | None,
    events: list[JobRunEvent],
    watchlist_ready: bool,
    snapshot_summary: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """根据 job_run 状态、事件、watchlist_ready、snapshot_summary 计算 7 步骤状态（含 computing_review）。

    [TIMELINE-FIX] duration_seconds 直接来自 _aggregate_step_events 严格校验过的结果；
    不再从 (finished_at - started_at) 这里二次计算，避免两端事件来源跨 attempt/时区 出偏差。
    若 _aggregate_step_events 返回的 duration=None 且两端都有，但 finished<started，
    则标记 warnings="invalid_order"，供前端显示"未知"而非 0。
    """
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
                "warnings": None,
            }
            for step in _PIPELINE_STEPS
        ]

    meta = _parse_metadata(job_run)
    orchestrator_status = meta.get("orchestrator_status")
    # [Phase8A] 旧四状态映射到 computing_features（历史 run 兼容读取）
    if orchestrator_status in _LEGACY_STATUS_MAP:
        orchestrator_status = _LEGACY_STATUS_MAP[orchestrator_status]
    last_completed_step = meta.get("last_completed_step")
    completed_idx = _COMPLETED_STEP_INDEX.get(last_completed_step, -1)
    step_events = _aggregate_step_events(events)
    # [AC-TERMINAL-01 2026-08-04] step_summary 是各步骤终态的权威来源
    # （由 orchestrator._persist_step_summary 落库）。
    raw_step_summary = meta.get("step_summary")
    step_summaries: dict[str, Any] = (
        raw_step_summary if isinstance(raw_step_summary, dict) else {}
    )

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
                    # [Phase8A] 映射旧四状态
                    if failed_step in _LEGACY_STATUS_MAP:
                        failed_step = _LEGACY_STATUS_MAP[failed_step]
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
        duration = stats.get("duration_seconds")

        # [TIMELINE-FIX] 附加跨 attempt/时区的顺序异常标记（仅诊断，不掩盖为 0）
        warnings_list: list[str] = []
        if (
            started_at is not None and finished_at is not None
            and duration is None
        ):
            # 两端都有但 duration 没计算出来（顺序异常或 delta≤0）
            warnings_list.append("invalid_order_or_zero_duration")

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
        elif job_run.status == AfterCloseRunStatus.INTERRUPTED.value:
            # [Repair] orchestrator 已中断但 snapshot 仍在 running，
            # computing_features 步骤应显示 running，提示"快照计算失联/待修复"。
            # [Phase8A] 旧 run 的 feature_snapshot 已映射到 computing_features
            if (
                step == AfterCloseRunStatus.COMPUTING_FEATURES.value
                and snapshot_summary is not None
                and snapshot_summary.get("status") == "running"
            ):
                step_status = "running"
            elif idx <= completed_idx:
                step_status = "completed"
            elif idx == current_idx:
                # 中断发生在该步骤上（如 computing_review）
                step_status = AfterCloseRunStatus.INTERRUPTED.value
            else:
                step_status = "pending"
        elif job_run.status == AfterCloseRunStatus.CANCELLED.value:
            # [AC-TERMINAL-01 2026-08-04] 取消不得让已完成步骤回退为 pending。
            # 检查点（last_completed_step）已由短路块保留为真实完成的步骤。
            if idx <= completed_idx:
                step_status = "completed"
            elif idx == current_idx:
                step_status = AfterCloseRunStatus.CANCELLED.value
            else:
                step_status = "pending"
        elif job_run.status == AfterCloseRunStatus.PARTIAL_SUCCESS.value:
            # [AC-TERMINAL-01 2026-08-04] 部分成功：核心步骤已完成，
            # 可选步骤（computing_review 等）按 step_summary 真实终态显示，
            # 不得笼统显示 pending 掩盖 failed/timed_out/unavailable。
            summary_status = _step_summary_status(step_summaries, step)
            if summary_status in _STEP_TERMINAL_STATUSES:
                step_status = summary_status
            elif idx <= completed_idx:
                step_status = "completed"
            else:
                step_status = "pending"
        else:
            # queued 或其他：已完成步骤显示 completed，当前及之后 pending
            step_status = "completed" if idx <= completed_idx else "pending"

        # running 状态：finished_at/duration 应置 None（即使聚合逻辑因异常标记了也清空）
        if step_status == "running":
            finished_at = None
            duration = None
            # 保留 started_at；清除 invalid_order warning（running 没结束是正常）
            warnings_list = [w for w in warnings_list if w != "invalid_order_or_zero_duration"]

        steps.append({
            "step": step,
            "status": step_status,
            "started_at": _format_dt(started_at),
            "finished_at": _format_dt(finished_at),
            "duration_seconds": duration,
            "counts": stats.get("counts", {}),
            "error_message": stats.get("error_message"),
            "warnings": warnings_list if warnings_list else None,
        })

    # [CHANGE-20260831-ADMIN-TIMELINE] legacy 兼容：历史 run 若真实产生过 publishing 事件，
    # 必须如实呈现（不得吞掉）。publishing 不再是 current canonical 默认步骤，
    # 因此仅在真实事件存在时补入，位置沿用 legacy DAG（computing_features 之后）。
    if AfterCloseRunStatus.PUBLISHING.value in step_events:
        pub_stats = step_events[AfterCloseRunStatus.PUBLISHING.value]
        pub_started = pub_stats.get("started_at")
        pub_finished = pub_stats.get("finished_at")
        if pub_finished is not None:
            pub_status = "completed"
        elif pub_started is not None:
            pub_status = "running"
        else:
            pub_status = "pending"
        steps.insert(
            _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_FEATURES.value) + 1,
            {
                "step": AfterCloseRunStatus.PUBLISHING.value,
                "status": pub_status,
                "started_at": _format_dt(pub_started),
                "finished_at": _format_dt(pub_finished),
                "duration_seconds": (
                    None
                    if pub_status == "running"
                    else pub_stats.get("duration_seconds")
                ),
                "counts": pub_stats.get("counts", {}),
                "error_message": pub_stats.get("error_message"),
                "warnings": None,
            },
        )

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
    # [AC-TERMINAL-01 2026-08-04] 终态如实透出，不再落到 not_started。
    # partial_success：核心已发布、可选阶段降级；cancelled：管理员主动取消；
    # interrupted：Worker 被接管。三者都不是"未开始"。
    if job_run.status == AfterCloseRunStatus.PARTIAL_SUCCESS.value:
        return AfterCloseRunStatus.PARTIAL_SUCCESS.value
    if job_run.status == AfterCloseRunStatus.CANCELLED.value:
        return AfterCloseRunStatus.CANCELLED.value
    if job_run.status == AfterCloseRunStatus.INTERRUPTED.value:
        return AfterCloseRunStatus.INTERRUPTED.value
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

    data_freshness = await _compute_data_freshness(db, now)
    steps = _compute_step_states(
        job_run, events, watchlist_ready, snapshot_summary,
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
    assert "computing_features" in _PIPELINE_STEPS
    assert "syncing_boards" in _PIPELINE_STEPS
    # [CHANGE-20260801-REVIEW-CLOSURE] 8 步序列（publishing→computing_history→computing_review→watchlist_ready）
    assert "computing_review" in _PIPELINE_STEPS, (
        "_PIPELINE_STEPS 必须包含 computing_review（复盘阶段）"
    )
    assert "computing_history" in _PIPELINE_STEPS, (
        "_PIPELINE_STEPS 必须包含 computing_history（历史状态推进阶段）"
    )
    assert len(_PIPELINE_STEPS) == 7, (
        f"7 步 canonical 序列（含 computing_history / computing_review），实际={len(_PIPELINE_STEPS)}"
    )
    # publishing 不再为 current canonical DAG 合成，但历史真实事件不得被吞掉
    assert AfterCloseRunStatus.PUBLISHING.value not in _PIPELINE_STEPS, (
        "publishing 不再是 current canonical 步骤，不得为当前 run 合成"
    )
    assert AfterCloseRunStatus.PUBLISHING.value in _LEGACY_EVENT_STEPS, (
        "publishing 必须保留在 _LEGACY_EVENT_STEPS 中，避免历史真实事件被吞掉"
    )
    # 顺序：computing_review 在 computing_features 之后、computing_history 之前
    feat_idx = _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_FEATURES.value)
    rev_idx = _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_REVIEW.value)
    hist_idx = _PIPELINE_STEPS.index(AfterCloseRunStatus.COMPUTING_HISTORY.value)
    wl_idx = _PIPELINE_STEPS.index("watchlist_ready")
    assert feat_idx < rev_idx < hist_idx < wl_idx, (
        f"顺序必须为 computing_features < computing_review < computing_history < watchlist_ready："
        f"feat={feat_idx}, rev={rev_idx}, hist={hist_idx}, wl={wl_idx}"
    )
    # 旧四状态映射到 computing_features 索引（=3）
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.WAITING_DSA_WORKER.value] == 3
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.FEATURE_SNAPSHOT.value] == 3
    # 新状态机索引（7 步：computing_review=4, computing_history=5, succeeded=6）
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.COMPUTING_FEATURES.value] == 3
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.COMPUTING_REVIEW.value] == 4
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.COMPUTING_HISTORY.value] == 5
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.SUCCEEDED.value] == 6
    # legacy publishing token：映射回 computing_features 完成度（核心已完成，review/history 未完成）
    assert _COMPLETED_STEP_INDEX[AfterCloseRunStatus.PUBLISHING.value] == 3
    # 旧四状态映射
    assert _LEGACY_STATUS_MAP[AfterCloseRunStatus.CREATING_DSA.value] == "computing_features"
    # 时区归一化 + 负耗时防御：_normalize_to_shanghai 基本行为
    naive_dt = datetime(2026, 7, 31, 15, 0, 0)
    sh_from_naive = _normalize_to_shanghai(naive_dt)
    assert sh_from_naive is not None
    assert sh_from_naive.tzinfo is not None
    utc_dt = datetime(2026, 7, 31, 7, 0, 0, tzinfo=UTC)  # UTC 07:00 = Shanghai 15:00
    sh_from_utc = _normalize_to_shanghai(utc_dt)
    assert sh_from_utc is not None
    assert sh_from_utc.hour == 15, (
        f"UTC 07:00 应转换为上海 15:00，实际 hour={sh_from_utc.hour}"
    )
    print("after_close_pipeline_service 常量与映射自测通过（含 computing_review 7 步 + 时区归一化）")
