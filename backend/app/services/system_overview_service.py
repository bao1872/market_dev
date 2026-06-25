# -*- coding: utf-8 -*-
"""系统概览服务 - /admin/system-overview 业务逻辑层。

从 admin_membership.py 路由抽出数据查询逻辑，新增市场阶段/监控运行时/盘后流水线状态。

设计原则：
- 单一数据源：所有状态判定基于 DB 实时查询，不引用历史/昨日数据满足今日状态
- 时区安全：使用注入的 now（上海时区）进行所有时间比较，TIMESTAMPTZ 自动转换
- 测试友好：get_system_overview 接受可选 now 参数，便于注入固定时间

用法：
    from app.services.system_overview_service import get_system_overview
    overview = await get_system_overview(db)

    # 测试时注入固定时间
    from app.core.time import SHANGHAI_TZ
    from datetime import datetime
    fixed_now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI_TZ)
    overview = await get_system_overview(db, now=fixed_now)

副作用：无（只读查询，不写库表/不改文件）。
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.strategy_keys import DSA_SELECTOR
from app.core.time import SHANGHAI_TZ, now_shanghai
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyRun
from app.models.watchlist import UserWatchlistItem
from app.models.worker_heartbeat import WorkerHeartbeat
from app.schemas.scheduler_job_run import RecentSchedulerJobSummary
from app.schemas.system_overview import (
    MONITOR_STATUS_DELAYED,
    MONITOR_STATUS_FAILED,
    MONITOR_STATUS_IDLE_EXPECTED,
    MONITOR_STATUS_NOT_APPLICABLE,
    MONITOR_STATUS_RUNNING,
    MONITOR_STATUS_SESSION_COMPLETED,
    MONITOR_STATUS_WORKER_OFFLINE,
    PIPELINE_STATUS_BARS_FAILED,
    PIPELINE_STATUS_BARS_RUNNING,
    PIPELINE_STATUS_DSA_COMPLETED,
    PIPELINE_STATUS_DSA_FAILED,
    PIPELINE_STATUS_DSA_QUEUED,
    PIPELINE_STATUS_DSA_RUNNING,
    PIPELINE_STATUS_NOT_STARTED,
    PIPELINE_STATUS_PUBLISHED,
    PIPELINE_STATUS_STALE,
    PIPELINE_STATUS_WAITING_DSA,
    WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT,
    WAITING_DSA_REASON_NO_RELEASED_VERSION,
    WAITING_DSA_REASON_NO_RUN_CREATED,
    WAITING_DSA_REASON_PUBLISH_FAILED,
    WAITING_DSA_REASON_QUALITY_GATE_FAILED,
    WAITING_DSA_REASON_QUEUED_NOT_CLAIMED,
    WAITING_DSA_REASON_RUN_FAILED,
    WAITING_DSA_SUGGESTIONS,
)
from app.services.market_status_service import (
    MARKET_SESSION_AFTERNOON,
    MARKET_SESSION_CLOSED,
    MARKET_SESSION_LUNCH,
    MARKET_SESSION_MORNING,
    MARKET_SESSION_NON_TRADING_DAY,
    MARKET_SESSION_PRE_OPEN,
    compute_market_session,
)

logger = logging.getLogger(__name__)

# [SystemOverview] - 心跳超时阈值（秒）：超过此值判定 worker 离线
HEARTBEAT_OFFLINE_THRESHOLD = 90

# [SystemOverview] - 数据新鲜度阈值（秒）：超过此值判定数据延迟
FRESHNESS_DELAYED_THRESHOLD = 180

# [SystemOverview] - worker 健康心跳窗口（秒）：用于基础字段 worker_health 判定
WORKER_HEALTH_WINDOW = 120

# [SystemOverview] - DSA 排队超时阈值（秒）：queued 超 30 分钟未被 worker 领取视为异常
WAITING_DSA_QUEUED_TIMEOUT = 1800

# [SystemOverview] - DSA 覆盖率阈值：低于此值判定为 DATA_COVERAGE_INSUFFICIENT
WAITING_DSA_COVERAGE_THRESHOLD = 0.9


async def get_system_overview(
    db: AsyncSession,
    now: datetime | None = None,
) -> dict[str, Any]:
    """系统概览 - 管理员仪表盘数据。

    Args:
        db: 异步数据库会话
        now: 上海时区当前时间（测试时可注入固定时间，默认 now_shanghai()）

    Returns:
        包含 17 个字段的系统概览字典（12 基础 + 5 新增）
    """
    if now is None:
        now = now_shanghai()

    business_date_obj = now.date()
    business_date_str = business_date_obj.isoformat()

    # 基础字段（12 个，向后兼容）
    base_fields = await _compute_base_fields(db, now)

    # market_session
    # [SystemOverview] - 延迟导入避免模块加载时触发 DB 配置初始化
    from app.services.calendar_service import is_trading_day_async
    is_trading_day = await is_trading_day_async(db, business_date_obj)
    market_session = compute_market_session(now, is_trading_day)

    # monitor_runtime
    monitor_runtime = await _compute_monitor_runtime(
        db, now, market_session, business_date_obj, business_date_str
    )

    # after_close_pipeline
    after_close_pipeline = await _compute_after_close_pipeline(
        db, now, business_date_obj, business_date_str
    )

    return {
        **base_fields,
        "server_time": now.isoformat(),
        "business_date": business_date_str,
        "market_session": market_session,
        "monitor_runtime": monitor_runtime,
        "after_close_pipeline": after_close_pipeline,
    }


async def _compute_base_fields(db: AsyncSession, now: datetime) -> dict[str, Any]:
    """计算 12 个基础字段（从原 admin_membership.py 迁移）。

    Args:
        db: 异步数据库会话
        now: 上海时区当前时间

    Returns:
        包含 12 个基础字段的字典
    """
    # 1. active_users: 有活跃自选股的去重用户数
    active_users_stmt = select(func.count(func.distinct(UserWatchlistItem.user_id))).where(
        UserWatchlistItem.active.is_(True),
    )
    active_users = await db.scalar(active_users_stmt) or 0

    # 2. distinct_monitored_instruments: 活跃自选股去重标的数
    distinct_instruments_stmt = select(
        func.count(func.distinct(UserWatchlistItem.instrument_id)),
    ).where(
        UserWatchlistItem.active.is_(True),
    )
    distinct_monitored_instruments = await db.scalar(distinct_instruments_stmt) or 0

    # 3. evaluations_last_minute: 最近 1 分钟完成的评估数
    one_minute_ago = now - timedelta(minutes=1)
    eval_last_min_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.calculated_at >= one_minute_ago,
        MonitorEvaluation.status.in_(["SUCCEEDED", "FAILED"]),
    )
    evaluations_last_minute = await db.scalar(eval_last_min_stmt) or 0

    # 4. evaluations_success_rate: 已完成评估的成功率
    total_completed_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status.in_(["SUCCEEDED", "FAILED", "DEAD"]),
    )
    total_completed = await db.scalar(total_completed_stmt) or 0
    succeeded_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status == "SUCCEEDED",
    )
    succeeded_count = await db.scalar(succeeded_stmt) or 0
    evaluations_success_rate = round(succeeded_count / total_completed, 4) if total_completed > 0 else 0.0

    # 5. failed_retry_count: 当前 FAILED 状态且可重试的评估数
    failed_retry_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.status == "FAILED",
    )
    failed_retry_count = await db.scalar(failed_retry_stmt) or 0

    # 6. latest_selector_run: dsa_selector 最近一次运行
    latest_selector_run = await _compute_latest_selector_run(db)

    # 7. queue_backlog: queued 状态的 StrategyRun 数量
    queued_count_stmt = select(func.count(StrategyRun.id)).where(
        StrategyRun.status == "queued",
    )
    queue_backlog = await db.scalar(queued_count_stmt) or 0

    # 8. worker_health / scheduler_health: 基于 worker_heartbeats 实时查询
    heartbeat_stmt = select(WorkerHeartbeat)
    heartbeats_result = await db.execute(heartbeat_stmt)
    hb_list = heartbeats_result.scalars().all()

    active_workers = [
        hb for hb in hb_list
        if hb.status == "running" and (now - hb.heartbeat_at).total_seconds() < WORKER_HEALTH_WINDOW
    ]
    all_running_workers = [hb for hb in hb_list if hb.status == "running"]

    scheduler_names = {hb.worker_name for hb in active_workers if "scheduler" in hb.worker_name}

    worker_health = "healthy" if active_workers else ("degraded" if all_running_workers else "unknown")
    scheduler_health = "healthy" if scheduler_names else ("degraded" if all_running_workers else "unknown")

    # 9. recent_scheduler_jobs: 最近 24 小时内各 job_name 最新一条记录
    one_day_ago = now - timedelta(days=1)
    recent_jobs_subq = (
        select(
            SchedulerJobRun,
            func.row_number().over(
                partition_by=SchedulerJobRun.job_name,
                order_by=SchedulerJobRun.created_at.desc(),
            ).label("rn"),
        )
        .where(SchedulerJobRun.created_at >= one_day_ago)
        .subquery()
    )
    recent_jobs_stmt = select(recent_jobs_subq).where(recent_jobs_subq.c.rn == 1)
    recent_jobs_result = await db.execute(recent_jobs_stmt)
    recent_scheduler_jobs = [
        RecentSchedulerJobSummary(
            job_name=row.job_name,
            status=row.status,
            business_date=row.business_date,
            started_at=row.started_at,
            finished_at=row.finished_at,
            progress=row.progress,
            succeeded_count=row.succeeded_count,
            failed_count=row.failed_count,
            error_message=row.error_message,
        ).model_dump()
        for row in recent_jobs_result
    ]

    return {
        "active_users": active_users,
        "distinct_monitored_instruments": distinct_monitored_instruments,
        "evaluations_last_minute": evaluations_last_minute,
        "evaluations_success_rate": evaluations_success_rate,
        "notification_delivery_rate": 0.0,
        "queue_backlog": queue_backlog,
        "failed_retry_count": failed_retry_count,
        "latest_selector_run": latest_selector_run,
        "worker_health": worker_health,
        "scheduler_health": scheduler_health,
        "recent_scheduler_jobs": recent_scheduler_jobs,
        "recent_anomalies": [],
    }


async def _compute_latest_selector_run(db: AsyncSession) -> dict[str, Any] | None:
    """查询 dsa_selector 最近一次运行。

    Args:
        db: 异步数据库会话

    Returns:
        运行摘要字典或 None
    """
    selector_def_stmt = select(StrategyDefinition.id).where(
        StrategyDefinition.strategy_key == DSA_SELECTOR,
    )
    selector_def_id = await db.scalar(selector_def_stmt)
    if selector_def_id is None:
        return None

    version_ids_stmt = select(StrategyVersion.id).where(
        StrategyVersion.strategy_definition_id == selector_def_id,
    )
    version_ids_result = await db.execute(version_ids_stmt)
    version_ids = [row[0] for row in version_ids_result.all()]
    if not version_ids:
        return None

    run_stmt = (
        select(StrategyRun)
        .where(StrategyRun.strategy_version_id.in_(version_ids))
        .order_by(StrategyRun.started_at.desc())
        .limit(1)
    )
    run_result = await db.execute(run_stmt)
    run = run_result.scalar_one_or_none()
    if run is None:
        return None

    return {
        "id": str(run.id),
        "status": run.status,
        "trade_date": run.trade_date,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "total_instruments": run.total_instruments,
        "succeeded_count": run.succeeded_count,
        "failed_count": run.failed_count,
    }


async def _compute_monitor_runtime(
    db: AsyncSession,
    now: datetime,
    market_session: str,
    business_date_obj: Any,
    business_date_str: str,
) -> dict[str, Any]:
    """计算监控运行时状态。

    判定规则（严格按 advice.md）：
    1. 只查 worker_name='monitor_scheduler' 的心跳（禁止全表汇总）
    2. 心跳 > 90s → WORKER_OFFLINE
    3. NON_TRADING_DAY → NOT_APPLICABLE
    4. MORNING/AFTERNOON: freshness > 180 → DELAYED，否则 RUNNING
    5. LUNCH_BREAK → IDLE_EXPECTED
    6. MARKET_CLOSED: 查下午盘 job，succeeded+failed=0 → SESSION_COMPLETED
    7. PRE_OPEN → IDLE_EXPECTED

    Args:
        db: 异步数据库会话
        now: 上海时区当前时间
        market_session: 市场阶段枚举
        business_date_obj: 业务日期 date 对象
        business_date_str: 业务日期字符串

    Returns:
        监控运行时状态字典
    """
    # [monitor_runtime] - 只查 monitor_scheduler 心跳（禁止全表汇总）
    hb_stmt = (
        select(WorkerHeartbeat)
        .where(WorkerHeartbeat.worker_name == "monitor_scheduler")
        .order_by(WorkerHeartbeat.heartbeat_at.desc())
        .limit(1)
    )
    hb_result = await db.execute(hb_stmt)
    hb = hb_result.scalar_one_or_none()

    heartbeat_at = hb.heartbeat_at if hb else None
    heartbeat_age_seconds: int | None = None
    if heartbeat_at is not None:
        heartbeat_age_seconds = int((now - heartbeat_at).total_seconds())

    # [monitor_runtime] - 当日 monitor 评估统计（按上海业务日期过滤）
    start_of_day = datetime.combine(business_date_obj, time.min, tzinfo=SHANGHAI_TZ)
    end_of_day = datetime.combine(
        business_date_obj + timedelta(days=1), time.min, tzinfo=SHANGHAI_TZ
    )

    # evaluated_count: 当日 monitor_evaluations 总数
    evaluated_count_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.calculated_at >= start_of_day,
        MonitorEvaluation.calculated_at < end_of_day,
    )
    evaluated_count = await db.scalar(evaluated_count_stmt) or 0

    # failed_count: 当日 monitor_evaluations status=FAILED 的数量
    failed_count_stmt = select(func.count()).select_from(MonitorEvaluation).where(
        MonitorEvaluation.calculated_at >= start_of_day,
        MonitorEvaluation.calculated_at < end_of_day,
        MonitorEvaluation.status == "FAILED",
    )
    failed_count = await db.scalar(failed_count_stmt) or 0

    # last_cycle_at: 当日最近一次 monitor 评估的 calculated_at
    last_cycle_stmt = select(func.max(MonitorEvaluation.calculated_at)).where(
        MonitorEvaluation.calculated_at >= start_of_day,
        MonitorEvaluation.calculated_at < end_of_day,
    )
    last_cycle_at = await db.scalar(last_cycle_stmt)

    # last_source_bar_time: 当日最近一次 monitor 评估的 source_bar_time
    last_bar_stmt = select(func.max(MonitorEvaluation.source_bar_time)).where(
        MonitorEvaluation.calculated_at >= start_of_day,
        MonitorEvaluation.calculated_at < end_of_day,
    )
    last_source_bar_time = await db.scalar(last_bar_stmt)

    # freshness_seconds: now - last_source_bar_time
    freshness_seconds: int | None = None
    if last_source_bar_time is not None:
        freshness_seconds = int((now - last_source_bar_time).total_seconds())

    # session_job_status: 当日最新 monitor_scheduler job_run 的 status
    job_stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == "monitor_scheduler",
            SchedulerJobRun.business_date == business_date_str,
        )
        .order_by(SchedulerJobRun.started_at.desc())
        .limit(1)
    )
    job_result = await db.execute(job_stmt)
    session_job = job_result.scalar_one_or_none()
    session_job_status = session_job.status if session_job else None

    # session_label
    if market_session == MARKET_SESSION_MORNING:
        session_label = "morning"
    elif market_session == MARKET_SESSION_AFTERNOON:
        session_label = "afternoon"
    else:
        session_label = None

    # [monitor_runtime] - 状态判定
    status = _determine_monitor_status(
        market_session, heartbeat_age_seconds, freshness_seconds,
        db, now, business_date_str,
    )
    # MARKET_CLOSED 需要异步查下午盘 job，单独处理
    if market_session == MARKET_SESSION_CLOSED:
        status = await _determine_market_closed_status(db, business_date_str)

    return {
        "status": status,
        "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at else None,
        "heartbeat_age_seconds": heartbeat_age_seconds,
        "business_date": business_date_str,
        "session_label": session_label,
        "session_job_status": session_job_status,
        "last_cycle_at": last_cycle_at.isoformat() if last_cycle_at else None,
        "last_source_bar_time": last_source_bar_time.isoformat() if last_source_bar_time else None,
        "evaluated_count": evaluated_count,
        "failed_count": failed_count,
        "freshness_seconds": freshness_seconds,
    }


def _determine_monitor_status(
    market_session: str,
    heartbeat_age_seconds: int | None,
    freshness_seconds: int | None,
    db: AsyncSession,
    now: datetime,
    business_date_str: str,
) -> str:
    """判定监控运行时状态（非 MARKET_CLOSED 场景）。

    Args:
        market_session: 市场阶段枚举
        heartbeat_age_seconds: 心跳年龄（秒）
        freshness_seconds: 数据新鲜度（秒）
        db: 数据库会话（未使用，保留用于未来扩展）
        now: 当前时间（未使用，保留用于未来扩展）
        business_date_str: 业务日期字符串（未使用，保留用于未来扩展）

    Returns:
        监控状态枚举字符串
    """
    if market_session == MARKET_SESSION_NON_TRADING_DAY:
        return MONITOR_STATUS_NOT_APPLICABLE

    if market_session in (MARKET_SESSION_PRE_OPEN, MARKET_SESSION_LUNCH):
        return MONITOR_STATUS_IDLE_EXPECTED

    if market_session in (MARKET_SESSION_MORNING, MARKET_SESSION_AFTERNOON):
        # [monitor_runtime] - 无心跳或心跳超时 → WORKER_OFFLINE
        # heartbeat_age_seconds=None 表示无心跳记录，盘中视为 worker 离线
        if heartbeat_age_seconds is None or heartbeat_age_seconds > HEARTBEAT_OFFLINE_THRESHOLD:
            return MONITOR_STATUS_WORKER_OFFLINE
        # 数据延迟 → DELAYED
        if freshness_seconds is not None and freshness_seconds > FRESHNESS_DELAYED_THRESHOLD:
            return MONITOR_STATUS_DELAYED
        # 正常运行（含 freshness=None 即尚无数据的情况）
        return MONITOR_STATUS_RUNNING

    # MARKET_CLOSED 由调用方异步处理
    return MONITOR_STATUS_IDLE_EXPECTED


async def _determine_market_closed_status(
    db: AsyncSession,
    business_date_str: str,
) -> str:
    """判定 MARKET_CLOSED 时的监控状态。

    查当日 afternoon session 的 scheduler_job_run：
    - succeeded 且 failed_count=0 → SESSION_COMPLETED
    - failed/interrupted → FAILED
    - 无记录 → IDLE_EXPECTED（今日下午盘无运行记录）

    Args:
        db: 异步数据库会话
        business_date_str: 业务日期字符串

    Returns:
        监控状态枚举字符串
    """
    # [monitor_runtime] - 查下午盘 job（session_label 存于 metadata_json）
    afternoon_job_stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == "monitor_scheduler",
            SchedulerJobRun.business_date == business_date_str,
            SchedulerJobRun.metadata_json.like('%"session_label": "afternoon"%'),
        )
        .order_by(SchedulerJobRun.started_at.desc())
        .limit(1)
    )
    afternoon_result = await db.execute(afternoon_job_stmt)
    afternoon_job = afternoon_result.scalar_one_or_none()

    if afternoon_job is None:
        # 无下午盘记录 → IDLE_EXPECTED
        return MONITOR_STATUS_IDLE_EXPECTED

    if afternoon_job.status == "succeeded" and (afternoon_job.failed_count or 0) == 0:
        return MONITOR_STATUS_SESSION_COMPLETED

    if afternoon_job.status == "failed":
        return MONITOR_STATUS_FAILED

    # running 或其他状态 → IDLE_EXPECTED（盘后不应 running）
    return MONITOR_STATUS_IDLE_EXPECTED


async def _compute_data_freshness(db: AsyncSession, now: datetime) -> dict[str, Any]:
    """[SystemOverview] - 计算数据新鲜度子结构（行情 + 选股两区块，Phase 9）。

    独立于流水线状态判定，始终基于 DB 实时查询，反映行情与选股的最新数据落盘情况。
    管理员可在任何时段查看数据新鲜度，不依赖盘后流水线是否启动。
    """
    from app.models.bar import Bar15Min, Bar60Min, BarDaily
    from app.models.calendar import TradingCalendar

    # ===== bars 子结构 =====
    # [data_freshness.bars] - 最新日线交易日
    # [Phase9] - 描述: 过滤 trade_date <= today，避免占位/未来日期（如 2099-12-31）
    # 干扰 latest_daily_trade_date 语义（"行情数据最后更新到哪一天"）
    today = now.date()
    latest_daily = await db.scalar(
        select(func.max(BarDaily.trade_date)).where(BarDaily.trade_date <= today)
    )
    latest_daily_trade_date = latest_daily if latest_daily is not None else None

    # [data_freshness.bars] - 日线覆盖率（基于 latest_daily_trade_date，复用现有 _compute_bars_coverage）
    daily_coverage: float | None = None
    if latest_daily_trade_date is not None:
        daily_coverage = await _compute_bars_coverage(db, latest_daily_trade_date)

    # [data_freshness.bars] - 最新 15m/60m bar 时间
    latest_15m = await db.scalar(select(func.max(Bar15Min.trade_time)))
    latest_60m = await db.scalar(select(func.max(Bar60Min.trade_time)))

    # [data_freshness.bars] - 最近的 bars_scheduler succeeded 任务 id
    last_success_job = await db.scalar(
        select(SchedulerJobRun.id)
        .where(
            SchedulerJobRun.job_name == "bars_scheduler",
            SchedulerJobRun.status == "succeeded",
        )
        .order_by(SchedulerJobRun.started_at.desc())
        .limit(1)
    )

    # [data_freshness.bars] - 最近交易日（trading_calendar WHERE is_trading_day=true AND trade_date <= today）
    latest_trading_day = await db.scalar(
        select(func.max(TradingCalendar.trade_date)).where(
            TradingCalendar.is_trading_day.is_(True),
            TradingCalendar.market == "A",
            TradingCalendar.trade_date <= today,
        )
    )

    # [data_freshness.bars] - is_behind_latest_trade_date: latest_daily < latest_trading_day
    is_behind = False
    if latest_daily_trade_date is not None and latest_trading_day is not None:
        is_behind = latest_daily_trade_date < latest_trading_day

    bars_freshness = {
        "latest_daily_trade_date": (
            latest_daily_trade_date.isoformat() if latest_daily_trade_date else None
        ),
        "daily_coverage": daily_coverage,
        "latest_15m_bar_time": latest_15m.isoformat() if latest_15m else None,
        "latest_60m_bar_time": latest_60m.isoformat() if latest_60m else None,
        "last_success_job_id": str(last_success_job) if last_success_job else None,
        "is_behind_latest_trade_date": is_behind,
    }

    # ===== strategy 子结构 =====
    # [data_freshness.strategy] - 最新计算交易日（所有状态）
    latest_compute = await db.scalar(select(func.max(StrategyRun.trade_date)))

    # [data_freshness.strategy] - 最新发布交易日（status='published'）
    latest_published = await db.scalar(
        select(func.max(StrategyRun.trade_date)).where(
            StrategyRun.status == "published"
        )
    )

    # [data_freshness.strategy] - 最近一条 strategy_runs
    # 排序：trade_date DESC, attempt_no DESC, started_at DESC NULLS LAST（避免 started_at 为 NULL 时排序不确定）
    latest_run_stmt = (
        select(StrategyRun)
        .order_by(
            StrategyRun.trade_date.desc(),
            StrategyRun.attempt_no.desc(),
            StrategyRun.started_at.desc().nullslast(),
        )
        .limit(1)
    )
    latest_run_result = await db.execute(latest_run_stmt)
    latest_run = latest_run_result.scalar_one_or_none()

    strategy_freshness = {
        "latest_compute_trade_date": (
            latest_compute.isoformat() if latest_compute else None
        ),
        "latest_published_trade_date": (
            latest_published.isoformat() if latest_published else None
        ),
        "strategy_run_id": str(latest_run.id) if latest_run else None,
        "status": latest_run.status if latest_run else None,
        "total_instruments": latest_run.total_instruments if latest_run else None,
        "failed_count": latest_run.failed_count if latest_run else None,
        "published_at": (
            latest_run.published_at.isoformat()
            if latest_run and latest_run.published_at
            else None
        ),
    }

    return {"bars": bars_freshness, "strategy": strategy_freshness}


async def _compute_after_close_pipeline(
    db: AsyncSession,
    now: datetime,
    business_date_obj: Any,
    business_date_str: str,
) -> dict[str, Any]:
    """计算盘后流水线状态。

    判定规则（严格按 advice.md）：
    1. 上海时间 < 16:00 → NOT_STARTED（不引用昨日 succeeded）
    2. 查 bars_scheduler 当日 job：running → BARS_RUNNING，failed → BARS_FAILED
    3. bars succeeded → 查 DSA（trade_date=今日, run_type=scheduled, attempt_no DESC）
    4. 禁止混用历史最近运行与今日盘后状态

    Args:
        db: 异步数据库会话
        now: 上海时区当前时间
        business_date_obj: 业务日期 date 对象
        business_date_str: 业务日期字符串

    Returns:
        盘后流水线状态字典
    """
    # [after_close_pipeline] - 16:00 前不启动
    if now.hour < 16:
        return {
            "status": PIPELINE_STATUS_NOT_STARTED,
            "bars_job": None,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
        }

    # [after_close_pipeline] - 查当日 bars_scheduler job（必须过滤 business_date）
    bars_stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == "bars_scheduler",
            SchedulerJobRun.business_date == business_date_str,
        )
        .order_by(SchedulerJobRun.started_at.desc())
        .limit(1)
    )
    bars_result = await db.execute(bars_stmt)
    bars_job = bars_result.scalar_one_or_none()

    bars_job_summary: dict[str, Any] | None = None
    if bars_job is not None:
        bars_job_summary = {
            "status": bars_job.status,
            "started_at": bars_job.started_at.isoformat() if bars_job.started_at else None,
            "finished_at": bars_job.finished_at.isoformat() if bars_job.finished_at else None,
            "error_message": bars_job.error_message,
        }

    # bars 无记录 → NOT_STARTED
    if bars_job is None:
        return {
            "status": PIPELINE_STATUS_NOT_STARTED,
            "bars_job": None,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
        }

    # bars running → BARS_RUNNING
    if bars_job.status == "running":
        return {
            "status": PIPELINE_STATUS_BARS_RUNNING,
            "bars_job": bars_job_summary,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
        }

    # bars failed → BARS_FAILED
    if bars_job.status == "failed":
        return {
            "status": PIPELINE_STATUS_BARS_FAILED,
            "bars_job": bars_job_summary,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
        }

    # [after_close_pipeline] - bars succeeded → 查 DSA（trade_date=今日, run_type=scheduled）
    dsa_stmt = (
        select(StrategyRun)
        .where(
            StrategyRun.trade_date == business_date_obj,
            StrategyRun.run_type == "scheduled",
        )
        .order_by(StrategyRun.attempt_no.desc())
        .limit(1)
    )
    dsa_result = await db.execute(dsa_stmt)
    dsa_run = dsa_result.scalar_one_or_none()

    dsa_run_summary: dict[str, Any] | None = None
    if dsa_run is not None:
        dsa_run_summary = {
            "id": str(dsa_run.id),
            "status": dsa_run.status,
            "run_type": dsa_run.run_type,
            "attempt_no": dsa_run.attempt_no,
            "trade_date": dsa_run.trade_date,
            "failed_count": dsa_run.failed_count,
            "succeeded_count": dsa_run.succeeded_count,
            "error_code": dsa_run.error_code,
            "error_message": dsa_run.error_message,
            "failure_stage": dsa_run.failure_stage,
            "queued_at": dsa_run.queued_at.isoformat() if dsa_run.queued_at else None,
            "worker_id": dsa_run.worker_id,
        }

    # DSA 状态映射
    if dsa_run is None:
        pipeline_status = PIPELINE_STATUS_WAITING_DSA
    elif dsa_run.status == "queued":
        pipeline_status = PIPELINE_STATUS_DSA_QUEUED
    elif dsa_run.status == "running":
        pipeline_status = PIPELINE_STATUS_DSA_RUNNING
    elif dsa_run.status == "completed":
        pipeline_status = PIPELINE_STATUS_DSA_COMPLETED
    elif dsa_run.status == "published":
        if (dsa_run.failed_count or 0) == 0:
            pipeline_status = PIPELINE_STATUS_PUBLISHED
        else:
            pipeline_status = PIPELINE_STATUS_DSA_COMPLETED
    elif dsa_run.status == "failed":
        pipeline_status = PIPELINE_STATUS_DSA_FAILED
    elif dsa_run.status == "partial_failed":
        pipeline_status = PIPELINE_STATUS_DSA_COMPLETED
    else:
        pipeline_status = PIPELINE_STATUS_STALE

    # [SystemOverview] - 细分 WAITING_DSA 原因（7 种），仅在 DSA 未成功 published 时填充
    waiting_dsa_reason, waiting_dsa_suggestion = await _compute_waiting_dsa_reason(
        db=db,
        pipeline_status=pipeline_status,
        dsa_run=dsa_run,
        business_date_obj=business_date_obj,
        now=now,
    )

    # [Phase9] - 数据新鲜度：行情数据 + 选股策略两个独立区块
    data_freshness = await _compute_data_freshness(db, now)

    return {
        "status": pipeline_status,
        "bars_job": bars_job_summary,
        "dsa_run": dsa_run_summary,
        "waiting_dsa_reason": waiting_dsa_reason,
        "waiting_dsa_suggestion": waiting_dsa_suggestion,
        "data_freshness": data_freshness,
    }


async def _compute_waiting_dsa_reason(
    db: AsyncSession,
    pipeline_status: str,
    dsa_run: StrategyRun | None,
    business_date_obj: Any,
    now: datetime,
) -> tuple[str | None, str | None]:
    """[SystemOverview] - 细分 WAITING_DSA 7 种原因及人类可读建议。

    仅在 DSA 未成功 published 时填充（成功终态 PUBLISHED/DSA_COMPLETED 等返回 None）。

    7 种原因判定优先级：
    1. WAITING_DSA (dsa_run is None):
       - DATA_COVERAGE_INSUFFICIENT: bars 覆盖率 < 90%
       - NO_RELEASED_VERSION: selector 策略无 released 版本
       - NO_RUN_CREATED: 默认（bars 成功但 DSA 未创建，多因调度未触发）
    2. DSA_QUEUED + queued_at > 30min + 无 worker_id:
       - QUEUED_NOT_CLAIMED
    3. DSA_FAILED:
       - QUALITY_GATE_FAILED: failure_stage == "QUALITY_GATE"
       - PUBLISH_FAILED: failure_stage == "PUBLISH"
       - RUN_FAILED: 其他 failure_stage（DATA_READINESS/LOAD_*/CALCULATE_*/...）

    Args:
        db: 异步数据库会话
        pipeline_status: 当前流水线状态枚举
        dsa_run: DSA StrategyRun 对象（可能为 None）
        business_date_obj: 业务日期 date 对象
        now: 上海时区当前时间

    Returns:
        (reason, suggestion) 元组，无原因时均为 None
    """
    # 成功终态无需细分原因
    if pipeline_status in (
        PIPELINE_STATUS_PUBLISHED,
        PIPELINE_STATUS_DSA_COMPLETED,
        PIPELINE_STATUS_DSA_RUNNING,
    ):
        return None, None

    # 场景 1: WAITING_DSA - bars 成功但 DSA run 未创建
    if pipeline_status == PIPELINE_STATUS_WAITING_DSA:
        # 1a. 检查 bars 覆盖率是否达标
        coverage = await _compute_bars_coverage(db, business_date_obj)
        if coverage is not None and coverage < WAITING_DSA_COVERAGE_THRESHOLD:
            reason = WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT
            return reason, WAITING_DSA_SUGGESTIONS[reason]

        # 1b. 检查 selector 策略是否有 released 版本
        has_released = await _has_released_selector_version(db)
        if not has_released:
            reason = WAITING_DSA_REASON_NO_RELEASED_VERSION
            return reason, WAITING_DSA_SUGGESTIONS[reason]

        # 1c. 默认：bars 成功 + 覆盖率达标 + 有 released 版本，但 DSA run 未创建
        # 多因 strategy_scheduler 18:30 未触发或 create_batch_run 内部异常
        reason = WAITING_DSA_REASON_NO_RUN_CREATED
        return reason, WAITING_DSA_SUGGESTIONS[reason]

    # 场景 2: DSA_QUEUED - 排队超时未被 worker 领取
    if pipeline_status == PIPELINE_STATUS_DSA_QUEUED and dsa_run is not None:
        if dsa_run.queued_at is not None and not dsa_run.worker_id:
            queued_age = (now - dsa_run.queued_at).total_seconds()
            if queued_age > WAITING_DSA_QUEUED_TIMEOUT:
                reason = WAITING_DSA_REASON_QUEUED_NOT_CLAIMED
                return reason, WAITING_DSA_SUGGESTIONS[reason]
        return None, None

    # 场景 3: DSA_FAILED - 按失败阶段细分
    if pipeline_status == PIPELINE_STATUS_DSA_FAILED and dsa_run is not None:
        from app.models.strategy_run import (
            FAILURE_STAGE_PUBLISH,
            FAILURE_STAGE_QUALITY_GATE,
        )

        if dsa_run.failure_stage == FAILURE_STAGE_QUALITY_GATE:
            reason = WAITING_DSA_REASON_QUALITY_GATE_FAILED
            return reason, WAITING_DSA_SUGGESTIONS[reason]
        if dsa_run.failure_stage == FAILURE_STAGE_PUBLISH:
            reason = WAITING_DSA_REASON_PUBLISH_FAILED
            return reason, WAITING_DSA_SUGGESTIONS[reason]
        # 其他失败阶段（DATA_READINESS/LOAD_*/CALCULATE_*/WORKER_INTERRUPTED 等）
        reason = WAITING_DSA_REASON_RUN_FAILED
        return reason, WAITING_DSA_SUGGESTIONS[reason]

    # 其他状态（STALE 等）暂不细分
    return None, None


async def _compute_bars_coverage(
    db: AsyncSession,
    business_date_obj: Any,
) -> float | None:
    """[SystemOverview] - 计算当日 bars 覆盖率（covered / active_total）。

    复用 bars_scheduler_service 的统计逻辑（不引入新依赖）。
    返回 None 表示无法计算（无活跃标的）。

    Args:
        db: 异步数据库会话
        business_date_obj: 业务日期 date 对象

    Returns:
        覆盖率 0.0-1.0，或 None
    """
    from app.models.bar import BarDaily
    from app.models.instrument import Instrument

    # 统计今日日线覆盖的标的数
    covered_result = await db.scalar(
        select(func.count(func.distinct(BarDaily.instrument_id)))
        .where(BarDaily.trade_date == business_date_obj)
    )
    covered = int(covered_result or 0)

    # 统计活跃标的数
    active_result = await db.scalar(
        select(func.count(Instrument.id)).where(Instrument.status == "active")
    )
    total = int(active_result or 0)

    if total == 0:
        return None
    return covered / total


async def _has_released_selector_version(db: AsyncSession) -> bool:
    """[SystemOverview] - 检查 selector 策略是否有 released 版本。

    查询 strategy_definitions WHERE kind='selector' JOIN strategy_versions WHERE status='released'。
    任一 selector 策略有 released 版本即返回 True。

    Args:
        db: 异步数据库会话

    Returns:
        True 表示至少有一个 selector 策略有 released 版本
    """
    from sqlalchemy import exists

    released_subq = (
        select(StrategyVersion.id)
        .where(
            StrategyVersion.strategy_definition_id == StrategyDefinition.id,
            StrategyVersion.status == "released",
        )
        .limit(1)
        .correlate(StrategyDefinition)
    )
    stmt = (
        select(func.count())
        .select_from(StrategyDefinition)
        .where(
            StrategyDefinition.kind == "selector",
            exists(released_subq),
        )
    )
    count = await db.scalar(stmt)
    return int(count or 0) > 0


if __name__ == "__main__":
    # 自测入口：验证状态枚举和阈值常量（无副作用，不连接数据库）
    print("=== system_overview_service 自测 ===")

    # 验证监控状态枚举
    monitor_statuses = {
        MONITOR_STATUS_RUNNING, MONITOR_STATUS_IDLE_EXPECTED,
        MONITOR_STATUS_SESSION_COMPLETED, MONITOR_STATUS_DELAYED,
        MONITOR_STATUS_FAILED, MONITOR_STATUS_WORKER_OFFLINE,
        MONITOR_STATUS_NOT_APPLICABLE,
    }
    assert len(monitor_statuses) == 7, f"monitor_status 应 7 值，实际 {len(monitor_statuses)}"
    print(f"monitor_statuses={sorted(monitor_statuses)}")

    # 验证流水线状态枚举
    pipeline_statuses = {
        PIPELINE_STATUS_NOT_STARTED, PIPELINE_STATUS_BARS_RUNNING,
        PIPELINE_STATUS_BARS_FAILED, PIPELINE_STATUS_WAITING_DSA,
        PIPELINE_STATUS_DSA_QUEUED, PIPELINE_STATUS_DSA_RUNNING,
        PIPELINE_STATUS_DSA_COMPLETED, PIPELINE_STATUS_PUBLISHED,
        PIPELINE_STATUS_DSA_FAILED, PIPELINE_STATUS_STALE,
    }
    assert len(pipeline_statuses) == 10, f"pipeline_status 应 10 值，实际 {len(pipeline_statuses)}"
    print(f"pipeline_statuses={sorted(pipeline_statuses)}")

    # 验证阈值
    assert HEARTBEAT_OFFLINE_THRESHOLD == 90
    assert FRESHNESS_DELAYED_THRESHOLD == 180
    assert WORKER_HEALTH_WINDOW == 120
    assert WAITING_DSA_QUEUED_TIMEOUT == 1800
    assert WAITING_DSA_COVERAGE_THRESHOLD == 0.9
    print(f"HEARTBEAT_OFFLINE_THRESHOLD={HEARTBEAT_OFFLINE_THRESHOLD}")
    print(f"FRESHNESS_DELAYED_THRESHOLD={FRESHNESS_DELAYED_THRESHOLD}")
    print(f"WORKER_HEALTH_WINDOW={WORKER_HEALTH_WINDOW}")
    print(f"WAITING_DSA_QUEUED_TIMEOUT={WAITING_DSA_QUEUED_TIMEOUT}")
    print(f"WAITING_DSA_COVERAGE_THRESHOLD={WAITING_DSA_COVERAGE_THRESHOLD}")

    # 验证 _determine_monitor_status 逻辑（非异步部分）
    from app.services.market_status_service import (
        MARKET_SESSION_NON_TRADING_DAY,
        MARKET_SESSION_PRE_OPEN,
        MARKET_SESSION_MORNING,
        MARKET_SESSION_LUNCH,
        MARKET_SESSION_AFTERNOON,
        MARKET_SESSION_CLOSED,
    )

    # 非交易日
    assert _determine_monitor_status(
        MARKET_SESSION_NON_TRADING_DAY, None, None, None, None, ""
    ) == MONITOR_STATUS_NOT_APPLICABLE

    # 盘前
    assert _determine_monitor_status(
        MARKET_SESSION_PRE_OPEN, None, None, None, None, ""
    ) == MONITOR_STATUS_IDLE_EXPECTED

    # 午休
    assert _determine_monitor_status(
        MARKET_SESSION_LUNCH, None, None, None, None, ""
    ) == MONITOR_STATUS_IDLE_EXPECTED

    # 盘中心跳超时
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 100, None, None, None, ""
    ) == MONITOR_STATUS_WORKER_OFFLINE

    # 盘中无心跳（heartbeat_age_seconds=None）→ WORKER_OFFLINE
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, None, None, None, None, ""
    ) == MONITOR_STATUS_WORKER_OFFLINE

    # 盘中数据延迟
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, 200, None, None, ""
    ) == MONITOR_STATUS_DELAYED

    # 盘中正常运行
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, 60, None, None, ""
    ) == MONITOR_STATUS_RUNNING

    # 盘中无数据（freshness=None）
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, None, None, None, ""
    ) == MONITOR_STATUS_RUNNING

    print("_determine_monitor_status 逻辑验证 OK")

    print("=== 自测结束 ===")
