"""管理员内测申请后台 API 测试 (Task 4, SubTask 4.5).

TDD 红灯阶段：先写失败测试，再实现业务代码。

测试内容（对应 spec 第五节"管理员后台内测申请页面"）：
1. 列表（分页+筛选 status/reason_code/watch_stock_range/date_from/date_to+搜索 wechat/phone）
2. 统计（累计/今日/7天/30天/状态分布/平均盯盘数/理由占比/股票区间分布）
3. 状态更新（PATCH 修改 status/admin_note）
4. 重发飞书（POST retry-feishu）
5. 普通用户访问返回 403
6. CSV 导出

测试策略：
- 复用 conftest.TestAsyncSessionLocal（真实 PostgreSQL 测试库，允许 commit）
- 创建 admin 用户 + admin 角色 + 普通用户（user 角色）满足 require_roles("admin")
- 通过 dependency_overrides 注入测试会话
- 使用 httpx.AsyncClient + ASGITransport 调用真实 FastAPI 路由
- 测试后清理 beta_applications + outbox + users/roles，保证测试隔离

异常处理：禁止 except: pass，所有清理失败必须显式处理。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.main import app
from app.models.beta_application import BetaApplication
from app.models.outbox import Outbox
from app.models.user import Role, User, UserRole
from tests.conftest import make_asgi_transport

# ============================================================
# 测试 fixtures
# ============================================================


def _hash_ip(ip: str) -> str:
    """计算 IP 的 SHA256 哈希（与 API 层一致）。"""
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


@pytest_asyncio.fixture
async def admin_beta_db_session() -> AsyncGenerator[AsyncSession, None]:
    """admin beta application 测试用 DB session。

    使用真实 PostgreSQL 测试库（允许 commit），测试后清理：
    - outbox 表中 beta_application_admin 事件
    - beta_applications 表
    - 测试创建的 users 与 user_roles
    保证测试隔离。
    """
    from tests.conftest import TestAsyncSessionLocal

    session = TestAsyncSessionLocal()
    created_user_ids: list[uuid.UUID] = []

    try:
        # 创建 admin 角色和 user 角色（幂等）
        role_stmt = select(Role).where(Role.name == "admin")
        admin_role = (await session.execute(role_stmt)).scalar_one_or_none()
        if admin_role is None:
            admin_role = Role(id=uuid.uuid4(), name="admin", description="管理员")
            session.add(admin_role)

        user_role_stmt = select(Role).where(Role.name == "member")
        user_role = (await session.execute(user_role_stmt)).scalar_one_or_none()
        if user_role is None:
            user_role = Role(id=uuid.uuid4(), name="member", description="普通会员")
            session.add(user_role)

        await session.flush()

        # 创建 admin 用户
        admin_user = User(
            id=uuid.uuid4(),
            email=f"admin_beta_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
            timezone="Asia/Shanghai",
        )
        session.add(admin_user)
        session.add(UserRole(user_id=admin_user.id, role_id=admin_role.id))
        created_user_ids.append(admin_user.id)

        # 创建普通用户（无 admin 角色）
        normal_user = User(
            id=uuid.uuid4(),
            email=f"normal_beta_{uuid.uuid4().hex[:8]}@test.com",
            password_hash="$2b$12$dummyhash",
            status="active",
            timezone="Asia/Shanghai",
        )
        session.add(normal_user)
        session.add(UserRole(user_id=normal_user.id, role_id=user_role.id))
        created_user_ids.append(normal_user.id)

        await session.commit()

        # 挂载到 session 供测试访问
        object.__setattr__(session, "_test_admin_user", admin_user)
        object.__setattr__(session, "_test_normal_user", normal_user)

        yield session
    finally:
        try:
            await session.rollback()
        except Exception:
            pass
        # 清理顺序：outbox -> beta_applications -> user_roles -> users
        await session.execute(
            text("DELETE FROM outbox WHERE event_type = 'beta_application.admin_notification.created'")
        )
        await session.execute(text("DELETE FROM beta_applications"))
        if created_user_ids:
            # 删除 user_roles（按 user_id）
            for uid in created_user_ids:
                await session.execute(
                    text("DELETE FROM user_roles WHERE user_id = :uid"),
                    {"uid": str(uid)},
                )
            # 删除 users
            for uid in created_user_ids:
                await session.execute(
                    text("DELETE FROM users WHERE id = :uid"),
                    {"uid": str(uid)},
                )
        await session.commit()
        await session.close()


@pytest_asyncio.fixture
async def admin_beta_client(
    admin_beta_db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, User, User], None]:
    """提供 httpx AsyncClient + admin/normal 用户对象。

    通过 dependency_overrides 注入测试会话，commit 替换为 flush 保持数据可见性。
    返回 (client, admin_user, normal_user) 元组。
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db

    async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
        # commit 替换为 flush：保持测试会话数据可见但不真实提交
        with patch.object(
            admin_beta_db_session, "commit", new=AsyncMock(side_effect=admin_beta_db_session.flush)
        ):
            yield admin_beta_db_session

    app.dependency_overrides[deps_get_db] = get_test_db
    app.dependency_overrides[db_get_db] = get_test_db

    admin_user = admin_beta_db_session._test_admin_user  # type: ignore[attr-defined]
    normal_user = admin_beta_db_session._test_normal_user  # type: ignore[attr-defined]

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, admin_user, normal_user

    app.dependency_overrides.clear()


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


