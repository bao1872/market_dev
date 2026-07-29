"""单股×阶段检查点服务 - per-stock commit、失败隔离、断点恢复。

核心能力（ref/instruction.md §三）：
1. create_run_items: 为 eligible universe 创建 core/pending items
2. claim_items: Worker 原子领取一批 pending/可恢复 items
3. mark_item_succeeded: 标记单股成功（per-stock commit 后调用）
4. mark_item_failed: 标记单股失败（per-stock rollback 后调用）
5. get_resume_items: 重启后获取需要处理的 items
6. get_run_progress: 获取 run 级进度统计

设计原则：
- batch 只控制吞吐/内存，不是完成或发布边界
- 单股结果 commit 成功后才标记 item succeeded
- 单股失败不回滚其他已成功股票
- 重启只处理 pending、可重试 failed、lease 过期 running
- input_hash 不变 + algorithm_version 不变 的 succeeded item 不重算

模块自测：
    PURE_UNIT_TEST=1 python -m app.services.snapshot_run_item_service
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot_run_item import (
    ITEM_FAILED,
    ITEM_PENDING,
    ITEM_RUNNING,
    ITEM_SKIPPED,
    ITEM_SUCCEEDED,
    PHASE_CORE,
    StockFeatureSnapshotRunItem,
)

logger = logging.getLogger("snapshot_run_item_service")

# 默认 lease 时长（秒），单股计算通常 < 30s
DEFAULT_ITEM_LEASE_SECONDS = 120

# 最大重试次数（超过后不再自动 resume）
MAX_ATTEMPT_COUNT = 3


async def create_run_items(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    instrument_ids: Sequence[uuid.UUID],
    *,
    phase: str = PHASE_CORE,
    input_hash: str | None = None,
) -> int:
    """为 eligible universe 创建 core/pending items（幂等）。

    在冻结计算范围后调用，为每只股票创建一个 pending item。
    expected_count 以 item 数量为准，不随股票状态变化漂移。

    幂等：已存在 (snapshot_run_id, instrument_id, phase) 的 item 跳过。

    Args:
        session: 异步 DB 会话
        snapshot_run_id: StockFeatureSnapshotRun.id
        instrument_ids: eligible universe
        phase: 阶段（默认 core）
        input_hash: 输入 hash（可选，用于重跑一致性校验）

    Returns:
        创建的 item 数量（已存在的跳过）
    """
    if not instrument_ids:
        return 0

    # 查找已存在的 items（幂等跳过）
    existing_stmt = (
        select(StockFeatureSnapshotRunItem.instrument_id)
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.phase == phase,
            StockFeatureSnapshotRunItem.instrument_id.in_(instrument_ids),
        )
    )
    existing_result = await session.execute(existing_stmt)
    existing_ids = {row[0] for row in existing_result}

    new_items = []
    for instrument_id in instrument_ids:
        if instrument_id in existing_ids:
            continue
        item = StockFeatureSnapshotRunItem(
            snapshot_run_id=snapshot_run_id,
            instrument_id=instrument_id,
            phase=phase,
            status=ITEM_PENDING,
            input_hash=input_hash,
        )
        new_items.append(item)

    if new_items:
        session.add_all(new_items)
        await session.flush()

    logger.info(
        "[RunItems] 创建 %d 个 %s/pending items (跳过 %d 已存在): "
        "snapshot_run_id=%s",
        len(new_items), phase, len(existing_ids), snapshot_run_id,
    )
    return len(new_items)


async def claim_items(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    *,
    worker_instance_id: str,
    batch_size: int = 25,
    phase: str = PHASE_CORE,
    lease_seconds: int = DEFAULT_ITEM_LEASE_SECONDS,
    max_attempt_count: int = MAX_ATTEMPT_COUNT,
) -> list[StockFeatureSnapshotRunItem]:
    """Worker 原子领取一批 pending/可恢复 items。

    原子性：使用 UPDATE ... WHERE status IN ('pending','failed') ...
    RETURNING 确保并发 Worker 不会重复领取。

    可领取的 items：
    - status=pending
    - status=failed 且 attempt_count < max_attempt_count
    - status=running 且 lease_expires_at < now()（lease 过期）

    领取后：
    - status=running
    - attempt_count += 1
    - lease_epoch += 1
    - worker_instance_id = worker
    - lease_expires_at = now + lease_seconds

    Args:
        session: 异步 DB 会话
        snapshot_run_id: StockFeatureSnapshotRun.id
        worker_instance_id: Worker 实例标识
        batch_size: 一次领取的 item 数（默认 25）
        phase: 阶段
        lease_seconds: lease 时长（秒）
        max_attempt_count: 最大重试次数

    Returns:
        领取到的 items 列表
    """
    now = datetime.now(UTC)
    lease_expires_at = now + timedelta(seconds=lease_seconds)

    # 原子领取：UPDATE ... WHERE ... RETURNING
    # 使用 raw SQL 确保 UPDATE + RETURNING 原子性
    claim_sql = text(
        """
        UPDATE stock_feature_snapshot_run_items
        SET status = 'running',
            attempt_count = attempt_count + 1,
            lease_epoch = lease_epoch + 1,
            worker_instance_id = :worker_id,
            started_at = COALESCE(started_at, :now),
            heartbeat_at = :now,
            lease_expires_at = :lease_expires,
            updated_at = :now
        WHERE id IN (
            SELECT id FROM stock_feature_snapshot_run_items
            WHERE snapshot_run_id = :snapshot_run_id
              AND phase = :phase
              AND (
                status = 'pending'
                OR (status = 'failed' AND attempt_count < :max_attempts)
                OR (status = 'running' AND lease_expires_at < :now)
              )
            ORDER BY created_at
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
        )
        RETURNING id, snapshot_run_id, instrument_id, phase, status,
                  attempt_count, input_hash, worker_instance_id,
                  lease_epoch, lease_expires_at, result_count, last_error,
                  started_at, heartbeat_at, completed_at, created_at, updated_at
        """
    )
    result = await session.execute(claim_sql, {
        "worker_id": worker_instance_id,
        "now": now,
        "lease_expires": lease_expires_at,
        "snapshot_run_id": snapshot_run_id,
        "phase": phase,
        "max_attempts": max_attempt_count,
        "batch_size": batch_size,
    })
    rows = result.fetchall()

    if not rows:
        return []

    # 转换为 ORM 对象（便于调用方使用）
    items: list[StockFeatureSnapshotRunItem] = []
    for row in rows:
        item = StockFeatureSnapshotRunItem(
            id=row[0],
            snapshot_run_id=row[1],
            instrument_id=row[2],
            phase=row[3],
            status=row[4],
            attempt_count=row[5],
            input_hash=row[6],
            worker_instance_id=row[7],
            lease_epoch=row[8],
            lease_expires_at=row[9],
            result_count=row[10],
            last_error=row[11],
            started_at=row[12],
            heartbeat_at=row[13],
            completed_at=row[14],
            created_at=row[15],
            updated_at=row[16],
        )
        items.append(item)

    logger.info(
        "[RunItems] Worker %s 领取 %d 个 items: snapshot_run_id=%s",
        worker_instance_id, len(items), snapshot_run_id,
    )
    return items


async def mark_item_succeeded(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    result_count: int | None = None,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 item 成功（per-stock commit 后调用）。

    [PRD §4.3 JOB-02] lease_epoch fencing：
    - lease_epoch 非 None 时校验，防止旧 Worker 覆盖
    - rowcount=0 表示 lease_epoch 不匹配（item 已被其他 Worker 接管）

    Args:
        session: 异步 DB 会话
        item_id: RunItem.id
        result_count: 结果数
        lease_epoch: 期望的 lease_epoch（None 时不校验）

    Returns:
        True 表示成功标记，False 表示 lease_epoch 不匹配
    """
    now = datetime.now(UTC)

    if lease_epoch is None:
        # Legacy 模式：不校验 lease_epoch
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
            )
            .values(
                status=ITEM_SUCCEEDED,
                completed_at=now,
                result_count=result_count,
                updated_at=now,
            )
        )
    else:
        # fenced UPDATE：校验 lease_epoch
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
                StockFeatureSnapshotRunItem.lease_epoch == lease_epoch,
            )
            .values(
                status=ITEM_SUCCEEDED,
                completed_at=now,
                result_count=result_count,
                updated_at=now,
            )
        )

    result = await session.execute(stmt)
    # 检查 rowcount（SQLAlchemy 2.0 返回 Result，需要 rowcount 属性）
    # 使用 fetchall 避免兼容性问题
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_item_failed(
    session: AsyncSession,
    item_id: uuid.UUID,
    error: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 item 失败（per-stock rollback 后调用）。

    失败只回滚该股票，不影响其他已成功股票。
    attempt_count 已在 claim 时递增，此处不重复。

    Args:
        session: 异步 DB 会话
        item_id: RunItem.id
        error: 失败原因
        lease_epoch: 期望的 lease_epoch

    Returns:
        True 表示成功标记，False 表示 lease_epoch 不匹配
    """
    now = datetime.now(UTC)

    if lease_epoch is None:
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
            )
            .values(
                status=ITEM_FAILED,
                last_error=error[:1000],
                completed_at=now,
                updated_at=now,
            )
        )
    else:
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
                StockFeatureSnapshotRunItem.lease_epoch == lease_epoch,
            )
            .values(
                status=ITEM_FAILED,
                last_error=error[:1000],
                completed_at=now,
                updated_at=now,
            )
        )

    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def mark_item_skipped(
    session: AsyncSession,
    item_id: uuid.UUID,
    reason: str,
    *,
    lease_epoch: int | None = None,
) -> bool:
    """标记单股 item 跳过（数据不足/停牌等，不算失败）。"""
    now = datetime.now(UTC)

    if lease_epoch is None:
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
            )
            .values(
                status=ITEM_SKIPPED,
                last_error=reason[:1000],
                completed_at=now,
                updated_at=now,
            )
        )
    else:
        stmt = (
            update(StockFeatureSnapshotRunItem)
            .where(
                StockFeatureSnapshotRunItem.id == item_id,
                StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
                StockFeatureSnapshotRunItem.lease_epoch == lease_epoch,
            )
            .values(
                status=ITEM_SKIPPED,
                last_error=reason[:1000],
                completed_at=now,
                updated_at=now,
            )
        )

    result = await session.execute(stmt)
    return result.rowcount > 0  # type: ignore[union-attr]


async def get_run_progress(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    *,
    phase: str = PHASE_CORE,
) -> dict[str, Any]:
    """获取 run 级进度统计（按 status 分组计数）。

    Returns:
        {
            "succeeded": int,
            "failed": int,
            "pending": int,
            "running": int,
            "skipped": int,
            "total": int,
            "coverage": float,  # succeeded / total
        }
    """
    stmt = (
        select(
            StockFeatureSnapshotRunItem.status,
            func.count(StockFeatureSnapshotRunItem.id).label("cnt"),
        )
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.phase == phase,
        )
        .group_by(StockFeatureSnapshotRunItem.status)
    )
    result = await session.execute(stmt)
    status_counts: dict[str, int] = {}
    for row in result:
        status_counts[row.status] = row.cnt

    succeeded = status_counts.get(ITEM_SUCCEEDED, 0)
    failed = status_counts.get(ITEM_FAILED, 0)
    pending = status_counts.get(ITEM_PENDING, 0)
    running = status_counts.get(ITEM_RUNNING, 0)
    skipped = status_counts.get(ITEM_SKIPPED, 0)
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


async def get_resume_items(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    *,
    phase: str = PHASE_CORE,
    max_attempt_count: int = MAX_ATTEMPT_COUNT,
) -> list[StockFeatureSnapshotRunItem]:
    """获取需要 resume 的 items（重启后调用）。

    可 resume 的 items：
    - status=pending
    - status=failed 且 attempt_count < max_attempt_count
    - status=running 且 lease_expires_at < now()（lease 过期）

    以下 items 不重算：
    - status=succeeded（且 input_hash 相同且 algorithm_version 相同）
    - status=skipped
    - status=failed 且 attempt_count >= max_attempt_count（需人工介入）
    """
    now = datetime.now(UTC)

    stmt = (
        select(StockFeatureSnapshotRunItem)
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.phase == phase,
        )
        .where(
            (StockFeatureSnapshotRunItem.status == ITEM_PENDING)
            | (
                (StockFeatureSnapshotRunItem.status == ITEM_FAILED)
                & (StockFeatureSnapshotRunItem.attempt_count < max_attempt_count)
            )
            | (
                (StockFeatureSnapshotRunItem.status == ITEM_RUNNING)
                & (StockFeatureSnapshotRunItem.lease_expires_at < now)
            )
        )
        .order_by(StockFeatureSnapshotRunItem.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def recover_stale_running_items(
    session: AsyncSession,
    snapshot_run_id: uuid.UUID,
    *,
    phase: str = PHASE_CORE,
) -> int:
    """将 lease 过期的 running items 恢复为 pending（watchdog 调用）。

    Returns:
        恢复的 item 数量
    """
    now = datetime.now(UTC)

    stmt = (
        update(StockFeatureSnapshotRunItem)
        .where(
            StockFeatureSnapshotRunItem.snapshot_run_id == snapshot_run_id,
            StockFeatureSnapshotRunItem.phase == phase,
            StockFeatureSnapshotRunItem.status == ITEM_RUNNING,
            StockFeatureSnapshotRunItem.lease_expires_at < now,
        )
        .values(
            status=ITEM_PENDING,
            last_error="lease expired, recovered to pending",
            updated_at=now,
        )
    )
    result = await session.execute(stmt)
    count = result.rowcount  # type: ignore[union-attr]
    if count and count > 0:
        logger.info(
            "[RunItems] 恢复 %d 个 lease 过期 running items 为 pending: "
            "snapshot_run_id=%s",
            count, snapshot_run_id,
        )
    return count or 0


if __name__ == "__main__":
    print(f"DEFAULT_ITEM_LEASE_SECONDS = {DEFAULT_ITEM_LEASE_SECONDS}")
    print(f"MAX_ATTEMPT_COUNT = {MAX_ATTEMPT_COUNT}")
    print("OK: snapshot_run_item_service imports verified")
