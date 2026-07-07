"""/watchlist/monitor-status 读取 feature snapshot 测试。

覆盖：
- 有 snapshot 时返回 _source='feature_snapshot'，不调用 MonitorSnapshotService
- 无 snapshot 且收盘后返回 WAITING_SNAPSHOT
- 无 snapshot 且未收盘/非交易日返回 NO_SNAPSHOT
- [Phase5 Run Gate] watchlist 只读取 succeeded run 对应的 snapshot 行

测试策略：
- 使用 conftest client fixture + 认证用户覆盖
- mock is_trading_day_async 固定交易日
- 插入 StockFeatureSnapshot + StockFeatureSnapshotRun 记录验证读取
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.main import app
from app.models.calendar import TradingCalendar
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
from app.models.watchlist import UserWatchlistItem


def _make_calendar_row(trade_date, is_trading: bool = True) -> TradingCalendar:
    """构造 TradingCalendar 行（market=A, status=OPEN）。"""
    return TradingCalendar(
        trade_date=trade_date,
        is_trading_day=is_trading,
        market="A",
        source="MOOTDX_HISTORICAL",
        status="OPEN" if is_trading else "CLOSED",
    )


@pytest_asyncio.fixture
async def snapshot_user(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
) -> AsyncGenerator[tuple[User, Instrument], None]:
    """创建已订阅用户 + 一只股票 + 自选记录。"""
    user = await user_factory(
        email=f"snap_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="fake-hash",
        timezone="Asia/Shanghai",
        roles=["member"],
    )
    instrument = await instrument_factory(
        symbol="600519", name="贵州茅台", market="SH", status="active"
    )
    await subscription_factory(
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="invite",
    )
    db_session.add(
        UserWatchlistItem(
            user_id=user.id,
            instrument_id=instrument.id,
            source="manual",
            active=True,
        )
    )
    await db_session.flush()

    async def _get_user() -> User:
        return user

    app.dependency_overrides[get_current_active_user] = _get_user
    yield user, instrument
    app.dependency_overrides.pop(get_current_active_user, None)


async def _create_watchlist_monitor_version(db_session: AsyncSession) -> StrategyVersion:
    """创建 watchlist_monitor 策略的 released 版本。"""
    definition = StrategyDefinition(
        strategy_key="watchlist_monitor",
        kind="monitor",
        display_name="自选监控",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={"outputs": []},
        build_hash="sha256_" + uuid.uuid4().hex,
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    return version


def _make_snapshot_record(instrument_id: uuid.UUID, trade_date) -> StockFeatureSnapshot:
    """构造一个 StockFeatureSnapshot 记录。"""
    return StockFeatureSnapshot(
        instrument_id=instrument_id,
        trade_date=trade_date,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        schema_version=1,
        source_primary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day,
            15, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        source_secondary_bar_time=datetime(
            trade_date.year, trade_date.month, trade_date.day,
            15, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        structural_payload={"primary": {"1d": {}}},
        temporal_payload={"derived_relation": {}},
        summary_payload={
            "_source": "feature_snapshot",
            "current_price": 1800.0,
            "change_pct": 1.5,
            "bb_upper": 1900.0,
            "bb_mid": 1800.0,
            "bb_lower": 1700.0,
            "poc_price": 1810.0,
        },
        degraded_reasons=[],
    )


def _make_succeeded_run(trade_date, run_type: str = "after_close") -> StockFeatureSnapshotRun:
    """构造一个 status='succeeded' 的 StockFeatureSnapshotRun 记录（已 published）。"""
    now = datetime.now(UTC)
    return StockFeatureSnapshotRun(
        trade_date=trade_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type=run_type,
        status="succeeded",
        expected_count=1,
        snapshot_count=1,
        failed_count=0,
        skipped_count=0,
        failure_rate=0.0,
        started_at=now,
        finished_at=now,
        published_at=now,
    )


def _make_running_run(trade_date, run_type: str = "after_close") -> StockFeatureSnapshotRun:
    """构造一个 status='running' 的 StockFeatureSnapshotRun 记录（未 published）。"""
    return StockFeatureSnapshotRun(
        trade_date=trade_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type=run_type,
        status="running",
        started_at=datetime.now(UTC),
    )


def _make_failed_run(trade_date, run_type: str = "after_close") -> StockFeatureSnapshotRun:
    """构造一个 status='failed' 的 StockFeatureSnapshotRun 记录（未 published）。"""
    now = datetime.now(UTC)
    return StockFeatureSnapshotRun(
        trade_date=trade_date,
        schema_version=1,
        primary_timeframe="1d",
        secondary_timeframe="15m",
        adj="qfq",
        run_type=run_type,
        status="failed",
        snapshot_count=5,
        failed_count=95,
        failure_rate=0.95,
        started_at=now,
        finished_at=now,
        # published_at 故意不设（failed 不发布）
    )


@pytest.mark.asyncio
async def test_monitor_status_uses_snapshot_when_present(
    db_session, snapshot_user, client
):
    """有 snapshot + succeeded run 时返回 _source='feature_snapshot'，不调用 MonitorSnapshotService。"""
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    # 插入今天的 snapshot + succeeded run（publish gate）
    from datetime import date
    today = date(2026, 7, 7)
    snapshot = _make_snapshot_record(instrument.id, today)
    db_session.add(snapshot)
    db_session.add(_make_succeeded_run(today))
    await db_session.flush()

    # mock 今天为交易日且已收盘
    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["instrument_id"] == str(instrument.id)
    # metrics 来自 snapshot
    assert item["metrics"]["_source"] == "feature_snapshot"
    assert item["metrics"]["current_price"] == 1800.0
    assert item["metrics"]["bb_upper"] == 1900.0
    # MonitorSnapshotService 已移除，metrics 必须来自 snapshot
    # （若意外调用会因 import 缺失抛 AttributeError，测试会失败）


@pytest.mark.asyncio
async def test_monitor_status_no_snapshot_after_close_returns_waiting(
    db_session, snapshot_user, client
):
    """无 snapshot 且收盘后返回 WAITING_SNAPSHOT。"""
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    # 无 snapshot 且收盘后 → WAITING_SNAPSHOT
    assert item["calculation_status"] == "WAITING_SNAPSHOT"
    # metrics 为空或仅含 _source
    assert item["metrics"] == {} or item["metrics"].get("_source") == "no_snapshot"


@pytest.mark.asyncio
async def test_monitor_status_no_snapshot_before_close_returns_no_snapshot(
    db_session, snapshot_user, client
):
    """无 snapshot 且未收盘返回 NO_SNAPSHOT。"""
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    # 无 snapshot 且未收盘 → NO_SNAPSHOT
    assert item["calculation_status"] == "NO_SNAPSHOT"


# ===== Blocker1: expected_snapshot_trade_date 三态规则 =====


@pytest.mark.asyncio
async def test_monitor_status_trading_day_before_close_reads_yesterday_snapshot(
    db_session, snapshot_user, client
):
    """交易日盘中（未收盘）应读取上一交易日 snapshot，返回 SUCCEEDED。

    规则：交易日未收盘 → expected_snapshot_trade_date = 上一个已完成交易日。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)  # 周二
    yesterday = date(2026, 7, 6)  # 周一

    # 插入 TradingCalendar：昨天和今天均为交易日
    db_session.add(_make_calendar_row(yesterday, is_trading=True))
    db_session.add(_make_calendar_row(today, is_trading=True))
    await db_session.flush()

    # 插入昨天的 snapshot（带特征 metrics）+ succeeded run（publish gate）
    snapshot = _make_snapshot_record(instrument.id, yesterday)
    snapshot.summary_payload["current_price"] = 12.34
    db_session.add(snapshot)
    db_session.add(_make_succeeded_run(yesterday))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # 盘中读取昨日 snapshot → SUCCEEDED
    assert item["calculation_status"] == "SUCCEEDED"
    # metrics 来自昨日 snapshot
    assert item["metrics"]["current_price"] == 12.34
    assert item["metrics"]["_source"] == "feature_snapshot"


