"""用户表格视图配置（user_table_view_presets）API 测试。

测试内容：
1. CRUD：创建 / 查询 / 更新 / 删除 preset
2. 用户隔离：用户 A 不能操作用户 B 的 preset
3. 重名冲突：(user_id, table_id, strategy_key, name) 唯一约束
4. quota：每 user+table_id+strategy_key 最多 20 个
5. 非法 JSON：config 字段校验（禁止保存 selectedKeys/page/activeRunId/rows）
6. 权限矩阵：401 未登录 / 403 无订阅 / 403 过期 / 200 admin / 200 active member

设计：
- 与 trend_selection 权限一致：require_active_subscription + require_feature("trend_selection")
- API 路径：/me/table-view-presets（GET 列表 / POST 创建 / PATCH 更新 / DELETE 删除）
- config 只保存 keyword/sort/filters/hiddenColumns/pageSize，禁止保存 selectedKeys/page/activeRunId/rows
- 默认配置：每 user+table_id+strategy_key 至多 1 个 is_default=true（设置新默认时旧默认自动取消）
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.invitation import InviteCode
from app.models.subscription import Subscription
from app.models.table_view_preset import UserTableViewPreset
from app.models.user import Role, User, UserRole
from app.services.subscription_service import (
    generate_invite_codes,
    register_with_invite_code,
)
from tests.conftest import TestAsyncSessionLocal

# ============================================================
# 测试辅助函数（复用 test_trend_selection_api_permissions 模式）
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
    db: AsyncSession, plan_code: str = "observe_20", grant_months: int = 1
) -> tuple[User, Subscription]:
    """通过邀请码注册创建 member 用户 + 订阅记录。"""
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
    """创建订阅已过期的 member 用户。"""
    user, subscription = await _create_member_with_plan(db, "observe_20")
    subscription.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db.flush()
    return user


# ============================================================
# fixtures
# ============================================================


@pytest_asyncio.fixture
async def perm_client(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncSession], None]:
    """提供 HTTP 客户端 + 测试 DB session，通过 dependency_overrides 注入。"""
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


def _valid_config() -> dict:
    """返回合法的 preset config（仅含允许字段）。"""
    return {
        "keyword": "茅台",
        "sort": {"key": "change_pct", "direction": "desc"},
        "filters": [
            {"key": "change_pct", "op": "gt", "value": 3.0},
        ],
        "hiddenColumns": ["symbol"],
        "pageSize": 50,
    }


# ============================================================
# 权限矩阵测试
# ============================================================


@pytest.mark.asyncio
async def test_list_presets_requires_auth(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未登录访问 GET /me/table-view-presets → 401。"""
    client, _ = perm_client
    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_create_preset_requires_auth(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """未登录 POST /me/table-view-presets → 401。"""
    client, _ = perm_client
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "默认",
            "config": _valid_config(),
        },
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.asyncio
async def test_list_presets_rejects_member_without_subscription(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """无订阅 member（features=[] 无 trend_selection）→ 403。"""
    client, db = perm_client
    user = await _create_member_without_subscription(db)
    await db.flush()

    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_list_presets_rejects_expired_member(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """过期订阅 member → 403。"""
    client, db = perm_client
    user = await _create_expired_member(db)
    await db.flush()

    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_list_presets_admin_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """admin → 200（admin 豁免 feature 检查）。"""
    client, db = perm_client
    admin = await _create_admin(db)
    await db.flush()

    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(admin.id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "items" in data
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_list_presets_active_member_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """active member + trend_selection feature → 200。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text


# ============================================================
# CRUD 测试
# ============================================================


@pytest.mark.asyncio
async def test_create_preset_success(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """创建 preset 成功 → 201。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "涨幅前 50",
            "config": _valid_config(),
            "is_default": False,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["table_id"] == "screener"
    assert data["strategy_key"] == "dsa_selector"
    assert data["name"] == "涨幅前 50"
    assert data["config"]["pageSize"] == 50
    assert data["is_default"] is False
    assert "id" in data
    assert "created_at" in data
    assert "updated_at" in data
    # user_id 应为当前认证用户，不接受 body 注入
    assert data["user_id"] == str(user.id)


@pytest.mark.asyncio
async def test_create_preset_default_null_strategy_key(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """strategy_key 可空（适用于无策略的表格）→ 201。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "watchlist",
            "strategy_key": None,
            "name": "默认视图",
            "config": {"pageSize": 30},
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["strategy_key"] is None


@pytest.mark.asyncio
async def test_list_presets_filter_by_table_id(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """按 table_id 过滤查询 preset 列表。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # 创建两个 table_id 的 preset
    for table_id in ("screener", "watchlist"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": table_id,
                "strategy_key": "dsa_selector",
                "name": f"{table_id}-view",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text

    # 按 table_id=screener 查询
    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["table_id"] == "screener"


@pytest.mark.asyncio
async def test_list_presets_filter_by_strategy_key(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """按 table_id + strategy_key 过滤查询 preset 列表。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # 同一 table_id 下两个 strategy_key
    for sk in ("dsa_selector", "watchlist_monitor"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": sk,
                "name": f"view-{sk}",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener", "strategy_key": "dsa_selector"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["strategy_key"] == "dsa_selector"


@pytest.mark.asyncio
async def test_update_preset_rename(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 重命名 preset。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    create_resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "old-name",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    assert create_resp.status_code == 201, create_resp.text
    preset_id = create_resp.json()["id"]

    resp = await client.patch(
        f"/me/table-view-presets/{preset_id}",
        json={"name": "new-name"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "new-name"
    assert data["id"] == preset_id


@pytest.mark.asyncio
async def test_update_preset_config(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 更新 config。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    create_resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    preset_id = create_resp.json()["id"]

    new_config = {"pageSize": 100, "keyword": "新能源"}
    resp = await client.patch(
        f"/me/table-view-presets/{preset_id}",
        json={"config": new_config},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["config"]["pageSize"] == 100
    assert data["config"]["keyword"] == "新能源"


@pytest.mark.asyncio
async def test_delete_preset_success(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """DELETE 删除 preset → 204。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    create_resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "to-delete",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    preset_id = create_resp.json()["id"]

    resp = await client.delete(
        f"/me/table-view-presets/{preset_id}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 204, resp.text

    # 验证已删除
    list_resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user.id),
    )
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_delete_preset_not_found(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """DELETE 不存在的 preset → 404。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.delete(
        f"/me/table-view-presets/{uuid.uuid4()}",
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_update_preset_not_found(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 不存在的 preset → 404。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.patch(
        f"/me/table-view-presets/{uuid.uuid4()}",
        json={"name": "x"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 404, resp.text


# ============================================================
# 用户隔离测试
# ============================================================


@pytest.mark.asyncio
async def test_user_isolation_list(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """用户 A 不能看到用户 B 的 preset。"""
    client, db = perm_client
    user_a, _ = await _create_member_with_plan(db, "observe_20")
    user_b, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # user_a 创建 preset
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "user-a-view",
            "config": _valid_config(),
        },
        headers=_auth_headers(user_a.id),
    )
    assert resp.status_code == 201, resp.text

    # user_b 查询，应看不到 user_a 的 preset
    list_resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener"},
        headers=_auth_headers(user_b.id),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert list_resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_user_isolation_update(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """用户 A 不能修改用户 B 的 preset → 404。"""
    client, db = perm_client
    user_a, _ = await _create_member_with_plan(db, "observe_20")
    user_b, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    create_resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "user-a-view",
            "config": _valid_config(),
        },
        headers=_auth_headers(user_a.id),
    )
    preset_id = create_resp.json()["id"]

    # user_b 尝试修改
    resp = await client.patch(
        f"/me/table-view-presets/{preset_id}",
        json={"name": "hijack"},
        headers=_auth_headers(user_b.id),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_user_isolation_delete(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """用户 A 不能删除用户 B 的 preset → 404。"""
    client, db = perm_client
    user_a, _ = await _create_member_with_plan(db, "observe_20")
    user_b, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    create_resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "user-a-view",
            "config": _valid_config(),
        },
        headers=_auth_headers(user_a.id),
    )
    preset_id = create_resp.json()["id"]

    resp = await client.delete(
        f"/me/table-view-presets/{preset_id}",
        headers=_auth_headers(user_b.id),
    )
    assert resp.status_code == 404, resp.text


# ============================================================
# 重名冲突测试
# ============================================================


@pytest.mark.asyncio
async def test_duplicate_name_conflict(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """同一 (user, table_id, strategy_key, name) 重复创建 → 409。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    payload = {
        "table_id": "screener",
        "strategy_key": "dsa_selector",
        "name": "dup-name",
        "config": _valid_config(),
    }
    resp1 = await client.post(
        "/me/table-view-presets", json=payload, headers=_auth_headers(user.id),
    )
    assert resp1.status_code == 201, resp1.text

    resp2 = await client.post(
        "/me/table-view-presets", json=payload, headers=_auth_headers(user.id),
    )
    assert resp2.status_code == 409, resp2.text


@pytest.mark.asyncio
async def test_duplicate_name_different_strategy_key_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """不同 strategy_key 下同名允许。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    for sk in ("dsa_selector", "watchlist_monitor"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": sk,
                "name": "same-name",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_rename_to_existing_name_conflict(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 重命名为已存在的 name → 409。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # 创建两个 preset
    ids = []
    for name in ("view-1", "view-2"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": name,
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        ids.append(resp.json()["id"])

    # 把 view-1 重命名为 view-2 → 409
    resp = await client.patch(
        f"/me/table-view-presets/{ids[0]}",
        json={"name": "view-2"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 409, resp.text


# ============================================================
# quota 测试
# ============================================================


@pytest.mark.asyncio
async def test_quota_limit_20(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """每 user+table_id+strategy_key 最多 20 个 → 第 21 个 409/422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    for i in range(20):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": f"view-{i:02d}",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, f"#{i} 创建失败: {resp.text}"

    # 第 21 个应被拒绝
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view-21",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code in (409, 422), resp.text
    assert "quota" in resp.text.lower() or "上限" in resp.text


@pytest.mark.asyncio
async def test_quota_independent_per_strategy_key(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """不同 strategy_key 独立计数。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # dsa_selector 创建 5 个
    for i in range(5):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": f"dsa-{i}",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text

    # watchlist_monitor 创建 5 个（不冲突）
    for i in range(5):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "watchlist_monitor",
                "name": f"wl-{i}",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text


# ============================================================
# 非法 config 字段测试
# ============================================================


@pytest.mark.asyncio
async def test_config_rejects_selected_keys(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 禁止保存 selectedKeys → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["selectedKeys"] = ["a", "b"]
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "bad",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_rejects_page(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 禁止保存 page → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["page"] = 3
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "bad",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_rejects_active_run_id(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 禁止保存 activeRunId → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["activeRunId"] = "run-xxx"
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "bad",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_rejects_rows(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 禁止保存 rows（结果数据）→ 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["rows"] = [{"symbol": "000001"}]
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "bad",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_empty_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 允许为空 dict {}。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "empty",
            "config": {},
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text


# ============================================================
# is_default 测试
# ============================================================


@pytest.mark.asyncio
async def test_set_default_unsets_previous(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """设置新默认时，旧默认自动取消（同 table_id + strategy_key 维度）。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # 创建 preset-1 并设为默认
    resp1 = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view-1",
            "config": _valid_config(),
            "is_default": True,
        },
        headers=_auth_headers(user.id),
    )
    assert resp1.status_code == 201, resp1.text
    id1 = resp1.json()["id"]
    assert resp1.json()["is_default"] is True

    # 创建 preset-2 并设为默认
    resp2 = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view-2",
            "config": _valid_config(),
            "is_default": True,
        },
        headers=_auth_headers(user.id),
    )
    assert resp2.status_code == 201, resp2.text
    id2 = resp2.json()["id"]

    # 查询列表，验证 id1 已自动取消默认
    list_resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener", "strategy_key": "dsa_selector"},
        headers=_auth_headers(user.id),
    )
    items = {item["id"]: item["is_default"] for item in list_resp.json()["items"]}
    assert items[id1] is False
    assert items[id2] is True


@pytest.mark.asyncio
async def test_patch_set_default_unsets_previous(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 设置 is_default=True 时，旧默认自动取消。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    # 创建两个非默认 preset
    resp1 = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view-1",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    id1 = resp1.json()["id"]

    resp2 = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view-2",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    id2 = resp2.json()["id"]

    # 把 view-1 设为默认
    await client.patch(
        f"/me/table-view-presets/{id1}",
        json={"is_default": True},
        headers=_auth_headers(user.id),
    )

    # 再把 view-2 设为默认
    await client.patch(
        f"/me/table-view-presets/{id2}",
        json={"is_default": True},
        headers=_auth_headers(user.id),
    )

    list_resp = await client.get(
        "/me/table-view-presets",
        params={"table_id": "screener", "strategy_key": "dsa_selector"},
        headers=_auth_headers(user.id),
    )
    items = {item["id"]: item["is_default"] for item in list_resp.json()["items"]}
    assert items[id1] is False
    assert items[id2] is True


# ============================================================
# 必填字段校验
# ============================================================


@pytest.mark.asyncio
async def test_create_missing_table_id(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """缺少 table_id → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_missing_name(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """缺少 name → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "config": _valid_config(),
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_missing_config(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """缺少 config → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_invalid_config_type(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config 不是 dict → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": "not-a-dict",
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_invalid_page_size_type(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.pageSize 不是 int → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["pageSize"] = "fifty"
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_create_invalid_filters_type(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.filters 不是 list → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["filters"] = "not-a-list"
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


# ============================================================
# user_id 注入安全测试
# ============================================================


@pytest.mark.asyncio
async def test_user_id_in_body_ignored(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """body 中传 user_id 应被忽略，实际写入由 JWT 上下文决定。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()
    fake_user_id = uuid.uuid4()

    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": _valid_config(),
            "user_id": str(fake_user_id),  # 应被忽略
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["user_id"] == str(user.id)
    assert data["user_id"] != str(fake_user_id)


# ============================================================
# NULL strategy_key 唯一约束测试（partial unique index）
#
# 背景：PostgreSQL 普通 UNIQUE 允许多个 NULL，当 strategy_key 为 NULL 时
# 同一 user+table_id+name 可重复插入。改用 partial unique index 修复：
#   - strategy_key IS NOT NULL → unique(user_id, table_id, strategy_key, name)
#   - strategy_key IS NULL     → unique(user_id, table_id, name)
# ============================================================


@pytest.mark.asyncio
async def test_duplicate_name_with_null_strategy_key_conflict(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """同一 user + table_id + strategy_key=NULL + name 重复创建 → 409。

    普通 UNIQUE 约束因 NULL != NULL 不会拦截，必须用 partial unique index。
    """
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    payload = {
        "table_id": "watchlist",
        "strategy_key": None,
        "name": "default-view",
        "config": {"pageSize": 30},
    }
    resp1 = await client.post(
        "/me/table-view-presets", json=payload, headers=_auth_headers(user.id),
    )
    assert resp1.status_code == 201, resp1.text

    resp2 = await client.post(
        "/me/table-view-presets", json=payload, headers=_auth_headers(user.id),
    )
    assert resp2.status_code == 409, resp2.text


@pytest.mark.asyncio
async def test_duplicate_name_null_strategy_key_different_table_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """不同 table_id + strategy_key=NULL + 同名 → 允许（201）。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    for table_id in ("watchlist", "screener"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": table_id,
                "strategy_key": None,
                "name": "same-name",
                "config": {"pageSize": 30},
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_duplicate_name_null_strategy_key_different_user_allowed(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """不同 user + 同一 table_id + strategy_key=NULL + 同名 → 允许（201）。"""
    client, db = perm_client
    user_a, _ = await _create_member_with_plan(db, "observe_20")
    user_b, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    for user in (user_a, user_b):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "watchlist",
                "strategy_key": None,
                "name": "same-name",
                "config": {"pageSize": 30},
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_rename_to_existing_name_null_strategy_key_conflict(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """PATCH 重命名时与 NULL strategy_key 维度其他 preset 同名 → 409。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    ids = []
    for name in ("view-a", "view-b"):
        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "watchlist",
                "strategy_key": None,
                "name": name,
                "config": {"pageSize": 30},
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text
        ids.append(resp.json()["id"])

    # 把 view-a 重命名为 view-b → 409
    resp = await client.patch(
        f"/me/table-view-presets/{ids[0]}",
        json={"name": "view-b"},
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 409, resp.text


# ============================================================
# config 校验加强测试（filters/hiddenColumns/sort 深度校验）
# ============================================================


@pytest.mark.asyncio
async def test_config_filters_element_must_be_dict(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.filters 元素不是 dict → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["filters"] = ["not-a-dict"]
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_filters_element_missing_key(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.filters 元素缺 key 字段 → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["filters"] = [{"op": "gt", "value": 3.0}]  # 缺 key
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_filters_invalid_op(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.filters op 不在白名单 → 422。

    白名单：contains/eq/gt/gte/lt/lte/between/empty/not_empty
    """
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["filters"] = [{"key": "change_pct", "op": "regex", "value": 3.0}]
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_filters_valid_ops_accepted(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.filters 所有合法 op 都应通过（contains/eq/gt/gte/lt/lte/between/empty/not_empty）。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    valid_ops = [
        {"key": "stock", "op": "contains", "value": "茅台"},
        {"key": "change_pct", "op": "eq", "value": 3.0},
        {"key": "change_pct", "op": "gt", "value": 3.0},
        {"key": "change_pct", "op": "gte", "value": 3.0},
        {"key": "change_pct", "op": "lt", "value": 3.0},
        {"key": "change_pct", "op": "lte", "value": 3.0},
        {"key": "change_pct", "op": "between", "value": 1.0, "value2": 5.0},
        {"key": "stock", "op": "empty", "value": ""},
        {"key": "stock", "op": "not_empty", "value": ""},
    ]
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "all-ops",
            "config": {"filters": valid_ops},
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_config_hidden_columns_element_must_be_string(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.hiddenColumns 元素不是 string → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["hiddenColumns"] = ["symbol", 123]  # 第二个不是 string
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_config_sort_key_must_be_nonempty_string(
    perm_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """config.sort.key 为空字符串 → 422。"""
    client, db = perm_client
    user, _ = await _create_member_with_plan(db, "observe_20")
    await db.flush()

    bad_config = _valid_config()
    bad_config["sort"] = {"key": "", "direction": "desc"}
    resp = await client.post(
        "/me/table-view-presets",
        json={
            "table_id": "screener",
            "strategy_key": "dsa_selector",
            "name": "view",
            "config": bad_config,
        },
        headers=_auth_headers(user.id),
    )
    assert resp.status_code == 422, resp.text


# ============================================================
# 跨 session 持久化测试（真实端到端：验证 API 内部 commit）
#
# 背景：原实现 create/update/delete 只 flush() 不 commit()，测试 fixture 复用
# 同一个 db_session，读到了未提交数据，导致生产请求结束后事务回滚、数据丢失。
# 以下测试使用独立的 TestAsyncSessionLocal session 模拟不同请求，验证写操作
# 提交后新 session 可见。
# ============================================================


@pytest.mark.asyncio
async def test_create_persists_across_sessions(client: AsyncClient) -> None:
    """POST 创建 preset 后，新 AsyncSession 必须能读到持久化记录。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async with TestAsyncSessionLocal() as session_a:
        user, _ = await _create_member_with_plan(session_a, "observe_20")
        await session_a.commit()

        async def _get_db_a() -> AsyncGenerator[AsyncSession, None]:
            yield session_a

        app.dependency_overrides[deps_get_db] = _get_db_a
        app.dependency_overrides[db_get_db] = _get_db_a

        resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": "跨会话测试",
                "config": _valid_config(),
            },
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 201, resp.text
        preset_id = resp.json()["id"]

    # 新 session 模拟新请求，验证持久化
    async with TestAsyncSessionLocal() as session_b:
        async def _get_db_b() -> AsyncGenerator[AsyncSession, None]:
            yield session_b

        app.dependency_overrides[deps_get_db] = _get_db_b
        app.dependency_overrides[db_get_db] = _get_db_b

        resp = await client.get(
            "/me/table-view-presets",
            params={"table_id": "screener", "strategy_key": "dsa_selector"},
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1, f"期望查到 1 条，实际 {data}"
        assert data["items"][0]["id"] == preset_id
        assert data["items"][0]["name"] == "跨会话测试"

        # 清理（独立 transaction 已真正提交，必须主动删除）
        await session_b.execute(
            delete(UserTableViewPreset).where(UserTableViewPreset.id == preset_id)
        )
        await session_b.execute(
            delete(InviteCode).where(InviteCode.used_by == user.id)
        )
        await session_b.execute(
            delete(Subscription).where(Subscription.user_id == user.id)
        )
        await session_b.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await session_b.execute(delete(User).where(User.id == user.id))
        await session_b.commit()

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_update_persists_across_sessions(client: AsyncClient) -> None:
    """PATCH 更新 preset 后，新 AsyncSession 必须读到更新后的 name/config/is_default。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async with TestAsyncSessionLocal() as session_a:
        user, _ = await _create_member_with_plan(session_a, "observe_20")
        await session_a.commit()

        async def _get_db_a() -> AsyncGenerator[AsyncSession, None]:
            yield session_a

        app.dependency_overrides[deps_get_db] = _get_db_a
        app.dependency_overrides[db_get_db] = _get_db_a

        create_resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": "更新前",
                "config": {"pageSize": 20},
                "is_default": False,
            },
            headers=_auth_headers(user.id),
        )
        assert create_resp.status_code == 201, create_resp.text
        preset_id = create_resp.json()["id"]

        patch_resp = await client.patch(
            f"/me/table-view-presets/{preset_id}",
            json={
                "name": "更新后",
                "config": {"pageSize": 100, "keyword": "新能源"},
                "is_default": True,
            },
            headers=_auth_headers(user.id),
        )
        assert patch_resp.status_code == 200, patch_resp.text

    async with TestAsyncSessionLocal() as session_b:
        async def _get_db_b() -> AsyncGenerator[AsyncSession, None]:
            yield session_b

        app.dependency_overrides[deps_get_db] = _get_db_b
        app.dependency_overrides[db_get_db] = _get_db_b

        resp = await client.get(
            "/me/table-view-presets",
            params={"table_id": "screener", "strategy_key": "dsa_selector"},
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1, data
        item = data["items"][0]
        assert item["id"] == preset_id
        assert item["name"] == "更新后"
        assert item["config"]["pageSize"] == 100
        assert item["config"]["keyword"] == "新能源"
        assert item["is_default"] is True

        await session_b.execute(
            delete(UserTableViewPreset).where(UserTableViewPreset.id == preset_id)
        )
        await session_b.execute(
            delete(InviteCode).where(InviteCode.used_by == user.id)
        )
        await session_b.execute(
            delete(Subscription).where(Subscription.user_id == user.id)
        )
        await session_b.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await session_b.execute(delete(User).where(User.id == user.id))
        await session_b.commit()

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_delete_persists_across_sessions(client: AsyncClient) -> None:
    """DELETE preset 后，新 AsyncSession 必须查不到该记录。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async with TestAsyncSessionLocal() as session_a:
        user, _ = await _create_member_with_plan(session_a, "observe_20")
        await session_a.commit()

        async def _get_db_a() -> AsyncGenerator[AsyncSession, None]:
            yield session_a

        app.dependency_overrides[deps_get_db] = _get_db_a
        app.dependency_overrides[db_get_db] = _get_db_a

        create_resp = await client.post(
            "/me/table-view-presets",
            json={
                "table_id": "screener",
                "strategy_key": "dsa_selector",
                "name": "待删除",
                "config": {"pageSize": 20},
            },
            headers=_auth_headers(user.id),
        )
        assert create_resp.status_code == 201, create_resp.text
        preset_id = create_resp.json()["id"]

        del_resp = await client.delete(
            f"/me/table-view-presets/{preset_id}",
            headers=_auth_headers(user.id),
        )
        assert del_resp.status_code == 204, del_resp.text

    async with TestAsyncSessionLocal() as session_b:
        async def _get_db_b() -> AsyncGenerator[AsyncSession, None]:
            yield session_b

        app.dependency_overrides[deps_get_db] = _get_db_b
        app.dependency_overrides[db_get_db] = _get_db_b

        resp = await client.get(
            "/me/table-view-presets",
            params={"table_id": "screener", "strategy_key": "dsa_selector"},
            headers=_auth_headers(user.id),
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 0, f"期望删除后 0 条，实际 {data}"

        # 清理用户（preset 已删除）
        await session_b.execute(
            delete(InviteCode).where(InviteCode.used_by == user.id)
        )
        await session_b.execute(
            delete(Subscription).where(Subscription.user_id == user.id)
        )
        await session_b.execute(delete(UserRole).where(UserRole.user_id == user.id))
        await session_b.execute(delete(User).where(User.id == user.id))
        await session_b.commit()

    app.dependency_overrides.clear()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
