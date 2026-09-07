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

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1，仅连接 `bz_stock_verify_<sha>`。
复用 conftest 的 client / db_session fixture。所有写入经统一 db_session，结束 rollback。
禁止本地 self-host（治理约束），经正式 panji-verify 入口执行。
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.instrument import Instrument
from app.models.user import Role, User, UserRole
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
