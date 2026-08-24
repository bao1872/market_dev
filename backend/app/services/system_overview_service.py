"""系统概览服务 - /admin/system-overview 业务逻辑层。

从 admin_subscription.py 路由抽出数据查询逻辑，新增市场阶段/监控运行时/盘后流水线状态。

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

import json
import logging
from datetime import date, datetime, time, timedelta
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
    OverallSummary,
    ProductionChainNode,
    PublicationStatus,
    TodayIssue,
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

    # [PRD §8.1/8.2] 统一数据生产与发布状态摘要（P1，后端直出，前端不再自行判定）
    summary = await _compute_summary(
        base_fields, market_session, monitor_runtime, after_close_pipeline
    )
    # [PRD §8.2] 数据生产中心：从各数据产品表查询完整 6 节点状态，覆盖纯派生的 3 节点
    summary["production_chain"] = await _compute_product_nodes(db, business_date_obj)

    return {
        **base_fields,
        "server_time": now.isoformat(),
        "business_date": business_date_str,
        "market_session": market_session,
        "monitor_runtime": monitor_runtime,
        "after_close_pipeline": after_close_pipeline,
        "summary": summary,
    }


async def _compute_base_fields(db: AsyncSession, now: datetime) -> dict[str, Any]:
    """计算 12 个基础字段（从原 admin_subscription.py 迁移）。

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
    db: AsyncSession | None,
    now: datetime | None,
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
    # [data_freshness.strategy] - 限定 dsa_selector（关联 strategy_versions + strategy_definitions）
    # 禁止取所有 StrategyRun 最新一条（advice.md: 选股新鲜度必须限定 dsa_selector）
    from app.models.strategy import StrategyDefinition, StrategyVersion
    dsa_version_ids_stmt = (
        select(StrategyVersion.id)
        .join(
            StrategyDefinition,
            StrategyDefinition.id == StrategyVersion.strategy_definition_id,
        )
        .where(StrategyDefinition.strategy_key == "dsa_selector")
    )
    dsa_version_ids_subq = dsa_version_ids_stmt.subquery()

    # [data_freshness.strategy] - 最新计算交易日（dsa_selector，所有状态）
    latest_compute = await db.scalar(
        select(func.max(StrategyRun.trade_date)).where(
            StrategyRun.strategy_version_id.in_(select(dsa_version_ids_subq))
        )
    )

    # [data_freshness.strategy] - 最新发布交易日（dsa_selector, status='published'）
    latest_published = await db.scalar(
        select(func.max(StrategyRun.trade_date)).where(
            StrategyRun.status == "published",
            StrategyRun.strategy_version_id.in_(select(dsa_version_ids_subq)),
        )
    )

    # [data_freshness.strategy] - 最近一条 dsa_selector strategy_runs
    # 排序：trade_date DESC, attempt_no DESC, started_at DESC NULLS LAST（避免 started_at 为 NULL 时排序不确定）
    latest_run_stmt = (
        select(StrategyRun)
        .where(StrategyRun.strategy_version_id.in_(select(dsa_version_ids_subq)))
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


async def _compute_after_close_orchestrator_summary(
    db: AsyncSession,
    business_date_str: str,
) -> dict[str, Any]:
    """[AfterClose] - 查询当日 after_close_orchestrator job_run 摘要字段。

    供系统概览返回 job_run_id / orchestrator_status / heartbeat_at /
    lease_expires_at / last_completed_step / scheduled_at / started_at /
    current_step，使前端能：
    - 进入任务详情（GET /admin/after-close-runs/{id}）
    - 断点继续 / 重试 / 强制执行
    - 判断冲突任务（创建按钮禁用条件）
    - 识别 worker 离线（heartbeat_at 过期）
    - [Phase8A] 区分计划启动时间(scheduled_at)与实际启动时间(started_at)

    Args:
        db: 异步数据库会话
        business_date_str: 业务日期字符串（YYYY-MM-DD）

    Returns:
        含 8 个字段的字典（无任务时均为 None）：
        job_run_id / orchestrator_status / heartbeat_at / lease_expires_at /
        last_completed_step / scheduled_at / started_at / current_step
    """
    empty = {
        "job_run_id": None,
        "orchestrator_status": None,
        "heartbeat_at": None,
        "lease_expires_at": None,
        "last_completed_step": None,
        "scheduled_at": None,
        "started_at": None,
        "current_step": None,
    }
    stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == "after_close_orchestrator",
            SchedulerJobRun.business_date == business_date_str,
        )
        .order_by(SchedulerJobRun.started_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    job_run = result.scalar_one_or_none()
    if job_run is None:
        return empty

    # [AfterClose] - 解析 metadata_json 提取 orchestrator_status + last_completed_step
    meta: dict[str, Any] = {}
    if job_run.metadata_json:
        try:
            meta = json.loads(job_run.metadata_json)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "[SystemOverview] after_close_orchestrator metadata_json 解析失败: "
                "run_id=%s, error=%s",
                job_run.id, exc,
            )

    orchestrator_status = meta.get("orchestrator_status")
    # [Phase8A] current_step = orchestrator_status（当前执行步骤）
    return {
        "job_run_id": str(job_run.id),
        "orchestrator_status": orchestrator_status,
        "heartbeat_at": (
            job_run.heartbeat_at.isoformat() if job_run.heartbeat_at else None
        ),
        "lease_expires_at": (
            job_run.lease_expires_at.isoformat() if job_run.lease_expires_at else None
        ),
        "last_completed_step": meta.get("last_completed_step"),
        # [Phase8A] 计划启动时间（16:00 调度创建时写入）vs 实际启动时间（Worker 领取时写入）
        "scheduled_at": (
            job_run.scheduled_at.isoformat() if job_run.scheduled_at else None
        ),
        "started_at": (
            job_run.started_at.isoformat() if job_run.started_at else None
        ),
        # [Phase8A] current_step 与 orchestrator_status 同义，便于前端直接读取
        "current_step": orchestrator_status,
    }


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

    [AfterClose] - 额外返回 after_close_orchestrator 当日 job_run 摘要字段：
    job_run_id / orchestrator_status / heartbeat_at / lease_expires_at / last_completed_step，
    供前端进入任务详情、断点继续、判断冲突任务、识别 worker 离线。

    Args:
        db: 异步数据库会话
        now: 上海时区当前时间
        business_date_obj: 业务日期 date 对象
        business_date_str: 业务日期字符串

    Returns:
        盘后流水线状态字典
    """
    # [data_freshness] - 开头无条件计算，所有返回分支都必须携带（新鲜度是数据库现状，不依赖任务状态）
    data_freshness = await _compute_data_freshness(db, now)

    # [after_close_pipeline] - 16:00 前不启动
    if now.hour < 16:
        return {
            "status": PIPELINE_STATUS_NOT_STARTED,
            "bars_job": None,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
            "data_freshness": data_freshness,
            "job_run_id": None,
            "orchestrator_status": None,
            "heartbeat_at": None,
            "lease_expires_at": None,
            "last_completed_step": None,
            # [Phase8A] 新增字段
            "scheduled_at": None,
            "started_at": None,
            "current_step": None,
        }

    # [AfterClose] - 查当日 after_close_orchestrator job_run（取最新一条）
    # 提取 job_run_id / orchestrator_status / heartbeat_at / lease_expires_at / last_completed_step
    # 供前端判断冲突任务、进入任务详情、断点继续、识别 worker 离线
    orchestrator_summary = await _compute_after_close_orchestrator_summary(
        db, business_date_str
    )

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

    # bars 无记录 → 检查 after_close_orchestrator 父任务是否活跃
    # advice.md: 不再要求必须存在独立 bars_scheduler 记录，优先按父任务状态显示
    if bars_job is None:
        orchestrator_status = orchestrator_summary.get("orchestrator_status")
        if orchestrator_status:
            # 父任务存在（queued/refreshing_daily/.../succeeded/failed），显示父任务阶段
            return {
                "status": orchestrator_status,
                "bars_job": None,
                "dsa_run": None,
                "waiting_dsa_reason": None,
                "waiting_dsa_suggestion": None,
                "data_freshness": data_freshness,
                **orchestrator_summary,
            }
        return {
            "status": PIPELINE_STATUS_NOT_STARTED,
            "bars_job": None,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
            "data_freshness": data_freshness,
            **orchestrator_summary,
        }

    # bars running → BARS_RUNNING
    if bars_job.status == "running":
        return {
            "status": PIPELINE_STATUS_BARS_RUNNING,
            "bars_job": bars_job_summary,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
            "data_freshness": data_freshness,
            **orchestrator_summary,
        }

    # bars failed → BARS_FAILED
    if bars_job.status == "failed":
        return {
            "status": PIPELINE_STATUS_BARS_FAILED,
            "bars_job": bars_job_summary,
            "dsa_run": None,
            "waiting_dsa_reason": None,
            "waiting_dsa_suggestion": None,
            "data_freshness": data_freshness,
            **orchestrator_summary,
        }

    # [after_close_pipeline] - bars succeeded → 查 DSA（trade_date=今日, run_type=scheduled, strategy_key=dsa_selector）
    # advice.md: DSA 查询必须关联 strategy_versions + strategy_definitions + strategy_key=dsa_selector
    from app.models.strategy import StrategyDefinition as SdModel
    from app.models.strategy import StrategyVersion as SvModel
    dsa_version_ids = (
        select(SvModel.id)
        .join(SdModel, SdModel.id == SvModel.strategy_definition_id)
        .where(SdModel.strategy_key == "dsa_selector")
        .subquery()
    )
    dsa_stmt = (
        select(StrategyRun)
        .where(
            StrategyRun.trade_date == business_date_obj,
            StrategyRun.run_type == "scheduled",
            StrategyRun.strategy_version_id.in_(select(dsa_version_ids)),
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
    # [Bugfix] - 传入 latest_daily_trade_date 让覆盖率查询与 data_freshness 口径对齐
    latest_daily_trade_date_str = data_freshness.get("bars", {}).get("latest_daily_trade_date")
    latest_daily_trade_date_obj: Any = None
    if latest_daily_trade_date_str:
        try:
            from datetime import date as date_cls
            latest_daily_trade_date_obj = date_cls.fromisoformat(latest_daily_trade_date_str)
        except ValueError:
            latest_daily_trade_date_obj = None

    waiting_dsa_reason, waiting_dsa_suggestion = await _compute_waiting_dsa_reason(
        db=db,
        pipeline_status=pipeline_status,
        dsa_run=dsa_run,
        business_date_obj=business_date_obj,
        now=now,
        latest_daily_trade_date=latest_daily_trade_date_obj,
    )

    # [Phase9] - 数据新鲜度已在函数开头无条件计算（data_freshness），此处直接复用

    return {
        "status": pipeline_status,
        "bars_job": bars_job_summary,
        "dsa_run": dsa_run_summary,
        "waiting_dsa_reason": waiting_dsa_reason,
        "waiting_dsa_suggestion": waiting_dsa_suggestion,
        "data_freshness": data_freshness,
        **orchestrator_summary,
    }


async def _compute_waiting_dsa_reason(
    db: AsyncSession,
    pipeline_status: str,
    dsa_run: StrategyRun | None,
    business_date_obj: Any,
    now: datetime,
    latest_daily_trade_date: Any = None,
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
        # [Bugfix] - 描述: 与 data_freshness.bars.daily_coverage 口径对齐
        # 原逻辑用 business_date_obj（today）查覆盖率，今日未回补时 0%，与系统概览显示的 ~98% 不一致
        # 修复：优先用 latest_daily_trade_date（最新已落盘日），fallback 到 business_date_obj
        coverage_date = business_date_obj
        if latest_daily_trade_date is not None:
            coverage_date = latest_daily_trade_date
        # 覆盖率门禁使用原始值，避免四舍五入边缘误判
        from app.services.bars_coverage_service import BarsCoverageService

        coverage_result = await BarsCoverageService.compute_daily_coverage(db, coverage_date)
        if coverage_result["total"] > 0:
            coverage_raw = coverage_result["coverage_raw"]
            if coverage_raw < WAITING_DSA_COVERAGE_THRESHOLD:
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

    复用 BarsCoverageService 统一 SQL（收口三处重复实现）。
    返回 None 表示无法计算（无活跃标的）。

    Args:
        db: 异步数据库会话
        business_date_obj: 业务日期 date 对象

    Returns:
        覆盖率 0.0-1.0，或 None
    """
    from app.services.bars_coverage_service import BarsCoverageService

    result = await BarsCoverageService.compute_daily_coverage(db, business_date_obj)
    if result["total"] == 0:
        return None
    return result["coverage"]


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


