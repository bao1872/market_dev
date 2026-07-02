"""趋势选股 API 权限测试（Phase 2 Task 2.6 / 2.7）。

验证权限矩阵（advice.md Phase 2）：
- GET /strategies/{key}/published-runs: require_active_subscription + require_feature("trend_selection")
- GET /strategies/{key}/results: require_active_subscription + require_feature("trend_selection")
- GET /strategy-runs/{run_id}/results: require_active_subscription + require_feature("trend_selection")
- GET /strategy-runs/{run_id}/results/{result_id}: require_active_subscription + require_feature("trend_selection")

权限分层设计：
- 所有 4 个趋势选股端点均需有效订阅 + trend_selection feature
- 过期/无订阅 member 返回 403；admin 豁免

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用真实 HTTP 端点
- 复用 test_me_access.py 的用户/角色创建模式（admin/member/expired/no-subscription）
- 角色名统一使用 member（不是 user），符合 AGENTS.md 规则 7
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.instrument import Instrument
from app.models.strategy import StrategyDefinition, StrategyVersion
from app.models.strategy_run import StrategyResult, StrategyRun
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)

# ============================================================
# 测试辅助函数
# ============================================================


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回（幂等）。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin(db: AsyncSession) -> User:
    """创建管理员用户（admin 角色，无 subscription，符合 AGENTS.md 规则 8）。"""
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
    db: AsyncSession, plan_code: str, grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建 member 用户 + 订阅记录（复用 subscription_service 唯一真源）。"""
    admin = await _create_admin(db)
    results = await generate_invite_codes(
        db=db,
        count=1,
        created_by=admin.id,
        plan_code=plan_code,
        grant_months=grant_months,
    )
    await db.flush()
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
    """创建无订阅记录的 member 用户（features=[]，无 trend_selection）。"""
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


async def _create_expired_member(db: AsyncSession) -> User:
    """创建订阅已过期的 member 用户（subscription_active=False，features 保留 trend_selection）。"""
    user, subscription = await _create_member_with_plan(db, "observe_20")
    # [AccessControl] - 描述: 手动设置过期，subscription_service 实时计算 effective_status
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()
    return user


