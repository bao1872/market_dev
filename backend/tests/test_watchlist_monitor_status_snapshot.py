"""/watchlist/monitor-status 读取 feature snapshot 测试。

覆盖：
- 有 snapshot 时返回 _source='feature_snapshot'，不调用 MonitorSnapshotService
- 无 snapshot 且收盘后返回 WAITING_SNAPSHOT
- 无 snapshot 且未收盘/非交易日返回 NO_SNAPSHOT

测试策略：
- 使用 conftest client fixture + 认证用户覆盖
- mock is_trading_day_async 固定交易日
- 插入 StockFeatureSnapshot 记录验证读取
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
from app.models.instrument import Instrument
from app.models.stock_feature_snapshot import StockFeatureSnapshot
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
from app.models.watchlist import UserWatchlistItem


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


@pytest.mark.asyncio
async def test_monitor_status_uses_snapshot_when_present(
    db_session, snapshot_user, client
):
    """有 snapshot 时返回 _source='feature_snapshot'，不调用 MonitorSnapshotService。"""
    user, instrument = snapshot_user
    _version = await _create_watchlist_monitor_version(db_session)

    # 插入今天的 snapshot
    from datetime import date
    today = date(2026, 7, 7)
    snapshot = _make_snapshot_record(instrument.id, today)
    db_session.add(snapshot)
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