async def _compute_summary(
    base_fields: dict[str, Any],
    market_session: str,
    monitor_runtime: dict[str, Any],
    after_close_pipeline: dict[str, Any],
) -> dict[str, Any]:
    """[PRD §8.1/8.2] 计算统一数据生产与发布状态摘要（P1）。

    由后端基于实时查询结果直出以下状态，前端只做展示不判定：
    - overall_status: ok / attention / blocked（系统是否需要管理员介入）
    - quality_gate: not_applicable / passed / failed / pending（选股质量门禁）
    - publication_status: published / unpublished / pending / failed（正式发布）
    - today_must_process: 今日需要管理员处理的事项列表
    - production_chain: 行情/选股/发布三个环节节点状态

    纯派生逻辑，无副作用（只读）。
    """
    pipeline_status = after_close_pipeline.get("status")
    waiting_reason = after_close_pipeline.get("waiting_dsa_reason")
    data_freshness = after_close_pipeline.get("data_freshness") or {}
    strategy_fresh = (data_freshness.get("strategy") or {})
    bars_fresh = (data_freshness.get("bars") or {})

    latest_compute = strategy_fresh.get("latest_compute_trade_date")
    latest_published = strategy_fresh.get("latest_published_trade_date")
    latest_run_status = strategy_fresh.get("status")

    issues: list[dict[str, Any]] = []
    chain: list[dict[str, Any]] = []

    # ===== 生产链节点 =====
    # 行情节点（bars）：新鲜度是否落后
    bars_behind = bool(bars_fresh.get("is_behind_latest_trade_date"))
    bars_chain_status = "failed" if bars_behind else ("ok" if bars_fresh.get("latest_daily_trade_date") else "pending")
    chain.append(
        ProductionChainNode(
            key="bars",
            label="行情",
            status=bars_chain_status,
            detail=(
                "行情落后最近交易日"
                if bars_behind
                else (f"更新至 {bars_fresh.get('latest_daily_trade_date')}" if bars_fresh.get("latest_daily_trade_date") else "今日尚无行情")
            ),
            trade_date=bars_fresh.get("latest_daily_trade_date"),
        ).model_dump()
    )

    # 选股节点（strategy）：最新计算状态
    strategy_chain_status = "pending"
    strategy_detail = "今日尚未计算"
    if latest_run_status == "published":
        strategy_chain_status = "ok"
        strategy_detail = "已发布"
    elif latest_run_status == "failed":
        strategy_chain_status = "failed"
        strategy_detail = "计算失败"
    elif latest_run_status in ("queued", "running"):
        strategy_chain_status = "running"
        strategy_detail = "计算中"
    elif latest_compute is not None:
        strategy_chain_status = "stale"
        strategy_detail = f"计算至 {latest_compute}"
    chain.append(
        ProductionChainNode(
            key="strategy",
            label="选股",
            status=strategy_chain_status,
            detail=strategy_detail,
            trade_date=latest_compute,
        ).model_dump()
    )

    # 发布节点（publish）：正式发布状态
    if latest_published is None:
        publish_status = "pending"
        publish_detail = "尚无正式发布"
    elif latest_run_status == "failed":
        publish_status = "failed"
        publish_detail = "发布失败"
    elif latest_published == latest_compute:
        publish_status = "ok"
        publish_detail = f"已发布至 {latest_published}"
    else:
        publish_status = "stale"
        publish_detail = f"已发布至 {latest_published}（落后最新计算 {latest_compute}）"
    chain.append(
        ProductionChainNode(
            key="publish",
            label="发布",
            status=publish_status,
            detail=publish_detail,
            trade_date=latest_published,
        ).model_dump()
    )

    # ===== 今日必须处理项 =====
    # Worker 离线（阻塞级）
    worker_offline = monitor_runtime.get("status") == MONITOR_STATUS_WORKER_OFFLINE
    if worker_offline:
        issues.append(
            TodayIssue(
                key="worker_offline",
                error_code="overview_worker_offline",
                severity="error",
                message="盘中监控 Worker 离线，实时计算已中断",
                retryable=False,
                resumable=False,
                recommended_action="检查 trading-worker 容器并重启",
                target_route="/admin/overview",
            ).model_dump()
        )

    # 行情落后（警告级）
    if bars_behind:
        issues.append(
            TodayIssue(
                key="bars_behind",
                error_code="overview_bars_behind",
                severity="warning",
                message="行情数据落后最近交易日",
                retryable=True,
                resumable=False,
                recommended_action="重新同步日线数据并重跑盘后编排",
                target_route="/admin/data-production",
            ).model_dump()
        )

    # 质量门禁失败（阻塞级）
    quality_gate = "not_applicable"
    if waiting_reason == WAITING_DSA_REASON_QUALITY_GATE_FAILED:
        quality_gate = "failed"
        issues.append(
            TodayIssue(
                key="quality_gate_failed",
                error_code="overview_quality_gate_failed",
                severity="error",
                message="选股质量门禁未通过，今日结果未发布",
                retryable=False,
                resumable=False,
                recommended_action="检查质量门禁配置与失败股票",
                target_route="/admin/data-production",
            ).model_dump()
        )
    elif pipeline_status == PIPELINE_STATUS_PUBLISHED:
        quality_gate = "passed"

    # 发布失败（警告级，可恢复）
    if waiting_reason == WAITING_DSA_REASON_PUBLISH_FAILED:
        issues.append(
            TodayIssue(
                key="publish_failed",
                error_code="overview_publish_failed",
                severity="error",
                message="选股发布失败",
                retryable=True,
                resumable=True,
                recommended_action="检查发布逻辑与 published_run 表，可恢复重试",
                target_route="/admin/data-production",
            ).model_dump()
        )

    # 数据覆盖不足（警告级，可重试）
    if waiting_reason == WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT:
        issues.append(
            TodayIssue(
                key="data_coverage_insufficient",
                error_code="overview_data_coverage_insufficient",
                severity="warning",
                message="日线数据覆盖率不足，选股未发布",
                retryable=True,
                resumable=False,
                recommended_action="重新同步日线数据后重跑",
                target_route="/admin/data-production",
            ).model_dump()
        )

    # 计算失败（警告级，可重试）
    if waiting_reason in (WAITING_DSA_REASON_RUN_FAILED, WAITING_DSA_REASON_NO_RUN_CREATED):
        issues.append(
            TodayIssue(
                key="dsa_run_failed",
                error_code="overview_dsa_run_failed",
                severity="warning",
                message="选股计算失败或未创建",
                retryable=True,
                resumable=True,
                recommended_action=(
                    "查看失败股票与 error_message，可重跑编排"
                ),
                target_route="/admin/data-production",
            ).model_dump()
        )

    # ===== 汇总状态（PRD §8.1 唯一判定规则，后端唯一权威）=====
    # 规则优先级（从高到低，返回首个命中的状态）：
    #   1. 存在 severity=error 的 issue → blocked（需立即处理）
    #   2. 存在 severity=warning 的 issue → attention（需关注）
    #   3. 无任何 issue → ok
    # 前端不得自行推导 overall_status，仅按此展示。
    has_error = any(i["severity"] == "error" for i in issues)
    has_warning = any(i["severity"] == "warning" for i in issues)
    if has_error:
        overall_status = "blocked"
    elif has_warning:
        overall_status = "attention"
    else:
        overall_status = "ok"

    publication_status = PublicationStatus(
        status=(
            "failed"
            if waiting_reason == WAITING_DSA_REASON_PUBLISH_FAILED
            else (
                "published"
                if latest_published is not None
                else "pending"
            )
        ),
        latest_published_trade_date=latest_published,
        latest_compute_trade_date=latest_compute,
        is_current=(latest_published is not None and latest_published == latest_compute),
        # [PRD §8.2] 三态语义：passed→True；failed→False；pending/not_applicable→None（未触发不代表未通过）
        quality_gate_passed=(
            True if quality_gate == "passed" else False if quality_gate == "failed" else None
        ),
    ).model_dump()

    return OverallSummary(
        overall_status=overall_status,
        quality_gate=quality_gate,
        publication_status=publication_status,
        today_must_process=issues,
        production_chain=chain,
    ).model_dump()


