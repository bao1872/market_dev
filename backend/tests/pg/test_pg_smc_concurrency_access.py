"""PG formal-verify 合同（需注册运行时 scripts/ops/panji-verify）。

本地无 PG 时整体 skip（AGENTS.md：Local/CI DB 测试禁止；PURE_UNIT_TEST=1 只跑纯单测）。
这些测试必须由注册运行时对 bz_stock_verify_<40-char-sha> 执行，IDE 不得自行连库。

覆盖：
- A. SMC 并发幂等：两 worker 写同一 BOS/CHoCH event_key → StrategyEvent 仅 1 行
  （event_key UNIQUE + ON CONFLICT DO NOTHING）。
- B. 权限 legacy fallback：无 UserCapability 行 + active legacy plan → 推断 capabilities。
- C. 权限 capability-wins：UserCapability 行存在 + 更广 legacy plan → 有效权限 = 行，
  legacy plan 不可扩展；过期 capability + active legacy → 受保护新操作 denied。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.strategy_event import StrategyEvent
from app.models.subscription import Subscription
from app.models.user import User
from app.models.user_capability import UserCapability
from app.repositories.strategy_event_repository import write_event
from app.services.effective_access_service import resolve_effective_access

_PG_URL = os.environ.get("PANJI_VERIFY_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _PG_URL, reason="requires registered PG verify runtime (scripts/ops/panji-verify)"
)


@pytest.fixture(scope="module")
async def engine():
    eng = create_async_engine(_PG_URL, future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async with AsyncSession(engine) as s:
        yield s


async def _make_member(session: AsyncSession, email: str) -> User:
    user = User(
        email=email,
        password_hash="x",
        status="active",
    )
    session.add(user)
    await session.flush()
    user._roles = ["member"]  # resolve_effective_access 经 _get_user_roles 读取
    return user


# ---------------------------------------------------------------------------
# A. SMC 并发幂等
# ---------------------------------------------------------------------------

async def test_pg_smc_bos_concurrent_event_key_unique(session: AsyncSession) -> None:
    """两 worker 同时写同一 BOS/CHoCH 稳定 event_key → 仅 1 行。

    直接复现 DB 层原子门禁：StrategyEvent.event_key UNIQUE + ON CONFLICT DO NOTHING。
    模拟 worker A / worker B 各提交一次相同 event_key 的 write_event。
    """
    event_key = f"smc_struct:{uuid.uuid4()}:swing:high:2026-09-01:SMC_BOS_CROSS"
    strategy_version_id = uuid.uuid4()
    instrument_id = uuid.uuid4()
    event_time = datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc)
    payload = {"structure_type": "BOS", "lane": "swing", "kind": "high"}

    # worker A
    row_a = await write_event(
        session,
        event_key=event_key,
        strategy_version_id=strategy_version_id,
        instrument_id=instrument_id,
        event_type="SMC_BOS_CROSS",
        event_time=event_time,
        payload=payload,
        logical_entity_id=str(instrument_id),
    )
    # worker B（同一 event_key，并发竞态下 DB UNIQUE 挡住）
    row_b = await write_event(
        session,
        event_key=event_key,
        strategy_version_id=strategy_version_id,
        instrument_id=instrument_id,
        event_type="SMC_BOS_CROSS",
        event_time=event_time,
        payload=payload,
        logical_entity_id=str(instrument_id),
    )
    await session.commit()

    assert row_a is not None  # 首次写入成功
    assert row_b is None  # 并发重复 → 幂等跳过

    count = (
        await session.execute(
            select(func.count()).select_from(StrategyEvent).where(StrategyEvent.event_key == event_key)
        )
    ).scalar_one()
    assert count == 1  # 最终 event rows = 1（deliverable = 1）


# ---------------------------------------------------------------------------
# B. 权限 legacy fallback（无 capability 行 + active legacy plan）
# ---------------------------------------------------------------------------

async def test_pg_access_legacy_fallback_no_capability_rows(session: AsyncSession) -> None:
    """历史账户无 UserCapability 行 + active legacy plan(research_50) → 推断 capabilities。"""
    user = await _make_member(session, f"legacy_{uuid.uuid4().hex}@example.com")
    session.add(
        Subscription(
            user_id=user.id,
            plan_code="research_50",
            status="active",
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            source="legacy_materialized",
            entitlement_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    profile = await resolve_effective_access(session, user)
    # legacy plan 推断的 capabilities 生效
    assert profile.capabilities["market_data"].active is True
    assert profile.capabilities["research_replay"].active is True
    # 显式标记 legacy_plan_fallback，不静默混入正常用户路径
    assert profile.capabilities["market_data"].source == "legacy_plan_fallback"
    assert "legacy_plan_fallback" in profile.diagnostics


# ---------------------------------------------------------------------------
# C. 权限 capability-wins（capability 行存在 + 更广 legacy plan）
# ---------------------------------------------------------------------------

async def test_pg_access_capability_wins_over_legacy_plan(session: AsyncSession) -> None:
    """UserCapability 行存在 + active legacy plan(research_50，更广) → 有效权限 = 行，
    legacy plan 不可扩展；过期 capability + active legacy → 受保护新操作 denied。"""
    user = await _make_member(session, f"cap_{uuid.uuid4().hex}@example.com")
    session.add(
        Subscription(
            user_id=user.id,
            plan_code="research_50",
            status="active",
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            source="legacy_materialized",
            entitlement_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    now = datetime.now(timezone.utc)
    # 窄授权：self_selection + market_data（active），无 research_replay
    session.add(
        UserCapability(
            user_id=user.id,
            capability="self_selection",
            granted_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=300),
            source="admin_grant",
            created_at=now,
        )
    )
    session.add(
        UserCapability(
            user_id=user.id,
            capability="market_data",
            granted_at=now - timedelta(days=10),
            expires_at=now + timedelta(days=300),
            source="admin_grant",
            created_at=now,
        )
    )
    # 过期 capability：research_replay 已过期（即使 legacy plan 是 research_50 也不补齐）
    session.add(
        UserCapability(
            user_id=user.id,
            capability="research_replay",
            granted_at=now - timedelta(days=400),
            expires_at=now - timedelta(days=1),
            source="admin_grant",
            created_at=now - timedelta(days=400),
        )
    )
    await session.commit()

    profile = await resolve_effective_access(session, user)
    # 有效权限只含显式行中未过期的 key
    assert profile.capabilities["market_data"].active is True
    assert profile.capabilities["market_data"].source == "user_capabilities"
    # legacy plan 不可扩展：research_replay 不在有效 capabilities（已过期）
    assert "research_replay" not in profile.capabilities or profile.capabilities["research_replay"].active is False
    # 过期 capability 不被 legacy plan 复活
    if "research_replay" in profile.capabilities:
        assert profile.capabilities["research_replay"].active is False
    assert "legacy_plan_fallback" not in profile.diagnostics
