"""自选股监控列表 fallback 测试。

覆盖：
- 当 MonitorState 不存在或 payload 无效时，monitor-status 端点通过 MonitorSnapshotService 只读计算 fallback 指标
- fallback 失败时单行降级（error_code=FALLBACK_FAILED），不阻断整个自选列表
- 已有有效 MonitorState 时仍优先使用 MonitorState.payload
- 多只自选中单只 fallback 失败不影响其他行

测试策略：
- 使用 conftest client fixture + 认证用户覆盖
- mock is_trading_day_async 固定交易日
- mock MonitorSnapshotService.get_snapshot 返回可控快照或异常
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.main import app
from app.models.instrument import Instrument
from app.models.monitor_state import MonitorState
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.user import User
from app.models.watchlist import UserWatchlistItem
from app.services.monitor_snapshot_service import MonitorSnapshot


@pytest_asyncio.fixture
async def monitor_user(
    db_session: AsyncSession,
    user_factory,
    subscription_factory,
    instrument_factory,
) -> AsyncGenerator[tuple[User, Instrument], None]:
    """创建已订阅用户 + 一只股票 + 自选记录。"""
    user = await user_factory(
        email=f"monitor_{uuid.uuid4().hex[:8]}@test.com",
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


def _make_snapshot(instrument_id: uuid.UUID) -> MonitorSnapshot:
    """构造一个用于 fallback 的 MonitorSnapshot。"""
    return MonitorSnapshot(
        instrument_id=str(instrument_id),
        symbol="600519",
        name="贵州茅台",
        as_of=datetime.now(UTC),
        current_price=1500.0,
        range_upper=1600.0,
        range_center=1500.0,
        range_lower=1400.0,
        upper_volume_zone=1580.0,
        lower_volume_zone=1420.0,
        most_traded_price=1510.0,
        range_position=0.55,
        previous_close=1490.0,
        change_pct=0.67,
    )


@pytest.mark.asyncio
async def test_monitor_status_fallback_when_no_monitor_state(
    db_session, monitor_user, client
):
    """无 MonitorState 时，通过 MonitorSnapshotService fallback 返回指标。"""
    user, instrument = monitor_user
    _version = await _create_watchlist_monitor_version(db_session)
    snapshot = _make_snapshot(instrument.id)

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.MonitorSnapshotService.get_snapshot",
        new_callable=AsyncMock,
        return_value=snapshot,
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["instrument_id"] == str(instrument.id)
    # fallback 来源标记
    assert item["metrics"]["_source"] == "fallback_snapshot"
    # 关键指标字段来自快照
    assert item["metrics"]["current_price"] == 1500.0
    assert item["metrics"]["bb_upper"] == 1600.0
    assert item["metrics"]["bb_mid"] == 1500.0
    assert item["metrics"]["bb_lower"] == 1400.0
    assert item["metrics"]["upper_node_price"] == 1580.0
    assert item["metrics"]["lower_node_price"] == 1420.0
    assert item["metrics"]["poc_price"] == 1510.0
    assert item["metrics"]["position_0_1"] == 0.55
    assert item["metrics"]["previous_close"] == 1490.0
    assert item["metrics"]["change_pct"] == 0.67
    # 无 MonitorState 时 freshness_seconds 应为 None
    assert item["freshness_seconds"] is None


@pytest.mark.asyncio
async def test_monitor_status_fallback_failure_single_row_degraded(
    db_session, monitor_user, client
):
    """fallback 计算失败时单行降级，整体列表仍返回 200。"""
    user, instrument = monitor_user
    _version = await _create_watchlist_monitor_version(db_session)

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.MonitorSnapshotService.get_snapshot",
        new_callable=AsyncMock,
        side_effect=RuntimeError("指标计算失败"),
    ):
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    data = response.json()
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["instrument_id"] == str(instrument.id)
    assert item["metrics"] == {}
    assert item["error_code"] == "FALLBACK_FAILED"


@pytest.mark.asyncio
async def test_monitor_status_uses_monitor_state_when_present(
    db_session, monitor_user, client
):
    """存在 MonitorState 时优先使用其 payload，不走 fallback。"""
    user, instrument = monitor_user
    version = await _create_watchlist_monitor_version(db_session)

    db_session.add(
        MonitorState(
            strategy_version_id=version.id,
            instrument_id=instrument.id,
            bar_time=datetime.now(UTC),
            calculation_id="calc_test_001",
            state_schema_version=1,
            payload={
                "current_price": 1800.0,
                "bb_upper": 1900.0,
                "bb_mid": 1800.0,
                "bb_lower": 1700.0,
                "upper_node": {"price_mid": 1850.0},
                "lower_node": {"price_mid": 1750.0},
                "poc_price": 1810.0,
                "position_0_1": 0.75,
            },
        )
    )
    await db_session.flush()

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ):
        # 本次不应调用 MonitorSnapshotService
        with patch(
            "app.api.watchlist.MonitorSnapshotService.get_snapshot",
            new_callable=AsyncMock,
        ) as mock_snapshot:
            response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    mock_snapshot.assert_not_awaited()
    item = response.json()["items"][0]
    assert item["metrics"]["current_price"] == 1800.0
    assert item["metrics"]["bb_upper"] == 1900.0
    assert "_source" not in item["metrics"]


@pytest.mark.asyncio
async def test_monitor_status_fallback_when_payload_empty(
    db_session, monitor_user, client
):
    """MonitorState 存在但 payload 为空/关键字段缺失时，应触发 fallback。"""
    user, instrument = monitor_user
    version = await _create_watchlist_monitor_version(db_session)

    db_session.add(
        MonitorState(
            strategy_version_id=version.id,
            instrument_id=instrument.id,
            bar_time=datetime.now(UTC),
            calculation_id="calc_test_002",
            state_schema_version=1,
            payload={},
        )
    )
    await db_session.flush()

    snapshot = _make_snapshot(instrument.id)

    with patch(
        "app.api.watchlist.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ), patch(
        "app.api.watchlist.MonitorSnapshotService.get_snapshot",
        new_callable=AsyncMock,
        return_value=snapshot,
    ) as mock_snapshot:
        response = await client.get("/watchlist/monitor-status")

    assert response.status_code == 200, f"响应体: {response.text}"
    mock_snapshot.assert_awaited_once()
    item = response.json()["items"][0]
    assert item["metrics"]["_source"] == "fallback_snapshot"
    assert item["metrics"]["current_price"] == 1500.0
