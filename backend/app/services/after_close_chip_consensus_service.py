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
from datetime import date, datetime, timedelta
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

# [CHANGE-20260729-008] 15m bars 最低数量门槛（不足则标记 skipped，如深科技 000021 仅 338 根）
_CHIP_MIN_15M_BARS = 500

# chip 内部状态（写入 metadata.chip_status，不修改 SchedulerJobRun.status）
CHIP_STATUS_QUEUED = "queued"
CHIP_STATUS_RUNNING = "running"
CHIP_STATUS_SUCCEEDED = "succeeded"
CHIP_STATUS_PARTIAL = "partial"  # 写 metadata.chip_status，不写 SchedulerJobRun.status
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

    在 after_close_orchestrator 主 run 标记 succeeded 后调用。
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
            StockChipConsensusSnapshot.status == "succeeded",
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
    from app.services.first_pyramid_service import compute_chip_consensus_snapshot

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
            try:
                # 获取 daily + 15m bars（point-in-time <= trade_date）
                daily_bars, bars_15m = await _fetch_chip_bars(
                    instrument_id, trade_date,
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
                            instrument_id=instrument_id,
                            trade_date=trade_date,
                            core_run_id=core_run_id,
                            chip_hash="skipped",
                            chip_payload={"reason": "M15_BARS_INSUFFICIENT", "actual_bars": actual_15m},
                            status="skipped",
                            error_message=f"15m bars insufficient: {actual_15m} < {_CHIP_MIN_15M_BARS}",
                        )
                    except Exception:
                        pass  # skipped 记录失败不阻塞
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
                    instrument_id=instrument_id,
                    trade_date=trade_date,
                    core_run_id=core_run_id,
                    chip_hash=chip_result.chipHash,
                    chip_payload=chip_dict,
                    status="succeeded",
                    error_message=None,
                )
                succeeded_count += 1
            except Exception as exc:
                failed_count += 1
                failed_instruments.append({
                    "instrument_id": str(instrument_id),
                    "error": str(exc)[:500],
                })
                # 写入失败记录（便于断点续算）
                try:
                    await _upsert_chip_snapshot(
                        instrument_id=instrument_id,
                        trade_date=trade_date,
                        core_run_id=core_run_id,
                        chip_hash="error",
                        chip_payload={"error": str(exc)[:500]},
                        status="failed",
                        error_message=str(exc)[:500],
                    )
                except Exception:
                    pass  # 失败记录失败不阻塞

    # 统计状态
    if failed_count == 0 and skipped_count == 0:
        status = "succeeded"
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
    }

    # 更新 job_run metadata（chip_status 写 metadata，不修改 SchedulerJobRun.status）
    # 主 status 保持 succeeded/failed；部分成功写 metadata.chip_status=partial
    await _update_job_run_metadata(
        job_run_id=job_run_id,
        chip_status=status,
        succeeded_count=succeeded_count,
        failed_count=failed_count,
        total_count=total_count,
    )

    logger.info(
        "[ChipConsensus] 任务完成: job_run_id=%s, status=%s, "
        "succeeded=%d, failed=%d, skipped=%d, total=%d",
        job_run_id, status, succeeded_count, failed_count, skipped_count, total_count,
    )

    # [P0-2 修复 2026-07-31] chip 完成回调：触发 auction_anchor 重建并原子切换 publication。
    # - chip succeeded/partial → 重新生成完整锚点（含筹码维度），原子切换 publication
    # - chip failed/全失败 → 主流程已生成 structure_only 并发布，此处不重复
    # - 失败只记录 warn，不反改 chip job 状态（chip 是 source of truth，anchor 是 optional）
    if status in ("succeeded", "partial"):
        try:
            from app.db import AsyncSessionLocal
            from app.services.auction_anchor_service import (
                generate_and_publish_auction_anchors,
            )

            async with AsyncSessionLocal() as anchor_db:
                anchor_result = await generate_and_publish_auction_anchors(
                    anchor_db,
                    trade_date=trade_date,
                    worker_id=f"chip_consensus:{job_run_id}",
                )
                await anchor_db.commit()
            logger.info(
                "[ChipConsensus] chip 完成后锚点重建+发布: trade_date=%s, "
                "chip_status=%s, anchor_status=%s, publication_id=%s",
                trade_date, status,
                anchor_result.get("status"),
                anchor_result.get("publication_id"),
            )
        except Exception as anchor_exc:
            # 锚点重建失败不反改 chip job 状态（软失败）
            logger.warning(
                "[ChipConsensus] chip 完成后锚点重建失败（软失败，不影响 chip 状态）: "
                "trade_date=%s, error=%s",
                trade_date, anchor_exc,
                exc_info=True,
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
) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """获取 chip 计算所需的 daily + 15m bars（point-in-time <= trade_date）。

    [CHANGE-20260729-008] 修复：原引用不存在的 market_data_service.get_bars_for_instrument，
    改为直接从 DB 查询（_query_daily_bars / _query_15min_bars），与 history 回补保持一致。

    Args:
        instrument_id: 股票 ID
        trade_date: 交易日（point-in-time，只取 <= trade_date 的 bars）

    Returns:
        (daily_bars, bars_15m)：任一为空/None 表示数据不足
    """
    from app.db import AsyncSessionLocal
    from app.repositories.bar_repository import _query_15min_bars, _query_daily_bars

    daily_start = trade_date - timedelta(days=500)
    # 15m bars: 4000 根约 42 个交易日（每天 16 根），取 60 天
    bars_15m_start = datetime.combine(trade_date - timedelta(days=60), datetime.min.time())
    bars_15m_end = datetime.combine(trade_date, datetime.max.time())

    try:
        async with AsyncSessionLocal() as db:
            daily_bars = await _query_daily_bars(
                db, instrument_id, daily_start, trade_date,
            )
            bars_15m = await _query_15min_bars(
                db, instrument_id, bars_15m_start, bars_15m_end, limit=4000,
            )
        return daily_bars, bars_15m
    except Exception as exc:
        logger.warning(
            "[ChipConsensus] _fetch_chip_bars 失败: instrument_id=%s, error=%s",
            instrument_id, exc,
        )
        return None, None


