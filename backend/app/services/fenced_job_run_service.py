"""Shared fenced lease helpers for long-running scheduler jobs.

A lease token is valid only while the referenced job remains ``running`` and
both ``worker_instance_id`` and ``lease_epoch`` still match.  Callers use the
same predicate for heartbeats, business writes, and terminal updates so a
stale worker cannot write after a watchdog/reclaimer has transferred ownership.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import AsyncSessionLocal
from app.models.scheduler_job_run import SchedulerJobRun

logger = logging.getLogger(__name__)
_TZ = ZoneInfo("Asia/Shanghai")


class JobLeaseLostError(RuntimeError):
    """Raised when a worker no longer owns the scheduler job lease."""


@dataclass(frozen=True, slots=True)
class FencedJobToken:
    job_run_id: uuid.UUID
    worker_instance_id: str
    lease_epoch: int
    lease_seconds: int


@dataclass(frozen=True, slots=True)
class ClaimedFencedJob:
    token: FencedJobToken
    previous_status: str
    metadata: dict[str, Any]


async def claim_next_job_run(
    db: AsyncSession,
    *,
    job_name: str,
    worker_instance_id: str,
    lease_seconds: int,
    eligible_statuses: tuple[str, ...] = ("queued", "resume_queued"),
    now: datetime | None = None,
) -> ClaimedFencedJob | None:
    """Atomically claim the oldest eligible job and increment its fence epoch."""
    claimed_at = now or datetime.now(_TZ)
    result = await db.execute(
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name == job_name,
            SchedulerJobRun.status.in_(eligible_statuses),
        )
        .order_by(SchedulerJobRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job_run = result.scalar_one_or_none()
    if job_run is None:
        return None

    previous_status = job_run.status
    job_run.status = "running"
    job_run.worker_instance_id = worker_instance_id
    job_run.started_at = job_run.started_at or claimed_at
    job_run.heartbeat_at = claimed_at
    job_run.lease_expires_at = claimed_at + timedelta(seconds=lease_seconds)
    job_run.lease_epoch += 1
    job_run.finished_at = None
    await db.flush()

    metadata: dict[str, Any] = {}
    if job_run.metadata_json:
        try:
            metadata = json.loads(job_run.metadata_json)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    return ClaimedFencedJob(
        token=FencedJobToken(
            job_run_id=job_run.id,
            worker_instance_id=worker_instance_id,
            lease_epoch=job_run.lease_epoch,
            lease_seconds=lease_seconds,
        ),
        previous_status=previous_status,
        metadata=metadata,
    )


def _owned_job_predicates(token: FencedJobToken) -> tuple[Any, ...]:
    return (
        SchedulerJobRun.id == token.job_run_id,
        SchedulerJobRun.status == "running",
        SchedulerJobRun.worker_instance_id == token.worker_instance_id,
        SchedulerJobRun.lease_epoch == token.lease_epoch,
    )


async def lock_owned_job_run(
    db: AsyncSession,
    token: FencedJobToken,
) -> SchedulerJobRun:
    """Lock and return the job only if ``token`` is still the live owner."""
    result = await db.execute(
        select(SchedulerJobRun)
        .where(*_owned_job_predicates(token))
        .with_for_update()
    )
    job_run = result.scalar_one_or_none()
    if job_run is None:
        raise JobLeaseLostError(
            f"scheduler job lease lost: job_run_id={token.job_run_id} "
            f"worker={token.worker_instance_id} epoch={token.lease_epoch}"
        )
    return job_run


async def refresh_job_lease(
    token: FencedJobToken,
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    now: datetime | None = None,
) -> bool:
    """Refresh heartbeat and lease using the full fencing predicate."""
    heartbeat_at = now or datetime.now(_TZ)
    async with session_factory() as db:
        result = await db.execute(
            update(SchedulerJobRun)
            .where(*_owned_job_predicates(token))
            .values(
                heartbeat_at=heartbeat_at,
                lease_expires_at=heartbeat_at + timedelta(seconds=token.lease_seconds),
                updated_at=heartbeat_at,
            )
        )
        if getattr(result, "rowcount", 0) != 1:
            await db.rollback()
            return False
        await db.commit()
        return True


async def finalize_job_run(
    token: FencedJobToken,
    *,
    status: str,
    metadata_updates: dict[str, Any],
    total_count: int,
    succeeded_count: int,
    failed_count: int,
    error_code: str | None = None,
    error_message: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    now: datetime | None = None,
) -> bool:
    """Write a terminal state iff the caller still owns the current lease."""
    if status not in {"succeeded", "failed", "skipped"}:
        raise ValueError(f"unsupported terminal status: {status}")
    finished_at = now or datetime.now(_TZ)
    async with session_factory() as db:
        try:
            job_run = await lock_owned_job_run(db, token)
        except JobLeaseLostError:
            await db.rollback()
            return False

        metadata: dict[str, Any] = {}
        if job_run.metadata_json:
            try:
                metadata = json.loads(job_run.metadata_json)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        metadata.update(metadata_updates)

        job_run.status = status
        job_run.finished_at = finished_at
        job_run.heartbeat_at = finished_at
        job_run.lease_expires_at = None
        job_run.worker_instance_id = None
        job_run.total_count = total_count
        job_run.succeeded_count = succeeded_count
        job_run.failed_count = failed_count
        job_run.progress = 1.0
        job_run.error_code = error_code
        job_run.error_message = error_message
        job_run.metadata_json = json.dumps(metadata, ensure_ascii=False)
        await db.commit()
        return True


async def merge_job_run_metadata(
    job_run_id: uuid.UUID,
    metadata_updates: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
) -> bool:
    """[Corrective-3 §二.4] 在任务终态之后合并治理 metadata。

    chip publication 发生在 `finalize_job_run` 之后（租约已释放），因此不能复用
    fenced 路径。本函数只做 metadata 合并，不修改 status/count 等终态字段，
    用于把 publication 成功/失败结果落到 SchedulerJobRun，使其可被治理与巡检。

    Returns:
        True 表示已写入；False 表示 job_run 不存在。
    """
    async with session_factory() as db:
        job_run = await db.get(SchedulerJobRun, job_run_id)
        if job_run is None:
            await db.rollback()
            return False

        metadata: dict[str, Any] = {}
        if job_run.metadata_json:
            try:
                metadata = json.loads(job_run.metadata_json)
            except (json.JSONDecodeError, TypeError):
                metadata = {}
        metadata.update(metadata_updates)
        job_run.metadata_json = json.dumps(metadata, ensure_ascii=False)
        await db.commit()
        return True


class FencedJobHeartbeat:
    """Background heartbeat whose failure permanently invalidates the token."""

    def __init__(
        self,
        token: FencedJobToken,
        *,
        interval_seconds: float = 30.0,
        refresh: Callable[[FencedJobToken], Awaitable[bool]] | None = None,
    ) -> None:
        self.token = token
        self.interval_seconds = interval_seconds
        self._refresh = refresh or refresh_job_lease
        self._task: asyncio.Task[None] | None = None
        self._lost = asyncio.Event()

    @property
    def task(self) -> asyncio.Task[None] | None:
        return self._task

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("heartbeat already started")
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval_seconds)
                try:
                    refreshed = await self._refresh(self.token)
                except Exception:
                    logger.exception(
                        "job heartbeat failed: job_run_id=%s epoch=%s",
                        self.token.job_run_id,
                        self.token.lease_epoch,
                    )
                    self._lost.set()
                    return
                if not refreshed:
                    self._lost.set()
                    return
        except asyncio.CancelledError:
            raise

    def ensure_owned(self) -> None:
        if self._lost.is_set():
            raise JobLeaseLostError(
                f"scheduler job heartbeat lost ownership: {self.token.job_run_id}"
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
