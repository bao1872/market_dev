"""SystemOverview schema - 系统概览响应 DTO。

定义 /admin/system-overview 接口的响应结构，包含：
- 基础字段（12 个，向后兼容）：active_users/monitored_instruments/evaluations 等
- 新增字段（5 个）：server_time/business_date/market_session/monitor_runtime/after_close_pipeline
- Phase 9：after_close_pipeline.data_freshness 子结构（行情 + 选股两区块）

用法：
    python -m app.schemas.system_overview    # 自测：验证 schema 字段
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict


# [SystemOverview] - 监控运行时状态枚举
MONITOR_STATUS_RUNNING = "RUNNING"
MONITOR_STATUS_IDLE_EXPECTED = "IDLE_EXPECTED"
MONITOR_STATUS_SESSION_COMPLETED = "SESSION_COMPLETED"
MONITOR_STATUS_DELAYED = "DELAYED"
MONITOR_STATUS_FAILED = "FAILED"
MONITOR_STATUS_WORKER_OFFLINE = "WORKER_OFFLINE"
MONITOR_STATUS_NOT_APPLICABLE = "NOT_APPLICABLE"


# [SystemOverview] - 盘后流水线状态枚举
PIPELINE_STATUS_NOT_STARTED = "NOT_STARTED"
PIPELINE_STATUS_BARS_RUNNING = "BARS_RUNNING"
PIPELINE_STATUS_BARS_FAILED = "BARS_FAILED"
PIPELINE_STATUS_WAITING_DSA = "WAITING_DSA"
PIPELINE_STATUS_DSA_QUEUED = "DSA_QUEUED"
PIPELINE_STATUS_DSA_RUNNING = "DSA_RUNNING"
PIPELINE_STATUS_DSA_COMPLETED = "DSA_COMPLETED"
PIPELINE_STATUS_PUBLISHED = "PUBLISHED"
PIPELINE_STATUS_DSA_FAILED = "DSA_FAILED"
PIPELINE_STATUS_STALE = "STALE"


# [SystemOverview] - WAITING_DSA 细分原因枚举（7 种）
# 当 DSA 未成功 published 时，细分具体原因，便于前端展示可读建议
WAITING_DSA_REASON_NO_RUN_CREATED = "NO_RUN_CREATED"
WAITING_DSA_REASON_QUEUED_NOT_CLAIMED = "QUEUED_NOT_CLAIMED"
WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT = "DATA_COVERAGE_INSUFFICIENT"
WAITING_DSA_REASON_NO_RELEASED_VERSION = "NO_RELEASED_VERSION"
WAITING_DSA_REASON_RUN_FAILED = "RUN_FAILED"
WAITING_DSA_REASON_QUALITY_GATE_FAILED = "QUALITY_GATE_FAILED"
WAITING_DSA_REASON_PUBLISH_FAILED = "PUBLISH_FAILED"

# 全部 WAITING_DSA 原因集合（用于校验取值合法性）
ALL_WAITING_DSA_REASONS = {
    WAITING_DSA_REASON_NO_RUN_CREATED,
    WAITING_DSA_REASON_QUEUED_NOT_CLAIMED,
    WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT,
    WAITING_DSA_REASON_NO_RELEASED_VERSION,
    WAITING_DSA_REASON_RUN_FAILED,
    WAITING_DSA_REASON_QUALITY_GATE_FAILED,
    WAITING_DSA_REASON_PUBLISH_FAILED,
}

# [SystemOverview] - WAITING_DSA 原因 → 人类可读建议 映射
WAITING_DSA_SUGGESTIONS: dict[str, str] = {
    WAITING_DSA_REASON_NO_RUN_CREATED: (
        "检查 strategy_scheduler 是否在 18:30 触发；或手动调用 POST /admin/after-close-runs"
    ),
    WAITING_DSA_REASON_QUEUED_NOT_CLAIMED: (
        "检查 trading-worker-strategy-batch 容器是否健康"
    ),
    WAITING_DSA_REASON_DATA_COVERAGE_INSUFFICIENT: (
        "重新同步日线数据"
    ),
    WAITING_DSA_REASON_NO_RELEASED_VERSION: (
        "在管理员策略页发布 selector 版本"
    ),
    WAITING_DSA_REASON_RUN_FAILED: (
        "查看失败股票和 error_message"
    ),
    WAITING_DSA_REASON_QUALITY_GATE_FAILED: (
        "检查质量门禁配置和失败股票"
    ),
    WAITING_DSA_REASON_PUBLISH_FAILED: (
        "检查发布逻辑和 published_run 表"
    ),
}


class LatestSelectorRun(BaseModel):
    """dsa_selector 最近一次运行摘要。"""

    id: str
    status: str
    trade_date: date | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total_instruments: int | None = None
    succeeded_count: int | None = None
    failed_count: int | None = None


class MonitorRuntime(BaseModel):
    """监控运行时状态 - 反映 monitor_scheduler 当前工作状态。"""

    status: str
    heartbeat_at: str | None = None
    heartbeat_age_seconds: int | None = None
    business_date: str | None = None
    session_label: str | None = None
    session_job_status: str | None = None
    last_cycle_at: str | None = None
    last_source_bar_time: str | None = None
    evaluated_count: int = 0
    failed_count: int = 0
    freshness_seconds: int | None = None


class BarsJobSummary(BaseModel):
    """盘后 bars 任务摘要。"""

    status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None


class DsaRunSummary(BaseModel):
    """盘后 DSA 运行摘要。"""

    id: str | None = None
    status: str | None = None
    run_type: str | None = None
    attempt_no: int | None = None
    trade_date: date | None = None
    failed_count: int | None = None
    succeeded_count: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    failure_stage: str | None = None


class BarsFreshness(BaseModel):
    """[SystemOverview] - 行情数据新鲜度（6 项）。

    反映 bars 表各周期最新数据日期与覆盖情况，供管理员判断行情是否落后。
    """

    # 最新日线交易日（bars_daily.max(trade_date)）
    latest_daily_trade_date: date | None = None
    # 当日有日线数据的股票数 / 活跃股票总数（基于 latest_daily_trade_date）
    daily_coverage: float | None = None
    # 最新 15 分钟线时间（bars_15min.max(trade_time)，datetime ISO 字符串）
    latest_15m_bar_time: str | None = None
    # 最新 60 分钟线时间（bars_60min.max(trade_time)，datetime ISO 字符串）
    latest_60m_bar_time: str | None = None
    # 最近的 bars_scheduler succeeded 任务 id
    last_success_job_id: str | None = None
    # latest_daily_trade_date < 最近交易日（查 trading_calendar WHERE is_trading_day=true）
    is_behind_latest_trade_date: bool = False


class StrategyFreshness(BaseModel):
    """[SystemOverview] - 选股策略新鲜度（7 项）。

    反映 strategy_runs 表最新计算/发布状态，供管理员判断选股是否已发布。
    """

    # 最新计算交易日（strategy_runs.max(trade_date)，所有状态）
    latest_compute_trade_date: date | None = None
    # 最新发布交易日（strategy_runs.max(trade_date) WHERE status='published'）
    latest_published_trade_date: date | None = None
    # 最近一条 strategy_runs 的 id
    strategy_run_id: str | None = None
    # 最近一条 strategy_runs 的 status
    status: str | None = None
    # 最近一条 strategy_runs 的 total_instruments
    total_instruments: int | None = None
    # 最近一条 strategy_runs 的 failed_count
    failed_count: int | None = None
    # 最近一条 strategy_runs 的 published_at（ISO 字符串）
    published_at: str | None = None


class DataFreshness(BaseModel):
    """[SystemOverview] - 数据新鲜度子结构（行情 + 选股两区块）。

    管理员仪表盘最后数据日期展示，独立于流水线状态判定，
    始终基于 DB 实时查询，反映行情与选股的最新数据落盘情况。
    """

    bars: BarsFreshness = BarsFreshness()
    strategy: StrategyFreshness = StrategyFreshness()


class AfterClosePipeline(BaseModel):
    """盘后流水线状态 - bars 刷新 + DSA 计算的整体进度。"""

    status: str
    bars_job: BarsJobSummary | None = None
    dsa_run: DsaRunSummary | None = None
    # [SystemOverview] - WAITING_DSA 细分原因（7 种之一，仅 DSA 未 published 时填充）
    waiting_dsa_reason: str | None = None
    # [SystemOverview] - 原因对应的人类可读建议（与 waiting_dsa_reason 配对）
    waiting_dsa_suggestion: str | None = None
    # [SystemOverview] - 数据新鲜度子结构（行情 + 选股两区块，Phase 9）
    data_freshness: DataFreshness = DataFreshness()
    # [AfterClose] - 当日 after_close_orchestrator 任务 ID（供前端进入任务详情/断点继续/判断冲突任务）
    job_run_id: str | None = None
    # [AfterClose] - 编排状态（queued/refreshing_daily/.../succeeded/failed，来自 metadata_json）
    orchestrator_status: str | None = None
    # [AfterClose] - Worker 最后心跳（ISO 字符串，供前端判断 worker 是否在线）
    heartbeat_at: str | None = None
    # [AfterClose] - 租约到期时间（ISO 字符串）
    lease_expires_at: str | None = None
    # [AfterClose] - 最后成功步骤（断点检查点，来自 metadata_json.last_completed_step）
    last_completed_step: str | None = None


class SystemOverviewResponse(BaseModel):
    """系统概览响应 - 管理员仪表盘数据。

    包含 12 个基础字段（向后兼容）+ 5 个新增字段。
    """

    model_config = ConfigDict(from_attributes=True)

    # 基础字段（12 个，向后兼容）
    active_users: int = 0
    distinct_monitored_instruments: int = 0
    evaluations_last_minute: int = 0
    evaluations_success_rate: float = 0.0
    notification_delivery_rate: float = 0.0
    queue_backlog: int = 0
    failed_retry_count: int = 0
    latest_selector_run: LatestSelectorRun | None = None
    worker_health: str = "unknown"
    scheduler_health: str = "unknown"
    recent_scheduler_jobs: list[dict[str, Any]] = []
    recent_anomalies: list[dict[str, Any]] = []

    # 新增字段（5 个）
    server_time: str | None = None
    business_date: str | None = None
    market_session: str | None = None
    monitor_runtime: MonitorRuntime | None = None
    after_close_pipeline: AfterClosePipeline | None = None


if __name__ == "__main__":
    # 自测入口：验证 schema 字段（无副作用）
    print("=== system_overview schema 自测 ===")

    # 验证 MonitorRuntime 状态枚举
    monitor_statuses = {
        MONITOR_STATUS_RUNNING, MONITOR_STATUS_IDLE_EXPECTED,
        MONITOR_STATUS_SESSION_COMPLETED, MONITOR_STATUS_DELAYED,
        MONITOR_STATUS_FAILED, MONITOR_STATUS_WORKER_OFFLINE,
        MONITOR_STATUS_NOT_APPLICABLE,
    }
    assert len(monitor_statuses) == 7, f"monitor_status 应 7 值，实际 {len(monitor_statuses)}"
    print(f"monitor_statuses={sorted(monitor_statuses)}")

    # 验证 pipeline 状态枚举
    pipeline_statuses = {
        PIPELINE_STATUS_NOT_STARTED, PIPELINE_STATUS_BARS_RUNNING,
        PIPELINE_STATUS_BARS_FAILED, PIPELINE_STATUS_WAITING_DSA,
        PIPELINE_STATUS_DSA_QUEUED, PIPELINE_STATUS_DSA_RUNNING,
        PIPELINE_STATUS_DSA_COMPLETED, PIPELINE_STATUS_PUBLISHED,
        PIPELINE_STATUS_DSA_FAILED, PIPELINE_STATUS_STALE,
    }
    assert len(pipeline_statuses) == 10, f"pipeline_status 应 10 值，实际 {len(pipeline_statuses)}"
    print(f"pipeline_statuses={sorted(pipeline_statuses)}")

    # 验证 WAITING_DSA 原因枚举（7 种）
    assert len(ALL_WAITING_DSA_REASONS) == 7, (
        f"WAITING_DSA 原因应 7 值，实际 {len(ALL_WAITING_DSA_REASONS)}"
    )
    print(f"waiting_dsa_reasons={sorted(ALL_WAITING_DSA_REASONS)}")

    # 验证每个原因都有对应建议
    for reason in ALL_WAITING_DSA_REASONS:
        assert reason in WAITING_DSA_SUGGESTIONS, (
            f"原因 {reason} 缺少对应建议"
        )
    print(f"waiting_dsa_suggestions 覆盖 {len(WAITING_DSA_SUGGESTIONS)} 种原因 ✓")

    # 验证 AfterClosePipeline 新增字段（含 Phase 9 data_freshness + AfterClose 编排详情）
    pipeline_fields = set(AfterClosePipeline.model_fields.keys())
    expected_new_fields = {
        "waiting_dsa_reason",
        "waiting_dsa_suggestion",
        "data_freshness",
        # [AfterClose] - 编排任务详情字段（供前端进入任务详情/断点继续/判断冲突）
        "job_run_id",
        "orchestrator_status",
        "heartbeat_at",
        "lease_expires_at",
        "last_completed_step",
    }
    missing = expected_new_fields - pipeline_fields
    assert not missing, f"AfterClosePipeline 缺少字段: {missing}"
    print(f"AfterClosePipeline fields={sorted(pipeline_fields)}")

    # [Phase 9] 验证 DataFreshness 子结构字段完整性
    bars_fields = set(BarsFreshness.model_fields.keys())
    expected_bars = {
        "latest_daily_trade_date", "daily_coverage", "latest_15m_bar_time",
        "latest_60m_bar_time", "last_success_job_id", "is_behind_latest_trade_date",
    }
    assert bars_fields == expected_bars, f"BarsFreshness 字段不匹配: {bars_fields ^ expected_bars}"
    print(f"BarsFreshness fields count={len(bars_fields)} (expected 6)")

    strategy_fields = set(StrategyFreshness.model_fields.keys())
    expected_strategy = {
        "latest_compute_trade_date", "latest_published_trade_date", "strategy_run_id",
        "status", "total_instruments", "failed_count", "published_at",
    }
    assert strategy_fields == expected_strategy, (
        f"StrategyFreshness 字段不匹配: {strategy_fields ^ expected_strategy}"
    )
    print(f"StrategyFreshness fields count={len(strategy_fields)} (expected 7)")

    # 验证 DataFreshness 默认值可构建
    df = DataFreshness()
    assert df.bars.is_behind_latest_trade_date is False
    assert df.bars.latest_daily_trade_date is None
    assert df.strategy.latest_published_trade_date is None
    print("DataFreshness 空值构建 OK")

    # 验证 SystemOverviewResponse 字段
    fields = SystemOverviewResponse.model_fields
    expected_fields = {
        "active_users", "distinct_monitored_instruments", "evaluations_last_minute",
        "evaluations_success_rate", "notification_delivery_rate", "queue_backlog",
        "failed_retry_count", "latest_selector_run", "worker_health",
        "scheduler_health", "recent_scheduler_jobs", "recent_anomalies",
        "server_time", "business_date", "market_session",
        "monitor_runtime", "after_close_pipeline",
    }
    missing = expected_fields - set(fields.keys())
    assert not missing, f"缺少字段: {missing}"
    print(f"SystemOverviewResponse fields count={len(fields)} (expected 17)")

    # 验证空响应可构建
    resp = SystemOverviewResponse()
    assert resp.active_users == 0
    assert resp.worker_health == "unknown"
    assert resp.recent_scheduler_jobs == []
    print("空响应构建 OK")

    print("=== 自测结束 ===")