async def _create_trend_selection_data(db: AsyncSession) -> dict:
    """创建趋势选股测试数据：策略定义 + released 版本 + published run + 结果。

    Returns:
        dict: {strategy_key, run_id, result_id, trade_date, instrument_id}
    """
    now = datetime.now(UTC)
    trade_date = date(2026, 6, 23)

    # 1. 策略定义 + released 版本
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

    # 2. 标的
    instrument = Instrument(
        symbol=f"T{uuid.uuid4().hex[:5]}",
        name="测试标的",
        market="SZ",
        status="active",
    )
    db.add(instrument)
    await db.flush()

    # 3. 已发布 run
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

    # 4. 结果
    result = StrategyResult(
        run_id=run.id,
        strategy_version_id=version.id,
        instrument_id=instrument.id,
        trade_date=trade_date,
        payload={
            "dsa_dir_bars": 50,
            "offset_mean": 0.01,
        },
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


# ============================================================
# fixtures
# ============================================================


@pytest_asyncio.fixture
async def perm_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。

    覆盖 app.core.deps.get_db 与 app.db.get_db 两个入口，确保路由拿到的 session
    与 fixture 中操作的是同一事务（测试后由 db_session fixture 回滚）。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# GET /strategies/{key}/published-runs 权限测试
# require_active_subscription + require_feature("trend_selection")
# ============================================================


@pytest.mark.asyncio
async def test_published_runs_requires_auth(perm_client: tuple[AsyncClient, AsyncSession]) -> None:
    """未登录访问 published-runs → 401。"""
    client, _ = perm_client
    resp = await client.get("/strategies/any_key/published-runs")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_published_runs_rejects_member_without_subscription(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member（features=[] 无 trend_selection）→ 403。"""
    client, db = perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get(
        "/strategies/any_key/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_published_runs_admin_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200（admin 豁免 feature 检查）。"""
    client, db = perm_client
    admin = await _create_admin(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_published_runs_active_member_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member + trend_selection feature → 200。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_published_runs_rejects_expired_member(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member → 403（require_active_subscription 拒绝）。"""
    client, db = perm_client
    user = await _create_expired_member(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/published-runs",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


# ============================================================
# GET /strategies/{key}/results 权限测试
# require_active_subscription + require_feature("trend_selection")
# ============================================================


@pytest.mark.asyncio
async def test_strategy_results_requires_auth(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未登录访问 results → 401。"""
    client, _ = perm_client
    resp = await client.get(
        "/strategies/any_key/results",
        params={"trade_date": "2026-06-23"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_strategy_results_rejects_member_without_subscription(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member（features=[] 无 trend_selection）→ 403。"""
    client, db = perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get(
        "/strategies/any_key/results",
        params={"trade_date": "2026-06-23"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_strategy_results_admin_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200。"""
    client, db = perm_client
    admin = await _create_admin(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_strategy_results_active_member_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member + trend_selection feature → 200。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_strategy_results_rejects_expired_member(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member → 403（require_active_subscription 拒绝）。"""
    client, db = perm_client
    user = await _create_expired_member(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategies/{data['strategy_key']}/results",
        params={"trade_date": data["trade_date"]},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


# ============================================================
# GET /strategy-runs/{run_id}/results 权限测试
# require_active_subscription + require_feature("trend_selection")
# ============================================================


@pytest.mark.asyncio
async def test_run_results_requires_auth(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未登录访问 strategy-runs/{run_id}/results → 401。"""
    client, _ = perm_client
    fake_run_id = uuid.uuid4()
    resp = await client.get(f"/strategy-runs/{fake_run_id}/results")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_run_results_rejects_member_without_subscription(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member → 403（require_active_subscription 拒绝）。"""
    client, db = perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    fake_run_id = uuid.uuid4()
    resp = await client.get(
        f"/strategy-runs/{fake_run_id}/results",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_run_results_rejects_expired_member(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member → 403（require_active_subscription 拒绝，subscription_active=False）。"""
    client, db = perm_client
    user = await _create_expired_member(db)
    await db.flush()

    fake_run_id = uuid.uuid4()
    resp = await client.get(
        f"/strategy-runs/{fake_run_id}/results",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_run_results_admin_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200（admin 豁免订阅与 feature 检查）。"""
    client, db = perm_client
    admin = await _create_admin(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_run_results_active_member_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member + trend_selection → 200。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


# ============================================================
# GET /strategy-runs/{run_id}/results/{result_id} 权限测试
# require_active_subscription + require_feature("trend_selection")
# ============================================================


@pytest.mark.asyncio
async def test_run_result_detail_requires_auth(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未登录访问 strategy-runs/{run_id}/results/{result_id} → 401。"""
    client, _ = perm_client
    fake_run_id = uuid.uuid4()
    fake_result_id = uuid.uuid4()
    resp = await client.get(f"/strategy-runs/{fake_run_id}/results/{fake_result_id}")
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_run_result_detail_rejects_member_without_subscription(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member → 403。"""
    client, db = perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    fake_run_id = uuid.uuid4()
    fake_result_id = uuid.uuid4()
    resp = await client.get(
        f"/strategy-runs/{fake_run_id}/results/{fake_result_id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_run_result_detail_rejects_expired_member(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member → 403。"""
    client, db = perm_client
    user = await _create_expired_member(db)
    await db.flush()

    fake_run_id = uuid.uuid4()
    fake_result_id = uuid.uuid4()
    resp = await client.get(
        f"/strategy-runs/{fake_run_id}/results/{fake_result_id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_run_result_detail_admin_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200。"""
    client, db = perm_client
    admin = await _create_admin(db)
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_run_result_detail_active_member_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member + trend_selection → 200。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    data = await _create_trend_selection_data(db)
    await db.flush()

    resp = await client.get(
        f"/strategy-runs/{data['run_id']}/results/{data['result_id']}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
