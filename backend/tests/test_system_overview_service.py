"""test_system_overview_service.py - 系统概览服务测试。

覆盖：
- market_session 6 种场景（通过 service 集成测试，mock is_trading_day_async）
- monitor_runtime 7 种状态（RUNNING/DELAYED/SESSION_COMPLETED/FAILED/WORKER_OFFLINE/NOT_APPLICABLE/IDLE_EXPECTED）
- after_close_pipeline 关键场景（含昨日 bars 不满足今日、backfill 不覆盖 scheduled 边界）

测试策略：
- service 函数接受可选 now 参数，注入固定时间（无需 freezegun）
- mock is_trading_day_async 控制交易日标志
- 使用 db_session fixture 创建测试数据，事务自动回滚
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.core.time import SHANGHAI_TZ
from app.models.monitor_evaluation import MonitorEvaluation
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.strategy_run import StrategyRun
from app.models.worker_heartbeat import WorkerHeartbeat
from app.services.market_status_service import (
    MARKET_SESSION_AFTERNOON,
    MARKET_SESSION_CLOSED,
    MARKET_SESSION_LUNCH,
    MARKET_SESSION_MORNING,
    MARKET_SESSION_NON_TRADING_DAY,
    MARKET_SESSION_PRE_OPEN,
)
from app.services.system_overview_service import (
    FRESHNESS_DELAYED_THRESHOLD,
    HEARTBEAT_OFFLINE_THRESHOLD,
    _determine_monitor_status,
    get_system_overview,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")

# 测试用固定日期（周二，交易日）
TEST_DATE = date(2026, 6, 24)
TEST_DATE_STR = "2026-06-24"
YESTERDAY_DATE = date(2026, 6, 23)
YESTERDAY_DATE_STR = "2026-06-23"


def _mock_trading_day(is_trading: bool = True):
    """创建 is_trading_day_async 的 mock 上下文。"""
    return patch(
        "app.services.calendar_service.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=is_trading,
    )


# ==================== market_session 测试（6 种场景）====================


@pytest.mark.asyncio
async def test_market_session_non_trading_day(db_session):
    """非交易日 → NON_TRADING_DAY。"""
    now = datetime(2026, 6, 20, 10, 0, tzinfo=SHANGHAI)  # 周六
    with _mock_trading_day(is_trading=False):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_NON_TRADING_DAY


@pytest.mark.asyncio
async def test_market_session_pre_open(db_session):
    """交易日 09:00 → PRE_OPEN。"""
    now = datetime(2026, 6, 24, 9, 0, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_PRE_OPEN


@pytest.mark.asyncio
async def test_market_session_morning(db_session):
    """交易日 10:00 → MORNING_SESSION。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_MORNING


@pytest.mark.asyncio
async def test_market_session_lunch_break(db_session):
    """交易日 12:00 → LUNCH_BREAK。"""
    now = datetime(2026, 6, 24, 12, 0, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_LUNCH


@pytest.mark.asyncio
async def test_market_session_afternoon(db_session):
    """交易日 14:00 → AFTERNOON_SESSION。"""
    now = datetime(2026, 6, 24, 14, 0, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_AFTERNOON


@pytest.mark.asyncio
async def test_market_session_closed(db_session):
    """交易日 15:35 → MARKET_CLOSED。"""
    now = datetime(2026, 6, 24, 15, 35, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["market_session"] == MARKET_SESSION_CLOSED


@pytest.mark.asyncio
async def test_server_time_and_business_date(db_session):
    """验证 server_time 和 business_date 字段。"""
    now = datetime(2026, 6, 24, 10, 30, tzinfo=SHANGHAI)
    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)
    assert result["server_time"] == now.isoformat()
    assert result["business_date"] == "2026-06-24"


# ==================== _determine_monitor_status 纯函数测试 ====================


def test_determine_monitor_status_non_trading_day():
    """非交易日 → NOT_APPLICABLE。"""
    assert _determine_monitor_status(
        MARKET_SESSION_NON_TRADING_DAY, None, None, None, None, ""
    ) == "NOT_APPLICABLE"


def test_determine_monitor_status_pre_open():
    """盘前 → IDLE_EXPECTED。"""
    assert _determine_monitor_status(
        MARKET_SESSION_PRE_OPEN, None, None, None, None, ""
    ) == "IDLE_EXPECTED"


def test_determine_monitor_status_lunch_break():
    """午休 → IDLE_EXPECTED。"""
    assert _determine_monitor_status(
        MARKET_SESSION_LUNCH, None, None, None, None, ""
    ) == "IDLE_EXPECTED"


def test_determine_monitor_status_worker_offline():
    """盘中心跳超时 → WORKER_OFFLINE。"""
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, HEARTBEAT_OFFLINE_THRESHOLD + 10, None, None, None, ""
    ) == "WORKER_OFFLINE"


def test_determine_monitor_status_no_heartbeat():
    """盘中无心跳（heartbeat_age_seconds=None）→ WORKER_OFFLINE。"""
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, None, None, None, None, ""
    ) == "WORKER_OFFLINE"


