"""PG formal-verify 合同（需注册运行时 scripts/ops/panji-verify）。

本地无 PG 时整体 skip（AGENTS.md：Local/CI DB 测试禁止；PURE_UNIT_TEST=1 只跑纯单测）。
这些测试必须由注册运行时对 bz_stock_verify_<40-char-sha> 执行，IDE 不得自行连库。

[fixture 生产代表性] 本文件 session 必须与生产 ``app/db.py::AsyncSessionLocal`` 同配置
（``expire_on_commit=False`` / ``autoflush=False``）：默认 ``AsyncSession(engine)`` 会在
commit 后 expire ORM 属性，异步上下文再访问 ``user.id`` 会触发 ``MissingGreenlet``。

覆盖：
- A. SMC 并发幂等（真实两事务竞争，``asyncio.Barrier(2)`` rendezvous）：两 worker 同时写
     **同一 canonical structure event_key** → StrategyEvent=1 且该事件的 Outbox=1
     （败者 write_event 返回 None → 复现生产 skip → 不 enqueue）。
     canonical identity owner = ``smc_realtime_transition_service.structure_transition_key``，
     dedupe_key = ``"smc_struct_transition:" + transition_key``（与 ``evaluate_realtime_smc_events``
     完全一致）。事件生成的语义（BOS/CHoCH 判定）已由 pure unit
     ``test_smc_realtime_event_service.py`` 锁定；本 PG 合同只验证真实 UNIQUE 事务仲裁 + Outbox exactly-once。
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
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_event import StrategyEvent
from app.models.subscription import Subscription
from app.models.user import User
from app.models.user_capability import UserCapability
from app.repositories.strategy_event_repository import write_event
from app.services.effective_access_service import resolve_effective_access
from app.services.outbox_relay import write_outbox
from app.services.smc_realtime_event_service import SMC_BOS_CROSS, SMC_CHOCH_CROSS
from app.services.smc_realtime_transition_service import structure_transition_key
from app.strategy.runtime import StrategyEventDraft

_PG_URL = os.environ.get("PANJI_VERIFY_DATABASE_URL") or os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _PG_URL, reason="requires registered PG verify runtime (scripts/ops/panji-verify)"
)

_NOTIFICATION_EVENT_TYPE = "notification.message.created"


@pytest.fixture(scope="module")
async def engine():
    eng = create_async_engine(_PG_URL, future=True)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    # [fixture 生产代表性] 与生产 AsyncSessionLocal 同配置（app/db.py:42-47）。
    async with AsyncSession(engine, expire_on_commit=False, autoflush=False) as s:
        yield s


def _new_session(engine) -> AsyncSession:
    """生产等价的独立 session（并发 worker / 独立 seed 用）。"""
    return AsyncSession(engine, expire_on_commit=False, autoflush=False)


async def _seed_strategy_version(engine, *, suffix: str) -> uuid.UUID:
    """seed 合法 StrategyDefinition + StrategyVersion 并返回 **scalar UUID**。

    ``StrategyEvent.strategy_version_id`` 有真实 FK → ``strategy_versions.id``；
    用随机 uuid 会违反 FK，属非法 PG fixture。这里用独立 session 建立合法父行后再返回
    scalar UUID（跨 session 只传 UUID，不传 ORM 对象）。
    """
    async with _new_session(engine) as s:
        definition = StrategyDefinition(
            strategy_key=f"verify_smc_conc_{suffix}",
            kind="monitor",
            display_name="verify",
        )
        s.add(definition)
        await s.flush()
        definition_id = definition.id
        assert definition_id is not None, "StrategyDefinition.id 必须由 flush 取回"

        version = StrategyVersion(
            strategy_definition_id=definition_id,
            version=f"verify-{suffix}",
            status="draft",
            manifest={},
            build_hash=f"verify-build-{suffix}",
        )
        s.add(version)
        await s.flush()
        version_id = version.id
        assert version_id is not None, "StrategyVersion.id 必须由 flush 取回"
        await s.commit()
    return version_id


async def _make_member(session: AsyncSession, email: str) -> User:
    user = User(email=email, password_hash="x", status="active")
    session.add(user)
    await session.flush()
    user._roles = ["member"]  # resolve_effective_access 经 _get_user_roles 读取
    return user


# ---------------------------------------------------------------------------
# A. SMC 并发幂等（真实两事务竞争）→ StrategyEvent=1 且该事件 Outbox=1
# ---------------------------------------------------------------------------

async def _concurrent_struct_event_once(
    engine,
    *,
    kind: str,
    event_type: str,
    anchor_time: str,
) -> None:
    """两真实并发 PG 事务竞争**同一 canonical structure event_key**。

    rendezvous 用 ``asyncio.Barrier(2)``：两个 worker 必须都到达后才同时 release，
    因此两者是在各自独立 AsyncSession 上**同时**进入 write_event 竞争同一 UNIQUE key
    （Event.set()+wait() 不是 barrier，先到者会被立即放行，不能证明“同时竞争”）。

    复现生产链路分支（monitor_batch_service._write_event_drafts → write_event
    → 若 None 则 skip → 胜者 _send_merged_notification → write_outbox）：
    败者 write_event 返回 None 后立即 skip，绝不再 enqueue Outbox。

    identity 直接取自 canonical owner ``structure_transition_key``（不重跑 transition 算法，
    也不调用已从生产路径切掉的旧 ``evaluate_smc_events``）。
    """
    inst_id = uuid.uuid4()
    strategy_version_id = await _seed_strategy_version(
        engine, suffix=uuid.uuid4().hex[:12]
    )
    now = datetime.now(timezone.utc)

    # canonical identity（与 evaluate_realtime_smc_events adapter 完全同形）：
    #   dedupe_key     = "smc_struct_transition:" + transition_key
    #   logical_entity = "smc_structure:"        + transition_key
    transition_key = structure_transition_key(inst_id, "swing", kind, anchor_time)
    event_key = "smc_struct_transition:" + transition_key
    logical_entity = "smc_structure:" + transition_key

    # [真实并发 rendezvous] asyncio.Barrier(2)：worker A 到达后**必须等待** worker B 也到达，
    # 两者才同时 release，再各自进入 write_event 竞争同一 UNIQUE event_key。
    # 反例：Event.set() + Event.wait() 不是 barrier —— 先到者立即放行，退化为
    # 「A 先 INSERT + commit，B 后撞 UNIQUE」，即使 PASS 也不能证明两事务**同时**竞争。
    barrier = asyncio.Barrier(2)

    async def worker(sess: AsyncSession) -> bool:
        draft = StrategyEventDraft(
            event_type=event_type, event_time=now, dedupe_key=event_key,
            logical_entity=logical_entity, payload={"event_key": event_key},
            state_ttl_seconds=600,
        )
        # 两 worker 都必须到达此处才 release（DB UNIQUE 才是真正仲裁者）
        await barrier.wait()
        event_orm = await write_event(
            sess, event_key=event_key, strategy_version_id=strategy_version_id,
            instrument_id=inst_id, event_type=event_type, event_time=now,
            payload=draft.payload, logical_entity_id=logical_entity,
        )
        if event_orm is not None:  # 生产分支 —— 败者（write_event=None）不 enqueue
            await write_outbox(
                sess, _NOTIFICATION_EVENT_TYPE, {"event_key": event_key},
                "strategy_event", aggregate_id=event_orm.id,
            )
        await sess.commit()
        return event_orm is not None

    async with _new_session(engine) as sa, _new_session(engine) as sb:
        r1, r2 = await asyncio.gather(worker(sa), worker(sb))
    # 恰好一个胜出（UNIQUE + ON CONFLICT DO NOTHING）
    assert (r1 and not r2) or (r2 and not r1), "exactly one worker must win the UNIQUE race"

    async with _new_session(engine) as s:
        event_rows = (
            await s.execute(
                select(StrategyEvent).where(StrategyEvent.event_key == event_key)
            )
        ).scalars().all()
        assert len(event_rows) == 1, "StrategyEvent rows must be exactly 1 for this event_key"
        event_row = event_rows[0]
        # Outbox 必须只数**本事件**（不得把 verify DB 内其他 required test 的 outbox 算进来）
        ob = (
            await s.execute(
                select(func.count()).select_from(Outbox).where(
                    Outbox.event_type == _NOTIFICATION_EVENT_TYPE,
                    Outbox.aggregate_type == "strategy_event",
                    Outbox.aggregate_id == event_row.id,
                )
            )
        ).scalar_one()
    assert ob == 1, "Outbox rows for THIS event must be 1 (loser did NOT enqueue)"


async def test_pg_smc_concurrent_bos_event_and_outbox_once(engine) -> None:
    """两 worker 并发写同一 canonical BOS structure identity → StrategyEvent=1 且 Outbox=1。"""
    await _concurrent_struct_event_once(
        engine, kind="high", event_type=SMC_BOS_CROSS, anchor_time="2026-09-01"
    )


async def test_pg_smc_concurrent_choch_event_and_outbox_once(engine) -> None:
    """两 worker 并发写同一 canonical CHoCH structure identity → StrategyEvent=1 且 Outbox=1。

    canonical structure identity 不含 event_type（同一 pivot 一生一次），故本用例使用独立
    标的/独立 pivot，确保与 BOS 用例是两个不同的 event_key。
    """
    await _concurrent_struct_event_once(
        engine, kind="high", event_type=SMC_CHOCH_CROSS, anchor_time="2026-09-01"
    )


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
            source="migration", entitlement_snapshot={},
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
            source="migration", entitlement_snapshot={},
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
            source="migration", entitlement_snapshot={},
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
