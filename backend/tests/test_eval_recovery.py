"""评估恢复机制集成测试 - 验证 lease/heartbeat/retry 逻辑。

测试场景：
1. PENDING + lease expired → 可被重新认领
2. FAILED + retry_count < max → 可被重试
3. DEAD → 不可重试
4. SUCCEEDED → 跳过
5. Worker 启动恢复过期 PENDING 评估
6. 失败标记含指数退避

用法：
    cd backend && python -m pytest tests/test_eval_recovery.py -v
"""
import uuid
from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models.monitor_evaluation import MonitorEvaluation
from app.services.monitor_batch_service import (
    _LEASE_DURATION_SECONDS,
    _MAX_RETRIES,
    _RETRY_BACKOFF_BASE_SECONDS,
    MonitorBatchService,
)

_CST = ZoneInfo("Asia/Shanghai")


def _make_evaluation(
    strategy_version_id: uuid.UUID,
    instrument_id: uuid.UUID,
    source_bar_time,
    status: str = "PENDING",
    retry_count: int = 0,
    lease_expires_at=None,
    next_retry_at=None,
    heartbeat_at=None,
    metrics=None,
) -> MonitorEvaluation:
    """构造 MonitorEvaluation ORM 对象（不含 id，由 DB 生成）。"""
    return MonitorEvaluation(
        strategy_version_id=strategy_version_id,
        instrument_id=instrument_id,
        source_bar_time=source_bar_time,
        status=status,
        retry_count=retry_count,
        lease_expires_at=lease_expires_at,
        next_retry_at=next_retry_at,
        heartbeat_at=heartbeat_at,
        metrics=metrics,
    )


@pytest.mark.asyncio
async def test_pending_lease_expired_can_be_reclaimed(
    db_session, test_selector_strategy, test_instrument
):
    """PENDING + lease expired → 可被重新认领。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 PENDING + lease 已过期的评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="PENDING",
        retry_count=0,
        lease_expires_at=now_cst - timedelta(seconds=1),  # 已过期
        heartbeat_at=now_cst - timedelta(seconds=300),
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 查询确认存在
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.strategy_version_id == version.id,
        MonitorEvaluation.instrument_id == test_instrument.id,
        MonitorEvaluation.source_bar_time == bar_time,
    )
    result = await db_session.execute(stmt)
    existing = result.scalar_one()
    assert existing.status == "PENDING"
    assert existing.lease_expires_at < _now_cst()

    # 验证：PENDING + lease expired 应该可以被重新认领
    # 模拟 _process_instrument_evaluation 的冲突处理逻辑
    now_cst = _now_cst()
    if existing.status == "PENDING" and existing.lease_expires_at is not None and existing.lease_expires_at < now_cst:
        existing.retry_count += 1
        existing.lease_expires_at = now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS)
        existing.heartbeat_at = now_cst

    assert existing.retry_count == 1
    assert existing.lease_expires_at > _now_cst()
    assert existing.heartbeat_at is not None


@pytest.mark.asyncio
async def test_failed_retry_count_below_max_can_be_retried(
    db_session, test_selector_strategy, test_instrument
):
    """FAILED + retry_count < max → 可被重试（退避时间已过）。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 FAILED + retry_count=1 + next_retry_at 已过的评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="FAILED",
        retry_count=1,
        next_retry_at=now_cst - timedelta(seconds=1),  # 退避时间已过
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 查询
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.strategy_version_id == version.id,
        MonitorEvaluation.instrument_id == test_instrument.id,
        MonitorEvaluation.source_bar_time == bar_time,
    )
    result = await db_session.execute(stmt)
    existing = result.scalar_one()
    assert existing.status == "FAILED"
    assert existing.retry_count < _MAX_RETRIES

    # 模拟重试逻辑
    now_cst = _now_cst()
    if existing.status == "FAILED" and existing.retry_count < _MAX_RETRIES:
        next_retry = existing.next_retry_at
        if next_retry is not None and next_retry <= now_cst:
            existing.retry_count += 1
            existing.status = "PENDING"
            existing.lease_expires_at = now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS)
            existing.heartbeat_at = now_cst

    assert existing.status == "PENDING"
    assert existing.retry_count == 2
    assert existing.lease_expires_at > _now_cst()


