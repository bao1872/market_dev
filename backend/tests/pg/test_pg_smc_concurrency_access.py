"""PG formal-verify 合同（需注册运行时 scripts/ops/panji-verify）。

本地无 PG 时整体 skip（AGENTS.md：Local/CI DB 测试禁止；PURE_UNIT_TEST=1 只跑纯单测）。
这些测试必须由注册运行时对 bz_stock_verify_<40-char-sha> 执行，IDE 不得自行连库。

覆盖：
- A. SMC 并发幂等（真实两事务竞争）：两 worker 同时写同一稳定 BOS/CHoCH event_key →
     StrategyEvent=1 且 Outbox=1（败者 write_event 返回 None → 复现生产 :907 skip → 不 enqueue）。
- B. 权限 legacy fallback（无 capability 行 + active legacy plan）→ 推断 capabilities。
- C. 权限 capability-wins（capability 行存在 + 更广 legacy plan）→ 有效权限 = 行，legacy 不可扩权。
- D. 权限 expired-rows（capability 行全过期 + active 更广 legacy plan）→ 不得 legacy fallback、
     不得被 legacy plan 重新扩权、受保护新操作 denied。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.outbox import Outbox
from app.models.strategy_event import StrategyEvent
from app.models.subscription import Subscription
from app.models.user import User
from app.models.user_capability import UserCapability
from app.repositories.strategy_event_repository import write_event
from app.services.effective_access_service import resolve_effective_access
from app.services.monitor_crossing_service import evaluate_smc_events
from app.services.outbox_relay import write_outbox
from app.services.smc_monitor_target_service import SmcMonitorTargetSet, SmcStructureTarget
from app.strategy.monitors.smc_monitor import SMC_BOS_CROSS, SMC_CHOCH_CROSS
from app.strategy.runtime import StrategyEventDraft

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
    user = User(email=email, password_hash="x", status="active")
    session.add(user)
    await session.flush()
    user._roles = ["member"]  # resolve_effective_access 经 _get_user_roles 读取
    return user


# ---------------------------------------------------------------------------
# A. SMC 并发幂等（真实两事务竞争）→ StrategyEvent=1 且 Outbox=1
# ---------------------------------------------------------------------------

async def _concurrent_struct_event_once(
    engine,
    kind: str,
    bias: int,
    price_last: float,
    price_curr: float,
    expected_event_type: str,
) -> None:
    """两真实并发 PG 事务竞争同一稳定 structure event_key。

    复现生产链路分支（monitor_batch_service._process_instrument_evaluation :890 write_event
    → :907 ``if event_orm is None: continue`` → _send_merged_notification :1633 write_outbox）：
    败者 write_event 返回 None 后立即 skip，绝不再 enqueue Outbox。用真实 evaluate_smc_events
    推导稳定 key + 真实 write_event + 真实 write_outbox，在 barrier 同步下两个事务同时 INSERT。
    """
    inst_id = uuid.uuid4()
    strategy_version_id = uuid.uuid4()
    anchor = "2026-09-01"
    now = datetime.now(timezone.utc)
    target = SmcStructureTarget(
        target_id="t", lane="swing", kind=kind, level=10.0, anchor_index=10, anchor_time=anchor
    )
    set_ = SmcMonitorTargetSet(
        contract_identity={}, input_identity={},
        structure_context={"swing_bias": bias, "internal_bias": bias, "slots": {}},
        active_structure_targets=(target,), active_order_block_targets=(), target_set_version="v",
    )
    drafts = evaluate_smc_events(inst_id, set_, price_last, price_curr, now, set(), set())
    assert len(drafts) == 1
    key = drafts[0].dedupe_key
    assert drafts[0].event_type == expected_event_type

    barrier = asyncio.Event()

    async def worker(sess: AsyncSession) -> bool:
        draft = StrategyEventDraft(
            event_type=expected_event_type, event_time=now, dedupe_key=key,
            logical_entity=key, payload={"event_key": key}, state_ttl_seconds=600,
        )
        # 同步两 worker 到同一 INSERT 点（DB UNIQUE 才是真正仲裁者）
        barrier.set()
        await barrier.wait()
        event_orm = await write_event(
            sess, event_key=key, strategy_version_id=strategy_version_id,
            instrument_id=inst_id, event_type=expected_event_type, event_time=now,
            payload=draft.payload, logical_entity_id=key,
        )
        outbox = None
        if event_orm is not None:  # 生产 :907 分支 —— 败者（write_event=None）不 enqueue
            outbox = await write_outbox(
                sess, "notification.message.created", {"event_key": key},
                "strategy_event", aggregate_id=event_orm.id,
            )
        await sess.commit()
        return event_orm is not None

    async with AsyncSession(engine) as sa, AsyncSession(engine) as sb:
        r1, r2 = await asyncio.gather(worker(sa), worker(sb))
    # 恰好一个胜出（UNIQUE + ON CONFLICT DO NOTHING）
    assert (r1 and not r2) or (r2 and not r1), "exactly one worker must win the UNIQUE race"

    async with AsyncSession(engine) as s:
        ev = (
            await s.execute(
                select(func.count()).select_from(StrategyEvent).where(StrategyEvent.event_key == key)
            )
        ).scalar_one()
        ob = (
            await s.execute(
                select(func.count()).select_from(Outbox).where(Outbox.event_type == "notification.message.created")
            )
        ).scalar_one()
    assert ev == 1, "StrategyEvent rows must be 1"
    assert ob == 1, "Outbox rows must be 1 (loser did NOT enqueue a second notification)"


async def test_pg_smc_concurrent_bos_event_and_outbox_once(engine) -> None:
    """两 worker 并发写同一 bullish BOS → StrategyEvent=1 且 Outbox=1。"""
    await _concurrent_struct_event_once(engine, "high", 1, 9.0, 11.0, SMC_BOS_CROSS)


async def test_pg_smc_concurrent_choch_event_and_outbox_once(engine) -> None:
    """两 worker 并发写同一 bearish CHoCH → StrategyEvent=1 且 Outbox=1。"""
    await _concurrent_struct_event_once(engine, "high", -1, 11.0, 9.0, SMC_CHOCH_CROSS)


# ---------------------------------------------------------------------------
# B. 权限 legacy fallback（无 capability 行 + active legacy plan）
# ---------------------------------------------------------------------------

async def test_pg_access_legacy_fallback_no_capability_rows(session: AsyncSession) -> None:
    """历史账户无 UserCapability 行 + active legacy plan(research_50) → 推断 capabilities。"""
    user = await _make_member(session, f"legacy_{uuid.uuid4().hex}@example.com")
    session.add(
        Subscription(
            user_id=user.id, plan_code="research_50", status="active",
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            source="legacy_materialized", entitlement_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    await session.commit()

    profile = await resolve_effective_access(session, user)
    assert profile.capabilities["market_data"].active is True
    assert profile.capabilities["research_replay"].active is True
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
            user_id=user.id, plan_code="research_50", status="active",
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            source="legacy_materialized", entitlement_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    now = datetime.now(timezone.utc)
    session.add(
        UserCapability(user_id=user.id, capability="self_selection", granted_at=now - timedelta(days=10),
                       expires_at=now + timedelta(days=300), source="admin_grant", created_at=now)
    )
    session.add(
        UserCapability(user_id=user.id, capability="market_data", granted_at=now - timedelta(days=10),
                       expires_at=now + timedelta(days=300), source="admin_grant", created_at=now)
    )
    session.add(
        UserCapability(user_id=user.id, capability="research_replay", granted_at=now - timedelta(days=400),
                       expires_at=now - timedelta(days=1), source="admin_grant",
                       created_at=now - timedelta(days=400))
    )
    await session.commit()

    profile = await resolve_effective_access(session, user)
    assert profile.capabilities["market_data"].active is True
    assert profile.capabilities["market_data"].source == "user_capabilities"
    assert "research_replay" not in profile.capabilities or profile.capabilities["research_replay"].active is False
    if "research_replay" in profile.capabilities:
        assert profile.capabilities["research_replay"].active is False
    assert "legacy_plan_fallback" not in profile.diagnostics


# ---------------------------------------------------------------------------
# D. 权限 expired-rows（capability 行全过期 + active 更广 legacy plan）
# ---------------------------------------------------------------------------

async def test_pg_access_expired_rows_no_legacy_fallback(session: AsyncSession) -> None:
    """UserCapability 行 EXIST 但全过期 + active legacy plan(research_50) → 不得 legacy fallback、
    不得被 legacy plan 重新扩权；受保护新操作(research_replay) denied。

    关键区别：fallback 仅在「无任何 capability 行」时触发；不是「无 currently-active 行」就 fallback。
    """
    user = await _make_member(session, f"expired_{uuid.uuid4().hex}@example.com")
    session.add(
        Subscription(
            user_id=user.id, plan_code="research_50", status="active",
            starts_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
            source="legacy_materialized", entitlement_snapshot={},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )
    now = datetime.now(timezone.utc)
    # 全部过期
    session.add(
        UserCapability(user_id=user.id, capability="market_data", granted_at=now - timedelta(days=400),
                       expires_at=now - timedelta(days=1), source="admin_grant",
                       created_at=now - timedelta(days=400))
    )
    session.add(
        UserCapability(user_id=user.id, capability="research_replay", granted_at=now - timedelta(days=400),
                       expires_at=now - timedelta(days=1), source="admin_grant",
                       created_at=now - timedelta(days=400))
    )
    await session.commit()

    profile = await resolve_effective_access(session, user)
    # 过期行 present → 解析为 active=False（不被 legacy plan 复活）
    assert profile.capabilities["market_data"].active is False
    assert profile.capabilities["market_data"].source == "user_capabilities"
    assert profile.capabilities["research_replay"].active is False
    # 不得 legacy fallback（fallback 仅当「无任何 capability 行」）
    assert "legacy_plan_fallback" not in profile.diagnostics
    # 受保护新操作 denied
    assert "research_replay" not in profile.capabilities or profile.capabilities["research_replay"].active is False
