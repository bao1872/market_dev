"""Shared persistence contract for after-close scheduler runs.

This module owns the small state/metadata boundary shared by the orchestrator,
recovery services, and read models.  It deliberately contains no orchestration
steps, worker dispatch, or business computation.
"""
from __future__ import annotations

import json
import logging
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.job_run_event_service import append_event

logger = logging.getLogger("after_close_run_contract")

AFTER_CLOSE_JOB_NAME = "after_close_orchestrator"


class AfterCloseRunStatus(StrEnum):
    """Persisted orchestration statuses, including legacy read compatibility."""

    QUEUED = "queued"
    REFRESHING_DAILY = "refreshing_daily"
    SYNCING_BOARDS = "syncing_boards"
    CHECKING_COVERAGE = "checking_coverage"
    # [REVIEW-V2-R1] canonical 复盘计算（Market Dashboard projection 重建）。
    # 语义边界：它是「当前正在执行的运行状态」，**不是 durable checkpoint** ——
    # 允许进入 AfterCloseRunStatus 与 _PIPELINE_STEPS，但不得进入 _CHECKPOINT_ORDER /
    # _COMPLETED_STEPS current stage / _COMPLETED_STEP_INDEX 的 key。
    REBUILDING_MARKET_DASHBOARD = "rebuilding_market_dashboard"
    CREATING_DSA = "creating_dsa"
    WAITING_DSA_WORKER = "waiting_dsa_worker"
    QUALITY_GATE = "quality_gate"
    FEATURE_SNAPSHOT = "feature_snapshot"
    COMPUTING_FEATURES = "computing_features"
    PUBLISHING = "publishing"
    COMPUTING_HISTORY = "computing_history"
    # [REVIEW-V2-R1] legacy persisted token：仅供历史 job_run_event / last_completed_step
    # 读取兼容。新 run 不再写、不再执行；不得作为合法 current mainchain_stage（fail closed）。
    COMPUTING_REVIEW = "computing_review"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL_SUCCESS = "partial_success"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


def parse_after_close_metadata(job_run: SchedulerJobRun) -> dict[str, Any]:
    """Decode scheduler metadata; malformed historical rows remain readable."""
    if not job_run.metadata_json:
        return {}
    try:
        return json.loads(job_run.metadata_json)
    except (json.JSONDecodeError, TypeError) as exc:
        logger.warning(
            "[AfterClose] metadata_json 解析失败 job_run_id=%s: %s",
            job_run.id,
            exc,
        )
        return {}


async def update_after_close_status(
    db: AsyncSession,
    job_run: SchedulerJobRun,
    status: AfterCloseRunStatus,
    message: str = "",
    payload: dict[str, Any] | None = None,
    dsa_run_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Update metadata and append its matching event without committing."""
    existing_meta = parse_after_close_metadata(job_run)
    trade_date_str = existing_meta.get("trade_date")
    if dsa_run_id is None:
        dsa_run_id_str = existing_meta.get("dsa_run_id")
        dsa_run_id = uuid.UUID(dsa_run_id_str) if dsa_run_id_str else None

    if trade_date_str is None and extra and "trade_date" in extra:
        trade_date_str = extra["trade_date"]

    new_meta: dict[str, Any] = dict(existing_meta)
    new_meta["orchestrator_status"] = status.value
    if trade_date_str is not None:
        new_meta["trade_date"] = trade_date_str
    if dsa_run_id is not None:
        new_meta["dsa_run_id"] = str(dsa_run_id)
    if extra:
        for key, value in extra.items():
            if key not in ("orchestrator_status", "trade_date", "dsa_run_id"):
                new_meta[key] = value

    job_run.metadata_json = json.dumps(new_meta, ensure_ascii=False)
    await db.flush()

    event_payload = dict(payload) if payload else {}
    event_payload["orchestrator_status"] = status.value
    await append_event(
        db=db,
        job_run_id=job_run.id,
        step=status.value,
        level="info" if status != AfterCloseRunStatus.FAILED else "error",
        message=message or f"编排状态切换: {status.value}",
        payload=event_payload,
    )
    await db.flush()


__all__ = [
    "AFTER_CLOSE_JOB_NAME",
    "AfterCloseRunStatus",
    "parse_after_close_metadata",
    "update_after_close_status",
]
