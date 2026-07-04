"""GET /admin/worker-heartbeats 端点测试。

覆盖 5 个场景：
1. admin 用户可列出心跳（200 + items 数组 + 字段完整）
2. 非 admin 用户被拒（403）
3. 未认证用户被拒（401）
4. status 筛选生效（只返回 running）
5. health_state 分类正确（fresh/stale/stopped 三类各一条）

测试环境：PostgreSQL 测试库（conftest.py 的 db_session / client fixtures）
设计要点：
- 复用 conftest.py 的 admin_user / member_user / client / db_session
- 使用 timezone-aware UTC（与 worker.py 一致）
- heartbeat_at 用 datetime.now(UTC) - timedelta 构造，使 heartbeat_age_seconds 可预测
- 不修改生产代码，仅验证只读 API 行为
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.user import User
from app.models.worker_heartbeat import WorkerHeartbeat


def _auth_headers(user_id: uuid.UUID) -> dict[str, str]:
    """生成 Bearer token 认证头。"""
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _make_heartbeat(
    *,
    worker_name: str,
    status: str = "running",
    heartbeat_age_seconds: int = 0,
    instance_id: str | None = None,
    build_sha: str = "test-sha-001",
) -> WorkerHeartbeat:
    """构造一个 WorkerHeartbeat 记录，heartbeat_at = now - age。"""
    if instance_id is None:
        instance_id = f"test-host:{uuid.uuid4().hex[:8]}"
    hb_at = datetime.now(UTC) - timedelta(seconds=heartbeat_age_seconds)
    return WorkerHeartbeat(
        worker_name=worker_name,
        instance_id=instance_id,
        started_at=hb_at,
        heartbeat_at=hb_at,
        status=status,
        build_sha=build_sha,
    )


@pytest_asyncio.fixture
async def admin_user(user_factory: Callable[..., User]) -> User:
    """创建管理员测试用户。"""
    return await user_factory(
        email="admin-hb@example.com",
        password_hash=get_password_hash("admin-password-123"),
        roles=["admin"],
    )


@pytest_asyncio.fixture
async def member_user(user_factory: Callable[..., User]) -> User:
    """创建普通会员测试用户（无 admin 角色）。"""
    return await user_factory(
        email="member-hb@example.com",
        password_hash=get_password_hash("member-password-123"),
        roles=["member"],
    )


# ============================================================
# 场景 1：admin 可列出心跳
# ============================================================


@pytest.mark.asyncio
async def test_admin_can_list_heartbeats(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
) -> None:
    """admin 用户可列出 worker_heartbeats，响应字段完整。"""
    hb = _make_heartbeat(
        worker_name="bars_scheduler",
        heartbeat_age_seconds=30,
        build_sha="abc123",
    )
    db_session.add(hb)
    await db_session.flush()

    response = await client.get(
        "/admin/worker-heartbeats",
        headers=_auth_headers(admin_user.id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert data["limit"] == 100
    assert data["offset"] == 0

    items = data["items"]
    target = next(
        (it for it in items if it["worker_name"] == "bars_scheduler"),
        None,
    )
    assert target is not None, "应包含刚插入的 bars_scheduler 心跳"
    assert target["instance_id"] == hb.instance_id
    assert target["status"] == "running"
    assert target["build_sha"] == "abc123"
    assert target["heartbeat_age_seconds"] >= 30
    assert target["health_state"] == "fresh"
    assert "started_at" in target
    assert "heartbeat_at" in target
    assert "updated_at" in target


# ============================================================
# 场景 2：非 admin 被拒（403）
# ============================================================


@pytest.mark.asyncio
async def test_non_admin_forbidden(
    client: AsyncClient,
    member_user: User,
) -> None:
    """非 admin 用户访问 /admin/worker-heartbeats 应返回 403。"""
    response = await client.get(
        "/admin/worker-heartbeats",
        headers=_auth_headers(member_user.id),
    )
    assert response.status_code == 403


# ============================================================
# 场景 3：未认证被拒（401）
# ============================================================


@pytest.mark.asyncio
async def test_unauthenticated_unauthorized(client: AsyncClient) -> None:
    """未携带 token 访问 /admin/worker-heartbeats 应返回 401。"""
    response = await client.get("/admin/worker-heartbeats")
    assert response.status_code == 401


# ============================================================
# 场景 4：status 筛选生效
# ============================================================


@pytest.mark.asyncio
async def test_status_filter(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
) -> None:
    """status=running 筛选只返回 running 记录，排除 stopped。"""
    running_hb = _make_heartbeat(
        worker_name="strategy_batch",
        status="running",
        heartbeat_age_seconds=30,
    )
    stopped_hb = _make_heartbeat(
        worker_name="outbox",
        status="stopped",
        heartbeat_age_seconds=30,
        instance_id="test-host:stopped-001",
    )
    db_session.add_all([running_hb, stopped_hb])
    await db_session.flush()

    response = await client.get(
        "/admin/worker-heartbeats?status=running",
        headers=_auth_headers(admin_user.id),
    )

    assert response.status_code == 200
    data = response.json()
    worker_names = {it["worker_name"] for it in data["items"]}
    assert "strategy_batch" in worker_names, "running 记录应被包含"
    assert "outbox" not in worker_names, "stopped 记录应被排除"


# ============================================================
# 场景 5：health_state 分类正确
# ============================================================


@pytest.mark.asyncio
async def test_health_state_classification(
    client: AsyncClient,
    admin_user: User,
    db_session: AsyncSession,
) -> None:
    """health_state 三类分类正确：fresh / stale / stopped。

    构造三条记录：
    - fresh:   running + age=30s（< 120s 阈值）
    - stale:   running + age=300s（120s ≤ age < 600s）
    - stopped: stopped + age=30s
    """
    fresh_hb = _make_heartbeat(
        worker_name="fresh_worker",
        status="running",
        heartbeat_age_seconds=30,
        instance_id="host:fresh-001",
    )
    stale_hb = _make_heartbeat(
        worker_name="stale_worker",
        status="running",
        heartbeat_age_seconds=300,
        instance_id="host:stale-001",
    )
    stopped_hb = _make_heartbeat(
        worker_name="stopped_worker",
        status="stopped",
        heartbeat_age_seconds=30,
        instance_id="host:stopped-001",
    )
    db_session.add_all([fresh_hb, stale_hb, stopped_hb])
    await db_session.flush()

    response = await client.get(
        "/admin/worker-heartbeats?limit=200",
        headers=_auth_headers(admin_user.id),
    )

    assert response.status_code == 200
    items = response.json()["items"]

    fresh_item = next(it for it in items if it["worker_name"] == "fresh_worker")
    stale_item = next(it for it in items if it["worker_name"] == "stale_worker")
    stopped_item = next(it for it in items if it["worker_name"] == "stopped_worker")

    assert fresh_item["health_state"] == "fresh", "age=30s running 应为 fresh"
    assert fresh_item["heartbeat_age_seconds"] >= 30

    assert stale_item["health_state"] == "stale", "age=300s running 应为 stale"
    assert stale_item["heartbeat_age_seconds"] >= 300

    assert stopped_item["health_state"] == "stopped", "stopped 状态应为 stopped"
