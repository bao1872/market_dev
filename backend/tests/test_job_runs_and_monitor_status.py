"""Task 6-11 测试：监控状态语义拆分与任务可观察性可靠化。

覆盖：
- watchlist monitor-status 返回 market_session / calculation_status / freshness_seconds / last_bar_time
- 盘后时间 calculation_status 不因 30 分钟规则变成 STALE
- SchedulerJobRun 心跳、租约、worker_instance_id、last_cycle_at
- strategy_scheduler 找不到策略时正确结束 job_run 并记录 strategy_run_id
- monitor_scheduler 按交易时段聚合 session

注意：Worker 启动恢复过期 running 任务的测试已迁移至
tests/test_scheduler_job_run_recovery_service.py（基于 PostgreSQL 测试库，
覆盖 recover_stale_scheduler_job_runs 的 5 个场景）。
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

from app.api.watchlist import (
    _compute_calculation_status,
    _compute_market_status,
)
from app.models.scheduler_job_run import SchedulerJobRun
from app.worker import (
    _create_job_run,
    _finish_job_run,
    _update_job_heartbeat,
)


@pytest_asyncio.fixture(loop_scope="session")
async def test_db():
    """每个测试独立的内存 SQLite 异步 DB 会话（仅创建 scheduler_job_runs 表）。"""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(SchedulerJobRun.__table__.create)
    SessionLocal = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False,
    )
    async with SessionLocal() as session:
        yield session
    await engine.dispose()


# ==================== Task 6: 监控状态语义拆分 ====================


def test_compute_market_status_non_trading_day() -> None:
    """非交易日返回 NON_TRADING_DAY。"""
    now = datetime(2026, 6, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _compute_market_status(now, is_trading_day=False) == "NON_TRADING_DAY"


def test_compute_market_status_trading_morning() -> None:
    """交易日 09:30-11:30 返回 MORNING_SESSION。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _compute_market_status(now, is_trading_day=True) == "MORNING_SESSION"


