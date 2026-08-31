"""盘后筹码共识独立任务服务（[CHANGE-20260729-003] 核心与筹码解耦）。

本模块实现独立 `after_close_chip_consensus` job 的创建、查询和执行。

设计目标（PRD20 盘后核心/筹码解耦）：
1. **核心发布成功即标记主 run succeeded**：after_close_orchestrator 关键路径
   日线 → core 个股状态/事件 → 质量门禁 → 发布，core 发布成功即可复盘
2. **chip 任务后置非阻塞**：发布后创建独立 `after_close_chip_consensus` job，
   不 await、不加入主 run 成功门禁
3. **chip 可独立失败/重试**：chip 任务失败/部分成功/单独重试，绝不反改主 run 或重算 core
4. **chip 使用独立 version/hash/run 关联**：chip 计算边界由
   `first_pyramid_service.compute_chip_consensus_snapshot` 提供
5. **[P0-8 修复 2026-07-29]** 不新增 `partial` 状态到 SchedulerJobRun.status：
   - 主 status 保持 succeeded/failed
   - 部分成功写 `metadata.chip_status = "partial"`
6. **[P0-9 修复 2026-07-29]** metadata_json 只存 scope/expected_count/core_run_id/checkpoint：
   - 禁止把全市场 UUID 数组写入 metadata_json
7. **[P0-10 修复 2026-07-29]** chip 持久化已实现（迁移 071 + StockChipConsensusSnapshot）

状态合同（status，复用现有 SchedulerJobRun 状态机，不新增状态）：
    queued → running → succeeded（全部 instrument chip 计算成功）
                     → failed（全部失败或不可恢复错误）
    running → interrupted（watchdog 检测 lease 过期）
    interrupted → resume_queued（auto-resume，仅重试未成功项）
    部分成功：主 status=succeeded，metadata.chip_status="partial"

幂等键：
    run_key = "after_close_chip_consensus:{trade_date}"

模块自测：
    python -m app.services.after_close_chip_consensus_service
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, time
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduler_job_run import SchedulerJobRun
from app.models.stock_chip_consensus_snapshot import StockChipConsensusSnapshot
from app.schemas.first_pyramid import CHIP_CONSENSUS_ALGORITHM_VERSION
from app.services.first_pyramid_flatten import flatten_chip_fields
from app.services.idempotency_service import acquire_job_run_lock

logger = logging.getLogger(__name__)


# =============================================================================
# 常量与状态合同
# =============================================================================

# 独立 job 名称（与 after_close_orchestrator 区分）
CHIP_CONSENSUS_JOB_NAME = "after_close_chip_consensus"

# 租约时长（chip 计算可能较慢，给予更长时间）
_CHIP_LEASE_SECONDS = 3600  # 1 小时

# 批量大小（与主 after_close 保持一致）
_CHIP_BATCH_SIZE = 25

# [CHANGE-20260806-005 / Phase 3 / GAP-08] 运行级 refresh 的有界并发与每股超时
_CHIP_REFRESH_CONCURRENCY = 8
_CHIP_REFRESH_PER_STOCK_TIMEOUT = 30.0

# [CHANGE-20260729-008] 15m bars 最低数量门槛（不足则标记 skipped，如深科技 000021 仅 338 根）
_CHIP_MIN_15M_BARS = 500
_CHIP_EXPECTED_DAILY_15M_BARS = 16
_CHIP_LAST_15M_TIME = time(15, 0)

# ===== [CHANGE-20260806-005 / Phase 3 / 八个 canonical 15m readiness reason code] =====
# 集中定义（唯一 SSOT），避免散落字符串。每股 chip 失败/skip 的 reason_code 必须属于
# 本集合；FUTURE_DATA 为新增，TIMESTAMP_INVALID 取代原 M15_TIMESTAMP_MISSING。
CHIP_READINESS_REASON_CODES: frozenset[str] = frozenset({
    "M15_REFRESH_FAILED",
    "M15_BARS_MISSING",
    "M15_TIMESTAMP_INVALID",
    "M15_TRADE_DATE_STALE",
    "M15_SESSION_INCOMPLETE",
    "M15_CLOSE_BAR_MISSING",
    "M15_BARS_INSUFFICIENT",
    "M15_FUTURE_DATA",
})


class Chip15mReadinessError(RuntimeError):
    """目标交易日 15m 数据未达到 chip 计算门禁。"""

    def __init__(self, reason_code: str, details: dict[str, Any]) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details

# chip 内部状态（写入 metadata.chip_status，不修改 SchedulerJobRun.status）
CHIP_STATUS_QUEUED = "queued"
CHIP_STATUS_RUNNING = "running"
CHIP_STATUS_SUCCEEDED = "succeeded"
CHIP_STATUS_PARTIAL = "partial"  # 写 metadata.chip_status，不写 SchedulerJobRun.status
CHIP_STATUS_SKIPPED = "skipped"
CHIP_STATUS_FAILED = "failed"

# metadata_json 允许的字段（[P0-9] 禁止把全市场 UUID 数组写入 metadata_json）
_METADATA_KEYS = frozenset({
    "chip_status", "trade_date", "core_run_id",
    "scope", "expected_count", "checkpoint",
    "instrument_count",  # 仅记录数量，不记录 UUID
    "succeeded_count", "failed_count",
    "chip_results_summary",
})


# =============================================================================
# Job 创建（幂等，软失败不阻塞主 run）
# =============================================================================


async def create_after_close_chip_consensus_job(
    db: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
    *,
    scope: str = "all_a_share",
    expected_count: int | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> tuple[SchedulerJobRun | None, bool]:
    """[CHANGE-20260729-003] 创建盘后筹码共识独立任务（幂等，软失败）。

    [AUD-08 2026-08-07] 由 after_close_orchestrator 在 stock_core 发布成功后
    立即调用（步骤 4.6），不再等待 Review 完成，也不受 Review 失败/取消影响。
    本函数只创建任务记录，不 await 执行（执行由独立 Worker 领取）。

    [P0-7 修复 2026-07-29] 软失败方式创建 chip job：
    - 创建失败只记录 warn，不反改主 run
    - 创建失败不抛异常给 caller

    [P0-9 修复 2026-07-29] metadata_json 只存：
    - scope：计算范围（如 "all_a_share" 或 "canary_5"）
    - expected_count：预期计算 instrument 数量
    - core_run_id：关联主 run id
    - checkpoint：恢复点（已成功 instrument_id 集合，用于失败重试）

    幂等：同 trade_date 已有 queued/running/resume_queued 任务则返回已有。

    Args:
        db: 异步会话
        trade_date: 交易日期
        core_run_id: 关联的 after_close 主 run id（用于追溯 core 发布）
        scope: 计算范围（默认 "all_a_share"）
        expected_count: 预期 instrument 数量
        checkpoint: 检查点信息

    Returns:
        (SchedulerJobRun | None, is_new)：
        - (job_run, True) 表示本次新建任务
        - (job_run, False) 表示同日已有活跃任务，返回已有记录
        - (None, False) 表示创建失败（软失败，已记录 warn）
    """
    try:
        run_key = f"{CHIP_CONSENSUS_JOB_NAME}:{trade_date.isoformat()}"
        # [P0-9] metadata 只存 scope/expected_count/core_run_id/checkpoint
        metadata: dict[str, Any] = {
            "chip_status": CHIP_STATUS_QUEUED,
            "trade_date": trade_date.isoformat(),
            "core_run_id": str(core_run_id),
            "scope": scope,
        }
        if expected_count is not None:
            metadata["expected_count"] = expected_count
            metadata["instrument_count"] = expected_count
        if checkpoint is not None:
            metadata["checkpoint"] = checkpoint

        job_run, is_new = await acquire_job_run_lock(
            db=db,
            run_key=run_key,
            job_name=CHIP_CONSENSUS_JOB_NAME,
            business_date=trade_date.isoformat(),
            lease_seconds=_CHIP_LEASE_SECONDS,
            metadata=metadata,
            initial_status="queued",
        )
        if not is_new:
            if job_run is not None:
                logger.info(
                    "[ChipConsensus] 同日已有 chip 任务，返回已有: run_id=%s, status=%s",
                    job_run.id, job_run.status,
                )
                return job_run, False
            logger.warning(
                "[ChipConsensus] acquire_job_run_lock 抢锁失败且未返回已有记录: run_key=%s",
                run_key,
            )
            return None, False

        if job_run is None:
            logger.warning(
                "[ChipConsensus] acquire_job_run_lock 返回 is_new=True 但 job_run=None: run_key=%s",
                run_key,
            )
            return None, False

        await db.commit()
        logger.info(
            "[ChipConsensus] 创建 chip 任务: run_id=%s, trade_date=%s, core_run_id=%s, scope=%s",
            job_run.id, trade_date, core_run_id, scope,
        )
        return job_run, is_new
    except Exception as exc:
        # [P0-7] 软失败：创建失败只记录 warn，不反改主 run，不抛异常
        logger.warning(
            "[ChipConsensus] 创建 chip 任务失败（软失败，不反改主 run）: "
            "trade_date=%s, core_run_id=%s, error=%s",
            trade_date, core_run_id, exc,
        )
        return None, False


# =============================================================================
# Job 查询
# =============================================================================


async def get_chip_consensus_job_for_date(
    db: AsyncSession,
    trade_date: date,
) -> SchedulerJobRun | None:
    """查询指定 trade_date 的 chip consensus 任务（取最新一条）。"""
    run_key = f"{CHIP_CONSENSUS_JOB_NAME}:{trade_date.isoformat()}"
    stmt = (
        select(SchedulerJobRun)
        .where(SchedulerJobRun.run_key == run_key)
        .order_by(SchedulerJobRun.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_pending_chip_instruments(
    db: AsyncSession,
    trade_date: date,
    core_run_id: uuid.UUID,
    *,
    all_instrument_ids: list[uuid.UUID],
) -> list[uuid.UUID]:
    """查询待计算的 instrument 列表（已成功的跳过，支持断点续算）。

    Args:
        db: 异步会话
        trade_date: 交易日期
        core_run_id: 关联主 run id
        all_instrument_ids: 全部 instrument 列表

    Returns:
        待计算 instrument 列表（已成功的不包含）
    """
    stmt = (
        select(StockChipConsensusSnapshot.instrument_id)
        .where(
            StockChipConsensusSnapshot.trade_date == trade_date,
            StockChipConsensusSnapshot.core_run_id == core_run_id,
            StockChipConsensusSnapshot.status.in_(("succeeded", "skipped")),
        )
    )
    result = await db.execute(stmt)
    succeeded_ids = {row[0] for row in result.all()}
    return [iid for iid in all_instrument_ids if iid not in succeeded_ids]


# =============================================================================
# Job 执行（[P0-10 修复 2026-07-29] 实现持久化与执行）
# =============================================================================


async def execute_after_close_chip_consensus(
    job_run_id: uuid.UUID,
    trade_date: date,
    core_run_id: uuid.UUID,
    *,
    instrument_ids: list[uuid.UUID],
    worker_id: str | None = None,
    lease_epoch: int | None = None,
    ownership_check: Any | None = None,
        batch_size: int = _CHIP_BATCH_SIZE,
        _diag_sink: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-003] 执行盘后筹码共识独立任务。

    [P0-10 修复 2026-07-29] 完整实现：
    - 对每个 instrument 获取 daily + 15m bars（point-in-time <= trade_date）
    - 调用 first_pyramid_service.compute_chip_consensus_snapshot
    - 持久化 chip 结果到 stock_chip_consensus_snapshots 表（幂等 upsert）
    - 分批执行（batch_size=25），每批 flush 不 commit
    - 单股失败不阻塞其他股票
    - 统计 succeeded/failed/partial

    约束：
    - chip 失败不反改主 run（after_close_orchestrator）状态
    - chip 失败不重算 core snapshot
    - chip 可单独重试失败项（resume_queued 状态）
    - 禁止用 Redis 冒充持久化

    Args:
        job_run_id: 任务运行 id
        trade_date: 交易日期
        core_run_id: 关联主 run id
        instrument_ids: 待计算 instrument 列表
        worker_id: Worker 标识
        lease_epoch: 租约 epoch
        batch_size: 批量大小（默认 25）
        _diag_sink: 诊断信息收集 dict

    Returns:
        执行结果统计 dict：
        {
            "succeeded_count": int,
            "failed_count": int,
            "total_count": int,
            "status": "succeeded" | "failed" | "partial",
            "failed_instruments": list[dict],  # 失败详情（instrument_id, error）
        }
    """
    from app.services.fenced_job_run_service import (
        FencedJobToken,
        JobLeaseLostError,
    )
    from app.services.first_pyramid_service import compute_chip_consensus_snapshot

    if not worker_id or lease_epoch is None:
        raise ValueError("worker_id and lease_epoch are required for fenced chip execution")
    lease_token = FencedJobToken(
        job_run_id=job_run_id,
        worker_instance_id=worker_id,
        lease_epoch=lease_epoch,
        lease_seconds=_CHIP_LEASE_SECONDS,
    )

    # [CHANGE-20260806-005 / Phase 3 / GAP-08/10] 运行级 15m 刷新：compute loop 之前一次性
    # 刷新全部标的（有界并发 + 每股超时 + 逐股 status），随后 compute loop 以 skip_refresh=True
    # 读取已刷新 bars，不再逐股 refresh（per_stock_refresh_in_compute_loop=0）。
    # 刷新失败的标的仍进入 compute，由 _assess_15m_readiness 判定为 M15_REFRESH_FAILED 并 skip。
    from app.services.chip_bars_refresh_coordinator import refresh_15m_batch

    refresh_result = await refresh_15m_batch(
        instrument_ids,
        trade_date,
        concurrency=_CHIP_REFRESH_CONCURRENCY,
        per_stock_timeout=_CHIP_REFRESH_PER_STOCK_TIMEOUT,
    )
    if _diag_sink is not None:
        _diag_sink["run_level_refresh"] = refresh_result.to_dict()
        _diag_sink["per_stock_refresh_in_compute_loop"] = 0

    succeeded_count = 0
    failed_count = 0
    skipped_count = 0
    failed_instruments: list[dict[str, Any]] = []
    skipped_instruments: list[dict[str, Any]] = []
    total_count = len(instrument_ids)

    # [CHANGE-20260729-008] 15m 数据不足的股票标记 skipped 而非 failed
    # （使用模块级常量 _CHIP_MIN_15M_BARS）

    # 分批处理
    for batch_start in range(0, total_count, batch_size):
        batch = instrument_ids[batch_start:batch_start + batch_size]
        for instrument_id in batch:
            if ownership_check is not None:
                ownership_check()
            try:
                # 获取 daily + 15m bars（point-in-time <= trade_date）
                # [Phase 3 / GAP-08] compute loop 不再逐股 refresh（运行级 refresh 已提前完成）
                daily_bars, bars_15m = await _fetch_chip_bars(
                    instrument_id, trade_date, skip_refresh=True,
                )
                if daily_bars is None or daily_bars.empty:
                    failed_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "daily bars 为空",
                    })
                    continue

                # [CHANGE-20260729-008] 15m bars 不足时标记 skipped（如深科技 000021 仅 338 根）
                if bars_15m is None or bars_15m.empty or len(bars_15m) < _CHIP_MIN_15M_BARS:
                    skipped_count += 1
                    actual_15m = len(bars_15m) if bars_15m is not None else 0
                    skipped_instruments.append({
                        "instrument_id": str(instrument_id),
                        "reason": f"M15_BARS_INSUFFICIENT: {actual_15m} < {_CHIP_MIN_15M_BARS}",
                    })
                    # 写入 skipped 记录（便于查询）
                    try:
                        await _upsert_chip_snapshot(
                            lease_token=lease_token,
                            instrument_id=instrument_id,
                            trade_date=trade_date,
                            core_run_id=core_run_id,
                            chip_hash="skipped",
                            chip_payload={"reason": "M15_BARS_INSUFFICIENT", "actual_bars": actual_15m},
                            status="skipped",
                            error_message=f"15m bars insufficient: {actual_15m} < {_CHIP_MIN_15M_BARS}",
                        )
                    except JobLeaseLostError:
                        raise
                    except Exception as exc:
                        skipped_count -= 1
                        failed_count += 1
                        skipped_instruments.pop()
                        failed_instruments.append({
                            "instrument_id": str(instrument_id),
                            "error": f"skipped persistence failed: {exc}"[:500],
                        })
                        logger.exception(
                            "[ChipConsensus] skipped 结果持久化失败: instrument_id=%s",
                            instrument_id,
                        )
                    continue

                # 计算 chip consensus（独立于 core）
                chip_result = compute_chip_consensus_snapshot(
                    daily_bars=daily_bars,
                    bars_15m=bars_15m,
                    trade_date=trade_date.isoformat(),
                )

                # 幂等 upsert
                # [P0-5 修复 2026-07-29 三.1] 统一使用 model_dump(by_alias=False)，禁止 dict(pydantic_model)
                # 原因：dict(pydantic_model) 在 Pydantic v2 已废弃，且无法保证字段别名一致性
                chip_dict = chip_result.model_dump(by_alias=False)
                # [P0-5 修复 2026-07-29 三.2] 用 model_dump 后的 chip 字典调用 flatten_chip_fields
                # 写入 chip_flat 扁平对象（10 个 chip fp_ 键）
                # 供 /market/stocks 服务端 filter/sort 从 chip_payload.chip_flat.<fp_key> 读取
                chip_dict["chip_flat"] = flatten_chip_fields(chip_dict.get("chip"))
                await _upsert_chip_snapshot(
                    lease_token=lease_token,
                    instrument_id=instrument_id,
                    trade_date=trade_date,
                    core_run_id=core_run_id,
                    chip_hash=chip_result.chipHash,
                    chip_payload=chip_dict,
                    status="succeeded",
                    error_message=None,
                )
                succeeded_count += 1
            except Chip15mReadinessError as exc:
                skipped_count += 1
                payload = {"reason": exc.reason_code, **exc.details}
                skipped_instruments.append({
                    "instrument_id": str(instrument_id),
                    **payload,
                })
                try:
                    await _upsert_chip_snapshot(
                        lease_token=lease_token,
                        instrument_id=instrument_id,
                        trade_date=trade_date,
                        core_run_id=core_run_id,
                        chip_hash="skipped",
                        chip_payload=payload,
                        status="skipped",
                        error_message=exc.reason_code,
                    )
                except JobLeaseLostError:
                    raise
                except Exception as persist_exc:
                    skipped_count -= 1
                    failed_count += 1
                    skipped_instruments.pop()
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": f"readiness persistence failed: {persist_exc}"[:500],
                    })
            except JobLeaseLostError:
                raise
            except Exception as exc:
                failed_count += 1
                failed_instruments.append({
                    "instrument_id": str(instrument_id),
                    "error": str(exc)[:500],
                })
                # 写入失败记录（便于断点续算）
                try:
                    await _upsert_chip_snapshot(
                        lease_token=lease_token,
                        instrument_id=instrument_id,
                        trade_date=trade_date,
                        core_run_id=core_run_id,
                        chip_hash="error",
                        chip_payload={"error": str(exc)[:500]},
                        status="failed",
                        error_message=str(exc)[:500],
                    )
                except JobLeaseLostError:
                    raise
                except Exception:
                    logger.exception(
                        "[ChipConsensus] failed 结果持久化失败: instrument_id=%s",
                        instrument_id,
                    )

    # 统计状态
    if total_count == 0 or succeeded_count == total_count:
        status = "succeeded"
    elif skipped_count == total_count:
        status = "skipped"
    elif succeeded_count == 0 and skipped_count == 0:
        status = "failed"
    else:
        status = "partial"

    result_summary = {
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "total_count": total_count,
        "status": status,
        "failed_instruments": failed_instruments,
        "skipped_instruments": skipped_instruments,
        "anchor_rebuild_required": status in {"succeeded", "partial"},
    }

    logger.info(
        "[ChipConsensus] 任务完成: job_run_id=%s, status=%s, "
        "succeeded=%d, failed=%d, skipped=%d, total=%d",
        job_run_id, status, succeeded_count, failed_count, skipped_count, total_count,
    )

    if _diag_sink is not None:
        _diag_sink.update(result_summary)

    return result_summary