@pytest.mark.asyncio
async def test_monitor_status_non_trading_day_reads_recent_trading_day_snapshot(
    db_session, snapshot_user, client
):
    """非交易日应读取最近一个交易日 snapshot，返回 SUCCEEDED。

    规则：非交易日 → expected_snapshot_trade_date = 最近一个交易日。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 11)  # 周六
    recent_trading_day = date(2026, 7, 10)  # 周五

    # 插入 TradingCalendar：周五为交易日
    db_session.add(_make_calendar_row(recent_trading_day, is_trading=True))
    await db_session.flush()

    # 插入周五的 snapshot + succeeded run（publish gate）
    snapshot = _make_snapshot_record(instrument.id, recent_trading_day)
    snapshot.summary_payload["current_price"] = 56.78
    db_session.add(snapshot)
    db_session.add(_make_succeeded_run(recent_trading_day))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # 非交易日读取最近交易日 snapshot → SUCCEEDED
    assert item["calculation_status"] == "SUCCEEDED"
    assert item["metrics"]["current_price"] == 56.78


@pytest.mark.asyncio
async def test_monitor_status_non_trading_day_no_history_returns_no_snapshot(
    db_session, snapshot_user, client
):
    """非交易日且无最近交易日 snapshot，返回 NO_SNAPSHOT。"""
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 11)  # 周六
    recent_trading_day = date(2026, 7, 10)  # 周五

    # 插入 TradingCalendar：周五为交易日（但无 snapshot）
    db_session.add(_make_calendar_row(recent_trading_day, is_trading=True))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=False,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 11, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # 非交易日无历史 snapshot → NO_SNAPSHOT（不是 WAITING_SNAPSHOT）
    assert item["calculation_status"] == "NO_SNAPSHOT"


@pytest.mark.asyncio
async def test_monitor_status_intraday_no_yesterday_snapshot_returns_no_snapshot(
    db_session, snapshot_user, client
):
    """交易日盘中（10:00），trading_calendar 存在上一交易日，但上一交易日无 snapshot。

    应返回 NO_SNAPSHOT（不是 WAITING_SNAPSHOT），metrics={}。

    防止盘中历史快照缺失被误报 WAITING_SNAPSHOT：
    WAITING_SNAPSHOT 仅用于"交易日已收盘但 snapshot 尚未生成"的场景。
    盘中读取昨日 snapshot 缺失时，应返回 NO_SNAPSHOT。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)  # 周二
    yesterday = date(2026, 7, 6)  # 周一

    # 插入 TradingCalendar：昨天和今天均为交易日（但无任何 snapshot）
    db_session.add(_make_calendar_row(yesterday, is_trading=True))
    db_session.add(_make_calendar_row(today, is_trading=True))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    # 盘中 + 上一交易日存在但无 snapshot → NO_SNAPSHOT（不是 WAITING_SNAPSHOT）
    assert item["calculation_status"] == "NO_SNAPSHOT", (
        f"盘中缺昨日 snapshot 应返回 NO_SNAPSHOT，实际: {item['calculation_status']}"
    )
    assert item["metrics"] == {}, (
        f"NO_SNAPSHOT 时 metrics 应为空 dict，实际: {item['metrics']}"
    )


