"""第一金字塔非筹码历史回补服务（[CHANGE-20260729-003] 核心与筹码解耦 - P0-11）。

设计目标（ref/instruction.md §三.11）：
1. **按个股为外层**：每只股票一次读完整可用日线，一次调用 history SSOT
2. **一次调用 history SSOT**：`compute_first_pyramid_history` 一次计算多日
   daily_state + 不可变 events，禁止逐日调用 snapshot
3. **保存最近 250 日 daily state 与不可变 events**：
   - daily_state: upsert 到 first_pyramid_history_daily_state（幂等）
   - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）
4. **分批 25—50 股**：默认 batch_size=25，每批一个事务（commit + checkpoint）
5. **幂等重跑**：相同 (instrument_id, trade_date, algorithm_version) 重复执行
   只更新 daily_state 内容，events 不重复插入
6. **禁止回补 chip**：chip 由独立 after_close_chip_consensus job 异步处理

调用链：
    backfill_first_pyramid_history_batch
      └─ for each instrument:
           ├─ bar_repository.get_bars(1d, qfq, completed_only=True)
           ├─ compute_first_pyramid_history(bars, include_chip=False)
           ├─ upsert daily_state rows (on_conflict_do_update)
           └─ insert events (on_conflict_do_nothing)

约束：
- 本服务不读取/写入 chip 相关数据
- 本服务不调用 compute_first_pyramid_snapshot（逐日）
- 单股失败不阻塞其他股票，写入 failed_instruments 列表

模块自测：
    python -m app.services.first_pyramid_history_service
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.first_pyramid_history import (
    FirstPyramidHistoryDailyState,
    FirstPyramidHistoryEvent,
)
from app.models.first_pyramid_history_run import (
    HISTORY_RUN_FAILED,
    HISTORY_RUN_PARTIAL,
    HISTORY_RUN_RUNNING,
    HISTORY_RUN_SUCCEEDED,
    FirstPyramidHistoryRun,
)
from app.models.first_pyramid_history_run_item import FirstPyramidHistoryRunItem
from app.schemas.first_pyramid import FIRST_PYRAMID_CORE_ALGORITHM_VERSION

logger = logging.getLogger(__name__)

# =============================================================================
# 常量
# =============================================================================

# 默认回补输出天数（与第一金字塔合同对齐）
_DEFAULT_OUTPUT_BARS = 250

# 默认批量大小（instruction 要求 25—50 股）
_DEFAULT_BATCH_SIZE = 25

# 单股失败阈值：超过则整体标 partial
_FAILURE_THRESHOLD = 0.3


# =============================================================================
# 主入口
# =============================================================================


async def backfill_first_pyramid_history_batch(
    session: AsyncSession,
    instrument_ids: Sequence[uuid.UUID],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    output_bars: int = _DEFAULT_OUTPUT_BARS,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
    _fetch_bars_func: Callable[..., Awaitable[pd.DataFrame | None]] | None = None,
) -> dict[str, Any]:
    """[P0-11] 第一金字塔非筹码历史回补批量入口。

    按"个股为外层，一次调用 history SSOT"模式回补：
    1. 每只股票读取完整可用日线（point-in-time <= 今日，qfq 复权）
    2. 一次调用 compute_first_pyramid_history(bars, include_chip=False)
    3. 持久化最近 output_bars 日 daily_state（upsert）+ events（on_conflict_do_nothing）
    4. 分批提交事务，每批后回调 progress_callback（checkpoint）

    Args:
        session: 异步 DB 会话（由 caller 控制 commit/rollback 边界）
        instrument_ids: 待回补 instrument ID 列表
        batch_size: 每批 instrument 数（默认 25）
        output_bars: 输出最近 N 个有效日的 daily state（默认 250）
        progress_callback: 进度回调，接收 processed/total/succeeded/failed
        _fetch_bars_func: 测试注入的 bars 获取函数（生产留空，使用 bar_repository）

    Returns:
        统计信息 dict：
        {
            "total_count": int,
            "succeeded_count": int,
            "failed_count": int,
            "skipped_count": int,  # bars 不足或为空
            "status": "succeeded" | "failed" | "partial",
            "failed_instruments": list[dict],  # 失败详情
            "algorithm_version": str,
            "output_bars": int,
        }
    """
    # 延迟导入避免循环依赖
    from app.services.first_pyramid_service import compute_first_pyramid_history

    total = len(instrument_ids)
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0
    failed_instruments: list[dict[str, Any]] = []

    for batch_start in range(0, total, batch_size):
        batch = list(instrument_ids[batch_start:batch_start + batch_size])
        for instrument_id in batch:
            try:
                # 1. 读取完整可用日线
                if _fetch_bars_func is not None:
                    bars = await _fetch_bars_func(instrument_id)
                else:
                    bars = await _fetch_history_daily_bars(instrument_id)

                if bars is None or bars.empty:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "daily bars 为空",
                    })
                    continue

                # 2. 一次调用 history SSOT（include_chip=False）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 3. 持久化 daily_state + events
                persisted = await _persist_history_result(
                    session=session,
                    instrument_id=instrument_id,
                    history=history,
                    algorithm_version=FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
                )

                if persisted["daily_state_count"] == 0:
                    skipped_count += 1
                    failed_instruments.append({
                        "instrument_id": str(instrument_id),
                        "error": "history daily_state 为空（可能 bars 长度不足）",
                    })
                    continue

                succeeded_count += 1
            except Exception as exc:
                failed_count += 1
                failed_instruments.append({
                    "instrument_id": str(instrument_id),
                    "error": str(exc)[:500],
                })
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    instrument_id, exc, exc_info=True,
                )

        # 每批 commit + checkpoint
        try:
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(
                "[HistoryBackfill] batch commit 失败 batch_start=%s: %s",
                batch_start, exc, exc_info=True,
            )
            raise

        if progress_callback is not None:
            try:
                await progress_callback(
                    processed=min(batch_start + len(batch), total),
                    total=total,
                    succeeded=succeeded_count,
                    failed=failed_count,
                    skipped=skipped_count,
                )
            except Exception as exc:
                logger.warning(
                    "[HistoryBackfill] progress_callback 失败: %s", exc,
                )

    # 统计状态
    if failed_count == 0 and succeeded_count > 0:
        status = "succeeded"
    elif succeeded_count == 0 and failed_count > 0:
        status = "failed"
    elif succeeded_count > 0 and failed_count > 0:
        status = "partial"
    else:
        status = "failed"  # 全部 skipped

    result = {
        "total_count": total,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "status": status,
        "failed_instruments": failed_instruments,
        "algorithm_version": FIRST_PYRAMID_CORE_ALGORITHM_VERSION,
        "output_bars": output_bars,
    }

    logger.info(
        "[HistoryBackfill] 批量回补完成: total=%d, succeeded=%d, failed=%d, skipped=%d, status=%s",
        total, succeeded_count, failed_count, skipped_count, status,
    )

    return result


# =============================================================================
# Run/Item 接入版（CHANGE-20260729-008）
# =============================================================================


_HISTORY_ITEM_PENDING = "pending"
_HISTORY_ITEM_RUNNING = "running"
_HISTORY_ITEM_SUCCEEDED = "succeeded"
_HISTORY_ITEM_FAILED = "failed"
_HISTORY_ITEM_SKIPPED = "skipped"

# 默认 lease 时长（秒），单股 history 计算通常 < 60s
_HISTORY_ITEM_LEASE_SECONDS = 300

# 最大重试次数
_HISTORY_MAX_ATTEMPT_COUNT = 3


def _compute_parameter_hash(output_bars: int, include_chip: bool) -> str:
    """计算历史回补参数 hash（output_bars + include_chip）。"""
    raw = f"output_bars={output_bars};include_chip={include_chip}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


async def create_history_run(
    session: AsyncSession,
    *,
    algorithm_version: str,
    output_bars: int,
    scope: str,
    instrument_ids: Sequence[uuid.UUID],
    scheduler_job_run_id: uuid.UUID | None = None,
    include_chip: bool = False,
) -> tuple[FirstPyramidHistoryRun, bool]:
    """创建历史回补 run（幂等：相同 algorithm_version + parameter_hash + scope 已有 running/succeeded 则返回已有）。

    Args:
        session: 异步 DB 会话（caller 控制 commit）
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        scope: 范围标识
        instrument_ids: eligible universe（用于 expected_count）
        scheduler_job_run_id: 关联 SchedulerJobRun（可选）
        include_chip: 是否含 chip（默认 False）

    Returns:
        (FirstPyramidHistoryRun, is_new)
    """
    parameter_hash = _compute_parameter_hash(output_bars, include_chip)

    # 幂等查找：同 algorithm_version + parameter_hash + scope 的活跃 run
    existing_stmt = (
        select(FirstPyramidHistoryRun)
        .where(
            FirstPyramidHistoryRun.algorithm_version == algorithm_version,
            FirstPyramidHistoryRun.parameter_hash == parameter_hash,
            FirstPyramidHistoryRun.scope == scope,
            FirstPyramidHistoryRun.status.in_(
                (HISTORY_RUN_RUNNING, HISTORY_RUN_PARTIAL, HISTORY_RUN_SUCCEEDED),
            ),
        )
        .order_by(FirstPyramidHistoryRun.created_at.desc())
        .limit(1)
    )
    existing = (await session.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    run = FirstPyramidHistoryRun(
        scheduler_job_run_id=scheduler_job_run_id,
        algorithm_version=algorithm_version,
        parameter_hash=parameter_hash,
        output_bars=output_bars,
        scope=scope,
        expected_count=len(instrument_ids),
        succeeded_count=0,
        failed_count=0,
        skipped_count=0,
        status=HISTORY_RUN_RUNNING,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run, True


async def create_history_run_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
    *,
    input_hash: str | None = None,
) -> int:
    """为 eligible universe 创建 history/pending items（幂等）。

    使用 INSERT ON CONFLICT DO NOTHING 保证并发安全。
    """
    if not instrument_ids:
        return 0

    # 查找已存在的 items
    existing_stmt = (
        select(FirstPyramidHistoryRunItem.instrument_id)
        .where(
            FirstPyramidHistoryRunItem.history_run_id == history_run_id,
            FirstPyramidHistoryRunItem.instrument_id.in_(instrument_ids),
        )
    )
    existing_ids = {
        row[0] for row in (await session.execute(existing_stmt))
    }

    new_items = []
    for instrument_id in instrument_ids:
        if instrument_id in existing_ids:
            continue
        new_items.append(FirstPyramidHistoryRunItem(
            history_run_id=history_run_id,
            instrument_id=instrument_id,
            status=_HISTORY_ITEM_PENDING,
            input_hash=input_hash,
        ))

    if new_items:
        session.add_all(new_items)
        await session.flush()

    return len(new_items)


async def claim_history_items(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    worker_instance_id: str,
    batch_size: int = 25,
    lease_seconds: int = _HISTORY_ITEM_LEASE_SECONDS,
    max_attempt_count: int = _HISTORY_MAX_ATTEMPT_COUNT,
) -> list[FirstPyramidHistoryRunItem]:
    """Worker 原子领取一批 pending/可恢复 history items。

    使用 UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED) RETURNING。
    """
    now = datetime.now(UTC)
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    claim_sql = text(
        """
        UPDATE first_pyramid_history_run_items
        SET status = 'running',
            attempt_count = attempt_count + 1,
            lease_epoch = lease_epoch + 1,
            worker_instance_id = :worker_id,
            started_at = COALESCE(started_at, :now),
            heartbeat_at = :now,
            lease_expires_at = :lease_expires,
            updated_at = :now
        WHERE id IN (
            SELECT id FROM first_pyramid_history_run_items
            WHERE history_run_id = :history_run_id
              AND (
                status = 'pending'
                OR (status = 'failed' AND attempt_count < :max_attempts)
                OR (status = 'running' AND lease_expires_at < :now)
              )
            ORDER BY created_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, history_run_id, instrument_id, status, attempt_count,
                  input_hash, worker_instance_id, lease_epoch, lease_expires_at,
                  daily_state_count, event_count, last_error, started_at,
                  heartbeat_at, completed_at, created_at, updated_at
        """
    )
    result = await session.execute(claim_sql, {
        "worker_id": worker_instance_id,
        "now": now,
        "lease_expires": lease_expires_at,
        "history_run_id": history_run_id,
        "max_attempts": max_attempt_count,
        "batch_size": batch_size,
    })
    rows = result.fetchall()
    if not rows:
        return []

    items: list[FirstPyramidHistoryRunItem] = []
    for row in rows:
        item = FirstPyramidHistoryRunItem(
            id=row[0],
            history_run_id=row[1],
            instrument_id=row[2],
            status=row[3],
            attempt_count=row[4],
            input_hash=row[5],
            worker_instance_id=row[6],
            lease_epoch=row[7],
            lease_expires_at=row[8],
            daily_state_count=row[9],
            event_count=row[10],
            last_error=row[11],
            started_at=row[12],
            heartbeat_at=row[13],
            completed_at=row[14],
            created_at=row[15],
            updated_at=row[16],
        )
        items.append(item)
    return items


async def mark_history_item_succeeded(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    daily_state_count: int | None = None,
    event_count: int | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 成功（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SUCCEEDED,
            daily_state_count=daily_state_count,
            event_count=event_count,
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_failed(
    session: AsyncSession,
    item_id: uuid.UUID,
    error: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 失败（带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_FAILED,
            last_error=error[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_history_item_skipped(
    session: AsyncSession,
    item_id: uuid.UUID,
    reason: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 history item 跳过（数据不足等，带 lease_epoch fencing）。"""
    now = datetime.now(UTC)
    conditions = [
        FirstPyramidHistoryRunItem.id == item_id,
        FirstPyramidHistoryRunItem.status == _HISTORY_ITEM_RUNNING,
    ]
    if lease_epoch is not None:
        conditions.append(FirstPyramidHistoryRunItem.lease_epoch == lease_epoch)
    stmt = (
        update(FirstPyramidHistoryRunItem)
        .where(*conditions)
        .values(
            status=_HISTORY_ITEM_SKIPPED,
            last_error=reason[:1000],
            completed_at=now,
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def get_history_run_progress(
    session: AsyncSession,
    history_run_id: uuid.UUID,
) -> dict[str, Any]:
    """获取 history run 级进度统计。"""
    stmt = (
        select(
            FirstPyramidHistoryRunItem.status,
            func.count(FirstPyramidHistoryRunItem.id).label("cnt"),
        )
        .where(FirstPyramidHistoryRunItem.history_run_id == history_run_id)
        .group_by(FirstPyramidHistoryRunItem.status)
    )
    rows = (await session.execute(stmt)).all()
    counts = {row.status: row.cnt for row in rows}

    succeeded = counts.get(_HISTORY_ITEM_SUCCEEDED, 0)
    failed = counts.get(_HISTORY_ITEM_FAILED, 0)
    pending = counts.get(_HISTORY_ITEM_PENDING, 0)
    running = counts.get(_HISTORY_ITEM_RUNNING, 0)
    skipped = counts.get(_HISTORY_ITEM_SKIPPED, 0)
    total = succeeded + failed + pending + running + skipped
    coverage = succeeded / total if total > 0 else 0.0

    return {
        "succeeded": succeeded,
        "failed": failed,
        "pending": pending,
        "running": running,
        "skipped": skipped,
        "total": total,
        "coverage": coverage,
    }


async def finish_history_run(
    session: AsyncSession,
    history_run_id: uuid.UUID,
    *,
    status: str,
) -> None:
    """更新 history run 的最终状态。"""
    progress = await get_history_run_progress(session, history_run_id)
    now = datetime.now(UTC)
    stmt = (
        update(FirstPyramidHistoryRun)
        .where(FirstPyramidHistoryRun.id == history_run_id)
        .values(
            status=status,
            succeeded_count=progress["succeeded"],
            failed_count=progress["failed"],
            skipped_count=progress["skipped"],
            completed_at=now,
            updated_at=now,
        )
    )
    await session.execute(stmt)


async def _fetch_db_only_daily_bars(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    *,
    output_bars: int,
) -> pd.DataFrame | None:
    """[CHANGE-20260731-003] SSOT 合规：通过 MDAS 读取日线行情。

    原实现直接调用 bar_repository._query_daily_bars 违反 SSOT 架构，
    改为通过 MarketDataAggregationService (MDAS) 统一出口。
    completed_only=True 保证只读取已完成 bar；include_realtime=False 禁用实时补充。
    若 DB 无数据，MDAS 会按 SSOT 标准行为尝试回补；返回空时 caller 标 skipped。
    """
    from app.services.market_data_aggregation_service import MarketDataAggregationService

    mdas = MarketDataAggregationService()
    agg = await mdas.get_bars(
        session,
        instrument_id,
        timeframe="1d",
        adj="qfq",
        include_realtime=False,
        completed_only=True,
        limit=output_bars * 2,  # 留余量，history SSOT 内部会截取 output_bars
    )
    df = agg.bars
    if df is None or df.empty:
        return None
    return df


async def backfill_history_with_run_items(
    *,
    history_run_id: uuid.UUID,
    algorithm_version: str,
    output_bars: int,
    worker_id: str = "history_worker",
    batch_size: int = 25,
    progress_callback: Callable[..., Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """[CHANGE-20260729-008] Run/Item 接入版历史回补（单股×检查点）。

    与 backfill_first_pyramid_history_batch 关键差异：
    - 使用 first_pyramid_history_run_items 表做单股 claim/lease/commit
    - 每只股票在独立短事务中计算并 commit（失败只回滚该股）
    - coverage 从 run_items 实时统计
    - 恢复只领 pending/可重试 failed/过期 running
    - **DB-only 取数**：直接调 _query_daily_bars，禁止自动 pytdx 拉取
    - 支持 resume：重启只处理 pending/failed/过期 running items

    流程：
    1. claim_history_items 领取一批
    2. 逐股：独立事务读 bars → 计算 history SSOT → 持久化 → commit → mark_item_succeeded
    3. 失败：mark_item_failed，继续下一股
    4. 无可领取 items 时结束

    Args:
        history_run_id: FirstPyramidHistoryRun.id
        algorithm_version: 算法版本
        output_bars: 输出最近 N 日
        worker_id: Worker 标识
        batch_size: claim 批次大小
        progress_callback: 进度回调

    Returns:
        统计 dict
    """
    from app.db import AsyncSessionLocal
    from app.services.first_pyramid_service import compute_first_pyramid_history

    total_processed = 0
    succeeded_count = 0
    failed_count = 0
    skipped_count = 0

    while True:
        # 1. claim 一批 items
        async with AsyncSessionLocal() as db:
            items = await claim_history_items(
                db, history_run_id,
                worker_instance_id=worker_id,
                batch_size=batch_size,
            )
            await db.commit()

        if not items:
            break

        # 2. 逐股计算（每股独立事务）
        for item in items:
            total_processed += 1
            try:
                # 2.1 DB-only 读取 bars（独立短事务）
                async with AsyncSessionLocal() as bars_db:
                    bars = await _fetch_db_only_daily_bars(
                        bars_db, item.instrument_id, output_bars=output_bars,
                    )

                if bars is None or bars.empty:
                    # 数据不足 → skipped（不算失败）
                    async with AsyncSessionLocal() as skip_db:
                        await mark_history_item_skipped(
                            skip_db, item.id, "daily bars 为空（DB-only）",
                            lease_epoch=item.lease_epoch,
                        )
                        await skip_db.commit()
                    skipped_count += 1
                    continue

                # 2.2 计算 history SSOT（include_chip=False，禁止 chip）
                history = compute_first_pyramid_history(
                    bars=bars,
                    symbol=str(item.instrument_id),
                    output_bars=output_bars,
                    include_chip=False,
                )

                # 2.3 持久化（独立短事务）
                async with AsyncSessionLocal() as persist_db:
                    persisted = await _persist_history_result(
                        session=persist_db,
                        instrument_id=item.instrument_id,
                        history=history,
                        algorithm_version=algorithm_version,
                    )
                    await persist_db.commit()

                # 2.4 标记 succeeded
                async with AsyncSessionLocal() as mark_db:
                    ok = await mark_history_item_succeeded(
                        mark_db, item.id,
                        daily_state_count=persisted["daily_state_count"],
                        event_count=persisted["events_count"],
                        lease_epoch=item.lease_epoch,
                    )
                    await mark_db.commit()

                if ok:
                    succeeded_count += 1
                else:
                    logger.warning(
                        "[HistoryBackfill] item %s lease_epoch 不匹配，已被接管",
                        item.id,
                    )

            except Exception as exc:
                failed_count += 1
                logger.error(
                    "[HistoryBackfill] instrument_id=%s 回补失败: %s",
                    item.instrument_id, exc, exc_info=True,
                )
                try:
                    async with AsyncSessionLocal() as fail_db:
                        await mark_history_item_failed(
                            fail_db, item.id, str(exc),
                            lease_epoch=item.lease_epoch,
                        )
                        await fail_db.commit()
                except Exception as mark_exc:
                    logger.error(
                        "mark_history_item_failed 失败 item_id=%s: %s",
                        item.id, mark_exc,
                    )

            # 2.5 进度回调
            if progress_callback is not None:
                try:
                    await progress_callback(
                        processed=total_processed,
                        succeeded=succeeded_count,
                        failed=failed_count,
                        skipped=skipped_count,
                    )
                except Exception as cb_exc:
                    logger.warning("progress_callback 失败: %s", cb_exc)

    # 3. 从 DB 统计最终进度
    async with AsyncSessionLocal() as db:
        progress = await get_history_run_progress(db, history_run_id)

    # 4. 更新 run 最终状态
    final_status = (
        HISTORY_RUN_SUCCEEDED if failed_count == 0 and succeeded_count > 0
        else HISTORY_RUN_PARTIAL if succeeded_count > 0
        else HISTORY_RUN_FAILED
    )
    async with AsyncSessionLocal() as db:
        await finish_history_run(db, history_run_id, status=final_status)
        await db.commit()

    logger.info(
        "[HistoryBackfill] run=%s 完成: status=%s, succeeded=%d, failed=%d, skipped=%d",
        history_run_id, final_status, succeeded_count, failed_count, skipped_count,
    )

    return {
        "history_run_id": str(history_run_id),
        "algorithm_version": algorithm_version,
        "output_bars": output_bars,
        "status": final_status,
        "succeeded_count": succeeded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "total_processed": total_processed,
        "progress": progress,
    }


# =============================================================================
# 内部辅助
# =============================================================================


async def _fetch_history_daily_bars(
    instrument_id: uuid.UUID,
) -> pd.DataFrame | None:
    """获取完整可用日线（point-in-time，qfq 复权，已完成 bar）。

    [CHANGE-20260731-003] SSOT 合规：通过 MarketDataAggregationService (MDAS) 读取行情，
    不再调用 bar_repository.get_bars（已在 SSOT 黑名单中）。
    断点：依赖 MDAS 真实接口，本地纯单元测试由 caller 注入 _fetch_bars_func mock。
    """
    try:
        from app.db import AsyncSessionLocal
        from app.services.market_data_aggregation_service import MarketDataAggregationService
    except ImportError:
        logger.warning(
            "[HistoryBackfill] MDAS 或 AsyncSessionLocal 不可用，返回空 bars",
        )
        return None

    mdas = MarketDataAggregationService()
    async with AsyncSessionLocal() as db:
        result = await mdas.get_bars(
            db,
            instrument_id,
            timeframe="1d",
            adj="qfq",
            include_realtime=False,
            completed_only=True,
        )
        return result.bars


async def _persist_history_result(
    session: AsyncSession,
    instrument_id: uuid.UUID,
    history: dict[str, Any],
    algorithm_version: str,
) -> dict[str, int]:
    """持久化 history SSOT 结果到两张表。

    - daily_state: upsert（on_conflict_do_update），更新 state_payload
    - events: insert on_conflict_do_nothing（不可变，重跑不覆盖）

    Args:
        session: 异步 DB 会话（不 commit，由 caller 控制）
        instrument_id: 股票 ID
        history: compute_first_pyramid_history 返回的 dict
        algorithm_version: 算法版本

    Returns:
        {"daily_state_count": int, "events_count": int}
    """
    daily_state_list = history.get("daily_state") or []
    events_list = history.get("events") or []
    meta = history.get("meta") or {}
    input_hash = meta.get("input_hash") or ""

    daily_state_count = 0
    events_count = 0

    # 1. upsert daily_state
    for state in daily_state_list:
        time_str = state.get("time")
        if not time_str:
            continue
        try:
            trade_date_val = pd.to_datetime(time_str).date()
        except (ValueError, TypeError):
            continue

        stmt = pg_insert(FirstPyramidHistoryDailyState).values(
            instrument_id=instrument_id,
            trade_date=trade_date_val,
            algorithm_version=algorithm_version,
            input_hash=input_hash,
            state_payload=state,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_first_pyramid_history_daily_state_instr_date_ver",
            set_={
                "input_hash": stmt.excluded.input_hash,
                "state_payload": stmt.excluded.state_payload,
                "updated_at": func.now(),
            },
        )
        await session.execute(stmt)
        daily_state_count += 1

    # 2. insert events (on_conflict_do_nothing - immutable)
    for evt in events_list:
        event_type = evt.get("type") or evt.get("event_type") or "UNKNOWN"
        # 构造稳定 event_id：优先用 event 自带的 id，其次 bar_index+type，最后 time+type
        event_id = (
            evt.get("event_id")
            or evt.get("id")
            or _build_event_id(evt, event_type)
        )
        if not event_id:
            continue

        event_time = evt.get("time") or evt.get("anchor_time")

        stmt = pg_insert(FirstPyramidHistoryEvent).values(
            instrument_id=instrument_id,
            algorithm_version=algorithm_version,
            event_type=event_type,
            event_id=str(event_id),
            event_time=str(event_time) if event_time else None,
            event_payload=evt,
        )
        stmt = stmt.on_conflict_do_nothing(
            constraint="uq_first_pyramid_history_events_instr_ver_evid",
        )
        await session.execute(stmt)
        events_count += 1

    await session.flush()

    return {
        "daily_state_count": daily_state_count,
        "events_count": events_count,
    }


def _build_event_id(evt: dict[str, Any], event_type: str) -> str:
    """构造事件稳定 ID（无自带 id 时使用）。

    优先级：bar_index+type > time+type > anchor_time+type
    """
    bar_index = evt.get("bar_index")
    if bar_index is not None:
        return f"{event_type}_{bar_index}"

    time_val = evt.get("time") or evt.get("anchor_time")
    if time_val:
        return f"{event_type}_{time_val}"

    # fallback: hash of payload
    import hashlib
    import json
    payload_str = json.dumps(evt, sort_keys=True, default=str)
    return f"{event_type}_{hashlib.md5(payload_str.encode()).hexdigest()[:12]}"


# =============================================================================
# 模块自测
# =============================================================================


if __name__ == "__main__":
    # 纯静态自测：验证 _build_event_id 稳定性
    evt1 = {"type": "BOS", "bar_index": 50, "time": "2026-07-01"}
    evt2 = {"type": "OB_CREATED", "anchor_time": "2026-07-01", "ob_id": "abc"}
    evt3 = {"type": "SQZ_RELEASE", "time": "2026-07-01", "direction": "up"}

    id1 = _build_event_id(evt1, "BOS")
    id2 = _build_event_id(evt2, "OB_CREATED")
    id3 = _build_event_id(evt3, "SQZ_RELEASE")

    assert id1 == "BOS_50", f"id1={id1}"
    assert id2 == "OB_CREATED_2026-07-01", f"id2={id2}"
    assert id3 == "SQZ_RELEASE_2026-07-01", f"id3={id3}"

    # 验证 model 字段
    from app.models.first_pyramid_history import (
        FirstPyramidHistoryDailyState,
        FirstPyramidHistoryEvent,
    )
    ds_cols = {c.name for c in FirstPyramidHistoryDailyState.__table__.columns}
    ev_cols = {c.name for c in FirstPyramidHistoryEvent.__table__.columns}
    assert "state_payload" in ds_cols
    assert "event_payload" in ev_cols

    print("OK: first_pyramid_history_service 自测通过")
    print(f"  daily_state cols: {sorted(ds_cols)}")
    print(f"  events cols: {sorted(ev_cols)}")
    print(f"  event_id samples: {id1}, {id2}, {id3}")