def test_determine_monitor_status_delayed():
    """盘中数据延迟 → DELAYED。"""
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, FRESHNESS_DELAYED_THRESHOLD + 20, None, None, ""
    ) == "DELAYED"


def test_determine_monitor_status_running():
    """盘中正常运行 → RUNNING。"""
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, 60, None, None, ""
    ) == "RUNNING"


def test_determine_monitor_status_running_no_data():
    """盘中无数据（freshness=None）→ RUNNING。"""
    assert _determine_monitor_status(
        MARKET_SESSION_MORNING, 30, None, None, None, ""
    ) == "RUNNING"


def test_determine_monitor_status_afternoon_running():
    """下午盘正常运行 → RUNNING。"""
    assert _determine_monitor_status(
        MARKET_SESSION_AFTERNOON, 30, 60, None, None, ""
    ) == "RUNNING"


# ==================== monitor_runtime 集成测试（7 种状态）====================


@pytest.mark.asyncio
async def test_monitor_runtime_running(db_session, test_selector_strategy, test_instrument):
    """盘中正常运行 → RUNNING。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id
    instrument_id = test_instrument.id

    # 心跳 30s 前
    hb = WorkerHeartbeat(
        worker_name="monitor_scheduler",
        instance_id="test:1234",
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(seconds=30),
        status="running",
    )
    db_session.add(hb)

    # 评估 60s 前的 bar
    ev = MonitorEvaluation(
        strategy_version_id=version_id,
        instrument_id=instrument_id,
        source_bar_time=now - timedelta(seconds=60),
        status="SUCCEEDED",
        calculated_at=now - timedelta(seconds=30),
    )
    db_session.add(ev)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    mr = result["monitor_runtime"]
    assert mr["status"] == "RUNNING"
    assert mr["heartbeat_age_seconds"] == 30
    assert mr["freshness_seconds"] == 60
    assert mr["evaluated_count"] == 1
    assert mr["session_label"] == "morning"
    assert mr["business_date"] == "2026-06-24"


@pytest.mark.asyncio
async def test_monitor_runtime_delayed(db_session, test_selector_strategy, test_instrument):
    """盘中数据延迟 > 180s → DELAYED。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id
    instrument_id = test_instrument.id

    hb = WorkerHeartbeat(
        worker_name="monitor_scheduler",
        instance_id="test:1234",
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(seconds=30),
        status="running",
    )
    db_session.add(hb)

    # source_bar_time 200s 前（> 180s 阈值）
    ev = MonitorEvaluation(
        strategy_version_id=version_id,
        instrument_id=instrument_id,
        source_bar_time=now - timedelta(seconds=200),
        status="SUCCEEDED",
        calculated_at=now - timedelta(seconds=190),
    )
    db_session.add(ev)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "DELAYED"
    assert result["monitor_runtime"]["freshness_seconds"] == 200


@pytest.mark.asyncio
async def test_monitor_runtime_session_completed(db_session):
    """收盘后下午盘已完成 → SESSION_COMPLETED。"""
    now = datetime(2026, 6, 24, 15, 35, tzinfo=SHANGHAI)

    # 下午盘 job succeeded, failed_count=0
    job = SchedulerJobRun(
        job_name="monitor_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 13, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 15, 0, tzinfo=SHANGHAI),
        succeeded_count=100,
        failed_count=0,
        metadata_json=json.dumps({"session_label": "afternoon"}),
    )
    db_session.add(job)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "SESSION_COMPLETED"


