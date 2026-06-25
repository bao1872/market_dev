"""SchedulerJobRun schema - 定时任务运行记录 DTO。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class SchedulerJobRunItem(BaseModel):
    """单个定时任务运行记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_name: str
    business_date: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    worker_instance_id: str | None = None
    last_cycle_at: datetime | None = None
    total_count: int | None = None
    succeeded_count: int | None = None
    failed_count: int | None = None
    progress: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata_json: str | None = None
    created_at: datetime
    updated_at: datetime


class SchedulerJobRunListResponse(BaseModel):
    """定时任务运行记录列表响应。"""

    items: list[SchedulerJobRunItem]
    total: int
    limit: int
    offset: int


class RecentSchedulerJobSummary(BaseModel):
    """系统概览中的最近定时任务摘要。"""

    job_name: str
    status: str
    business_date: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: float | None = None
    succeeded_count: int | None = None
    failed_count: int | None = None
    error_message: str | None = None


# [JobRunEvent] - 任务事件时间线 DTO
class JobRunEventItem(BaseModel):
    """单条任务执行事件。"""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_run_id: UUID
    step: str
    level: str
    message: str
    payload: dict | None = None
    created_at: datetime


class JobRunEventListResponse(BaseModel):
    """任务事件时间线响应。"""

    items: list[JobRunEventItem]
    total: int


# [AfterClose] - 盘后编排状态响应 DTO
class AfterCloseRunStatusResponse(BaseModel):
    """盘后编排任务状态响应（含编排状态 + DSA run 状态 + 事件时间线 + [Phase7] 详情）。"""

    job_run_id: str
    job_name: str
    business_date: str | None = None
    status: str  # SchedulerJobRun.status: running/succeeded/failed/interrupted
    orchestrator_status: str  # AfterCloseRunStatus: queued/refreshing_daily/.../succeeded/failed
    trade_date: str | None = None
    dsa_run_id: str | None = None
    dsa_run_status: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None
    # [Phase7] - 详情字段（管理后台展示）
    worker_instance_id: str | None = None  # Worker 实例标识
    heartbeat_at: str | None = None  # 最后心跳时间（ISO 格式）
    lease_expires_at: str | None = None  # 租约到期时间（ISO 格式）
    last_completed_step: str | None = None  # 最后成功步骤（断点检查点）
    interrupt_reason: str | None = None  # 中断原因（error_code: error_message）
    is_retryable: bool = False  # 是否允许重试（status in failed/interrupted）
    heartbeat_stale: bool = False  # 心跳是否超时（running 且 heartbeat_at > 60s 前）
    events: list[JobRunEventItem] = []


class AfterCloseRunCreateResponse(BaseModel):
    """盘后编排任务创建/重试响应。"""

    job_run_id: str
    status: str
    orchestrator_status: str
    trade_date: str
    message: str
