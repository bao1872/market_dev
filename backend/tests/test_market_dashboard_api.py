"""F2 Market Dashboard API 合同（HTTP 层，复用 market_data permission）。

- 各端点 200 且返回派生 DTO（不碰 bars_daily / F1A）
- scope 不存在 → 404
- 无 token → 401（market_data 授权边界生效）
- lookback != 5 → 422（V1 产品合同锁死）

权限：复用现有 ``market_data`` capability；admin 角色（user_roles）自动豁免，
等价于生产 require_capability 的真实实现（本文件只 override 身份来源
get_current_active_user，require_capability / require_authenticated /
get_access_context 全部保持生产实现）。

隔离：get_db override 到本用例的 db_session（savepoint），种子与读取在同一
连接/事务内可见；用例退出时外层事务 rollback，天然无跨用例污染。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.deps import _fetch_user_with_roles, get_current_active_user
from app.core.deps import get_db as deps_get_db
from app.db import AsyncSessionLocal
from app.db import get_db as db_get_db
from app.main import app
from app.models.user import Role, User, UserRole
from tests.test_market_dashboard_read_pg import _seed

pytestmark = pytest.mark.postgres


async def _ensure_role(db, name: str) -> Role:
    role = (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if role is None:
        role = Role(id=uuid4(), name=name)
        db.add(role)
        await db.flush()
    return role


async def _create_admin_user() -> UUID:
    """真实提交一个具备 admin 角色的用户（admin → market_data 豁免）。"""
    async with AsyncSessionLocal() as s:
        user = User(
            id=uuid4(),
            email=f"dash_{uuid4().hex[:12]}@test.local",
            password_hash="not-a-real-hash",
            status="active",
        )
        s.add(user)
        await s.flush()
        role = await _ensure_role(s, "admin")
        s.add(UserRole(user_id=user.id, role_id=role.id))
        await s.commit()
        return user.id


def _auth_as(user_id: UUID) -> None:
    """只替换身份来源；require_capability 保持真实实现。

    用生产同一个 _fetch_user_with_roles 重新加载用户（真实 roles 来自 DB），
    再 expunge 使其脱离 session 后仍可读，等价 deps.get_current_active_user 的产物。
    """

    async def _override_current_user() -> User:
        async with AsyncSessionLocal() as s:
            user = await _fetch_user_with_roles(s, user_id)
            s.expunge(user)
            return user

    app.dependency_overrides[get_current_active_user] = _override_current_user


@pytest.fixture(autouse=True)
def _clear_overrides():
    """每个用例前后清空 dependency_overrides，避免跨用例污染。"""
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


@pytest.fixture
async def client(db_session):
    """真实 ASGI HTTP client：get_db override 到本用例 db_session（种子可见）。

    override 必须是 async generator 本身（FastAPI 会迭代它并 yield 出 session）；
    不能包成「返回 generator 的普通函数」，否则 FastAPI 会把 generator 对象直接
    当作 db 传入依赖（'async_generator' object has no attribute 'execute'）。
    """

    async def _override_get_db():
        yield db_session

    app.dependency_overrides[deps_get_db] = _override_get_db
    app.dependency_overrides[db_get_db] = _override_get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_market_dashboard_api_endpoints_ok(client, db_session):
    async with db_session.begin():
        boards = await _seed(db_session)

    user_id = await _create_admin_user()
    _auth_as(user_id)

    r_market = await client.get("/v1/market-dashboard/market")
    assert r_market.status_code == 200, r_market.text
    body = r_market.json()
    assert body["projection_trade_date"] == "2026-09-03"
    assert body["cards"]["ma20"] is None  # 最新一行 valid_count=0
    assert len(body["series"]) == 3

    r_rank = await client.get(
        "/v1/market-dashboard/rankings?scope_type=industry&hierarchy_level=L1",
    )
    assert r_rank.status_code == 200, r_rank.text
    assert r_rank.json()["top"][0]["board_id"] == str(boards["a"].id)

    r_scope = await client.get(f"/v1/market-dashboard/scopes/{boards['a'].id}")
    assert r_scope.status_code == 200, r_scope.text
    assert r_scope.json()["metadata"]["board_id"] == str(boards["a"].id)

    r_cmp = await client.get(
        f"/v1/market-dashboard/compare?board_ids={boards['e'].id},{boards['f'].id}",
    )
    assert r_cmp.status_code == 200, r_cmp.text
    assert len(r_cmp.json()["boards"]) == 2
    assert r_cmp.json()["boards"][0]["points"][0]["ew_index"] == pytest.approx(100.0)


async def test_market_dashboard_api_scope_404(client, db_session):
    async with db_session.begin():
        boards = await _seed(db_session)

    user_id = await _create_admin_user()
    _auth_as(user_id)

    r = await client.get(f"/v1/market-dashboard/scopes/{uuid4()}")
    assert r.status_code == 404
    # 未激活 board → 404
    r2 = await client.get(f"/v1/market-dashboard/scopes/{boards['d'].id}")
    assert r2.status_code == 404


async def test_market_dashboard_api_unauth_401(client, db_session):
    # 不应用 auth override → 无 token → 401（market_data 授权边界生效）
    r = await client.get("/v1/market-dashboard/market")
    assert r.status_code == 401


async def test_market_dashboard_api_invalid_lookback_422(client, db_session):
    async with db_session.begin():
        await _seed(db_session)

    user_id = await _create_admin_user()
    _auth_as(user_id)

    r = await client.get(
        "/v1/market-dashboard/rankings?scope_type=industry&hierarchy_level=L1&lookback=10",
    )
    assert r.status_code == 422
