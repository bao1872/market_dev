"""订阅到期端到端集成测试（Phase 2 Task 2.7）。

验证权限矩阵（真实 HTTP + PostgreSQL + Alembic 迁移结构）：
- expired member：4 个趋势选股端点 403、4 个自选股端点 403、/me/access 200、/auth/renew 可续期
- no-subscription member：与 expired member 相同
- active member：所有端点正常访问
- admin：无 subscription 也可正常访问
- 续期恢复：将过期 subscription 的 expires_at 延到未来后，同一接口自动返回 200

端点覆盖：
- 趋势选股：
  - GET /strategies/{key}/published-runs
  - GET /strategies/{key}/results
  - GET /strategy-runs/{run_id}/results
  - GET /strategy-runs/{run_id}/results/{result_id}
- 自选股：
  - GET /watchlist
  - POST /watchlist
  - DELETE /watchlist/{instrument_id}
  - GET /watchlist/monitor-status

测试策略：
- 使用 conftest 的 client fixture（真实 HTTP + 注入 db_session）
- 创建真实 User / Role / Subscription / Instrument / StrategyRun / StrategyResult 数据
- 角色名统一使用 member（AGENTS.md 规则 7）
- admin 不绑定 subscription（AGENTS.md 规则 8）
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyResult, StrategyRun
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.subscription_service import generate_invite_codes


# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    from sqlalchemy import select

    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（admin 角色，无 subscription）。"""
    admin = User(
        id=uuid.uuid4(),
        email=f"admin_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("admin-password-123"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(admin)
    admin_role = await _ensure_role(db, "admin")
    db.add(UserRole(user_id=admin.id, role_id=admin_role.id))
    await db.flush()
    return admin


async def _create_member_with_plan(
    db: AsyncSession, plan_code: str = "observe_20"
) -> tuple[User, Subscription]:
    """通过邀请码注册创建 member 用户 + active subscription。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code=plan_code,
        grant_months=1,
    )
    await db.flush()

    from app.services.subscription_service import register_with_invite_code

    email = f"member_{uuid.uuid4().hex[:8]}@test.com"
    user, subscription = await register_with_invite_code(
        db=db,
        email=email,
        password="password-12345",
        raw_invite_code=results[0][1],
    )
    await db.flush()
    return user, subscription


async def _create_member_without_subscription(db: AsyncSession) -> User:
    """创建无 subscription 记录的 member 用户。"""
    user = User(
        id=uuid.uuid4(),
        email=f"nomember_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(user)
    member_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=member_role.id))
    await db.flush()
    return user


async def _create_expired_member(
    db: AsyncSession, plan_code: str = "observe_20"
) -> tuple[User, Subscription]:
    """创建订阅已过期的 member 用户。"""
    user, subscription = await _create_member_with_plan(db, plan_code)
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()
    return user, subscription


async def _create_trend_selection_data(db: AsyncSession) -> dict:
    """创建趋势选股测试数据：策略定义 + released 版本 + published run + 结果。"""
    now = datetime.now(UTC)
    trade_date = date(2026, 6, 23)

    strategy_key = f"test_selector_{uuid.uuid4().hex[:8]}"
    definition = StrategyDefinition(
        strategy_key=strategy_key,
        kind="selector",
        display_name="测试选股策略",
    )
    db.add(definition)
    await db.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=now,
    )
    db.add(version)
    await db.flush()

    instrument = Instrument(
        symbol=f"T{uuid.uuid4().hex[:5]}",
        name="测试标的",
        market="SZ",
        status="active",
    )
    db.add(instrument)
    await db.flush()

    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:{version.id}:scheduled:{trade_date}",
        published_at=now,
    )
    db.add(run)
    await db.flush()

    result = StrategyResult(
        run_id=run.id,
        strategy_version_id=version.id,
        instrument_id=instrument.id,
        trade_date=trade_date,
        payload={"dsa_dir_bars": 50, "offset_mean": 0.01},
    )
    db.add(result)
    await db.flush()

    return {
        "strategy_key": strategy_key,
        "run_id": run.id,
        "result_id": result.id,
        "trade_date": trade_date.isoformat(),
        "instrument_id": instrument.id,
    }


async def _create_instruments(db: AsyncSession, count: int) -> list[Instrument]:
    """创建若干测试标的。"""
    instruments = []
    for _ in range(count):
        inst = Instrument(
            symbol=f"T{uuid.uuid4().hex[:5]}",
            name="测试标的",
            market="SZ",
            status="active",
        )
        db.add(inst)
        instruments.append(inst)
    await db.flush()
    return instruments


async def _add_active_watchlist(
    db: AsyncSession, user: User, instruments: list[Instrument], count: int
) -> None:
    """为用户添加 count 个 active 自选股。"""
    from app.models.watchlist import UserWatchlistItem

    for i in range(count):
        item = UserWatchlistItem(
            user_id=user.id,
            instrument_id=instruments[i].id,
            source="manual",
            active=True,
        )
        db.add(item)
    await db.flush()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _assert_trend_blocked(
    client: AsyncClient,
    db: AsyncSession,
    user_id: uuid.UUID,
) -> dict:
    """断言 4 个趋势选股端点对指定用户返回 403。"""
    data = await _create_trend_selection_data(db)
    await db.flush()

    # GET /strategies/{key}/published-runs
    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    # GET /strategies/{key}/results
    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    # GET /strategy-runs/{run_id}/results
    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    # GET /strategy-runs/{run_id}/results/{result_id}
    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    return data


async def _assert_watchlist_blocked(
    client: AsyncClient,
    db: AsyncSession,
    user_id: uuid.UUID,
) -> None:
    """断言 4 个自选股端点对指定用户返回 403。"""
    instruments = await _create_instruments(db, 1)
    await db.flush()

    # GET /watchlist
    resp = await client.get("/watchlist", headers=_auth_headers(user_id))
    assert resp.status_code == 403, resp.text

    # POST /watchlist
    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    # DELETE /watchlist/{instrument_id}
    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 403, resp.text

    # GET /watchlist/monitor-status
    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user_id))
    assert resp.status_code == 403, resp.text


async def _assert_me_access_ok(client: AsyncClient, user_id: uuid.UUID) -> None:
    """断言 /me/access 对指定用户返回 200。"""
    resp = await client.get("/me/access", headers=_auth_headers(user_id))
    assert resp.status_code == 200, resp.text


# ============================================================
# expired member 端到端测试
# ============================================================


@pytest.mark.asyncio
async def test_expired_member_blocked_on_trend_selection(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """过期 member 访问 4 个趋势选股端点 → 403。"""
    user, _ = await _create_expired_member(db_session)
    await db_session.flush()
    await _assert_trend_blocked(client, db_session, user.id)


@pytest.mark.asyncio
async def test_expired_member_blocked_on_watchlist(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """过期 member 访问 4 个自选股端点 → 403。"""
    user, _ = await _create_expired_member(db_session)
    await db_session.flush()
    await _assert_watchlist_blocked(client, db_session, user.id)


@pytest.mark.asyncio
async def test_expired_member_me_access_and_renew_allowed(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """过期 member 仍可 GET /me/access 和 POST /auth/renew。"""
    user, _ = await _create_expired_member(db_session)
    await db_session.flush()

    # /me/access 200
    resp = await client.get("/me/access", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_active"] is False

    # 生成邀请码用于续期
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db_session.flush()

    # /auth/renew 可访问（登录用户即可调用）
    resp = await client.post(
        "/auth/renew",
        json={"invite_code": results[0][1]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["membership_status"] == "active"


# ============================================================
# no-subscription member 端到端测试
# ============================================================


@pytest.mark.asyncio
async def test_no_subscription_member_blocked_on_trend_selection(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """无 subscription member 访问 4 个趋势选股端点 → 403。"""
    user = await _create_member_without_subscription(db_session)
    await db_session.flush()
    await _assert_trend_blocked(client, db_session, user.id)


@pytest.mark.asyncio
async def test_no_subscription_member_blocked_on_watchlist(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """无 subscription member 访问 4 个自选股端点 → 403。"""
    user = await _create_member_without_subscription(db_session)
    await db_session.flush()
    await _assert_watchlist_blocked(client, db_session, user.id)


@pytest.mark.asyncio
async def test_no_subscription_member_me_access_and_renew_allowed(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """无 subscription member 可 GET /me/access 和 POST /auth/renew。"""
    user = await _create_member_without_subscription(db_session)
    await db_session.flush()

    resp = await client.get("/me/access", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["subscription_active"] is False

    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db_session.flush()

    resp = await client.post(
        "/auth/renew",
        json={"invite_code": results[0][1]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["membership_status"] == "active"


# ============================================================
# active member 端到端测试
# ============================================================


@pytest.mark.asyncio
async def test_active_member_allowed_on_trend_selection(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """active member 访问 4 个趋势选股端点 → 200。"""
    user, _ = await _create_member_with_plan(db_session, "observe_20")
    data = await _create_trend_selection_data(db_session)
    await db_session.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_active_member_allowed_on_watchlist(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """active member 访问 4 个自选股端点 → 200/201/204。"""
    user, _ = await _create_member_with_plan(db_session, "observe_20")
    instruments = await _create_instruments(db_session, 2)
    await db_session.flush()

    # GET /watchlist
    resp = await client.get("/watchlist", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text

    # POST /watchlist
    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text

    # DELETE /watchlist/{instrument_id}
    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 204, resp.text

    # GET /watchlist/monitor-status
    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text


# ============================================================
# admin 端到端测试
# ============================================================


@pytest.mark.asyncio
async def test_admin_allowed_on_trend_selection_without_subscription(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """admin 无 subscription 也可访问 4 个趋势选股端点 → 200。"""
    admin = await _create_admin(db_session)
    data = await _create_trend_selection_data(db_session)
    await db_session.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_admin_allowed_on_watchlist_without_subscription(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """admin 无 subscription 也可访问 4 个自选股端点 → 200/201/204。"""
    admin = await _create_admin(db_session)
    instruments = await _create_instruments(db_session, 2)
    await db_session.flush()

    resp = await client.get("/watchlist", headers=_auth_headers(admin.id))
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 201, resp.text

    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 204, resp.text

    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(admin.id))
    assert resp.status_code == 200, resp.text


# ============================================================
# 续期恢复端到端测试
# ============================================================


@pytest.mark.asyncio
async def test_renewal_restores_trend_selection_access(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """过期 member 续期后，4 个趋势选股端点自动恢复 200。"""
    user, subscription = await _create_expired_member(db_session)
    data = await _create_trend_selection_data(db_session)
    await db_session.flush()

    # 续期前 403
    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text

    # 生成邀请码并续期
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db_session.flush()

    resp = await client.post(
        "/auth/renew",
        json={"invite_code": results[0][1]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    # 刷新 subscription 对象，避免测试 session 缓存过期值
    await db_session.refresh(subscription)
    assert subscription.expires_at > datetime.now(UTC)

    # 续期后同一接口 200
    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_renewal_restores_watchlist_access(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """过期 member 续期后，4 个自选股端点自动恢复 200/201/204。"""
    user, subscription = await _create_expired_member(db_session)
    instruments = await _create_instruments(db_session, 2)
    await db_session.flush()

    # 续期前 POST 403
    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text

    # 生成邀请码并续期
    admin = await _create_admin(db_session)
    results = await generate_invite_codes(
        db=db_session,
        count=1,
        created_by=admin.id,
        plan_code="observe_20",
        grant_months=1,
    )
    await db_session.flush()

    resp = await client.post(
        "/auth/renew",
        json={"invite_code": results[0][1]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(subscription)
    assert subscription.expires_at > datetime.now(UTC)

    # 续期后自选股数据仍保留（本测试无旧数据，重点验证可新增）
    resp = await client.get("/watchlist", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text

    resp = await client.post(
        "/watchlist",
        json={"instrument_id": str(instruments[0].id), "source": "manual"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text

    resp = await client.delete(
        f"/watchlist/{instruments[0].id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 204, resp.text

    resp = await client.get("/watchlist/monitor-status", headers=_auth_headers(user.id))
    assert resp.status_code == 200, resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
