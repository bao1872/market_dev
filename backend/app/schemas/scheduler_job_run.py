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