@pytest.mark.asyncio
async def test_monitor_runtime_failed(db_session):
    """收盘后下午盘失败 → FAILED。"""
    now = datetime(2026, 6, 24, 15, 35, tzinfo=SHANGHAI)

    job = SchedulerJobRun(
        job_name="monitor_scheduler",
        business_date=TEST_DATE_STR,
        status="failed",
        started_at=datetime(2026, 6, 24, 13, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 14, 0, tzinfo=SHANGHAI),
        error_message="worker crashed",
        metadata_json=json.dumps({"session_label": "afternoon"}),
    )
    db_session.add(job)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "FAILED"


@pytest.mark.asyncio
async def test_monitor_runtime_worker_offline(db_session):
    """盘中 worker 离线（心跳 > 90s）→ WORKER_OFFLINE。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)

    # 心跳 100s 前（> 90s 阈值）
    hb = WorkerHeartbeat(
        worker_name="monitor_scheduler",
        instance_id="test:1234",
        started_at=now - timedelta(hours=2),
        heartbeat_at=now - timedelta(seconds=100),
        status="running",
    )
    db_session.add(hb)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "WORKER_OFFLINE"
    assert result["monitor_runtime"]["heartbeat_age_seconds"] == 100


@pytest.mark.asyncio
async def test_monitor_runtime_not_applicable(db_session):
    """非交易日 → NOT_APPLICABLE。"""
    now = datetime(2026, 6, 20, 10, 0, tzinfo=SHANGHAI)  # 周六

    with _mock_trading_day(is_trading=False):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "NOT_APPLICABLE"


@pytest.mark.asyncio
async def test_monitor_runtime_idle_expected_lunch(db_session):
    """午休 → IDLE_EXPECTED。"""
    now = datetime(2026, 6, 24, 12, 0, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "IDLE_EXPECTED"


@pytest.mark.asyncio
async def test_monitor_runtime_idle_expected_no_afternoon_job(db_session):
    """收盘后无下午盘记录 → IDLE_EXPECTED。"""
    now = datetime(2026, 6, 24, 15, 35, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["monitor_runtime"]["status"] == "IDLE_EXPECTED"


@pytest.mark.asyncio
async def test_monitor_runtime_only_queries_monitor_scheduler(db_session):
    """验证只查 monitor_scheduler 心跳，不汇总其他 worker。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)

    # bars_scheduler 心跳新鲜，但 monitor_scheduler 无心跳
    hb_bars = WorkerHeartbeat(
        worker_name="bars_scheduler",
        instance_id="test:5678",
        started_at=now - timedelta(hours=1),
        heartbeat_at=now - timedelta(seconds=10),
        status="running",
    )
    db_session.add(hb_bars)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    # monitor_scheduler 无心跳 → heartbeat_at=None，盘中判 WORKER_OFFLINE
    mr = result["monitor_runtime"]
    assert mr["heartbeat_at"] is None
    assert mr["heartbeat_age_seconds"] is None
    assert mr["status"] == "WORKER_OFFLINE"


# ==================== after_close_pipeline 测试（关键场景）====================


@pytest.mark.asyncio
async def test_pipeline_not_started_before_16(db_session):
    """16:00 前 → NOT_STARTED。"""
    now = datetime(2026, 6, 24, 15, 35, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "NOT_STARTED"
    assert pipeline["bars_job"] is None
    assert pipeline["dsa_run"] is None


@pytest.mark.asyncio
async def test_pipeline_bars_running(db_session):
    """bars 运行中 → BARS_RUNNING。"""
    now = datetime(2026, 6, 24, 16, 5, tzinfo=SHANGHAI)

    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="running",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
    )
    db_session.add(job)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "BARS_RUNNING"
    assert pipeline["bars_job"]["status"] == "running"


@pytest.mark.asyncio
async def test_pipeline_bars_failed(db_session):
    """bars 失败 → BARS_FAILED。"""
    now = datetime(2026, 6, 24, 16, 30, tzinfo=SHANGHAI)

    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="failed",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 15, tzinfo=SHANGHAI),
        error_message="bars download failed",
    )
    db_session.add(job)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "BARS_FAILED"
    assert pipeline["bars_job"]["error_message"] == "bars download failed"


@pytest.mark.asyncio
async def test_pipeline_waiting_dsa(db_session):
    """bars 成功等待 DSA → WAITING_DSA。"""
    now = datetime(2026, 6, 24, 16, 30, tzinfo=SHANGHAI)

    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    )
    db_session.add(job)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "WAITING_DSA"
    assert pipeline["bars_job"]["status"] == "succeeded"
    assert pipeline["dsa_run"] is None


