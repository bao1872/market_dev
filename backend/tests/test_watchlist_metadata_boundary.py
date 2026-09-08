"""自选能力边界测试 — monitor-status 按 self_selection 授权（Commit A 权限模型纠偏）。

背景（Commit A 纠偏 b22a2491→4c4f0fd3 的过度收紧）：
- /v1/watchlist/monitor-status 只暴露「当前用户自己的 active 自选集合」的 full metrics，
  universe 由端点内部以 UserWatchlistItem.user_id==current_user AND active.is_(True) 强制，
  不存在跨用户数据泄漏 → 按 PA-13 resource-scope 合同仅要求 self_selection 单权限，
  不再叠加 market_data（self_selection-only 用户应能正常使用自选盘中监控）。
- GET /v1/watchlist 保持 metadata-only summary（symbol/name/market/source/created_at），
  仅要求 self_selection，不暴露任何行情/策略指标。

覆盖矩阵（monitor-status 四态）：
- self_selection-only → 200（自选监控对其开放，只返回该用户自己的自选）
- market_data-only → 403（无 self_selection，端点不向其开放）
- both（self_selection + market_data）→ 200
- admin → 200（豁免）

覆盖矩阵（GET /v1/watchlist metadata-only）：
- self_selection-only → 200，且响应不含 price/change_pct/metrics/latest_event/BB/node/POC 等字段
- market_data-only → 403（GET /v1/watchlist 仍要求 self_selection）
- both → 200

覆盖矩阵（自选额度事务正确性 — Commit B1，见文件末节）：
- Q1 limit=3/active=3 → 第 4 只 409，final=3
- Q2 limit=3/active=3 → 恢复软删除 409，final=3
- Q3 额度降为 3 且 active=4 → 第 5 只 409，existing 4 只不自动删除
- Q4（真实并发）limit=3/active=2 → 两个独立 PG 事务并发加两只 → 1×201 + 1×409，final=3
- Q5 Add 已按旧额度 5 鉴权后管理员降额到 3 并提交 → 锁内重新 resolve → 409

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1，仅连接 `bz_stock_verify_<sha>`。
复用 conftest 的 client / db_session fixture。所有写入经统一 db_session，结束 rollback。
禁止本地 self-host（治理约束），经正式 panji-verify 入口执行。

Q4/Q5 例外：需要**真实数据库并发**，必须使用两个独立 AsyncSession + 两个独立
PostgreSQL transaction（conftest.client 把所有请求绑定到同一个 db_session，
不能证明 FOR UPDATE 生效），因此这两例用 TestAsyncSessionLocal 独立建连、
真实 commit 种子数据，并在用例结束时清理。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.instrument import Instrument
from app.models.invitation import InviteCode, InviteRedemption
from app.models.subscription import Subscription
from app.models.user import Role, User, UserRole
from app.models.user_capability import UserCapability
from app.models.watchlist import UserWatchlistItem
from app.services.subscription_service import generate_invite_codes, register_with_invite_code

pytestmark = pytest.mark.postgres

_TEST_EMAIL_PREFIX = "selfonly-boundary-"


async def _admin_user(db: AsyncSession) -> User:
    user = User(
        email=f"{_TEST_EMAIL_PREFIX}admin-{uuid.uuid4().hex[:8]}@test.local",
        password_hash=get_password_hash("test-pass-123"),
        status="active",
        timezone="Asia/Shanghai",
    )
    db.add(user)
    await db.flush()
    role = await db.scalar(select(Role).where(Role.name == "admin"))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    return user


async def _register_with_capabilities(db: AsyncSession, email: str, caps: list[dict]) -> User:
    """用显式 capabilities 生成邀请码并注册，返回 user（仅显式 capability 行）。"""
    created_by = await _admin_user(db)
    codes = await generate_invite_codes(
        db=db, count=1, note="selfonly-boundary", capabilities=caps, created_by=created_by.id
    )
    code = codes[0][1]
    user, _subscription = await register_with_invite_code(
        db, email=email, raw_invite_code=code, password="test-pass-123"
    )
    return user


async def _create_instrument(db: AsyncSession) -> Instrument:
    inst = Instrument(
        symbol=f"T{uuid.uuid4().hex[:6].upper()}",
        name="测试标的",
        market="SZ",
        status="active",
    )
    db.add(inst)
    await db.flush()
    return inst


def _auth(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


# ============================================================
# monitor-status 四态矩阵
# ============================================================


@pytest.mark.asyncio
async def test_monitor_status_self_selection_only_200(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """self_selection-only → monitor-status 200（Commit A：自选监控对 self_selection-only 开放）。

    该端点 universe 仅限当前用户 active 自选（端点内部强制），self_selection-only
    用户可正常使用自己的盘中监控，无需 market_data。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}self-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    r = await client.get("/v1/watchlist/monitor-status", headers=_auth(user))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_monitor_status_market_data_only_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """market_data-only → monitor-status 403（端点要求 self_selection）。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}market-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    r = await client.get("/v1/watchlist/monitor-status", headers=_auth(user))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_monitor_status_both_200(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """both（self_selection + market_data）→ monitor-status 200。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}both-{uuid.uuid4().hex[:8]}@test.local",
        [
            {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
            {"capability": "market_data", "months": 1},
        ],
    )
    r = await client.get("/v1/watchlist/monitor-status", headers=_auth(user))
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_monitor_status_admin_200(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """admin → monitor-status 200（豁免）。"""
    admin = await _admin_user(db_session)
    r = await client.get("/v1/watchlist/monitor-status", headers=_auth(admin))
    assert r.status_code == 200, r.text


# ============================================================
# GET /v1/watchlist metadata-only 边界
# ============================================================


@pytest.mark.asyncio
async def test_get_watchlist_self_selection_only_metadata_only(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """self_selection-only → GET /v1/watchlist 200，且响应仅 metadata（无行情/策略字段）。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wself-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    r = await client.get("/v1/watchlist", headers=_auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body and "total" in body
    # 空自选（未加任何自选）：items 为空，但仍证明端点可访问且 metadata 契约成立
    assert body["total"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_get_watchlist_metadata_shape_no_market_fields(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """GET /v1/watchlist 响应项不得包含任何行情/策略指标字段。

    通过加入一条自选，断言返回的 summary item 仅含 metadata 字段，
    且不含 price/change_pct/metrics/latest_event/BB/node/POC/first_pyramid 等。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wshape-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    inst = await _create_instrument(db_session)
    # 加入一条自选（走 POST /v1/watchlist，仅要求 self_selection）
    r_add = await client.post(
        "/v1/watchlist",
        json={"instrument_id": str(inst.id), "source": "manual"},
        headers=_auth(user),
    )
    assert r_add.status_code == 201, r_add.text

    r = await client.get("/v1/watchlist", headers=_auth(user))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]

    # metadata 字段必须存在
    for field in ("watchlist_item_id", "instrument_id", "symbol", "name", "market", "source", "created_at"):
        assert field in item, f"summary item 必须包含 {field}"

    # 禁止行情/策略指标字段
    forbidden = (
        "current_price", "previous_close", "change_pct", "metrics", "latest_event",
        "bb_upper", "bb_mid", "bb_lower", "upper_node_price", "lower_node_price",
        "position_0_1", "poc_price", "bars", "first_pyramid", "dsa_state", "monitor_status",
        "evaluation_status", "market_session", "calculation_status", "freshness_seconds",
        "last_bar_time", "source_bar_time", "updated_at", "retry_count", "error_code",
    )
    for field in forbidden:
        assert field not in item, f"summary item 禁止包含行情/策略字段 {field}"

    # metadata 内容正确
    assert item["instrument_id"] == str(inst.id)
    assert item["symbol"] == inst.symbol
    assert item["name"] == inst.name
    assert item["market"] == inst.market


@pytest.mark.asyncio
async def test_get_watchlist_market_data_only_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """market_data-only → GET /v1/watchlist 403（仍要求 self_selection）。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wmarket-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    r = await client.get("/v1/watchlist", headers=_auth(user))
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_get_watchlist_both_200(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """both → GET /v1/watchlist 200。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wboth-{uuid.uuid4().hex[:8]}@test.local",
        [
            {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
            {"capability": "market_data", "months": 1},
        ],
    )
    r = await client.get("/v1/watchlist", headers=_auth(user))
    assert r.status_code == 200, r.text


# ============================================================
# market_data-only 写入口 403（POST / DELETE 均要求 self_selection）
# ============================================================


@pytest.mark.asyncio
async def test_post_watchlist_market_data_only_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """market_data-only → POST /v1/watchlist 403（加自选仍要求 self_selection）。

    使用真实存在的 instrument，证明即使请求本身合法也被鉴权拒绝（403 先于业务），
    而非 404/400 之类的业务错误。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wpost-market-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    inst = await _create_instrument(db_session)
    r = await client.post(
        "/v1/watchlist",
        json={"instrument_id": str(inst.id), "source": "manual"},
        headers=_auth(user),
    )
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_delete_watchlist_market_data_only_403(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """market_data-only → DELETE /v1/watchlist/{instrument_id} 403（删自选仍要求 self_selection）。

    使用真实存在的 instrument，证明即使请求本身合法也被鉴权拒绝（403 先于业务）。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}wdel-market-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    inst = await _create_instrument(db_session)
    r = await client.delete(f"/v1/watchlist/{inst.id}", headers=_auth(user))
    assert r.status_code == 403, r.text