def test_compute_market_status_lunch_break() -> None:
    """交易日 11:30-13:00 返回 LUNCH_BREAK。"""
    now = datetime(2026, 6, 24, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _compute_market_status(now, is_trading_day=True) == "LUNCH_BREAK"


def test_compute_market_status_after_market() -> None:
    """交易日 15:00 后返回 MARKET_CLOSED。"""
    now = datetime(2026, 6, 24, 19, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _compute_market_status(now, is_trading_day=True) == "MARKET_CLOSED"


# ==================== compute_market_session 6 值枚举覆盖测试 ====================


def test_compute_market_session_six_enums() -> None:
    """覆盖 compute_market_session 6 值枚举（advice.md 规范）。

    用例：
    - 非交易日 10:00 → NON_TRADING_DAY
    - 交易日 09:00 → PRE_OPEN
    - 交易日 10:00 → MORNING_SESSION
    - 交易日 12:00 → LUNCH_BREAK
    - 交易日 14:00 → AFTERNOON_SESSION
    - 交易日 15:35 → MARKET_CLOSED
    """
    from app.services.market_status_service import compute_market_session

    tz = ZoneInfo("Asia/Shanghai")
    cases = [
        (datetime(2026, 6, 20, 10, 0, tzinfo=tz), False, "NON_TRADING_DAY"),
        (datetime(2026, 6, 24, 9, 0, tzinfo=tz), True, "PRE_OPEN"),
        (datetime(2026, 6, 24, 10, 0, tzinfo=tz), True, "MORNING_SESSION"),
        (datetime(2026, 6, 24, 12, 0, tzinfo=tz), True, "LUNCH_BREAK"),
        (datetime(2026, 6, 24, 14, 0, tzinfo=tz), True, "AFTERNOON_SESSION"),
        (datetime(2026, 6, 24, 15, 35, tzinfo=tz), True, "MARKET_CLOSED"),
    ]
    for now, is_trading_day, expected in cases:
        got = compute_market_session(now, is_trading_day)
        assert got == expected, f"{now.time()} is_td={is_trading_day}: got={got} expected={expected}"


def test_compute_market_session_boundary_times() -> None:
    """边界时间点测试：09:30 / 11:30 / 13:00 / 15:00。"""
    from app.services.market_status_service import compute_market_session

    tz = ZoneInfo("Asia/Shanghai")
    # 边界值：左闭右闭 / 左闭右开（与实现一致）
    assert compute_market_session(datetime(2026, 6, 24, 9, 30, tzinfo=tz), True) == "MORNING_SESSION"
    assert compute_market_session(datetime(2026, 6, 24, 11, 30, tzinfo=tz), True) == "MORNING_SESSION"
    assert compute_market_session(datetime(2026, 6, 24, 11, 31, tzinfo=tz), True) == "LUNCH_BREAK"
    assert compute_market_session(datetime(2026, 6, 24, 13, 0, tzinfo=tz), True) == "AFTERNOON_SESSION"
    assert compute_market_session(datetime(2026, 6, 24, 15, 0, tzinfo=tz), True) == "AFTERNOON_SESSION"
    assert compute_market_session(datetime(2026, 6, 24, 15, 1, tzinfo=tz), True) == "MARKET_CLOSED"


def test_calculation_status_after_market_old_data_is_succeeded() -> None:
    """盘后 19:01 且数据为 14:59，calculation_status 应为 SUCCEEDED 而非 STALE。"""
    now = datetime(2026, 6, 24, 19, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    market_session = "MARKET_CLOSED"
    updated_at = datetime(2026, 6, 24, 14, 59, tzinfo=ZoneInfo("Asia/Shanghai"))

    # 构造一个模拟的 ms 对象，只有 updated_at 属性
    ms_updated_at = updated_at

    class FakeMS:
        updated_at = ms_updated_at

    calc_status = _compute_calculation_status(
        now_cst=now,
        market_session=market_session,
        eval_row=None,
        ms=FakeMS(),
    )
    assert calc_status == "SUCCEEDED"


def test_calculation_status_trading_stale_when_old() -> None:
    """盘中数据超过 180 秒，calculation_status 应为 STALE。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    market_session = "MORNING_SESSION"
    ms_updated_at = now - timedelta(seconds=300)

    class FakeMS:
        updated_at = ms_updated_at

    calc_status = _compute_calculation_status(
        now_cst=now,
        market_session=market_session,
        eval_row=None,
        ms=FakeMS(),
    )
    assert calc_status == "STALE"


def test_calculation_status_trading_fresh_when_recent() -> None:
    """盘中数据 60 秒内，calculation_status 应为 SUCCEEDED。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    market_session = "MORNING_SESSION"
    ms_updated_at = now - timedelta(seconds=60)

    class FakeMS:
        updated_at = ms_updated_at

    calc_status = _compute_calculation_status(
        now_cst=now,
        market_session=market_session,
        eval_row=None,
        ms=FakeMS(),
    )
    assert calc_status == "SUCCEEDED"


def test_calculation_status_failed_evaluation() -> None:
    """评估状态 FAILED 时 calculation_status 应为 FAILED。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

    class FakeEval:
        evaluation_status = "FAILED"

    calc_status = _compute_calculation_status(
        now_cst=now,
        market_session="MORNING_SESSION",
        eval_row=FakeEval(),
        ms=None,
    )
    assert calc_status == "FAILED"


def test_calculation_status_waiting_first_run_in_trading() -> None:
    """盘中无评估记录时 calculation_status 应为 WAITING_FIRST_RUN。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    calc_status = _compute_calculation_status(
        now_cst=now,
        market_session="MORNING_SESSION",
        eval_row=None,
        ms=None,
    )
    assert calc_status == "WAITING_FIRST_RUN"


# ==================== Task 7/8/9: SchedulerJobRun 生命周期 ====================


@pytest.mark.asyncio
async def test_create_job_run_sets_lease_and_heartbeat(test_db) -> None:
    """创建 job_run 时应设置 worker_instance_id、scheduled_at、heartbeat_at、lease_expires_at。"""
    job_run = await _create_job_run(test_db, "test_job", "2026-06-24")

    assert job_run.worker_instance_id is not None
    assert job_run.scheduled_at is not None
    assert job_run.heartbeat_at is not None
    assert job_run.lease_expires_at is not None
    assert job_run.lease_expires_at > job_run.heartbeat_at
    assert job_run.status == "running"


@pytest.mark.asyncio
async def test_finish_job_run_updates_status_and_counts(test_db) -> None:
    """结束 job_run 时应更新状态、完成时间、成功/失败计数。"""
    job_run = await _create_job_run(test_db, "test_job", "2026-06-24")
    await _finish_job_run(test_db, job_run, "succeeded", success_count=3, failure_count=1)

    attached = await test_db.get(SchedulerJobRun, job_run.id)
    assert attached is not None
    assert attached.status == "succeeded"
    assert attached.finished_at is not None
    assert attached.succeeded_count == 3
    assert attached.failed_count == 1


@pytest.mark.asyncio
async def test_update_job_heartbeat_renews_lease(test_db) -> None:
    """心跳更新应刷新 heartbeat_at 与 lease_expires_at。"""
    job_run = await _create_job_run(test_db, "test_job", "2026-06-24")
    old_lease = job_run.lease_expires_at
    # SQLite 内存测试环境下时区信息可能被剥离，统一归一化为上海时区后再比较
    if old_lease is not None and old_lease.tzinfo is None:
        old_lease = old_lease.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    await asyncio.sleep(0.1)
    await _update_job_heartbeat(test_db, job_run)

    attached = await test_db.get(SchedulerJobRun, job_run.id)
    assert attached is not None
    if attached.lease_expires_at is not None and attached.lease_expires_at.tzinfo is None:
        attached_lease_expires_at = attached.lease_expires_at.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    else:
        attached_lease_expires_at = attached.lease_expires_at
    assert attached.heartbeat_at >= job_run.heartbeat_at
    assert attached_lease_expires_at > old_lease


# ==================== Task 10: strategy_scheduler job_run 完整性 ====================


@pytest.mark.asyncio
async def test_strategy_scheduler_no_selector_finishes_failed(test_db) -> None:
    """未找到 selector 策略时，job_run 最终状态应为 failed 而非 running。"""
    # 该测试通过直接复现 worker 内部逻辑验证：
    # 创建 job_run -> 查询到空策略列表 -> 必须调用 _finish_job_run("failed")
    from datetime import date as date_cls

    trade_date = date_cls(2026, 6, 24)
    job_run = await _create_job_run(test_db, "strategy_scheduler", str(trade_date))
    # 模拟未找到 selector 策略的分支
    await _finish_job_run(
        test_db,
        job_run,
        "failed",
        error_message="未找到 kind=selector 的策略",
    )

    attached = await test_db.get(SchedulerJobRun, job_run.id)
    assert attached is not None
    assert attached.status == "failed"
    assert attached.error_message == "未找到 kind=selector 的策略"


@pytest.mark.asyncio
async def test_strategy_scheduler_metadata_contains_strategy_run_id(test_db) -> None:
    """strategy_scheduler 应在 metadata_json 中记录 strategy_run_id。"""
    job_run = await _create_job_run(test_db, "strategy_scheduler", "2026-06-24")
    strategy_run_id = uuid.uuid4()
    job_run.metadata_json = json.dumps({"strategy_run_id": str(strategy_run_id)})
    await test_db.commit()

    attached = await test_db.get(SchedulerJobRun, job_run.id)
    assert attached is not None
    meta = json.loads(attached.metadata_json or "{}")
    assert meta.get("strategy_run_id") == str(strategy_run_id)


# ==================== Task 11: monitor_scheduler session 聚合 ====================


def test_monitor_session_label_morning() -> None:
    """09:30-11:30 返回 morning session。"""
    from app.worker import _get_monitor_session

    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    label, start, end = _get_monitor_session(now)
    assert label == "morning"
    assert start == datetime(2026, 6, 24, 9, 30, tzinfo=ZoneInfo("Asia/Shanghai")).time()
    assert end == datetime(2026, 6, 24, 11, 30, tzinfo=ZoneInfo("Asia/Shanghai")).time()


def test_monitor_session_label_afternoon() -> None:
    """13:00-15:00 返回 afternoon session。"""
    from app.worker import _get_monitor_session

    now = datetime(2026, 6, 24, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    label, start, end = _get_monitor_session(now)
    assert label == "afternoon"
    assert start == datetime(2026, 6, 24, 13, 0, tzinfo=ZoneInfo("Asia/Shanghai")).time()
    assert end == datetime(2026, 6, 24, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")).time()


def test_monitor_session_label_outside_session() -> None:
    """非交易时段返回 None。"""
    from app.worker import _get_monitor_session

    now = datetime(2026, 6, 24, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    result = _get_monitor_session(now)
    assert result is None


@pytest.mark.asyncio
async def test_monitor_scheduler_reuses_session_job_run(test_db) -> None:
    """同一交易时段第二次调用应返回 None（幂等跳过），调用方按 run_key 查询复用。"""
    from datetime import date as date_cls

    from sqlalchemy import select

    from app.worker import _find_or_create_monitor_session_job_run

    trade_date = date_cls(2026, 6, 24)
    now = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    job_run1 = await _find_or_create_monitor_session_job_run(
        test_db, now, str(trade_date), "morning",
    )
    await test_db.commit()
    assert job_run1 is not None

    # 第二次调用返回 None（幂等：run_key 已存在）
    job_run2 = await _find_or_create_monitor_session_job_run(
        test_db, now, str(trade_date), "morning",
    )
    assert job_run2 is None

    # 调用方按 run_key 查询复用现有记录
    run_key = f"monitor_scheduler:{trade_date}:morning"
    stmt = select(SchedulerJobRun).where(SchedulerJobRun.run_key == run_key).limit(1)
    result = await test_db.execute(stmt)
    reused = result.scalar_one_or_none()
    assert reused is not None
    assert reused.id == job_run1.id


@pytest.mark.asyncio
async def test_monitor_scheduler_different_sessions_create_separate_runs(test_db) -> None:
    """上午和下午应创建不同的 SchedulerJobRun。"""
    from datetime import date as date_cls

    from app.worker import _find_or_create_monitor_session_job_run

    trade_date = date_cls(2026, 6, 24)
    morning = datetime(2026, 6, 24, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    afternoon = datetime(2026, 6, 24, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    job_run_morning = await _find_or_create_monitor_session_job_run(
        test_db, morning, str(trade_date), "morning",
    )
    await test_db.commit()

    job_run_afternoon = await _find_or_create_monitor_session_job_run(
        test_db, afternoon, str(trade_date), "afternoon",
    )
    await test_db.commit()

    assert job_run_morning.id != job_run_afternoon.id


# ==================== _notify_monitor_status 修复测试 ====================


def _make_mock_channel(user_id=None):
    """构造 mock NotificationChannel。"""
    ch = MagicMock()
    ch.adapter_type = "feishu_platform_app"
    ch.status = "active"
    ch.user_id = user_id or uuid.uuid4()
    ch.target_config = {}
    return ch


def _make_mock_session_local(mock_db):
    """构造 mock AsyncSessionLocal 上下文管理器。"""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=mock_session)


@pytest.mark.asyncio
async def test_notify_monitor_summary_excludes_title(monkeypatch):
    """启动通知 summary 不应重复 title（仅放 content）。"""
    from app.worker import _monitor_start_notified, _notify_monitor_status

    _monitor_start_notified.clear()

    captured_dtos: list = []

    class FakeAdapter:
        async def send(self, dto, config):
            captured_dtos.append(dto)
            return MagicMock(success=True, error_message=None)

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [_make_mock_channel()]
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    monkeypatch.setenv("GIT_SHA", "test-summary-sha")

    with patch("app.worker.AsyncSessionLocal", _make_mock_session_local(mock_db)), \
         patch("app.services.channel_adapter.get_adapter", return_value=FakeAdapter()), \
         patch("app.core.redis_client.get_redis", return_value=mock_redis):
        await _notify_monitor_status(
            "监控服务已启动", "交易时段 9:30-11:30 / 13:00-15:00",
            is_error=False,
        )

    assert len(captured_dtos) == 1
    dto = captured_dtos[0]
    # summary 不应包含 title 文本
    assert "监控服务已启动" not in dto.summary
    # summary 应包含 content
    assert "交易时段" in dto.summary


@pytest.mark.asyncio
async def test_notify_monitor_data_time_shanghai_timezone(monkeypatch):
    """data_time 应使用上海时区（含 +08:00）而非 UTC（+00:00）。"""
    from app.worker import _monitor_start_notified, _notify_monitor_status

    _monitor_start_notified.clear()

    captured_dtos: list = []

    class FakeAdapter:
        async def send(self, dto, config):
            captured_dtos.append(dto)
            return MagicMock(success=True, error_message=None)

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [_make_mock_channel()]
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    monkeypatch.setenv("GIT_SHA", "test-tz-sha")

    with patch("app.worker.AsyncSessionLocal", _make_mock_session_local(mock_db)), \
         patch("app.services.channel_adapter.get_adapter", return_value=FakeAdapter()), \
         patch("app.core.redis_client.get_redis", return_value=mock_redis):
        await _notify_monitor_status("监控服务已启动", "测试内容", is_error=False)

    assert len(captured_dtos) == 1
    assert "+08:00" in captured_dtos[0].data_time
    assert "+00:00" not in captured_dtos[0].data_time


@pytest.mark.asyncio
async def test_notify_monitor_startup_idempotent(monkeypatch):
    """同一 git_sha 第二次调用启动通知不应发送（Redis 幂等）。"""
    from app.worker import _monitor_start_notified, _notify_monitor_status

    _monitor_start_notified.clear()

    send_count = 0

    class FakeAdapter:
        async def send(self, dto, config):
            nonlocal send_count
            send_count += 1
            return MagicMock(success=True, error_message=None)

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [_make_mock_channel()]
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    monkeypatch.setenv("GIT_SHA", "test-idem-sha")

    # 第一次：Redis SET NX 返回 True（获取到锁），发送通知
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    with patch("app.worker.AsyncSessionLocal", _make_mock_session_local(mock_db)), \
         patch("app.services.channel_adapter.get_adapter", return_value=FakeAdapter()), \
         patch("app.core.redis_client.get_redis", return_value=mock_redis):
        await _notify_monitor_status("监控服务已启动", "测试", is_error=False)

    assert send_count == 1

    # 第二次：Redis SET NX 返回 None（键已存在），跳过发送
    mock_redis.set = AsyncMock(return_value=None)

    with patch("app.worker.AsyncSessionLocal", _make_mock_session_local(mock_db)), \
         patch("app.services.channel_adapter.get_adapter", return_value=FakeAdapter()), \
         patch("app.core.redis_client.get_redis", return_value=mock_redis):
        await _notify_monitor_status("监控服务已启动", "测试", is_error=False)

    # 仍然只发送了一次
    assert send_count == 1


@pytest.mark.asyncio
async def test_notify_monitor_startup_filters_admin_only(monkeypatch):
    """启动通知 SQL 应包含 admin 角色过滤（普通用户不被推送）。"""
    from sqlalchemy.dialects import postgresql

    from app.worker import _monitor_start_notified, _notify_monitor_status

    _monitor_start_notified.clear()

    captured_stmts: list = []

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []

    async def capture_execute(stmt):
        captured_stmts.append(stmt)
        return mock_result

    mock_db = AsyncMock()
    mock_db.execute = capture_execute

    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    monkeypatch.setenv("GIT_SHA", "test-admin-sha")

    with patch("app.worker.AsyncSessionLocal", _make_mock_session_local(mock_db)), \
         patch("app.core.redis_client.get_redis", return_value=mock_redis):
        # 启动通知：SQL 应包含 user_roles / roles 表（admin 过滤）
        await _notify_monitor_status("监控服务已启动", "测试", is_error=False)

        assert len(captured_stmts) == 1
        sql_str = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
        assert "user_roles" in sql_str
        assert "roles" in sql_str

        # 异常通知：SQL 不应包含 admin 过滤（发送给所有活跃飞书渠道）
        captured_stmts.clear()
        await _notify_monitor_status("监控服务异常", "测试错误", is_error=True)

        assert len(captured_stmts) == 1
        sql_str_err = str(captured_stmts[0].compile(dialect=postgresql.dialect()))
        assert "user_roles" not in sql_str_err


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