@pytest.mark.asyncio
async def test_pipeline_dsa_queued(db_session, test_selector_strategy):
    """DSA queued → DSA_QUEUED。"""
    now = datetime(2026, 6, 24, 16, 30, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    )
    db_session.add(job)

    run = StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
    )
    db_session.add(run)
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_QUEUED"
    assert pipeline["dsa_run"]["status"] == "queued"
    assert pipeline["dsa_run"]["run_type"] == "scheduled"


@pytest.mark.asyncio
async def test_pipeline_dsa_running(db_session, test_selector_strategy):
    """DSA running → DSA_RUNNING。"""
    now = datetime(2026, 6, 24, 16, 30, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="running",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    assert result["after_close_pipeline"]["status"] == "DSA_RUNNING"


@pytest.mark.asyncio
async def test_pipeline_dsa_published(db_session, test_selector_strategy):
    """DSA published 且 failed_count=0 → PUBLISHED。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=0,
        succeeded_count=100,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "PUBLISHED"
    assert pipeline["dsa_run"]["failed_count"] == 0


@pytest.mark.asyncio
async def test_pipeline_dsa_failed(db_session, test_selector_strategy):
    """DSA failed → DSA_FAILED。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="failed",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=50,
        error_message="calculation error",
        failure_stage="CALCULATE_INSTRUMENTS",
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_FAILED"
    assert pipeline["dsa_run"]["failure_stage"] == "CALCULATE_INSTRUMENTS"


# ==================== after_close_pipeline 关键边界测试 ====================


@pytest.mark.asyncio
async def test_pipeline_yesterday_bars_not_satisfy_today(db_session):
    """昨日 bars 成功不满足今日状态（关键边界）。"""
    now = datetime(2026, 6, 24, 16, 30, tzinfo=SHANGHAI)

    # 昨日的 bars job succeeded
    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=YESTERDAY_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 23, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 23, 16, 20, tzinfo=SHANGHAI),
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    # 今日无 bars job → NOT_STARTED（不引用昨日 succeeded）
    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "NOT_STARTED"
    assert pipeline["bars_job"] is None


@pytest.mark.asyncio
async def test_pipeline_backfill_not_cover_scheduled(db_session, test_selector_strategy):
    """历史 backfill/manual 不覆盖 scheduled 状态（关键边界）。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    # 今日 bars succeeded
    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))

    # backfill 运行（不应覆盖 scheduled 状态）
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="backfill",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=0,
        succeeded_count=100,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    # 无 scheduled 运行 → WAITING_DSA（backfill 不覆盖）
    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "WAITING_DSA"
    assert pipeline["dsa_run"] is None


@pytest.mark.asyncio
async def test_pipeline_dsa_completed_partial(db_session, test_selector_strategy):
    """DSA published 且 failed_count>0 → DSA_COMPLETED（部分成功）。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=5,
        succeeded_count=95,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_COMPLETED"
    assert pipeline["dsa_run"]["failed_count"] == 5


@pytest.mark.asyncio
async def test_pipeline_attempt_no_desc(db_session, test_selector_strategy):
    """验证按 attempt_no DESC 取最新运行。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    db_session.add(SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=TEST_DATE_STR,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    ))

    # attempt_no=1 failed
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="failed",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}:1",
        attempt_no=1,
        failed_count=100,
    ))
    # attempt_no=2 published（最新）
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}:2",
        attempt_no=2,
        failed_count=0,
        succeeded_count=100,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    # 应取 attempt_no=2（published, failed_count=0）
    assert pipeline["status"] == "PUBLISHED"
    assert pipeline["dsa_run"]["attempt_no"] == 2


# ==================== 基础字段回归测试 ====================


@pytest.mark.asyncio
async def test_base_fields_backward_compatible(db_session):
    """验证 12 个基础字段向后兼容。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    # 验证 12 个基础字段都存在
    base_keys = {
        "active_users", "distinct_monitored_instruments", "evaluations_last_minute",
        "evaluations_success_rate", "notification_delivery_rate", "queue_backlog",
        "failed_retry_count", "latest_selector_run", "worker_health",
        "scheduler_health", "recent_scheduler_jobs", "recent_anomalies",
    }
    for key in base_keys:
        assert key in result, f"缺少基础字段: {key}"

    # 验证字段类型（不假设 DB 为空，只验证类型正确）
    assert isinstance(result["active_users"], int)
    assert isinstance(result["distinct_monitored_instruments"], int)
    assert isinstance(result["evaluations_last_minute"], int)
    assert isinstance(result["evaluations_success_rate"], float)
    assert isinstance(result["notification_delivery_rate"], float)
    assert isinstance(result["queue_backlog"], int)
    assert isinstance(result["failed_retry_count"], int)
    assert isinstance(result["worker_health"], str)
    assert isinstance(result["scheduler_health"], str)
    assert isinstance(result["recent_scheduler_jobs"], list)
    assert isinstance(result["recent_anomalies"], list)
    assert result["recent_anomalies"] == []
    assert result["notification_delivery_rate"] == 0.0