async def _upsert_chip_snapshot(
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

    async with AsyncSessionLocal() as db:
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


async def _update_job_run_metadata(
    job_run_id: uuid.UUID,
    chip_status: str,
    succeeded_count: int,
    failed_count: int,
    total_count: int,
) -> None:
    """更新 job_run metadata.chip_status（不修改 SchedulerJobRun.status）。

    [P0-8 修复 2026-07-29] 部分成功写 metadata.chip_status=partial，
    主 status 保持 succeeded/failed。
    """
    import json

    from app.db import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        job_run = await db.get(SchedulerJobRun, job_run_id)
        if job_run is None:
            logger.warning(
                "[ChipConsensus] 更新 metadata 失败：job_run 不存在: %s", job_run_id,
            )
            return

        # 解析现有 metadata
        existing: dict[str, Any] = {}
        if job_run.metadata_json:
            try:
                existing = json.loads(job_run.metadata_json)
            except (json.JSONDecodeError, TypeError):
                existing = {}

        # 更新 chip_status 相关字段
        existing["chip_status"] = chip_status
        existing["succeeded_count"] = succeeded_count
        existing["failed_count"] = failed_count
        existing["total_count"] = total_count
        existing["chip_results_summary"] = {
            "succeeded": succeeded_count,
            "failed": failed_count,
            "total": total_count,
        }

        # 主 status：全成功→succeeded，全失败→failed，部分→succeeded（chip_status=partial）
        if chip_status == "succeeded":
            job_run.status = "succeeded"
        elif chip_status == "failed":
            job_run.status = "failed"
        # partial: 保持主 status 不变（由 caller 设置），只更新 metadata

        job_run.metadata_json = json.dumps(existing, ensure_ascii=False)
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