async def _compute_product_nodes(
    db: AsyncSession,
    business_date: date,
) -> list[dict[str, Any]]:
    """[PRD §8.2] 数据生产中心：从各数据产品表查询完整 6 节点状态。

    覆盖行情 / 第一金字塔 / 板块分析 / 复盘 / 竞价准备 / 正式发布 六个产品环节，
    每项返回 trade_date / status / run_id / quality_gate / publication_status /
    blocking_reason / recommended_action。纯只读查询，无副作用。

    Args:
        db: 异步数据库会话
        business_date: 业务交易日

    Returns:
        6 个 ProductionChainNode 的 dict 列表
    """
    nodes: list[dict[str, Any]] = []

    # ===== 1. 行情（bars_daily）=====
    from app.models.bar import BarDaily
    latest_daily = await db.scalar(
        select(func.max(BarDaily.trade_date)).where(BarDaily.trade_date <= business_date)
    )
    if latest_daily is None:
        nodes.append(
            ProductionChainNode(
                key="bars", label="行情", status="pending",
                detail="今日尚无行情数据", trade_date=None,
                publication_status="not_applicable",
                blocking_reason="bars_daily 无数据", recommended_action="等待或触发日线同步",
            ).model_dump()
        )
    elif latest_daily < business_date:
        nodes.append(
            ProductionChainNode(
                key="bars", label="行情", status="stale",
                detail=f"最新日线为 {latest_daily}（落后今日）", trade_date=latest_daily,
                publication_status="not_applicable",
                blocking_reason="行情落后最近交易日", recommended_action="重新同步日线数据",
            ).model_dump()
        )
    else:
        nodes.append(
            ProductionChainNode(
                key="bars", label="行情", status="ok",
                detail=f"行情已更新至 {latest_daily}", trade_date=latest_daily,
                publication_status="not_applicable",
            ).model_dump()
        )

    # ===== 2. 第一金字塔（FactorPublication stock_core 发布指针 = 正式生产事实源）=====
    # 不能读 first_pyramid_history_runs（历史回补任务，无 trade_date，不代表今日生产状态）。
    # 正式状态应从 stock_core 发布指针获取（含 trade_date/coverage_ratio/data_run_id/published_at）。
    from app.models.factor_publication import FactorPublication
    fp_pub = await db.scalar(
        select(FactorPublication)
        .where(FactorPublication.publication_kind == "stock_core")
        .order_by(FactorPublication.published_at.desc())
        .limit(1)
    )
    if fp_pub is None:
        nodes.append(
            ProductionChainNode(
                key="first_pyramid", label="第一金字塔", status="pending",
                detail="尚无 stock_core 正式发布", trade_date=None,
                publication_status="pending",
                blocking_reason="无 stock_core 发布指针", recommended_action="等待第一金字塔计算并发布 stock_core",
            ).model_dump()
        )
    else:
        fp_cov_ok = fp_pub.coverage_ratio is not None and fp_pub.coverage_ratio >= 0.98
        nodes.append(
            ProductionChainNode(
                key="first_pyramid", label="第一金字塔",
                status="ok" if fp_cov_ok else "attention",
                detail=(
                    f"{fp_pub.trade_date} 覆盖率 {(fp_pub.coverage_ratio * 100):.0f}%"
                    if fp_pub.coverage_ratio is not None
                    else f"{fp_pub.trade_date} 已发布（覆盖率未知）"
                ),
                trade_date=fp_pub.trade_date,
                run_id=str(fp_pub.data_run_id),
                quality_gate="passed" if fp_cov_ok else "failed",
                publication_status="published",
                blocking_reason=None if fp_cov_ok else "stock_core 覆盖率未达 98%",
                recommended_action=None if fp_cov_ok else "检查第一金字塔计算覆盖并重新发布",
            ).model_dump()
        )

    # ===== 3. 板块/复盘事实（sourced from published Unified Review）=====
    # Slice 4A8 — 不再把 legacy 板块运行表表述为当前正式板块产品。
    # 板块事实来自已发布的 MarketReviewRun 及其 canonical board scopes。
    from app.models.market_review import MarketReviewRun, ReviewScopeObservationFact
    _BOARD_SCOPE_TYPES = ("concept", "industry_l1", "industry_l2", "industry_l3")
    board_review = await db.scalar(
        select(MarketReviewRun)
        .where(MarketReviewRun.status == "published")
        .order_by(MarketReviewRun.trade_date.desc())
        .limit(1)
    )
    if board_review is None:
        nodes.append(
            ProductionChainNode(
                key="board", label="板块/复盘事实", status="pending",
                detail="尚无已发布复盘", trade_date=None,
                publication_status="not_applicable",
                blocking_reason="无已发布复盘", recommended_action="发布复盘",
            ).model_dump()
        )
    else:
        exp_total, prov_total, scope_cnt = (
            await db.execute(
                select(
                    func.coalesce(func.sum(ReviewScopeObservationFact.pit_member_count), 0),
                    func.coalesce(func.sum(ReviewScopeObservationFact.provided_member_count), 0),
                    func.count(ReviewScopeObservationFact.id),
                ).where(
                    ReviewScopeObservationFact.review_run_id == board_review.id,
                    ReviewScopeObservationFact.scope_type.in_(_BOARD_SCOPE_TYPES),
                )
            )
        ).one()
        exp_total = int(exp_total)
        prov_total = int(prov_total)
        board_cov = prov_total / exp_total if exp_total > 0 else 0.0
        board_cov_ok = exp_total > 0 and board_cov >= 0.95
        nodes.append(
            ProductionChainNode(
                key="board", label="板块/复盘事实",
                status="ok" if board_cov_ok else "failed",
                detail=(
                    f"{board_review.trade_date} 已发布复盘板块范围 {scope_cnt} 个"
                    f"，成员覆盖 {board_cov * 100:.0f}%（{prov_total}/{exp_total}）"
                ),
                trade_date=board_review.trade_date,
                run_id=str(board_review.id),
                quality_gate="passed" if board_cov_ok else "failed",
                publication_status="published" if board_review.status == "published" else "pending",
                blocking_reason=None if board_cov_ok else "已发布复盘板块成员覆盖未达 95%",
                recommended_action=None if board_cov_ok else "检查已发布复盘板块范围",
            ).model_dump()
        )

    # ===== 4. 复盘（market_review_runs，最近一个 trade_date）=====
    from app.models.market_review import MarketReviewRun
    review_date = await db.scalar(select(func.max(MarketReviewRun.trade_date)))
    if review_date is None:
        nodes.append(
            ProductionChainNode(
                key="review", label="复盘", status="pending",
                detail="尚无复盘 run", trade_date=None,
                publication_status="not_applicable",
                blocking_reason="无复盘记录", recommended_action="触发复盘计算",
            ).model_dump()
        )
    else:
        review_run = await db.scalar(
            select(MarketReviewRun)
            .where(MarketReviewRun.trade_date == review_date)
            .order_by(MarketReviewRun.created_at.desc())
            .limit(1)
        )
        review_published = review_run is not None and review_run.status == "published"
        nodes.append(
            ProductionChainNode(
                key="review", label="复盘",
                status="ok" if review_published else ("running" if review_run and review_run.status in ("created", "computing") else "failed"),
                detail=f"{review_date} 状态：{review_run.status if review_run else '无'}"
                if review_run else "无复盘",
                trade_date=review_date,
                run_id=str(review_run.id) if review_run else None,
                quality_gate="passed" if review_published else ("pending" if review_run else "failed"),
                publication_status="published" if review_published else "pending",
                blocking_reason=None if review_published else (review_run.status if review_run else "无 run"),
                recommended_action=None if review_published else "检查复盘计算或发布",
            ).model_dump()
        )

    # ===== 5. 竞价准备（auction_anchor_snapshots，最近一个 trade_date）=====
    from app.models.auction import AuctionAnchorSnapshot
    auction_date = await db.scalar(select(func.max(AuctionAnchorSnapshot.trade_date)))
    if auction_date is None:
        nodes.append(
            ProductionChainNode(
                key="auction", label="竞价准备", status="pending",
                detail="尚无竞价锚点快照", trade_date=None,
                publication_status="not_applicable",
                blocking_reason="无快照", recommended_action="触发竞价分析计算",
            ).model_dump()
        )
    else:
        auction_snap = await db.scalar(
            select(AuctionAnchorSnapshot)
            .where(AuctionAnchorSnapshot.trade_date == auction_date)
            .order_by(AuctionAnchorSnapshot.created_at.desc())
            .limit(1)
        )
        auction_ok = auction_snap is not None and auction_snap.status == "succeeded"
        nodes.append(
            ProductionChainNode(
                key="auction", label="竞价准备",
                status="ok" if auction_ok else ("running" if auction_snap and auction_snap.status == "running" else "failed"),
                detail=f"{auction_date} 状态：{auction_snap.status if auction_snap else '无'}"
                if auction_snap else "无竞价",
                trade_date=auction_date,
                run_id=str(auction_snap.id) if auction_snap else None,
                quality_gate="passed" if auction_ok else "failed",
                publication_status="not_applicable",
                blocking_reason=None if auction_ok else (auction_snap.status if auction_snap else "无快照"),
                recommended_action=None if auction_ok else "查看竞价分析结果",
            ).model_dump()
        )

    # ===== 6. 正式发布（StrategyRun 最新 published，限定 dsa_selector）=====
    # 必须关联 strategy_versions + strategy_definitions 限定 strategy_key='dsa_selector'，
    # 不能取所有 StrategyRun.status='published' 最新一条（其他策略也会产生 published run）。
    from app.models.strategy import StrategyDefinition as _SdModel
    from app.models.strategy import StrategyVersion as _SvModel
    from app.models.strategy_run import StrategyRun
    _dsa_selector_version_ids_subq = (
        select(_SvModel.id)
        .join(_SdModel, _SdModel.id == _SvModel.strategy_definition_id)
        .where(_SdModel.strategy_key == "dsa_selector")
        .subquery()
    )
    latest_published_run = await db.scalar(
        select(StrategyRun)
        .where(
            StrategyRun.status == "published",
            StrategyRun.strategy_version_id.in_(select(_dsa_selector_version_ids_subq)),
        )
        .order_by(StrategyRun.trade_date.desc())
        .limit(1)
    )
    if latest_published_run is None:
        nodes.append(
            ProductionChainNode(
                key="publish", label="正式发布", status="pending",
                detail="尚无正式发布", trade_date=None,
                publication_status="pending",
                blocking_reason="无 published run", recommended_action="等待 DSA 发布",
            ).model_dump()
        )
    else:
        nodes.append(
            ProductionChainNode(
                key="publish", label="正式发布",
                status="ok",
                detail=f"已发布至 {latest_published_run.trade_date}",
                trade_date=latest_published_run.trade_date,
                run_id=str(latest_published_run.id),
                quality_gate="passed",
                publication_status="published",
            ).model_dump()
        )

    return nodes


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
        MARKET_SESSION_AFTERNOON,
        MARKET_SESSION_CLOSED,
        MARKET_SESSION_LUNCH,
        MARKET_SESSION_MORNING,
        MARKET_SESSION_NON_TRADING_DAY,
        MARKET_SESSION_PRE_OPEN,
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