# ============================================================
# 自选额度（watchlist_limit）事务正确性 — Commit B1
#
# 缺陷：POST /v1/watchlist 原实现为「dependency 阶段先解析额度 → COUNT → INSERT」，
# COUNT 与 INSERT 之间没有任何数据库锁：
#
#     请求 A: COUNT=2 (limit=3) ┐
#     请求 B: COUNT=2 (limit=3) ├─ 两者都判定 2 < 3 → 都放行 → 最终 4 只
#     请求 A: INSERT            │
#     请求 B: INSERT            ┘
#
# 且 dependency 阶段读到的额度在真正写入时可能已被管理员下调（旧额度残留）。
#
# 修复后顺序（同一事务临界区内，禁止提前 commit）：
#     validate instrument
#           ↓
#     SELECT User ... FOR UPDATE   ← 与管理员 capability mutation 同一并发序列点
#           ↓
#     锁内重新 resolve capability + quota（不信任请求开始时缓存的额度）
#           ↓
#     check existing（active → 409）
#           ↓
#     COUNT active（锁内）
#           ↓
#     quota comparison（超限 → 409）
#           ↓
#     restore / INSERT
#           ↓
#     COMMIT（唯一一次）
# ============================================================


async def _post_watchlist(
    client: AsyncClient, headers: dict, instrument_id
) -> httpx.Response:
    """POST /v1/watchlist（加入自选）。"""
    return await client.post(
        "/v1/watchlist",
        json={"instrument_id": str(instrument_id), "source": "manual"},
        headers=headers,
    )


