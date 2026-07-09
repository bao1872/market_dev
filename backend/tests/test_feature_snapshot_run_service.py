"""StockFeatureSnapshotRun 服务函数单元测试（Phase 4）。

验证维度：
1. create_snapshot_run: 创建 running run + 幂等复用 + failed 后可新建
2. finish_snapshot_run: succeeded 写 published_at + failed 不写 + counts/failure_rate

用法：
    cd backend && APP_ENV=test TEST_DATABASE_URL=postgresql+psycopg://... \
        pytest tests/test_feature_snapshot_run_service.py -v
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.stock_feature_snapshot_run import (
    STATUS_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
    StockFeatureSnapshotRun,
)
from app.services.feature_snapshot_service import (
    _SCHEMA_VERSION,
    create_snapshot_run,
    finish_snapshot_run,
)

# ===== 1. create_snapshot_run =====


@pytest.mark.asyncio
async def test_create_snapshot_run_creates_running_record(
    db_session: AsyncSession,
) -> None:
    """create_snapshot_run 创建 status='running' 的 run 记录。"""
    trade_date = date(2026, 7, 8)
    run = await create_snapshot_run(
        db_session,
        trade_date=trade_date,
        run_type="after_close",
        expected_count=100,
    )

    assert run.id is not None
    assert run.trade_date == trade_date
    assert run.run_type == "after_close"
    assert run.status == STATUS_RUNNING
    assert run.schema_version == _SCHEMA_VERSION
    assert run.primary_timeframe == "1d"
    assert run.secondary_timeframe == "15m"
    assert run.adj == "qfq"
    assert run.expected_count == 100
    assert run.started_at is not None
    assert run.published_at is None  # running 时未发布


@pytest.mark.asyncio
async def test_create_snapshot_run_idempotent_returns_existing_running(
    db_session: AsyncSession,
) -> None:
    """已存在 running run 时，再次调用返回同一条记录（幂等）。"""
    trade_date = date(2026, 7, 8)
    run1 = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="backfill",
    )

    run2 = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="backfill",
    )

    # 应返回同一条记录
    assert run1.id == run2.id
    assert run2.status == STATUS_RUNNING

    # DB 中只有一条 running 记录
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == trade_date,
        StockFeatureSnapshotRun.run_type == "backfill",
        StockFeatureSnapshotRun.status == STATUS_RUNNING,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_create_snapshot_run_allows_new_after_previous_failed(
    db_session: AsyncSession,
) -> None:
    """前一个 run failed 后，可创建新 running run（部分唯一索引仅约束 running）。"""
    trade_date = date(2026, 7, 8)
    run1 = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="after_close",
    )
    await finish_snapshot_run(
        db_session, run1, status=STATUS_FAILED, failed_count=10,
    )

    # 失败后可创建新 run
    run2 = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="after_close",
    )
    assert run2.id != run1.id
    assert run2.status == STATUS_RUNNING
    assert run1.status == STATUS_FAILED

    # DB 中有 2 条记录（1 failed + 1 running）
    stmt = select(StockFeatureSnapshotRun).where(
        StockFeatureSnapshotRun.trade_date == trade_date,
        StockFeatureSnapshotRun.run_type == "after_close",
    ).order_by(StockFeatureSnapshotRun.created_at)
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 2
    assert rows[0].status == STATUS_FAILED
    assert rows[1].status == STATUS_RUNNING


# ===== 2. finish_snapshot_run =====


@pytest.mark.asyncio
async def test_finish_snapshot_run_succeeded_writes_published_at(
    db_session: AsyncSession,
) -> None:
    """succeeded 状态写入 published_at + finished_at + counts。"""
    trade_date = date(2026, 7, 8)
    run = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="after_close",
        expected_count=100,
    )

    await finish_snapshot_run(
        db_session, run,
        status=STATUS_SUCCEEDED,
        snapshot_count=95,
        failed_count=3,
        skipped_count=2,
        failure_rate=0.03,
    )

    # 重新查询验证
    stmt = select(StockFeatureSnapshotRun).where(StockFeatureSnapshotRun.id == run.id)
    finished = (await db_session.execute(stmt)).scalar_one()
    assert finished.status == STATUS_SUCCEEDED
    assert finished.snapshot_count == 95
    assert finished.failed_count == 3
    assert finished.skipped_count == 2
    assert finished.failure_rate == pytest.approx(0.03)
    assert finished.finished_at is not None
    assert finished.published_at is not None  # succeeded 必须写 published_at


@pytest.mark.asyncio
async def test_finish_snapshot_run_failed_does_not_write_published_at(
    db_session: AsyncSession,
) -> None:
    """failed 状态不写 published_at（watchlist 不读取该 run 的 snapshot）。"""
    trade_date = date(2026, 7, 8)
    run = await create_snapshot_run(
        db_session, trade_date=trade_date, run_type="after_close",
        expected_count=100,
    )

    await finish_snapshot_run(
        db_session, run,
        status=STATUS_FAILED,
        snapshot_count=20,
        failed_count=80,
        failure_rate=0.8,
        metadata={"reason": "failure_threshold_exceeded"},
    )

    stmt = select(StockFeatureSnapshotRun).where(StockFeatureSnapshotRun.id == run.id)
    finished = (await db_session.execute(stmt)).scalar_one()
    assert finished.status == STATUS_FAILED
    assert finished.snapshot_count == 20
    assert finished.failed_count == 80
    assert finished.failure_rate == pytest.approx(0.8)
    assert finished.finished_at is not None
    assert finished.published_at is None  # failed 不写 published_at
    assert finished.metadata_ is not None
    assert finished.metadata_["reason"] == "failure_threshold_exceeded"


@pytest.mark.asyncio
async def test_finish_snapshot_run_accepts_metadata_for_audit(
    db_session: AsyncSession,
) -> None:
    """metadata 字段记录审计信息（如 rollback_reason、failure_threshold）。"""
    trade_date = date(2026, 7, 8)
    run = await create_snapshot_run(
        db_session,
        trade_date=trade_date,
        run_type="backfill",
        metadata={"batch_size": 20, "source": "instrument_first"},
    )

    # create 时 metadata 已写入
    assert run.metadata_ is not None
    assert run.metadata_["batch_size"] == 20

    await finish_snapshot_run(
        db_session, run,
        status=STATUS_SUCCEEDED,
        snapshot_count=50,
        metadata={"rollback_reason": None, "duration_sec": 120.5},
    )

    stmt = select(StockFeatureSnapshotRun).where(StockFeatureSnapshotRun.id == run.id)
    finished = (await db_session.execute(stmt)).scalar_one()
    # finish 时的 metadata 覆盖 create 时的 metadata
    assert finished.metadata_ is not None
    assert finished.metadata_["duration_sec"] == 120.5
    assert finished.metadata_ is not None
    assert finished.metadata_["rollback_reason"] is None
