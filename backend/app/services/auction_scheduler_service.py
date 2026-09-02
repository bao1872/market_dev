"""[P0-3 修复 2026-07-31] 竞价分析调度服务 - 接入现有 SchedulerJobRun 框架。

设计目标（PRD75 §3.1 / ref/instruction.md §三.3）：
1. **09:25:05 Asia/Shanghai 创建 auction_final:{date}**：
   最终竞价扫描任务（基于已发布锚点分析竞价价格位置和事件）。
2. **10:00 Asia/Shanghai 创建 auction_open_confirmation:{date}**：
   开盘确认任务（基于开盘后窗口数据更新事件生命周期）。
3. **接入现有 Scheduler/Worker，不新建容器**：
   - 使用 SchedulerJobRun、run_key、heartbeat、lease、fencing、retry 和恢复
   - 新增 WORKER_TYPE=auction_scheduler 分支（与 bars_scheduler/calendar_scheduler 等并列）
4. **幂等**：同 run_key 已 queued/running 时 acquire_job_run_lock 返回 (existing, False)，
   调用方应 SKIPPED_DUPLICATE。

调用链：
- run_auction_scheduler_worker → _auction_scheduler_poll_once
  - 检查时间窗口：09:25:05 ± 30s 创建 auction_final job；10:00 ± 30s 创建 auction_open_confirmation job
  - 领取 queued auction job → execute_auction_scan_run / execute_auction_open_confirmation

约束：
- 锚点未发布时 AuctionScanRun 抛 AnchorNotPublishedError，Worker 标记 failed 并记录
- chip 未完成 → 锚点为 structure_only（仍可扫描）；chip 完成后回调重建锚点
- 仅交易日执行（is_trading_day_async）
- 不新增常驻容器，仅新增 WORKER_TYPE 分支

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.auction_scheduler_service
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.services.idempotency_service import acquire_job_run_lock

logger = logging.getLogger(__name__)

# =============================================================================
# 常量与合同
# =============================================================================

# Scheduler job names（与 after_close_chip_consensus 同级）
AUCTION_FINAL_JOB_NAME = "auction_final"
AUCTION_OPEN_CONFIRMATION_JOB_NAME = "auction_open_confirmation"

# run_key 前缀（与 chip_consensus 风格一致）
AUCTION_FINAL_RUN_KEY_PREFIX = "auction_final"
AUCTION_OPEN_CONFIRMATION_RUN_KEY_PREFIX = "auction_open_confirmation"

# 租约时长：auction_final 扫描全市场约 5-10 分钟；open_confirmation 较快
AUCTION_FINAL_LEASE_SECONDS = 1800  # 30 分钟
AUCTION_OPEN_CONFIRMATION_LEASE_SECONDS = 600  # 10 分钟

# 触发时间窗口（Asia/Shanghai）
# 09:25:05 ± 30s 容差，避免时钟漂移错过触发
AUCTION_FINAL_TRIGGER_HOUR = 9
AUCTION_FINAL_TRIGGER_MINUTE = 25
AUCTION_FINAL_TRIGGER_SECOND = 5
AUCTION_FINAL_TRIGGER_TOLERANCE_SECONDS = 30

# 10:00 ± 30s 容差
AUCTION_OPEN_CONFIRMATION_TRIGGER_HOUR = 10
AUCTION_OPEN_CONFIRMATION_TRIGGER_MINUTE = 0
AUCTION_OPEN_CONFIRMATION_TRIGGER_SECOND = 0
AUCTION_OPEN_CONFIRMATION_TRIGGER_TOLERANCE_SECONDS = 30

# Worker 轮询间隔
AUCTION_SCHEDULER_POLL_INTERVAL = 30  # 30s 轮询一次（09:25/10:00 窗口内更频繁）

_TZ_SHANGHAI = ZoneInfo("Asia/Shanghai")


async def run_verified_auction_pipeline(
    db: AsyncSession,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    test_namespace: str = "production",
    expected_symbols: list[tuple[str, str]] | None = None,
    providers: list[Any] | None = None,
) -> dict[str, Any]:
    """采集来源证据、验证真值、扫描、聚合并切换正式 pointer。"""
    from app.models.instrument import Instrument
    from app.services.auction_aggregation_service import compute_auction_aggregation
    from app.services.auction_publication_service import publish_auction_analysis
    from app.services.auction_quote_capture_service import capture_auction_final_quotes
    from app.services.auction_quote_provider import MootdxAuctionQuoteProvider
    from app.services.auction_scan_service import (
        AnchorExpiredError,
        AnchorNotPublishedError,
        run_auction_scan,
    )
    from app.services.auction_truth_service import (
        StaticAuctionQuoteProvider,
        VerifiedAuctionQuoteProvider,
        aggregate_auction_truth,
        fetch_quote_sources,
    )
    from app.services.auction_v32_analysis_service import run_v32_auction_analysis

    if expected_symbols is None:
        rows = (await db.execute(select(Instrument.symbol, Instrument.market).where(
            Instrument.status == "active",
            Instrument.symbol.op("~")(r"^\d{6}$"),
        ))).all()
        expected_symbols = [(str(symbol), str(market)) for symbol, market in rows]

    resolved_providers = providers or [MootdxAuctionQuoteProvider()]
    source_batches = await fetch_quote_sources(resolved_providers, expected_symbols)
    source_capture_ids: list[str] = []
    for source_id, family, quotes in source_batches:
        source_capture = await capture_auction_final_quotes(
            db,
            trade_date,
            test_namespace=test_namespace,
            provider=StaticAuctionQuoteProvider(
                quotes, source_id=source_id, provider_family=family,
            ),
            worker_id=worker_id,
            expected_symbols=expected_symbols,
        )
        source_capture_ids.append(str(source_capture["capture_run_id"]))

    truth = aggregate_auction_truth(
        (quotes for _, _, quotes in source_batches),
        expected_symbols=expected_symbols,
        provider_families=(family for _, family, _ in source_batches),
    )
    if truth["status"] != "verified":
        return {
            "status": truth["status"],
            "reason": (
                "blocked_external_auction_truth_source"
                if truth["status"] == "blocked_external"
                else f"auction_truth_{truth['status']}"
            ),
            "truth_status": truth["status"],
            "truth_coverage": truth["coverage"],
            "source_capture_run_ids": source_capture_ids,
            "decisions": [
                {
                    "symbol": decision.symbol,
                    "market": decision.market,
                    "status": decision.status,
                    "reason_codes": list(decision.reason_codes),
                }
                for decision in truth["decisions"]
            ],
        }

    consensus_capture = await capture_auction_final_quotes(
        db,
        trade_date,
        test_namespace=test_namespace,
        provider=VerifiedAuctionQuoteProvider(truth["verified_quotes"]),
        worker_id=worker_id,
        expected_symbols=expected_symbols,
    )
    # ------------------------------------------------------------------
    # V3.2 lane — committed in the SAME transaction as the pipeline.  A legacy
    # Anchor *precondition* failure (AnchorNotPublishedError / AnchorExpiredError)
    # is isolated below so it cannot roll V3.2 back.  The two lanes report
    # separate statuses; neither masquerades as the other.
    # ------------------------------------------------------------------
    v32_outcome = await run_v32_auction_analysis(
        db,
        trade_date=trade_date,
        capture_run_id=consensus_capture["capture_run_id"],
        worker_id=worker_id,
        lease_epoch=lease_epoch,
        truth_status="verified",
        test_namespace=test_namespace,
    )
    v32_fields = {
        "v32_status": v32_outcome.status,
        "v32_run_id": v32_outcome.run_id,
        "v32_scope_count": v32_outcome.scope_count,
        "v32_detail": v32_outcome.detail,
    }

    # Legacy lane runs AFTER V3.2.  V3.2 has already flushed its ScopeResult
    # and publication into this same transaction, so a legacy Anchor *precondition*
    # failure must NOT abort the pipeline: that would roll back V3.2 too.  Only
    # these two known legacy precondition errors are isolated; everything else
    # (including AuctionScanConflictError) is left to propagate to the caller.
    try:
        scan = await run_auction_scan(
            db,
            trade_date,
            auction_type="final",
            worker_id=worker_id,
            lease_epoch=lease_epoch,
        )
    except (AnchorNotPublishedError, AnchorExpiredError) as exc:
        return {
            **v32_fields,
            "status": "legacy_unavailable",
            "legacy_status": "unavailable",
            "legacy_reason": (
                "anchor_not_published"
                if isinstance(exc, AnchorNotPublishedError)
                else "anchor_expired"
            ),
            "truth_status": "verified",
            "capture_run_id": consensus_capture["capture_run_id"],
        }

    run_id = scan.get("run_id")
    if run_id is None or scan.get("status") not in ("succeeded", "partial"):
        # legacy lane failed/skipped: V3.2 has ALREADY run and keeps its own status
        return {
            **scan,
            **v32_fields,
            "truth_status": "verified",
            "capture_run_id": consensus_capture["capture_run_id"],
        }

    aggregation = await compute_auction_aggregation(db, run_id)
    publication = await publish_auction_analysis(
        db,
        scan_run_id=run_id,
        capture_run_id=consensus_capture["capture_run_id"],
        truth_status="verified",
        test_namespace=test_namespace,
    )
    return {
        **scan,
        **v32_fields,
        "truth_status": "verified",
        "truth_coverage": truth["coverage"],
        "capture_run_id": consensus_capture["capture_run_id"],
        "source_capture_run_ids": source_capture_ids,
        "aggregation_status": "succeeded",
        "aggregation": aggregation,
        "publication_id": publication.id,
    }


# =============================================================================
# Job 创建（幂等）
# =============================================================================


async def create_auction_final_job(
    db: AsyncSession,
    trade_date: date,
    *,
    worker_instance_id: str | None = None,
) -> tuple[SchedulerJobRun | None, bool]:
    """[P0-3] 创建 auction_final:{date} 任务（幂等）。

    09:25:05 Asia/Shanghai 触发，基于已发布锚点扫描最终竞价。

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        trade_date: 业务交易日
        worker_instance_id: Worker 实例标识

    Returns:
        (SchedulerJobRun | None, is_new)：
        - (job_run, True)：本次新建任务
        - (job_run, False)：同日已有活跃任务（SKIPPED_DUPLICATE）
        - (None, False)：抢锁失败
    """
    run_key = f"{AUCTION_FINAL_RUN_KEY_PREFIX}:{trade_date.isoformat()}"
    metadata: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "auction_type": "final",
        "trigger_time": "09:25:05 Asia/Shanghai",
    }
    return await acquire_job_run_lock(
        db=db,
        run_key=run_key,
        job_name=AUCTION_FINAL_JOB_NAME,
        business_date=trade_date.isoformat(),
        lease_seconds=AUCTION_FINAL_LEASE_SECONDS,
        metadata=metadata,
        worker_instance_id=worker_instance_id,
        initial_status="queued",  # 由 Worker 领取
    )


async def create_auction_open_confirmation_job(
    db: AsyncSession,
    trade_date: date,
    *,
    worker_instance_id: str | None = None,
) -> tuple[SchedulerJobRun | None, bool]:
    """[P0-3] 创建 auction_open_confirmation:{date} 任务（幂等）。

    10:00 Asia/Shanghai 触发，基于开盘后窗口数据更新事件生命周期。

    Args:
        db: 异步会话（不 commit，由调用方控制事务）
        trade_date: 业务交易日
        worker_instance_id: Worker 实例标识

    Returns:
        (SchedulerJobRun | None, is_new)：
        - (job_run, True)：本次新建任务
        - (job_run, False)：同日已有活跃任务
        - (None, False)：抢锁失败
    """
    run_key = f"{AUCTION_OPEN_CONFIRMATION_RUN_KEY_PREFIX}:{trade_date.isoformat()}"
    metadata: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "auction_type": "open_confirmation",
        "trigger_time": "10:00:00 Asia/Shanghai",
    }
    return await acquire_job_run_lock(
        db=db,
        run_key=run_key,
        job_name=AUCTION_OPEN_CONFIRMATION_JOB_NAME,
        business_date=trade_date.isoformat(),
        lease_seconds=AUCTION_OPEN_CONFIRMATION_LEASE_SECONDS,
        metadata=metadata,
        worker_instance_id=worker_instance_id,
        initial_status="queued",
    )


# =============================================================================
# Job 查询
# =============================================================================


async def get_queued_auction_job(
    db: AsyncSession,
) -> SchedulerJobRun | None:
    """[P0-3] 查询一条 queued 状态的 auction job（FOR UPDATE SKIP LOCKED）。

    Returns:
        SchedulerJobRun 或 None（无 queued 任务）
    """
    stmt = (
        select(SchedulerJobRun)
        .where(
            SchedulerJobRun.job_name.in_(
                (AUCTION_FINAL_JOB_NAME, AUCTION_OPEN_CONFIRMATION_JOB_NAME)
            ),
            SchedulerJobRun.status == "queued",
        )
        .order_by(SchedulerJobRun.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_auction_final_job_for_date(
    db: AsyncSession,
    trade_date: date,
) -> SchedulerJobRun | None:
    """查询指定 trade_date 的 auction_final 任务（取最新一条）。"""
    run_key = f"{AUCTION_FINAL_RUN_KEY_PREFIX}:{trade_date.isoformat()}"
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_auction_open_confirmation_job_for_date(
    db: AsyncSession,
    trade_date: date,
) -> SchedulerJobRun | None:
    """查询指定 trade_date 的 auction_open_confirmation 任务（取最新一条）。"""
    run_key = f"{AUCTION_OPEN_CONFIRMATION_RUN_KEY_PREFIX}:{trade_date.isoformat()}"
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


# =============================================================================
# 时间窗口检查
# =============================================================================


def _is_in_trigger_window(
    now: datetime,
    *,
    hour: int,
    minute: int,
    second: int,
    tolerance_seconds: int,
) -> bool:
    """[P0-3] 检查当前时间是否在触发窗口内（±tolerance_seconds）。"""
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    delta = abs((now - target).total_seconds())
    return delta <= tolerance_seconds


def should_create_auction_final_job(now: datetime) -> bool:
    """检查当前时间是否应该创建 auction_final 任务（09:25:05 ± 30s）。"""
    return _is_in_trigger_window(
        now,
        hour=AUCTION_FINAL_TRIGGER_HOUR,
        minute=AUCTION_FINAL_TRIGGER_MINUTE,
        second=AUCTION_FINAL_TRIGGER_SECOND,
        tolerance_seconds=AUCTION_FINAL_TRIGGER_TOLERANCE_SECONDS,
    )


def should_create_auction_open_confirmation_job(now: datetime) -> bool:
    """检查当前时间是否应该创建 auction_open_confirmation 任务（10:00 ± 30s）。"""
    return _is_in_trigger_window(
        now,
        hour=AUCTION_OPEN_CONFIRMATION_TRIGGER_HOUR,
        minute=AUCTION_OPEN_CONFIRMATION_TRIGGER_MINUTE,
        second=AUCTION_OPEN_CONFIRMATION_TRIGGER_SECOND,
        tolerance_seconds=AUCTION_OPEN_CONFIRMATION_TRIGGER_TOLERANCE_SECONDS,
    )


# =============================================================================
# 执行器
# =============================================================================


async def execute_auction_scan_run(
    job_run_id: uuid.UUID,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    test_namespace: str | None = None,
    expected_symbols: list[tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """[P0-3] 执行 auction_final 任务：采集最终竞价 + 扫描全市场。

    流程：
    1. 调用 capture_auction_final_quotes（09:25:05 后从 mootdx 采集并写入 auction_final_quotes）
    2. 调用 run_auction_scan（auction_type=final，从 auction_final_quotes 读取）
    3. 标记 SchedulerJobRun succeeded/failed
    4. 不触发 aggregation（由 after_close_orchestrator 或独立流程触发）

    约束：
    - 锚点未发布 → AnchorNotPublishedError，标记 failed
    - AuctionScanConflictError → 标记 skipped（已有成功 run，无需重复）
    - capture 失败但部分有效 → scan 仍可基于 partial 数据执行
    - capture 完全失败（valid_count=0） → scan 标记 failed，不阻塞
    - 其他异常 → 标记 failed + error_message

    Args:
        job_run_id: SchedulerJobRun ID
        trade_date: 业务交易日
        worker_id: Worker 标识
        lease_epoch: 租约 epoch
        test_namespace: 隔离命名空间（None 使用 production；Canary 模式使用 auction_v1_canary_*）
        expected_symbols: Canary 模式下的预期股票列表（None 表示全市场）

    Returns:
        执行结果 dict（含 status、run_id、coverage、capture_run_id 等）
    """
    from app.db import AsyncSessionLocal
    from app.services.auction_quote_capture_service import PRODUCTION_NAMESPACE
    from app.services.auction_scan_service import AuctionScanConflictError

    namespace = test_namespace or PRODUCTION_NAMESPACE

    logger.info(
        "[AuctionScheduler] 开始执行 auction_final: job_run_id=%s, trade_date=%s, "
        "namespace=%s",
        job_run_id, trade_date, namespace,
    )

    try:
        async with AsyncSessionLocal() as db:
            result = await run_verified_auction_pipeline(
                db,
                trade_date,
                test_namespace=namespace,
                worker_id=worker_id,
                lease_epoch=lease_epoch,
                expected_symbols=expected_symbols,
            )
            await db.commit()

        # 标记 SchedulerJobRun succeeded
        await _mark_job_run_succeeded(job_run_id, result)
        logger.info(
            "[AuctionScheduler] auction_final 完成: job_run_id=%s, status=%s, "
            "coverage=%.4f, events=%d",
            job_run_id, result.get("status"),
            result.get("coverage_ratio", result.get("truth_coverage", 0.0)),
            result.get("event_count", 0),
        )
        return result

    except AuctionScanConflictError as exc:
        # 已有成功 run，幂等命中，标记 succeeded 并附 reason
        logger.info(
            "[AuctionScheduler] auction_final 幂等命中: job_run_id=%s, reason=%s",
            job_run_id, exc,
        )
        await _mark_job_run_succeeded(
            job_run_id,
            {"status": "skipped", "reason": str(exc)[:500]},
        )
        return {"status": "skipped", "reason": str(exc)[:500]}

    except Exception as exc:
        logger.exception(
            "[AuctionScheduler] auction_final 执行失败: job_run_id=%s, error=%s",
            job_run_id, exc,
        )
        await _mark_job_run_failed(job_run_id, exc)
        return {"status": "failed", "error": str(exc)[:1000]}


async def execute_auction_open_confirmation_run(
    job_run_id: uuid.UUID,
    trade_date: date,
    *,
    worker_id: str | None = None,
    lease_epoch: int | None = None,
) -> dict[str, Any]:
    """[P0-3] 执行 auction_open_confirmation 任务：更新事件生命周期。

    流程：
    1. 查询当日已 succeeded 的 auction_final AuctionScanRun
    2. 调用 update_event_lifecycle（开盘后窗口数据更新事件状态）
    3. 标记 SchedulerJobRun succeeded/failed

    约束：
    - 无 succeeded scan_run → 标记 failed（前置依赖未满足）
    - 异常 → 标记 failed + error_message

    Args:
        job_run_id: SchedulerJobRun ID
        trade_date: 业务交易日
        worker_id: Worker 标识
        lease_epoch: 租约 epoch

    Returns:
        执行结果 dict（含 transitions 等）
    """
    from app.db import AsyncSessionLocal
    from app.models.auction import AuctionScanRun
    from app.services.auction_scan_service import (
        AUCTION_SCAN_ALGORITHM_VERSION,
        update_event_lifecycle,
    )

    logger.info(
        "[AuctionScheduler] 开始执行 auction_open_confirmation: "
        "job_run_id=%s, trade_date=%s",
        job_run_id, trade_date,
    )

    try:
        async with AsyncSessionLocal() as db:
            # 查询当日已 succeeded 的 scan_run
            stmt = (
                select(AuctionScanRun)
                .where(
                    AuctionScanRun.trade_date == trade_date,
                    AuctionScanRun.auction_type == "final",
                    AuctionScanRun.algorithm_version == AUCTION_SCAN_ALGORITHM_VERSION,
                    AuctionScanRun.status == "succeeded",
                )
                .order_by(AuctionScanRun.created_at.desc())
                .limit(1)
            )
            scan_run = (await db.execute(stmt)).scalar_one_or_none()

            if scan_run is None:
                # 前置依赖未满足
                result = {
                    "status": "skipped",
                    "reason": "no_succeeded_auction_final_run",
                    "trade_date": trade_date.isoformat(),
                }
                await _mark_job_run_succeeded(job_run_id, result)
                logger.info(
                    "[AuctionScheduler] auction_open_confirmation 跳过（无 succeeded scan_run）: "
                    "job_run_id=%s, trade_date=%s",
                    job_run_id, trade_date,
                )
                return result

            result = await update_event_lifecycle(db, scan_run.id)
            await db.commit()

        await _mark_job_run_succeeded(job_run_id, result)
        logger.info(
            "[AuctionScheduler] auction_open_confirmation 完成: "
            "job_run_id=%s, transitions=%s",
            job_run_id, result.get("transitions"),
        )
        return result

    except Exception as exc:
        logger.exception(
            "[AuctionScheduler] auction_open_confirmation 执行失败: "
            "job_run_id=%s, error=%s",
            job_run_id, exc,
        )
        await _mark_job_run_failed(job_run_id, exc)
        return {"status": "failed", "error": str(exc)[:1000]}


# =============================================================================
# SchedulerJobRun 状态更新
# =============================================================================


async def _mark_job_run_succeeded(
    job_run_id: uuid.UUID,
    result: dict[str, Any],
) -> None:
    """标记 SchedulerJobRun succeeded，附加结果 metadata。"""
    from app.db import AsyncSessionLocal
    from app.models.job_run_event import JobRunEvent

    async with AsyncSessionLocal() as db:
        jr = await db.get(SchedulerJobRun, job_run_id)
        if jr is None:
            logger.warning("[AuctionScheduler] job_run not found: %s", job_run_id)
            return

        tz = _TZ_SHANGHAI
        now = datetime.now(tz)
        jr.status = "succeeded"
        jr.finished_at = now
        jr.lease_expires_at = now  # 释放 run_key 锁
        jr.heartbeat_at = now

        # 合并结果到 metadata
        existing_meta = json.loads(jr.metadata_json) if jr.metadata_json else {}
        existing_meta.update({
            "result_status": result.get("status"),
            "result_summary": {
                k: v for k, v in result.items()
                if k != "error" and not isinstance(v, (dict, list))
            } if isinstance(result, dict) else None,
        })
        jr.metadata_json = json.dumps(existing_meta, default=str)

        db.add(JobRunEvent(
            job_run_id=jr.id,
            step="execute",
            level="INFO",
            message=f"auction job succeeded: status={result.get('status')}",
            payload={"result": str(result)[:1000]},
        ))
        await db.commit()


async def _mark_job_run_failed(
    job_run_id: uuid.UUID,
    exc: Exception,
) -> None:
    """标记 SchedulerJobRun failed，附加错误信息。"""
    from app.db import AsyncSessionLocal
    from app.models.job_run_event import JobRunEvent

    async with AsyncSessionLocal() as db:
        jr = await db.get(SchedulerJobRun, job_run_id)
        if jr is None:
            logger.warning("[AuctionScheduler] job_run not found: %s", job_run_id)
            return

        tz = _TZ_SHANGHAI
        now = datetime.now(tz)
        jr.status = "failed"
        jr.finished_at = now
        jr.lease_expires_at = now  # 释放 run_key 锁
        jr.error_message = f"auction job failed: {exc}"[:500]

        db.add(JobRunEvent(
            job_run_id=jr.id,
            step="execute",
            level="ERROR",
            message=f"auction job failed: {exc}"[:500],
            payload={"error_type": type(exc).__name__},
        ))
        await db.commit()


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    print("auction_scheduler_service 自测...")

    # 常量校验
    assert AUCTION_FINAL_JOB_NAME == "auction_final"
    assert AUCTION_OPEN_CONFIRMATION_JOB_NAME == "auction_open_confirmation"
    assert AUCTION_FINAL_RUN_KEY_PREFIX == "auction_final"
    assert AUCTION_OPEN_CONFIRMATION_RUN_KEY_PREFIX == "auction_open_confirmation"
    assert AUCTION_FINAL_LEASE_SECONDS == 1800
    assert AUCTION_OPEN_CONFIRMATION_LEASE_SECONDS == 600
    assert AUCTION_FINAL_TRIGGER_HOUR == 9
    assert AUCTION_FINAL_TRIGGER_MINUTE == 25
    assert AUCTION_FINAL_TRIGGER_SECOND == 5
    assert AUCTION_OPEN_CONFIRMATION_TRIGGER_HOUR == 10
    assert AUCTION_OPEN_CONFIRMATION_TRIGGER_MINUTE == 0
    assert AUCTION_SCHEDULER_POLL_INTERVAL == 30

    # _is_in_trigger_window
    tz = _TZ_SHANGHAI
    target_time = datetime(2026, 7, 31, 9, 25, 5, tzinfo=tz)
    # 完全命中
    assert _is_in_trigger_window(
        target_time,
        hour=9, minute=25, second=5,
        tolerance_seconds=30,
    ) is True
    # ±30s 命中
    assert _is_in_trigger_window(
        target_time + timedelta(seconds=30),
        hour=9, minute=25, second=5,
        tolerance_seconds=30,
    ) is True
    assert _is_in_trigger_window(
        target_time - timedelta(seconds=30),
        hour=9, minute=25, second=5,
        tolerance_seconds=30,
    ) is True
    # 超过容差
    assert _is_in_trigger_window(
        target_time + timedelta(seconds=31),
        hour=9, minute=25, second=5,
        tolerance_seconds=30,
    ) is False

    # should_create_auction_final_job
    assert should_create_auction_final_job(target_time) is True
    assert should_create_auction_final_job(
        datetime(2026, 7, 31, 10, 0, 0, tzinfo=tz)
    ) is False
    # should_create_auction_open_confirmation_job
    assert should_create_auction_open_confirmation_job(
        datetime(2026, 7, 31, 10, 0, 0, tzinfo=tz)
    ) is True
    assert should_create_auction_open_confirmation_job(target_time) is False

    print("OK")