@pytest.mark.asyncio
async def test_response_has_17_fields(db_session):
    """验证响应包含 17 个字段（12 基础 + 5 新增）。"""
    now = datetime(2026, 6, 24, 10, 0, tzinfo=SHANGHAI)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    expected_keys = {
        # 基础 12 个
        "active_users", "distinct_monitored_instruments", "evaluations_last_minute",
        "evaluations_success_rate", "notification_delivery_rate", "queue_backlog",
        "failed_retry_count", "latest_selector_run", "worker_health",
        "scheduler_health", "recent_scheduler_jobs", "recent_anomalies",
        # 新增 5 个
        "server_time", "business_date", "market_session",
        "monitor_runtime", "after_close_pipeline",
    }
    assert set(result.keys()) == expected_keys, (
        f"字段不匹配，多余: {set(result.keys()) - expected_keys}, "
        f"缺少: {expected_keys - set(result.keys())}"
    )


# ==================== WAITING_DSA 细分原因测试（7 种场景）====================


async def _create_bars_succeeded_job(db_session, business_date_str: str = TEST_DATE_STR):
    """创建当日 bars_scheduler succeeded job（盘后流水线前置条件）。"""
    job = SchedulerJobRun(
        job_name="bars_scheduler",
        business_date=business_date_str,
        status="succeeded",
        started_at=datetime(2026, 6, 24, 16, 0, tzinfo=SHANGHAI),
        finished_at=datetime(2026, 6, 24, 16, 20, tzinfo=SHANGHAI),
    )
    db_session.add(job)
    await db_session.flush()
    return job


async def _create_active_instruments(db_session, count: int = 5):
    """创建指定数量的 active 标的（满足 FK 约束）。"""
    from app.models.instrument import Instrument

    instruments = []
    for i in range(count):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:6]}",
            name=f"测试标的{i}",
            market="SZ",
            status="active",
        )
        db_session.add(inst)
        instruments.append(inst)
    await db_session.flush()
    return instruments


async def _create_bars_daily(db_session, instruments, trade_date: date):
    """为指定标的列表创建当日 BarDaily 记录（全部覆盖）。"""
    from decimal import Decimal

    from app.models.bar import BarDaily

    for inst in instruments:
        db_session.add(BarDaily(
            instrument_id=inst.id,
            trade_date=trade_date,
            open=Decimal("10.0"),
            high=Decimal("11.0"),
            low=Decimal("9.0"),
            close=Decimal("10.5"),
            volume=Decimal("1000000"),
        ))
    await db_session.flush()


async def _create_bars_daily_for_all_active(db_session, trade_date: date) -> int:
    """为 DB 中所有 active 标的创建当日 BarDaily（确保覆盖率 100%）。

    用于需要覆盖率达标（>= 90%）的 WAITING_DSA 场景测试（NO_RUN_CREATED / NO_RELEASED_VERSION）。
    测试库存在 seed active 标的，若仅覆盖测试创建的标的会导致覆盖率 < 90% 而误判为
    DATA_COVERAGE_INSUFFICIENT。本辅助函数补全所有 active 标的的 BarDaily（幂等，跳过已存在）。

    Returns:
        新创建的 BarDaily 记录数
    """
    from decimal import Decimal

    from sqlalchemy import select

    from app.models.bar import BarDaily
    from app.models.instrument import Instrument

    # 查询所有 active 标的（含 seed 数据与测试创建的）
    all_active = (await db_session.execute(
        select(Instrument.id).where(Instrument.status == "active")
    )).scalars().all()

    # 查询已有当日 BarDaily 的标的（避免重复 PK）
    existing_ids = set((await db_session.execute(
        select(BarDaily.instrument_id).where(BarDaily.trade_date == trade_date)
    )).scalars().all())

    created = 0
    for inst_id in all_active:
        if inst_id in existing_ids:
            continue
        db_session.add(BarDaily(
            instrument_id=inst_id,
            trade_date=trade_date,
            open=Decimal("10.0"),
            high=Decimal("11.0"),
            low=Decimal("9.0"),
            close=Decimal("10.5"),
            volume=Decimal("1000000"),
        ))
        created += 1
    await db_session.flush()
    return created