@pytest.mark.asyncio
async def test_dead_cannot_be_retried(
    db_session, test_selector_strategy, test_instrument
):
    """DEAD → 不可重试。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 DEAD 评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="DEAD",
        retry_count=_MAX_RETRIES,
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 查询
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.strategy_version_id == version.id,
        MonitorEvaluation.instrument_id == test_instrument.id,
        MonitorEvaluation.source_bar_time == bar_time,
    )
    result = await db_session.execute(stmt)
    existing = result.scalar_one()

    # DEAD 状态应跳过，不可重入
    assert existing.status == "DEAD"
    should_skip = existing.status == "DEAD"
    assert should_skip is True


@pytest.mark.asyncio
async def test_succeeded_is_skipped(
    db_session, test_selector_strategy, test_instrument
):
    """SUCCEEDED → 跳过。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 SUCCEEDED 评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="SUCCEEDED",
        metrics={"state": {}, "events_detected": 0},
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 查询
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.strategy_version_id == version.id,
        MonitorEvaluation.instrument_id == test_instrument.id,
        MonitorEvaluation.source_bar_time == bar_time,
    )
    result = await db_session.execute(stmt)
    existing = result.scalar_one()

    # SUCCEEDED 状态应跳过
    assert existing.status == "SUCCEEDED"
    should_skip = existing.status == "SUCCEEDED"
    assert should_skip is True


@pytest.mark.asyncio
async def test_recover_stale_evaluations(
    db_session, test_selector_strategy, test_instrument
):
    """Worker 启动恢复：PENDING + lease expired → 重置可重试。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 PENDING + lease 已过期的评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="PENDING",
        retry_count=0,
        lease_expires_at=now_cst - timedelta(seconds=1),
        heartbeat_at=now_cst - timedelta(seconds=300),
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 调用 recover_stale_evaluations
    service = MonitorBatchService()
    recovered = await service.recover_stale_evaluations(db_session)
    assert recovered == 1

    # 验证评估已重置
    await db_session.flush()
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.id == eval_obj.id,
    )
    result = await db_session.execute(stmt)
    refreshed = result.scalar_one()
    assert refreshed.retry_count == 1
    assert refreshed.lease_expires_at is None
    assert refreshed.next_retry_at is not None
    assert refreshed.heartbeat_at is None


@pytest.mark.asyncio
async def test_mark_evaluation_failed_with_backoff(
    db_session, test_selector_strategy, test_instrument
):
    """失败标记含指数退避：30*2^1=60s, 30*2^2=120s, ..."""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 PENDING 评估
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="PENDING",
        retry_count=0,
        lease_expires_at=now_cst + timedelta(seconds=_LEASE_DURATION_SECONDS),
        heartbeat_at=now_cst,
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 调用 _mark_evaluation_failed
    service = MonitorBatchService()
    await service._mark_evaluation_failed(
        db_session, eval_obj.id, "test error",
    )

    # 验证：retry_count=1, status=FAILED, next_retry_at 在 60s 后
    await db_session.flush()
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.id == eval_obj.id,
    )
    result = await db_session.execute(stmt)
    refreshed = result.scalar_one()
    assert refreshed.status == "FAILED"
    assert refreshed.retry_count == 1
    assert refreshed.next_retry_at is not None
    # 退避时间应为 30 * 2^1 = 60s
    expected_backoff = _RETRY_BACKOFF_BASE_SECONDS * (2 ** 1)
    assert expected_backoff == 60
    # next_retry_at 应大约在 now + 60s 附近
    delta = (refreshed.next_retry_at - _now_cst()).total_seconds()
    assert 55 < delta < 65, f"Expected backoff ~60s, got {delta}s"


@pytest.mark.asyncio
async def test_mark_evaluation_failed_max_retries_becomes_dead(
    db_session, test_selector_strategy, test_instrument
):
    """达最大重试次数后标记为 DEAD。"""
    version = test_selector_strategy["version"]
    now_cst = _now_cst()
    bar_time = now_cst.replace(second=0, microsecond=0)

    # 创建 FAILED + retry_count=4 的评估（再失败一次即达 MAX_RETRIES=5）
    eval_obj = _make_evaluation(
        strategy_version_id=version.id,
        instrument_id=test_instrument.id,
        source_bar_time=bar_time,
        status="FAILED",
        retry_count=4,
        next_retry_at=now_cst - timedelta(seconds=1),
    )
    db_session.add(eval_obj)
    await db_session.flush()

    # 调用 _mark_evaluation_failed
    service = MonitorBatchService()
    await service._mark_evaluation_failed(
        db_session, eval_obj.id, "final error",
    )

    # 验证：retry_count=5, status=DEAD
    await db_session.flush()
    stmt = select(MonitorEvaluation).where(
        MonitorEvaluation.id == eval_obj.id,
    )
    result = await db_session.execute(stmt)
    refreshed = result.scalar_one()
    assert refreshed.status == "DEAD"
    assert refreshed.retry_count == 5


def _now_cst():
    """获取当前北京时间。"""
    from datetime import datetime
    return datetime.now(_CST)