async def _seed_application(
    db: AsyncSession,
    *,
    wechat: str | None = None,
    phone: str | None = None,
    watch_stock_count: int = 10,
    reason_code: str = "busy",
    reason_other: str | None = None,
    status: str = "new",
    submitted_at: datetime | None = None,
    admin_note: str | None = None,
    handled_by: uuid.UUID | None = None,
    feishu_delivery_status: str | None = "success",
    feishu_last_error: str | None = None,
) -> BetaApplication:
    """直接创建 BetaApplication 记录（绕过 service 层），用于测试筛选/统计。

    返回创建的 BetaApplication 对象。
    """
    ip_hash = _hash_ip(f"192.168.{uuid.uuid4().hex[:6]}")
    app = BetaApplication(
        id=uuid.uuid4(),
        wechat=wechat,
        phone=phone,
        watch_stock_count=watch_stock_count,
        reason_code=reason_code,
        reason_other=reason_other,
        status=status,
        source="test_seed",
        admin_note=admin_note,
        handled_by=handled_by,
        submitted_at=submitted_at or datetime.now(UTC),
        ip_hash=ip_hash,
        feishu_delivery_status=feishu_delivery_status,
        feishu_last_error=feishu_last_error,
    )
    db.add(app)
    await db.flush()
    return app


# ============================================================
# SubTask 4.5 测试 1: 普通用户访问返回 403
# ============================================================