async def _active_count(db: AsyncSession, user_id) -> int:
    """当前用户 active 自选数量（直接查库，不依赖 API 返回）。"""
    result = await db.execute(
        select(func.count(UserWatchlistItem.id)).where(
            UserWatchlistItem.user_id == user_id,
            UserWatchlistItem.active.is_(True),
        )
    )
    return int(result.scalar_one() or 0)


@pytest.mark.asyncio
async def test_quota_q1_fourth_add_blocked_at_limit(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Q1: limit=3，active=3 → POST 第 4 只 → 409，final active = 3。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}q1-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 3}],
    )
    headers = _auth(user)
    instruments = [await _create_instrument(db_session) for _ in range(4)]

    for inst in instruments[:3]:
        r = await _post_watchlist(client, headers, inst.id)
        assert r.status_code == 201, r.text

    r4 = await _post_watchlist(client, headers, instruments[3].id)
    assert r4.status_code == 409, r4.text
    assert "监控数量已达上限" in r4.json()["detail"]

    # 不允许超额写入：最终仍为 3
    assert await _active_count(db_session, user.id) == 3


@pytest.mark.asyncio
async def test_quota_q2_restore_soft_deleted_blocked_at_limit(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Q2: limit=3，active=3 → 恢复软删除记录 → 409，final active = 3。

    restore 与 INSERT 同样计入额度（恢复后 active 数量 +1），必须在锁内校验。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}q2-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 3}],
    )
    headers = _auth(user)
    instruments = [await _create_instrument(db_session) for _ in range(4)]

    # A/B/C 三只 active（达到额度 3）
    for inst in instruments[:3]:
        r = await _post_watchlist(client, headers, inst.id)
        assert r.status_code == 201, r.text

    # 软删除 A → active=2
    r_del = await client.delete(f"/v1/watchlist/{instruments[0].id}", headers=headers)
    assert r_del.status_code == 204, r_del.text
    assert await _active_count(db_session, user.id) == 2

    # 加入 D → active=3（再次达到额度）
    r_d = await _post_watchlist(client, headers, instruments[3].id)
    assert r_d.status_code == 201, r_d.text
    assert await _active_count(db_session, user.id) == 3

    # 恢复 A → 409（恢复后将为 4 > 3）
    r_restore = await _post_watchlist(client, headers, instruments[0].id)
    assert r_restore.status_code == 409, r_restore.text
    assert "监控数量已达上限" in r_restore.json()["detail"]

    assert await _active_count(db_session, user.id) == 3