@pytest.mark.asyncio
async def test_waiting_dsa_reason_no_run_created(db_session, test_selector_strategy):
    """WAITING_DSA 原因 1/7: NO_RUN_CREATED。

    场景：bars succeeded + 覆盖率达标 + 有 released selector 版本，但无 DSA run。
    原因：strategy_scheduler 18:30 未触发或 create_batch_run 异常。
    """
    now = datetime(2026, 6, 24, 17, 0, tzinfo=SHANGHAI)

    # bars succeeded
    await _create_bars_succeeded_job(db_session)

    # 创建 active 标的 + 为所有 active 标的（含 seed）创建 BarDaily，确保覆盖率 100%
    await _create_active_instruments(db_session, count=5)
    await _create_bars_daily_for_all_active(db_session, TEST_DATE)

    # test_selector_strategy fixture 已创建 released selector 版本

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "WAITING_DSA"
    assert pipeline["waiting_dsa_reason"] == "NO_RUN_CREATED"
    assert pipeline["waiting_dsa_suggestion"] is not None
    assert "strategy_scheduler" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_queued_not_claimed(db_session, test_selector_strategy):
    """WAITING_DSA 原因 2/7: QUEUED_NOT_CLAIMED。

    场景：DSA run 已创建 30+ 分钟但仍为 queued 且无 worker_id。
    原因：trading-worker-strategy-batch 容器不健康。
    """
    now = datetime(2026, 6, 24, 17, 30, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    # DSA run queued 35 分钟前，无 worker_id
    queued_at = now - timedelta(minutes=35)
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        queued_at=queued_at,
        worker_id=None,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_QUEUED"
    assert pipeline["waiting_dsa_reason"] == "QUEUED_NOT_CLAIMED"
    assert "trading-worker-strategy-batch" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_data_coverage_insufficient(db_session):
    """WAITING_DSA 原因 3/7: DATA_COVERAGE_INSUFFICIENT。

    场景：bars succeeded 但覆盖率 < 90%，无 DSA run。
    原因：日线数据未完整同步。
    注意：不使用 test_selector_strategy fixture，避免创建 released 版本干扰优先级。
    """
    now = datetime(2026, 6, 24, 17, 0, tzinfo=SHANGHAI)

    await _create_bars_succeeded_job(db_session)

    # 创建 10 个 active 标的，但只为 5 个创建 BarDaily（coverage = 50% < 90%）
    instruments = await _create_active_instruments(db_session, count=10)
    await _create_bars_daily(db_session, instruments[:5], TEST_DATE)

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "WAITING_DSA"
    assert pipeline["waiting_dsa_reason"] == "DATA_COVERAGE_INSUFFICIENT"
    assert "重新同步日线数据" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_no_released_version(db_session):
    """WAITING_DSA 原因 4/7: NO_RELEASED_VERSION。

    场景：bars succeeded + 覆盖率达标，但无 selector released 版本。
    原因：管理员未发布 selector 策略版本。
    注意：不使用 test_selector_strategy fixture，确保无 released selector。
    """
    now = datetime(2026, 6, 24, 17, 0, tzinfo=SHANGHAI)

    await _create_bars_succeeded_job(db_session)

    # 创建 active 标的 + 为所有 active 标的（含 seed）创建 BarDaily，确保覆盖率 100%
    await _create_active_instruments(db_session, count=3)
    await _create_bars_daily_for_all_active(db_session, TEST_DATE)

    # 不创建任何 selector 策略 → _has_released_selector_version 返回 False

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "WAITING_DSA"
    assert pipeline["waiting_dsa_reason"] == "NO_RELEASED_VERSION"
    assert "发布 selector 版本" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_run_failed(db_session, test_selector_strategy):
    """WAITING_DSA 原因 5/7: RUN_FAILED。

    场景：DSA run failed，failure_stage = CALCULATE_INSTRUMENTS（非 QUALITY_GATE/PUBLISH）。
    原因：计算阶段异常。
    """
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="failed",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=100,
        error_message="计算异常",
        failure_stage="CALCULATE_INSTRUMENTS",
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_FAILED"
    assert pipeline["waiting_dsa_reason"] == "RUN_FAILED"
    assert "查看失败股票" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_quality_gate_failed(db_session, test_selector_strategy):
    """WAITING_DSA 原因 6/7: QUALITY_GATE_FAILED。

    场景：DSA run failed，failure_stage = QUALITY_GATE。
    原因：质量门禁未通过。
    """
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="failed",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=0,
        succeeded_count=60,
        total_instruments=100,
        error_message="质量门禁未通过",
        failure_stage="QUALITY_GATE",
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_FAILED"
    assert pipeline["waiting_dsa_reason"] == "QUALITY_GATE_FAILED"
    assert "质量门禁" in pipeline["waiting_dsa_suggestion"]


@pytest.mark.asyncio
async def test_waiting_dsa_reason_publish_failed(db_session, test_selector_strategy):
    """WAITING_DSA 原因 7/7: PUBLISH_FAILED。

    场景：DSA run failed，failure_stage = PUBLISH。
    原因：发布逻辑异常。
    """
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="failed",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=0,
        succeeded_count=100,
        total_instruments=100,
        error_message="发布失败",
        failure_stage="PUBLISH",
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_FAILED"
    assert pipeline["waiting_dsa_reason"] == "PUBLISH_FAILED"
    assert "发布逻辑" in pipeline["waiting_dsa_suggestion"]


# ==================== WAITING_DSA 边界测试 ====================


@pytest.mark.asyncio
async def test_waiting_dsa_reason_none_when_published(db_session, test_selector_strategy):
    """边界：DSA published 且 failed_count=0 → reason=None（成功终态不细分）。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="published",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        failed_count=0,
        succeeded_count=100,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "PUBLISHED"
    assert pipeline["waiting_dsa_reason"] is None
    assert pipeline["waiting_dsa_suggestion"] is None


@pytest.mark.asyncio
async def test_waiting_dsa_queued_not_claimed_within_timeout(db_session, test_selector_strategy):
    """边界：DSA queued 但未超 30 分钟 → reason=None（正常排队中）。"""
    now = datetime(2026, 6, 24, 17, 10, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    # queued 5 分钟前（未超 30 分钟阈值）
    queued_at = now - timedelta(minutes=5)
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        queued_at=queued_at,
        worker_id=None,
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_QUEUED"
    # 未超时 → reason 为 None
    assert pipeline["waiting_dsa_reason"] is None


@pytest.mark.asyncio
async def test_waiting_dsa_queued_with_worker_id(db_session, test_selector_strategy):
    """边界：DSA queued 超 30 分钟但有 worker_id → reason=None（已被领取）。"""
    now = datetime(2026, 6, 24, 18, 0, tzinfo=SHANGHAI)
    version_id = test_selector_strategy["version"].id

    await _create_bars_succeeded_job(db_session)

    # queued 40 分钟前，但有 worker_id（已被领取，可能 worker 卡住但非未领取）
    queued_at = now - timedelta(minutes=40)
    db_session.add(StrategyRun(
        strategy_version_id=version_id,
        run_type="scheduled",
        trade_date=TEST_DATE,
        status="queued",
        input_overrides={},
        idempotency_key=f"test:{uuid.uuid4().hex}",
        attempt_no=1,
        queued_at=queued_at,
        worker_id="worker-1:12345",
    ))
    await db_session.flush()

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert pipeline["status"] == "DSA_QUEUED"
    # 有 worker_id → 不算 QUEUED_NOT_CLAIMED
    assert pipeline["waiting_dsa_reason"] is None


@pytest.mark.asyncio
async def test_after_close_pipeline_has_waiting_dsa_fields(db_session):
    """验证 after_close_pipeline 始终包含 waiting_dsa_reason/suggestion 字段。"""
    now = datetime(2026, 6, 24, 15, 0, tzinfo=SHANGHAI)  # 16:00 前

    with _mock_trading_day(is_trading=True):
        result = await get_system_overview(db_session, now=now)

    pipeline = result["after_close_pipeline"]
    assert "waiting_dsa_reason" in pipeline
    assert "waiting_dsa_suggestion" in pipeline
    # NOT_STARTED 状态下应为 None
    assert pipeline["waiting_dsa_reason"] is None
    assert pipeline["waiting_dsa_suggestion"] is None