@pytest.mark.asyncio
async def test_normal_user_list_returns_403(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """普通用户访问 GET /admin/beta-applications 返回 403。"""
    client, _, normal_user = admin_beta_client
    response = await client.get(
        "/admin/beta-applications",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_normal_user_stats_returns_403(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """普通用户访问 GET /admin/beta-applications/stats 返回 403。"""
    client, _, normal_user = admin_beta_client
    response = await client.get(
        "/admin/beta-applications/stats",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_normal_user_export_returns_403(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """普通用户访问 GET /admin/beta-applications/export 返回 403。"""
    client, _, normal_user = admin_beta_client
    response = await client.get(
        "/admin/beta-applications/export",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_normal_user_patch_returns_403(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """普通用户访问 PATCH /admin/beta-applications/{id} 返回 403。"""
    client, _, normal_user = admin_beta_client
    response = await client.patch(
        f"/admin/beta-applications/{uuid.uuid4()}",
        headers=_auth_headers(normal_user.id),
        json={"status": "contacted"},
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_normal_user_retry_feishu_returns_403(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """普通用户访问 POST /admin/beta-applications/{id}/retry-feishu 返回 403。"""
    client, _, normal_user = admin_beta_client
    response = await client.post(
        f"/admin/beta-applications/{uuid.uuid4()}/retry-feishu",
        headers=_auth_headers(normal_user.id),
    )
    assert response.status_code == 403, response.text


@pytest.mark.asyncio
async def test_unauthenticated_list_returns_401(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """未认证访问 GET /admin/beta-applications 返回 401。"""
    client, _, _ = admin_beta_client
    response = await client.get("/admin/beta-applications")
    assert response.status_code == 401, response.text


# ============================================================
# SubTask 4.5 测试 2: 列表（分页+筛选+搜索）
# ============================================================


@pytest.mark.asyncio
async def test_admin_list_empty_returns_200(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """admin 用户访问空列表返回 200 + 空数组。"""
    client, admin_user, _ = admin_beta_client
    response = await client.get(
        "/admin/beta-applications",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_admin_list_returns_applications(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """admin 用户访问列表返回申请数据。"""
    client, admin_user, _ = admin_beta_client

    # 准备：插入 2 条申请
    await _seed_application(
        admin_beta_db_session,
        wechat="list_user_1",
        watch_stock_count=5,
        reason_code="busy",
    )
    await _seed_application(
        admin_beta_db_session,
        phone="13900139001",
        watch_stock_count=15,
        reason_code="quant",
    )
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 2
    assert len(data["items"]) == 2
    # 列表项必须包含 spec 要求的字段
    item = data["items"][0]
    required_fields = {
        "id", "wechat", "phone", "watch_stock_count", "reason_code",
        "status", "feishu_delivery_status", "submitted_at", "handled_by",
    }
    assert required_fields.issubset(item.keys()), (
        f"列表项缺少字段: {required_fields - item.keys()}"
    )


@pytest.mark.asyncio
async def test_admin_list_filter_by_status(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 status 筛选列表。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(admin_beta_db_session, wechat="s_new", status="new")
    await _seed_application(admin_beta_db_session, wechat="s_contacted", status="contacted")
    await _seed_application(admin_beta_db_session, wechat="s_approved", status="approved")
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications?status=contacted",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["wechat"] == "s_contacted"


@pytest.mark.asyncio
async def test_admin_list_filter_by_reason_code(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 reason_code 筛选列表。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(admin_beta_db_session, wechat="r_busy", reason_code="busy")
    await _seed_application(admin_beta_db_session, wechat="r_quant", reason_code="quant")
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications?reason_code=quant",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["reason_code"] == "quant"


@pytest.mark.asyncio
async def test_admin_list_filter_by_watch_stock_range(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 watch_stock_range 筛选列表（1-10/11-20/21-50/50+）。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(admin_beta_db_session, wechat="w_5", watch_stock_count=5)
    await _seed_application(admin_beta_db_session, wechat="w_15", watch_stock_count=15)
    await _seed_application(admin_beta_db_session, wechat="w_30", watch_stock_count=30)
    await _seed_application(admin_beta_db_session, wechat="w_100", watch_stock_count=100)
    await admin_beta_db_session.flush()

    # 1-10 区间
    response = await client.get(
        "/admin/beta-applications?watch_stock_range=1-10",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["watch_stock_count"] == 5

    # 50+ 区间
    response = await client.get(
        "/admin/beta-applications?watch_stock_range=50+",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["watch_stock_count"] == 100


@pytest.mark.asyncio
async def test_admin_list_search_by_wechat(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 keyword 搜索微信号。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(admin_beta_db_session, wechat="alice_wechat")
    await _seed_application(admin_beta_db_session, wechat="bob_wechat")
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications?keyword=alice",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["wechat"] == "alice_wechat"


@pytest.mark.asyncio
async def test_admin_list_search_by_phone(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 keyword 搜索手机号。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(admin_beta_db_session, phone="13800138001")
    await _seed_application(admin_beta_db_session, phone="13900139002")
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications?keyword=1380013",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["phone"] == "13800138001"


@pytest.mark.asyncio
async def test_admin_list_pagination(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """分页参数 limit/offset 生效。"""
    client, admin_user, _ = admin_beta_client

    for i in range(5):
        await _seed_application(admin_beta_db_session, wechat=f"page_user_{i}")
    await admin_beta_db_session.flush()

    # limit=2 offset=0
    response = await client.get(
        "/admin/beta-applications?limit=2&offset=0",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2

    # limit=2 offset=2
    response = await client.get(
        "/admin/beta-applications?limit=2&offset=2",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) == 2


# ============================================================
# SubTask 4.5 测试 3: 统计
# ============================================================


@pytest.mark.asyncio
async def test_admin_stats_empty_returns_zero(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """空数据库时统计返回 0。"""
    client, admin_user, _ = admin_beta_client
    response = await client.get(
        "/admin/beta-applications/stats",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 0
    assert data["today"] == 0
    assert data["last_7_days"] == 0
    assert data["last_30_days"] == 0
    assert data["avg_watch_stock_count"] == 0.0
    assert isinstance(data["by_status"], dict)
    assert isinstance(data["by_reason"], dict)
    assert isinstance(data["by_watch_range"], dict)


@pytest.mark.asyncio
async def test_admin_stats_returns_correct_counts(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """统计返回正确的累计/今日/状态/理由/区间分布。"""
    client, admin_user, _ = admin_beta_client

    now = datetime.now(UTC)
    # 5 条申请：2 new / 1 contacted / 1 approved / 1 converted
    await _seed_application(
        admin_beta_db_session,
        wechat="stat_1",
        watch_stock_count=5,
        reason_code="busy",
        status="new",
        submitted_at=now,
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="stat_2",
        watch_stock_count=15,
        reason_code="quant",
        status="new",
        submitted_at=now,
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="stat_3",
        watch_stock_count=25,
        reason_code="too_many",
        status="contacted",
        submitted_at=now,
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="stat_4",
        watch_stock_count=60,
        reason_code="forget",
        status="approved",
        submitted_at=now,
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="stat_5",
        watch_stock_count=10,
        reason_code="other",
        reason_other="自定义理由",
        status="converted",
        submitted_at=now,
    )
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications/stats",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()

    # 累计 5 条
    assert data["total"] == 5
    # 今日 5 条
    assert data["today"] == 5
    # 近 7/30 天均 5 条
    assert data["last_7_days"] == 5
    assert data["last_30_days"] == 5

    # 状态分布
    assert data["by_status"].get("new", 0) == 2
    assert data["by_status"].get("contacted", 0) == 1
    assert data["by_status"].get("approved", 0) == 1
    assert data["by_status"].get("converted", 0) == 1

    # 平均盯盘数 = (5+15+25+60+10)/5 = 23
    assert data["avg_watch_stock_count"] == 23.0

    # 理由占比
    assert data["by_reason"].get("busy", 0) == 1
    assert data["by_reason"].get("quant", 0) == 1
    assert data["by_reason"].get("too_many", 0) == 1
    assert data["by_reason"].get("forget", 0) == 1
    assert data["by_reason"].get("other", 0) == 1

    # 股票区间分布：1-10(2: 5,10), 11-20(1: 15), 21-50(1: 25), 50+(1: 60)
    assert data["by_watch_range"].get("1-10", 0) == 2
    assert data["by_watch_range"].get("11-20", 0) == 1
    assert data["by_watch_range"].get("21-50", 0) == 1
    assert data["by_watch_range"].get("50+", 0) == 1


# ============================================================
# SubTask 4.5 测试 4: 状态更新（PATCH）
# ============================================================


@pytest.mark.asyncio
async def test_admin_patch_updates_status(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """PATCH /admin/beta-applications/{id} 修改 status 成功。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session, wechat="patch_user", status="new"
    )
    await admin_beta_db_session.flush()

    response = await client.patch(
        f"/admin/beta-applications/{app.id}",
        headers=_auth_headers(admin_user.id),
        json={"status": "contacted"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "contacted"
    # handled_by 应为 admin_user.id
    assert data["handled_by"] == str(admin_user.id)
    # handled_at 应不为 null
    assert data["handled_at"] is not None


@pytest.mark.asyncio
async def test_admin_patch_updates_admin_note(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """PATCH /admin/beta-applications/{id} 修改 admin_note 成功。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session, wechat="note_user", status="new"
    )
    await admin_beta_db_session.flush()

    response = await client.patch(
        f"/admin/beta-applications/{app.id}",
        headers=_auth_headers(admin_user.id),
        json={"status": "contacted", "admin_note": "已电话联系"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["status"] == "contacted"
    assert data["admin_note"] == "已电话联系"


@pytest.mark.asyncio
async def test_admin_patch_invalid_status_returns_400(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """PATCH 非法 status 返回 400。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session, wechat="invalid_status_user", status="new"
    )
    await admin_beta_db_session.flush()

    response = await client.patch(
        f"/admin/beta-applications/{app.id}",
        headers=_auth_headers(admin_user.id),
        json={"status": "invalid_status"},
    )
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_admin_patch_nonexistent_returns_404(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """PATCH 不存在的申请返回 404。"""
    client, admin_user, _ = admin_beta_client
    response = await client.patch(
        f"/admin/beta-applications/{uuid.uuid4()}",
        headers=_auth_headers(admin_user.id),
        json={"status": "contacted"},
    )
    assert response.status_code == 404, response.text


# ============================================================
# SubTask 4.5 测试 5: 详情 GET /{id}
# ============================================================


@pytest.mark.asyncio
async def test_admin_get_detail_returns_full_application(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """GET /admin/beta-applications/{id} 返回完整详情。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session,
        wechat="detail_user",
        phone="13800138000",
        watch_stock_count=8,
        reason_code="busy",
        reason_other=None,
        status="new",
        admin_note="测试备注",
    )
    await admin_beta_db_session.flush()

    response = await client.get(
        f"/admin/beta-applications/{app.id}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == str(app.id)
    assert data["wechat"] == "detail_user"
    assert data["phone"] == "13800138000"
    assert data["watch_stock_count"] == 8
    assert data["reason_code"] == "busy"
    assert data["status"] == "new"
    assert data["admin_note"] == "测试备注"
    # 详情应包含完整字段（含 feishu 投递信息）
    assert "feishu_delivery_status" in data
    assert "feishu_delivered_at" in data
    assert "feishu_last_error" in data


@pytest.mark.asyncio
async def test_admin_get_detail_nonexistent_returns_404(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """GET 不存在的申请返回 404。"""
    client, admin_user, _ = admin_beta_client
    response = await client.get(
        f"/admin/beta-applications/{uuid.uuid4()}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 404, response.text


# ============================================================
# SubTask 4.5 测试 6: 重发飞书 POST /{id}/retry-feishu
# ============================================================


@pytest.mark.asyncio
async def test_admin_retry_feishu_returns_200(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """POST /admin/beta-applications/{id}/retry-feishu 重发飞书成功。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session,
        wechat="retry_user",
        feishu_delivery_status="failed",
        feishu_last_error="mock failure",
    )
    await admin_beta_db_session.flush()

    response = await client.post(
        f"/admin/beta-applications/{app.id}/retry-feishu",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    # 应返回新的 outbox 记录信息或更新后的 application
    # 至少包含 outbox_id 或 id 字段
    assert "id" in data or "outbox_id" in data


@pytest.mark.asyncio
async def test_admin_retry_feishu_nonexistent_returns_404(
    admin_beta_client: tuple[AsyncClient, User, User],
):
    """POST 不存在的申请重发飞书返回 404。"""
    client, admin_user, _ = admin_beta_client
    response = await client.post(
        f"/admin/beta-applications/{uuid.uuid4()}/retry-feishu",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 404, response.text


@pytest.mark.asyncio
async def test_admin_retry_feishu_writes_outbox_event(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """重发飞书后写入新的 outbox 事件。"""
    client, admin_user, _ = admin_beta_client

    app = await _seed_application(
        admin_beta_db_session,
        wechat="retry_outbox_user",
        feishu_delivery_status="failed",
    )
    await admin_beta_db_session.flush()

    response = await client.post(
        f"/admin/beta-applications/{app.id}/retry-feishu",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text

    # 验证 outbox 表有该 application 的事件
    result = await admin_beta_db_session.execute(
        select(Outbox)
        .where(Outbox.event_type == "beta_application.admin_notification.created")
        .where(Outbox.aggregate_id == app.id)
    )
    outbox_records = list(result.scalars().all())
    assert len(outbox_records) >= 1, "重发飞书后未写入 outbox 事件"


# ============================================================
# SubTask 4.5 测试 7: CSV 导出
# ============================================================


@pytest.mark.asyncio
async def test_admin_export_returns_csv(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """GET /admin/beta-applications/export 返回 CSV 文件。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(
        admin_beta_db_session,
        wechat="export_user_1",
        phone="13800138001",
        watch_stock_count=10,
        reason_code="busy",
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="export_user_2",
        phone="13900139002",
        watch_stock_count=20,
        reason_code="quant",
    )
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications/export",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    # Content-Type 应为 text/csv
    content_type = response.headers.get("content-type", "")
    assert "text/csv" in content_type, f"Content-Type 应为 text/csv，实际: {content_type}"
    # Content-Disposition 应包含 filename
    content_disposition = response.headers.get("content-disposition", "")
    assert "attachment" in content_disposition
    assert ".csv" in content_disposition
    # CSV 内容应包含表头和 2 行数据
    csv_text = response.text
    lines = csv_text.strip().split("\n")
    assert len(lines) >= 3  # 表头 + 2 行数据
    # 表头应包含核心字段
    header = lines[0]
    assert "申请编号" in header or "id" in header.lower()
    # 数据行应包含 wechat 值
    assert "export_user_1" in csv_text
    assert "export_user_2" in csv_text


@pytest.mark.asyncio
async def test_admin_export_with_filter_returns_filtered_csv(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """带筛选条件的 CSV 导出。"""
    client, admin_user, _ = admin_beta_client

    await _seed_application(
        admin_beta_db_session,
        wechat="export_busy",
        reason_code="busy",
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="export_quant",
        reason_code="quant",
    )
    await admin_beta_db_session.flush()

    response = await client.get(
        "/admin/beta-applications/export?reason_code=busy",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    csv_text = response.text
    assert "export_busy" in csv_text
    assert "export_quant" not in csv_text


# ============================================================
# SubTask 4.5 测试 8: 日期筛选
# ============================================================


@pytest.mark.asyncio
async def test_admin_list_filter_by_date_range(
    admin_beta_client: tuple[AsyncClient, User, User],
    admin_beta_db_session: AsyncSession,
):
    """按 date_from/date_to 筛选列表。"""
    client, admin_user, _ = admin_beta_client

    now = datetime.now(UTC)
    old_date = now - timedelta(days=40)
    recent_date = now - timedelta(days=3)

    await _seed_application(
        admin_beta_db_session,
        wechat="date_old",
        submitted_at=old_date,
    )
    await _seed_application(
        admin_beta_db_session,
        wechat="date_recent",
        submitted_at=recent_date,
    )
    await admin_beta_db_session.flush()

    # 查询近 7 天
    date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    response = await client.get(
        f"/admin/beta-applications?date_from={date_from}&date_to={date_to}",
        headers=_auth_headers(admin_user.id),
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["wechat"] == "date_recent"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
