"""capture token 隔离测试（Phase 3 Task 3.3 + Phase C Task C.11.1）。

安全约束：
- capture token 是短期截图模式令牌（TTL 通常 300 秒），仅通过 URL query parameter
  或 Authorization header 传给 /api/v1/capture/* 端点，不经过 get_current_user。
- access token 是常规 API 认证令牌，通过 Authorization: Bearer <token> 传递。
- Capture Token 必须校验 type=capture + scope=stock_detail_capture（advice.md 第六节）。

本测试验证：
1. capture token 通过 Authorization header 访问 GET /me 应返回 401（隔离）
2. access token 通过 Authorization header 访问 GET /me 应返回 200（正常）
3. capture token 通过 Authorization header 访问写端点 POST /me 相关也应返回 401
   （此处以 GET /me/access 覆盖，证明隔离对所有依赖 get_current_user 的端点生效）
4. capture token 访问 /instruments/{id} 返回 401（C.11.1 扩展）
5. capture token 访问 /watchlist 返回 401（C.11.1 扩展）
6. access token 访问 /api/v1/capture/stocks/{id}/snapshot 返回 401（C.11.1 扩展）
7. capture token instrument_id 与 path 不匹配返回 403（C.11.1 扩展）
8. 缺失 token 返回 401（C.11.1 扩展）

测试策略：
- 使用 conftest 的 db_session fixture（PostgreSQL 测试库 bz_stock_test）
- 通过 dependency_overrides 注入测试 session 到 app
- 使用 ASGITransport + AsyncClient 调用真实 HTTP 端点
- 复用 test_me_access.py 的辅助函数模式（admin 用户创建）
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, create_capture_token, get_password_hash
from app.main import app
from app.models.user import Role, User, UserRole
from tests.conftest import make_asgi_transport


async def _ensure_role(db: AsyncSession, name: str) -> Role:
    """确保角色存在并返回。"""
    result = await db.execute(select(Role).where(Role.name == name))
    role = result.scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _create_active_user(db: AsyncSession) -> User:
    """创建 active 状态的普通用户（member 角色），用于 token 隔离测试。"""
    now = datetime.now(UTC)
    user = User(
        id=uuid.uuid4(),
        email=f"isolation_{uuid.uuid4().hex[:8]}@test.com",
        password_hash=get_password_hash("password-12345"),
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    user_role = await _ensure_role(db, "member")
    db.add(UserRole(user_id=user.id, role_id=user_role.id))
    await db.flush()
    return user


@pytest_asyncio.fixture
async def isolation_client(
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

    transport = make_asgi_transport(app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, db_session

    app.dependency_overrides.clear()


def _access_token_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 access token 的 Bearer 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _capture_token_headers(
    user_id: uuid.UUID,
    instrument_id: uuid.UUID | str | None = None,
    scope: str = "stock_detail_capture",
) -> dict[str, str]:
    """生成 capture token 的 Bearer 认证头（携带 scope/instrument_id/user_id）。

    [Capture] - 描述: stock_detail 链路必须传 scope=stock_detail_capture + instrument_id
    """
    inst_str = str(instrument_id) if instrument_id else "evt-isolation-test"
    token = create_capture_token(
        subject=str(user_id),
        event_id=inst_str,
        expires_delta=timedelta(minutes=5),
        scope=scope,
        instrument_id=inst_str,
        user_id=str(user_id),
    )
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# capture token 隔离测试（原有 + C.11.1 扩展）
# ============================================================


@pytest.mark.asyncio
async def test_capture_token_rejected_for_general_api(
    isolation_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """capture token 通过 Authorization header 访问 GET /me 应返回 401。

    [Security] - 描述: capture token 仅用于截图场景（URL query param），
    不得通过 Authorization header 访问常规 API 端点
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get("/me", headers=_capture_token_headers(user.id))

    assert resp.status_code == 401
    assert "token 类型错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_access_token_accepted_for_general_api(
    isolation_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """access token 通过 Authorization header 访问 GET /me 应返回 200。

    [Security] - 描述: access token 是常规 API 认证令牌，应正常通过 get_current_user
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get("/me", headers=_access_token_headers(user.id))

    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


@pytest.mark.asyncio
async def test_capture_token_rejected_for_access_endpoint(
    isolation_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """capture token 通过 Authorization header 访问 GET /me/access 也应返回 401。

    [Security] - 描述: 隔离对所有依赖 get_current_user 的端点生效，不只是 /me
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get("/me/access", headers=_capture_token_headers(user.id))

    assert resp.status_code == 401
    assert "token 类型错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_capture_token_rejected_for_stock_memos_api(
    isolation_client: tuple[AsyncClient, AsyncSession],
    test_instrument,
) -> None:
    """capture token 访问 GET /stock-memos 应返回 401（C.11.1 扩展）。

    [Security] - 描述: capture token 不能访问普通用户 API（/stock-memos 走 get_current_active_user）
    注：/instruments/{id} 是公开端点（无认证依赖），capture token 访问返回 200 属正常行为，
    故改用 /stock-memos 验证隔离。
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get(
        f"/instruments/{test_instrument.id}/memo",
        headers=_capture_token_headers(user.id),
    )

    assert resp.status_code == 401
    assert "token 类型错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_capture_token_rejected_for_watchlist_api(
    isolation_client: tuple[AsyncClient, AsyncSession],
) -> None:
    """capture token 访问 GET /watchlist 应返回 401（C.11.1 扩展）。

    [Security] - 描述: capture token 不能访问普通用户 API（/watchlist 走 get_current_user）
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get("/watchlist", headers=_capture_token_headers(user.id))

    assert resp.status_code == 401
    assert "token 类型错误" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_access_token_rejected_for_capture_snapshot(
    isolation_client: tuple[AsyncClient, AsyncSession],
    test_instrument,
) -> None:
    """access token 访问 /api/v1/capture/stocks/{id}/snapshot 应返回 401（C.11.1 扩展）。

    [Security] - 描述: Capture API 只接受 capture token，普通 access token 被拒绝
    （advice.md 第十节硬规则：Capture Token 只能访问 Capture API，反向也成立）
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    resp = await client.get(
        f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
        headers=_access_token_headers(user.id),
    )

    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert "token 类型错误" in detail or "需要 capture token" in detail, f"detail={detail}"


@pytest.mark.asyncio
async def test_capture_token_instrument_id_mismatch_returns_403(
    isolation_client: tuple[AsyncClient, AsyncSession],
    test_instrument,
) -> None:
    """capture token instrument_id 与 path 不匹配应返回 403（C.11.1 扩展）。

    [Security] - 描述: Capture Token 中的 instrument_id 必须与 path 一致（防越权）
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    # token 中的 instrument_id 与 path 不同
    other_instrument_id = uuid.uuid4()
    headers = _capture_token_headers(user.id, instrument_id=other_instrument_id)

    resp = await client.get(
        f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
        headers=headers,
    )

    assert resp.status_code == 403
    assert "不匹配" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_capture_token_missing_returns_401(
    isolation_client: tuple[AsyncClient, AsyncSession],
    test_instrument,
) -> None:
    """缺失 Capture Token 应返回 401（C.11.1 扩展）。

    [Security] - 描述: Capture API 必须携带 token，缺失时拒绝访问
    """
    client, db = isolation_client

    resp = await client.get(
        f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
    )

    assert resp.status_code == 401
    assert "Capture Token" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_capture_token_scope_mismatch_returns_401(
    isolation_client: tuple[AsyncClient, AsyncSession],
    test_instrument,
) -> None:
    """capture token scope 不是 stock_detail_capture 应返回 401（C.11.1 扩展）。

    [Security] - 描述: scope 校验确保 token 仅用于指定场景（advice.md 第六节硬规则）
    """
    client, db = isolation_client
    user = await _create_active_user(db)
    await db.flush()

    # 故意使用错误的 scope
    headers = _capture_token_headers(
        user.id, instrument_id=test_instrument.id, scope="wrong_scope"
    )

    resp = await client.get(
        f"/api/v1/capture/stocks/{test_instrument.id}/snapshot",
        headers=headers,
    )

    assert resp.status_code == 401
    assert "scope" in resp.json()["detail"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
