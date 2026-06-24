from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateDSABackfillRequest(BaseModel):
    start_date: date
    end_date: date
    skip_published: bool = True
    auto_publish: bool = True
    max_workers: int = Field(4, ge=1, le=16)


class DSABackfillJobResponse(BaseModel):
    backfill_job_id: UUID
    status: str
    target_trade_dates: int
    total_stocks: int
    start_date: date
    end_date: date
    auto_publish: bool
    created_at: datetime


class DSABackfillSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    backfill_job_id: UUID
    status: str
    strategy_version_id: UUID
    start_date: date
    end_date: date
    target_trade_dates: int
    target_trade_dates_list: list[date]
    total_stocks: int
    processed_stocks: int
    succeeded_stocks: int
    failed_stocks: int
    selected_result_count: int
    current_instrument_id: UUID | None
    error_summary: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class DSABackfillInstrumentProgressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    instrument_id: UUID
    symbol: str
    status: str
    attempt_count: int
    result_count: int
    error_code: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None


class DSABackfillInstrumentProgressListResponse(BaseModel):
    items: list[DSABackfillInstrumentProgressResponse]
    total: int


class DSABackfillDateRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    run_id: UUID
    trade_date: date
    status: str
    total_instruments: int | None
    succeeded_count: int | None
    failed_count: int | None
    skipped_count: int | None
    published_at: datetime | None


class DSABackfillDateRunListResponse(BaseModel):
    items: list[DSABackfillDateRunResponse]
    total: int


class DSABackfillRetryResponse(BaseModel):
    backfill_job_id: UUID
    retried_count: int
    status: str


class DSABackfillCancelResponse(BaseModel):
    backfill_job_id: UUID
    status: str
