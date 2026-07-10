"""盘后流水线可视化 API 的 Pydantic 响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.system_overview import DataFreshness


class PipelineStep(BaseModel):
    """单个流水线步骤状态。"""

    step: str
    status: str  # pending / running / completed / failed / skipped
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    counts: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


class AfterCloseRunSummary(BaseModel):
    """after_close_orchestrator job_run 摘要。"""

    job_run_id: str
    status: str
    orchestrator_status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    lease_expires_at: str | None = None
    last_completed_step: str | None = None
    error_message: str | None = None
    worker_instance_id: str | None = None
    trade_date: str | None = None
    feature_snapshot_run_id: str | None = None
    feature_snapshot_progress: dict[str, Any] | None = None
    feature_snapshot_stalled: bool = False


class FeatureSnapshotRunSummary(BaseModel):
    """stock_feature_snapshot_run 摘要。"""

    run_id: str
    run_type: str
    status: str
    scope: str
    snapshot_count: int | None = None
    failed_count: int | None = None
    skipped_count: int | None = None
    expected_count: int | None = None
    published_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class PipelineEventItem(BaseModel):
    """job_run_event 时间线项。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    job_run_id: str
    step: str
    level: str
    message: str
    payload: dict[str, Any] | None = None
    created_at: str | None = None


class AfterClosePipelineResponse(BaseModel):
    """盘后流水线聚合状态响应。"""

    trade_date: str
    market_session: str
    overall_status: str  # not_started / running / succeeded / failed / blocked / skipped
    watchlist_ready: bool
    watchlist_reason: str
    has_backfill_full: bool = False
    after_close_run: AfterCloseRunSummary | None = None
    steps: list[PipelineStep]
    data_freshness: DataFreshness
    feature_snapshot_run: FeatureSnapshotRunSummary | None = None
    feature_snapshot_lost_contact: bool = False
    feature_snapshot_stalled: bool = False
    events: list[PipelineEventItem]


class PipelineRunItem(BaseModel):
    """最近运行列表中的单条记录（after_close_orchestrator 或 snapshot_run）。"""

    kind: str  # after_close_orchestrator / snapshot_run
    # after_close_orchestrator 字段
    job_run_id: str | None = None
    # snapshot_run 字段
    run_id: str | None = None
    trade_date: str | None = None
    status: str
    orchestrator_status: str | None = None
    run_type: str | None = None
    scope: str | None = None
    snapshot_count: int | None = None
    failed_count: int | None = None
    published_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    worker_instance_id: str | None = None
    last_completed_step: str | None = None


class AfterClosePipelineRunListResponse(BaseModel):
    """最近运行列表响应。"""

    items: list[PipelineRunItem]
    total: int


class AfterClosePipelineRunRequest(BaseModel):
    """POST /admin/after-close/pipeline/run 请求体。"""

    trade_date: str


class AfterClosePipelineRunResponse(BaseModel):
    """POST /admin/after-close/pipeline/run 响应。"""

    job_run_id: str
    trade_date: str
    status: str
    orchestrator_status: str | None = None
    is_new: bool


if __name__ == "__main__":
    # 自测入口：验证模型可实例化
    from app.schemas.system_overview import BarsFreshness, StrategyFreshness

    response = AfterClosePipelineResponse(
        trade_date="2026-07-08",
        market_session="MARKET_CLOSED",
        overall_status="not_started",
        watchlist_ready=False,
        watchlist_reason="test",
        steps=[
            PipelineStep(step="refreshing_daily", status="pending"),
        ],
        data_freshness=DataFreshness(
            bars=BarsFreshness(),
            strategy=StrategyFreshness(),
        ),
        events=[],
    )
    print(response.model_dump())
