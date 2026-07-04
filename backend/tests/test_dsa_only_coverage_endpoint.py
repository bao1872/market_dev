"""dsa-only 端点覆盖率 fallback 对齐测试。

覆盖：
- 请求今日但 bars_daily 当日无数据时，fallback 到最新可用交易日
- 请求今日且当日有数据时，不 fallback
- fallback 后的交易日覆盖率不足时，返回 409 并透传 fallback 日期

测试策略：
- 使用 conftest client fixture + admin 用户认证
- 通过 patch shanghai_business_date 固定今日，避免服务器时区差异
- 为全部 active instruments 写入 bars_daily，确保覆盖率可控
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.models.bar import BarDaily
from app.models.instrument import Instrument
from app.models.scheduler_job_run import SchedulerJobRun
from app.models.user import Role, User, UserRole

TODAY = date(2026, 6, 25)
YESTERDAY = date(2026, 6, 24)


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """创建 admin 用户 + admin 角色以满足 require_roles("admin")。"""
    from sqlalchemy import select as sa_select

    role_stmt = sa_select(Role).where(Role.name == "admin")
    role_result = await db_session.execute(role_stmt)
    admin_role = role_result.scalar_one_or_none()
    if admin_role is None:
        admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
        db_session.add(admin_role)

    user = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
        timezone="Asia/Shanghai",
    )
    db_session.add(user)
    db_session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    await db_session.flush()
    return user


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _create_active_instruments(
    db_session: AsyncSession, count: int
) -> list[Instrument]:
    """创建 count 个活跃 A 股标的（SH 主板）。"""
    instruments = []
    for i in range(count):
        inst = Instrument(
            id=uuid.uuid4(),
            symbol=f"{600000 + i:06d}",
            name=f"测试标的{i}",
            market="SH",
            status="active",
        )
        db_session.add(inst)
        instruments.append(inst)
    await db_session.flush()
    return instruments


async def _insert_daily_bars_for_instruments(
    db_session: AsyncSession,
    instruments: list[Instrument],
    trade_date: date,
) -> None:
    """为给定标的列表插入当日 bars_daily。"""
    for inst in instruments:
        db_session.add(
            BarDaily(
                instrument_id=inst.id,
                trade_date=trade_date,
                open=Decimal("10.0"),
                high=Decimal("10.5"),
                low=Decimal("9.8"),
                close=Decimal("10.2"),
                volume=Decimal("100000"),
                amount=Decimal("1000000"),
                adj_factor=Decimal("1.0"),
            )
        )
    await db_session.flush()


async def _insert_daily_bars_for_all_active(
    db_session: AsyncSession, trade_date: date
) -> None:
    """为所有 active instruments 插入当日 bars_daily（保证覆盖率 100%）。"""
    result = await db_session.execute(
        select(Instrument).where(Instrument.status == "active")
    )
    all_active = list(result.scalars().all())
    await _insert_daily_bars_for_instruments(db_session, all_active, trade_date)


@pytest.mark.asyncio
async def test_dsa_only_fallback_to_latest_trade_date(db_session, admin_user, client):
    """今日无日线数据时，dsa-only 应 fallback 到最新可用交易日并创建任务。"""

    # 创建 10 个活跃 A 股标的并写入昨日日线
    instruments = await _create_active_instruments(db_session, count=10)
    await _insert_daily_bars_for_instruments(db_session, instruments, YESTERDAY)

    with patch(
        "app.core.time.shanghai_business_date", return_value=TODAY
    ), patch(
        "app.api.admin_after_close.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = await client.post(
            "/admin/after-close-runs/dsa-only",
            headers=_auth_headers(admin_user.id),
            json={"trade_date": TODAY.isoformat()},
        )

    assert response.status_code == 201, f"响应体: {response.text}"
    data = response.json()
    assert data["trade_date"] == YESTERDAY.isoformat()
    assert data["status"] == "queued"

    job_run = await db_session.get(SchedulerJobRun, uuid.UUID(data["job_run_id"]))
    assert job_run is not None
    meta = json.loads(job_run.metadata_json)
    assert meta["trade_date"] == YESTERDAY.isoformat()
    assert meta["mode"] == "dsa_only"
    assert meta["last_completed_step"] == "daily_ready"


@pytest.mark.asyncio
async def test_dsa_only_no_fallback_when_today_has_bars(db_session, admin_user, client):
    """今日有日线数据时，dsa-only 不应 fallback。"""

    # 创建 10 个活跃 A 股标的并写入今日日线
    instruments = await _create_active_instruments(db_session, count=10)
    await _insert_daily_bars_for_instruments(db_session, instruments, TODAY)

    with patch(
        "app.core.time.shanghai_business_date", return_value=TODAY
    ), patch(
        "app.api.admin_after_close.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = await client.post(
            "/admin/after-close-runs/dsa-only",
            headers=_auth_headers(admin_user.id),
            json={"trade_date": TODAY.isoformat()},
        )

    assert response.status_code == 201, f"响应体: {response.text}"
    data = response.json()
    assert data["trade_date"] == TODAY.isoformat()


@pytest.mark.asyncio
async def test_dsa_only_fallback_date_insufficient_coverage_returns_409(
    db_session, admin_user, client
):
    """fallback 到最新可用日后覆盖率仍不足，应返回 409 并透传 fallback 日期。"""

    # 创建 10 个活跃股票，但只在昨日为其中 5 个写入日线
    instruments = await _create_active_instruments(db_session, count=10)
    await _insert_daily_bars_for_instruments(db_session, instruments[:5], YESTERDAY)

    with patch(
        "app.core.time.shanghai_business_date", return_value=TODAY
    ), patch(
        "app.api.admin_after_close.is_trading_day_async",
        new_callable=AsyncMock,
        return_value=True,
    ):
        response = await client.post(
            "/admin/after-close-runs/dsa-only",
            headers=_auth_headers(admin_user.id),
            json={"trade_date": TODAY.isoformat()},
        )

    assert response.status_code == 409, f"响应体: {response.text}"
    detail = response.json()["detail"]
    assert detail["reason"] == "DATA_COVERAGE_INSUFFICIENT"
    assert detail["trade_date"] == YESTERDAY.isoformat()
    assert detail["requested_trade_date"] == TODAY.isoformat()
    assert detail["daily_coverage"] < 0.9