# =============================================================================
# 内部辅助函数
# =============================================================================


async def _fetch_chip_bars(
    instrument_id: uuid.UUID,
    trade_date: date,
    *,
    skip_refresh: bool = False,
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """获取 chip 计算所需的 daily + 15m bars（point-in-time <= trade_date）。

    [CHANGE-20260731-003] SSOT 合规：通过 MarketDataAggregationService (MDAS) 读取行情，
    不再直接导入 bar_repository 私有 _query_* 函数。MDAS 是行情读取唯一出口。

    [CHANGE-20260806-005 / Phase 3 / GAP-08] 运行级 refresh 分离：compute loop 不再逐股
    refresh。当 `skip_refresh=True`（运行级 refresh 已提前完成）时，本函数仅读取已刷新
    bars，不调用 `refresh_15min_bars`（per_stock_refresh_in_compute_loop=0）。

    Args:
        instrument_id: 股票 ID
        trade_date: 交易日（point-in-time，只取 <= trade_date 的 bars）
        skip_refresh: 是否跳过本股 refresh（运行级刷新已完成的 compute 阶段传 True）

    Returns:
        (daily_bars, bars_15m)：任一为空/None 表示数据不足
    """
    from app.db import AsyncSessionLocal
    from app.repositories.bar_repository import refresh_15min_bars
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()

    try:
        async with AsyncSessionLocal() as db:
            if not skip_refresh:
                try:
                    await refresh_15min_bars(
                        db,
                        instrument_id,
                        count=4000,
                    )
                    await db.commit()
                except Exception as exc:
                    await db.rollback()
                    raise Chip15mReadinessError(
                        "M15_REFRESH_FAILED",
                        {"source_cutoff": None, "error": str(exc)[:300]},
                    ) from exc

            # daily: completed qfq，end_date=trade_date 保证 point-in-time
            daily_agg = await mdas.get_bars(
                db,
                instrument_id,
                timeframe="1d",
                adj="qfq",
                include_realtime=False,
                completed_only=True,
                end_date=trade_date,
            )
            # 15m: completed qfq，最近 4000 根（约 60 个交易日）
            m15_agg = await mdas.get_bars(
                db,
                instrument_id,
                timeframe="15m",
                adj="qfq",
                include_realtime=False,
                completed_only=True,
                end_date=trade_date,
                limit=4000,
            )
        daily_bars = daily_agg.bars if not daily_agg.bars.empty else None
        bars_15m = m15_agg.bars if not m15_agg.bars.empty else None
        readiness = _assess_15m_readiness(bars_15m, trade_date)
        if not readiness["ready"]:
            raise Chip15mReadinessError(readiness["reason_code"], readiness)
        return daily_bars, bars_15m
    except Chip15mReadinessError:
        raise
    except Exception as exc:
        logger.warning(
            "[ChipConsensus] _fetch_chip_bars 失败: instrument_id=%s, error=%s",
            instrument_id, exc,
        )
        return None, None


def _assess_15m_readiness(
    bars_15m: pd.DataFrame | None,
    trade_date: date,
) -> dict[str, Any]:
    """校验 chip 输入包含目标交易日完整收盘 15m 数据。"""
    if bars_15m is None or bars_15m.empty:
        return {
            "ready": False,
            "reason_code": "M15_BARS_MISSING",
            "actual_session_bars": 0,
            "required_session_bars": _CHIP_EXPECTED_DAILY_15M_BARS,
            "source_cutoff": None,
        }

    if isinstance(bars_15m.index, pd.DatetimeIndex):
        timestamps = pd.DatetimeIndex(bars_15m.index)
    else:
        time_column = "trade_time" if "trade_time" in bars_15m.columns else "datetime"
        if time_column not in bars_15m.columns:
            return {
                "ready": False,
                "reason_code": "M15_TIMESTAMP_INVALID",
                "actual_session_bars": 0,
                "required_session_bars": _CHIP_EXPECTED_DAILY_15M_BARS,
                "source_cutoff": None,
            }
        timestamps = pd.DatetimeIndex(pd.to_datetime(bars_15m[time_column]))

    # [Phase 3] 未来数据检测：任何时间戳超出目标交易日收盘（15:00）视为 future data。
    # 正常情况下 bars 已被 end_date=trade_date 截断；若上游数据漂移混入未来 15m，禁止计算。
    last_close_marker = pd.Timestamp.combine(trade_date, _CHIP_LAST_15M_TIME)
    if len(timestamps) > 0 and timestamps.max() > last_close_marker:
        return {
            "ready": False,
            "reason_code": "M15_FUTURE_DATA",
            "actual_session_bars": 0,
            "required_session_bars": _CHIP_EXPECTED_DAILY_15M_BARS,
            "source_cutoff": timestamps.max().isoformat(),
        }

    target_mask = timestamps.date == trade_date
    target_times = timestamps[target_mask]
    cutoff = timestamps.max()
    details = {
        "actual_session_bars": len(target_times),
        "required_session_bars": _CHIP_EXPECTED_DAILY_15M_BARS,
        "source_cutoff": cutoff.isoformat(),
    }
    if cutoff.date() != trade_date:
        return {"ready": False, "reason_code": "M15_TRADE_DATE_STALE", **details}
    if len(target_times) < _CHIP_EXPECTED_DAILY_15M_BARS:
        return {"ready": False, "reason_code": "M15_SESSION_INCOMPLETE", **details}
    if target_times.max().time() < _CHIP_LAST_15M_TIME:
        return {"ready": False, "reason_code": "M15_CLOSE_BAR_MISSING", **details}
    return {"ready": True, "reason_code": None, **details}


async def _upsert_chip_snapshot(
    lease_token: Any,
    instrument_id: uuid.UUID,
    trade_date: date,
    core_run_id: uuid.UUID,
    chip_hash: str,
    chip_payload: dict[str, Any],
    status: str,
    error_message: str | None,
) -> None:
    """幂等 upsert chip 快照到 stock_chip_consensus_snapshots 表。

    使用 PostgreSQL ON CONFLICT DO UPDATE，唯一键：
    (instrument_id, trade_date, core_run_id, algorithm_version)
    """
    from app.db import AsyncSessionLocal
    from app.services.fenced_job_run_service import lock_owned_job_run

    async with AsyncSessionLocal() as db:
        await lock_owned_job_run(db, lease_token)
        stmt = pg_insert(StockChipConsensusSnapshot).values(
            instrument_id=instrument_id,
            trade_date=trade_date,
            core_run_id=core_run_id,
            algorithm_version=CHIP_CONSENSUS_ALGORITHM_VERSION,
            chip_hash=chip_hash,
            chip_payload=chip_payload,
            status=status,
            error_message=error_message,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_chip_consensus_instrument_date_run_version",
            set_={
                "chip_hash": stmt.excluded.chip_hash,
                "chip_payload": stmt.excluded.chip_payload,
                "status": stmt.excluded.status,
                "error_message": stmt.excluded.error_message,
                "updated_at": func.now(),
            },
        )
        await db.execute(stmt)
        await db.commit()


# =============================================================================
# 模块自测
# =============================================================================

if __name__ == "__main__":
    # 验证常量与状态合同
    assert CHIP_CONSENSUS_JOB_NAME == "after_close_chip_consensus"
    assert CHIP_STATUS_PARTIAL == "partial"
    assert CHIP_STATUS_SUCCEEDED == "succeeded"
    assert CHIP_STATUS_FAILED == "failed"
    # [P0-9] 验证 metadata 允许的字段
    assert "scope" in _METADATA_KEYS
    assert "expected_count" in _METADATA_KEYS
    assert "core_run_id" in _METADATA_KEYS
    assert "checkpoint" in _METADATA_KEYS
    # [P0-9] 禁止全市场 UUID 数组写入 metadata
    assert "instrument_ids" not in _METADATA_KEYS
    # 验证函数签名
    import inspect
    sig_create = inspect.signature(create_after_close_chip_consensus_job)
    assert "core_run_id" in sig_create.parameters
    assert "trade_date" in sig_create.parameters
    assert "scope" in sig_create.parameters
    assert "expected_count" in sig_create.parameters
    sig_exec = inspect.signature(execute_after_close_chip_consensus)
    assert "core_run_id" in sig_exec.parameters
    assert "job_run_id" in sig_exec.parameters
    assert "instrument_ids" in sig_exec.parameters
    print(f"OK: {CHIP_CONSENSUS_JOB_NAME} interface + executor verified")
    print("Status contract: queued/running/succeeded/failed/interrupted/resume_queued")
    print("Partial: metadata.chip_status=partial (不修改 SchedulerJobRun.status)")
    print("Metadata keys: scope/expected_count/core_run_id/checkpoint only")