# ===== Phase 5: Run Gate（publish gate）=====


@pytest.mark.asyncio
async def test_monitor_status_running_run_blocks_snapshot_read(
    db_session, snapshot_user, client
):
    """[RunGate] snapshot 存在但 run 仍 running → 不得返回 SUCCEEDED。

    publish gate 规则：watchlist 只读取 succeeded run 对应的 snapshot 行。
    running run 表示快照尚未完成，即使 snapshot 行已存在也不得被读取。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    # 插入 snapshot 但 run 仍 running（未 succeeded）
    snapshot = _make_snapshot_record(instrument.id, today)
    db_session.add(snapshot)
    db_session.add(_make_running_run(today))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # running run → 不得返回 SUCCEEDED，应为 WAITING_SNAPSHOT（收盘后）
    assert item["calculation_status"] != "SUCCEEDED", (
        f"running run 时不得返回 SUCCEEDED，实际: {item['calculation_status']}"
    )
    assert item["calculation_status"] == "WAITING_SNAPSHOT", (
        f"收盘后 running run 应返回 WAITING_SNAPSHOT，实际: {item['calculation_status']}"
    )
    # metrics 应为空（不读取未 published 的 snapshot）
    assert item["metrics"] == {}


@pytest.mark.asyncio
async def test_monitor_status_failed_run_blocks_snapshot_read(
    db_session, snapshot_user, client
):
    """[RunGate] snapshot 存在但 run failed → 不得返回 SUCCEEDED。

    failed run 表示快照计算失败（半成品），即使 snapshot 行已存在也不得被读取。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    # 插入 snapshot 但 run failed（未 published）
    snapshot = _make_snapshot_record(instrument.id, today)
    db_session.add(snapshot)
    db_session.add(_make_failed_run(today))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # failed run → 不得返回 SUCCEEDED
    assert item["calculation_status"] != "SUCCEEDED", (
        f"failed run 时不得返回 SUCCEEDED，实际: {item['calculation_status']}"
    )
    assert item["metrics"] == {}


@pytest.mark.asyncio
async def test_monitor_status_succeeded_run_allows_snapshot_read(
    db_session, snapshot_user, client
):
    """[RunGate] snapshot + succeeded run → 返回 SUCCEEDED + metrics。

    succeeded run（published_at 非空）表示快照计算完成并已发布，
    watchlist 可读取对应 snapshot 行。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    # 插入 snapshot + succeeded run
    snapshot = _make_snapshot_record(instrument.id, today)
    snapshot.summary_payload["current_price"] = 99.88
    db_session.add(snapshot)
    db_session.add(_make_succeeded_run(today))
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # succeeded run → SUCCEEDED + metrics
    assert item["calculation_status"] == "SUCCEEDED"
    assert item["metrics"]["current_price"] == 99.88
    assert item["metrics"]["_source"] == "feature_snapshot"


@pytest.mark.asyncio
async def test_monitor_status_no_run_blocks_snapshot_read(
    db_session, snapshot_user, client
):
    """[RunGate] snapshot 存在但无任何 run 记录 → 不得返回 SUCCEEDED。

    无 run 记录表示快照从未经过 publish gate 校验，
    即使 snapshot 行存在也不得被读取（可能是 smoke test 残留数据）。
    """
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    from datetime import date
    today = date(2026, 7, 7)

    # 插入 snapshot 但不插入任何 run 记录
    snapshot = _make_snapshot_record(instrument.id, today)
    db_session.add(snapshot)
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.shanghai_business_date",
        return_value=today,
    ), patch(
        "app.api.watchlist.now_shanghai",
        return_value=datetime(2026, 7, 7, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    item = data["items"][0]
    # 无 run → 不得返回 SUCCEEDED
    assert item["calculation_status"] != "SUCCEEDED", (
        f"无 run 记录时不得返回 SUCCEEDED，实际: {item['calculation_status']}"
    )
    assert item["metrics"] == {}