@pytest.mark.asyncio
async def test_quota_q3_lowered_limit_blocks_new_add_without_deleting(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Q3: 额度 5 → 管理员降为 3，此时 active=4 → POST 第 5 只 → 409，existing 4 只保留。

    降级不删除原则：已有 4 只不得被自动清理，只禁止新增。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}q3-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 5}],
    )
    headers = _auth(user)
    instruments = [await _create_instrument(db_session) for _ in range(5)]

    # 额度 5 下加入 4 只（4 < 5，允许）
    for inst in instruments[:4]:
        r = await _post_watchlist(client, headers, inst.id)
        assert r.status_code == 201, r.text
    assert await _active_count(db_session, user.id) == 4

    # 模拟管理员降额到 3（直接改 self_selection capability 行）
    caps = (
        await db_session.execute(
            select(UserCapability).where(
                UserCapability.user_id == user.id,
                UserCapability.capability == "self_selection",
            )
        )
    ).scalars().all()
    assert caps, "测试前提失败：注册未物化显式 self_selection capability 行"
    for cap in caps:
        cap.watchlist_limit = 3
    await db_session.flush()

    # 第 5 只 → 409（4 >= 3）
    r5 = await _post_watchlist(client, headers, instruments[4].id)
    assert r5.status_code == 409, r5.text
    assert "监控数量已达上限" in r5.json()["detail"]

    # 降级不删除：4 只仍然保留
    assert await _active_count(db_session, user.id) == 4


# ------------------------------------------------------------
# 真实并发基础设施（Q4 / Q5）
# ------------------------------------------------------------


async def _seed_quota_user_committed(limit: int, n_instruments: int, n_active: int) -> dict:
    """在**独立事务**中真实提交种子数据，供需要真实数据库并发的用例使用。

    conftest.db_session 的数据位于测试事务内，对其他连接不可见。真实并发要求两个
    独立 PG 连接都能看到同一份种子数据，因此这里用 TestAsyncSessionLocal 独立建连
    并真实 commit（测试结束由 _cleanup_quota_user 清理）。
    """
    from tests.conftest import TestAsyncSessionLocal

    async with TestAsyncSessionLocal() as s:
        admin = await _admin_user(s)
        codes = await generate_invite_codes(
            db=s,
            count=1,
            note="quota-b1",
            capabilities=[
                {"capability": "self_selection", "months": 1, "watchlist_limit": limit}
            ],
            created_by=admin.id,
        )
        email = f"{_TEST_EMAIL_PREFIX}qseed-{uuid.uuid4().hex[:8]}@test.local"
        user, _subscription = await register_with_invite_code(
            s, email=email, raw_invite_code=codes[0][1], password="test-pass-123"
        )
        instruments = [await _create_instrument(s) for _ in range(n_instruments)]
        await s.flush()
        for inst in instruments[:n_active]:
            s.add(
                UserWatchlistItem(
                    user_id=user.id,
                    instrument_id=inst.id,
                    source="manual",
                    active=True,
                )
            )
        await s.flush()
        await s.commit()
        return {
            "user_id": user.id,
            "admin_id": admin.id,
            "instrument_ids": [inst.id for inst in instruments],
        }


async def _cleanup_quota_user(seed: dict) -> None:
    """清理 _seed_quota_user_committed 真实提交的数据（按外键依赖顺序）。"""
    from tests.conftest import TestAsyncSessionLocal

    uid, aid = seed["user_id"], seed["admin_id"]
    async with TestAsyncSessionLocal() as s:
        try:
            for model, cond in (
                (UserWatchlistItem, UserWatchlistItem.user_id.in_([uid, aid])),
                (UserCapability, UserCapability.user_id.in_([uid, aid])),
                (UserRole, UserRole.user_id.in_([uid, aid])),
                (Subscription, Subscription.user_id.in_([uid, aid])),
                (InviteRedemption, InviteRedemption.user_id.in_([uid, aid])),
            ):
                await s.execute(delete(model).where(cond))
            await s.execute(delete(InviteCode).where(InviteCode.created_by == aid))
            await s.execute(delete(User).where(User.id.in_([uid, aid])))
            await s.execute(
                delete(Instrument).where(Instrument.id.in_(seed["instrument_ids"]))
            )
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def _fresh_session_client():
    """提供 get_db 覆盖为「每请求独立 AsyncSession / 独立 PG transaction」的客户端。

    与 conftest.client 的关键差异：conftest.client 把所有请求绑定到**同一个**
    db_session —— 两个并发请求共享一个 AsyncSession，根本不会争用数据库锁，
    因此**不能**用它证明 SELECT User ... FOR UPDATE 生效（会 false green）。

    本工厂让每个请求从 TestAsyncSessionLocal 取新 session：
      - 两个独立 AsyncSession
      - 两个独立 PostgreSQL transaction
      - 同一个 bz_stock_verify_<SHA>
    """
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db
    from app.main import app
    from tests.conftest import TestAsyncSessionLocal, make_asgi_transport

    async def _fresh_db():
        async with TestAsyncSessionLocal() as s:
            yield s

    saved = dict(app.dependency_overrides)
    app.dependency_overrides[deps_get_db] = _fresh_db
    app.dependency_overrides[db_get_db] = _fresh_db
    try:
        async with AsyncClient(
            transport=make_asgi_transport(app), base_url="http://test"
        ) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(saved)


