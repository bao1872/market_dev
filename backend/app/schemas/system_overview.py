"""SystemOverview schema - 系统概览响应 DTO。

定义 /admin/system-overview 接口的响应结构，包含：
- 基础字段（12 个，向后兼容）：active_users/monitored_instruments/evaluations 等
- 新增字段（5 个）：server_time/business_date/market_session/monitor_runtime/after_close_pipeline

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


class AfterClosePipeline(BaseModel):
    """盘后流水线状态 - bars 刷新 + DSA 计算的整体进度。"""

    status: str
    bars_job: BarsJobSummary | None = None
    dsa_run: DsaRunSummary | None = None
    # [SystemOverview] - WAITING_DSA 细分原因（7 种之一，仅 DSA 未 published 时填充）
    waiting_dsa_reason: str | None = None
    # [SystemOverview] - 原因对应的人类可读建议（与 waiting_dsa_reason 配对）
    waiting_dsa_suggestion: str | None = None


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

    # 验证 AfterClosePipeline 新增字段
    pipeline_fields = set(AfterClosePipeline.model_fields.keys())
    expected_new_fields = {"waiting_dsa_reason", "waiting_dsa_suggestion"}
    missing = expected_new_fields - pipeline_fields
    assert not missing, f"AfterClosePipeline 缺少字段: {missing}"
    print(f"AfterClosePipeline fields={sorted(pipeline_fields)}")

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