@pytest.mark.asyncio
async def test_quota_q4_concurrent_add_two_sessions_one_wins() -> None:
    """Q4（最关键）: limit=3 / active=2 → 两个独立 PG 事务并发 POST 两只不同股票。

    必须一个 201、一个 409，且 final active = 3（不允许变成 4）。

    禁止用 `asyncio.gather(client.post(...), client.post(...))` 复用 conftest 共享的
    db_session —— 那不是真实 DB 并发（同一 AsyncSession / 同一 transaction），
    会 false green。本用例两个请求各自持有独立 PG 事务，直接证明 User 行锁生效。
    """
    from tests.conftest import TestAsyncSessionLocal

    seed = await _seed_quota_user_committed(limit=3, n_instruments=4, n_active=2)
    headers = {"Authorization": f"Bearer {create_access_token(str(seed['user_id']))}"}
    try:
        async with _fresh_session_client() as c:
            r_a, r_b = await asyncio.gather(
                _post_watchlist(c, headers, seed["instrument_ids"][2]),
                _post_watchlist(c, headers, seed["instrument_ids"][3]),
            )

        codes = sorted([r_a.status_code, r_b.status_code])
        assert codes == [201, 409], (
            f"并发结果异常: {codes} | A={r_a.text} | B={r_b.text}"
        )

        async with TestAsyncSessionLocal() as s:
            assert await _active_count(s, seed["user_id"]) == 3
    finally:
        await _cleanup_quota_user(seed)


@pytest.mark.asyncio
async def test_quota_q5_limit_re_resolved_inside_lock_after_admin_downgrade() -> None:
    """Q5: Add 已按旧额度 5 通过鉴权后，管理员降额到 3 并提交 → Add 必须在锁内读到 3。

    时序由 User 行锁保证（不依赖 sleep 的精确时长）：
      1. Add 请求开始，dependency 阶段解析 AccessContext（此时额度仍为 5）
      2. Add 执行到 SELECT User ... FOR UPDATE → 被管理员事务阻塞
      3. 管理员事务把 watchlist_limit 改为 3 并 COMMIT
      4. Add 获得行锁 → 锁内重新 resolve → 读到 3 → COUNT=4 → 409

    若实现仍信任 dependency 阶段缓存的额度（Commit B1 之前的旧行为），
    这里会拿着旧额度 5 判定 4 < 5 → 返回 201（错误）。
    """
    seed = await _seed_quota_user_committed(limit=5, n_instruments=5, n_active=4)
    headers = {"Authorization": f"Bearer {create_access_token(str(seed['user_id']))}"}
    admin_session = None
    try:
        from tests.conftest import TestAsyncSessionLocal

        admin_session = TestAsyncSessionLocal()
        # 1) 管理员事务先取 User 行锁（与 Add 同一并发序列点）
        await admin_session.execute(
            select(User).where(User.id == seed["user_id"]).with_for_update()
        )
        # 2) 锁内降额到 3（尚未提交，对 Add 不可见）
        caps = (
            await admin_session.execute(
                select(UserCapability).where(
                    UserCapability.user_id == seed["user_id"],
                    UserCapability.capability == "self_selection",
                )
            )
        ).scalars().all()
        assert caps, "测试前提失败：种子用户缺少显式 self_selection capability 行"
        for cap in caps:
            cap.watchlist_limit = 3
        await admin_session.flush()

        async with _fresh_session_client() as c:
            task = asyncio.create_task(
                _post_watchlist(c, headers, seed["instrument_ids"][4])
            )
            # 让 Add 跑到 FOR UPDATE 并阻塞在行锁上（此时 dependency 阶段已用旧额度 5 解析完）
            await asyncio.sleep(1.0)
            # 3) 管理员提交，释放行锁
            await admin_session.commit()
            resp = await task

        # 4) Add 必须在锁内重新解析到 3 → COUNT=4 → 409
        assert resp.status_code == 409, resp.text
        assert "监控数量已达上限" in resp.json()["detail"]
    finally:
        if admin_session is not None:
            await admin_session.close()
        await _cleanup_quota_user(seed)
