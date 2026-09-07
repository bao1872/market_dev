"""P0 权限矩阵测试 — self_selection → market 越权泄露闭合（Capability Boundary）。

覆盖修复（仅 P0 Capability Boundary，不含 C10 Plan→Authorization / C2 Quota cleanup）：
- C7：/market/stocks、/market/export 作用域感知守卫（scope=market→market_data；scope=watchlist→self_selection）
- C11：/bars、/quote、chart-snapshot、indicators、structural-factors、temporal-features
       全部 require_capability("market_data")（匿名→401/403，self_selection-only→403）

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1，仅连接 `bz_stock_verify_<sha>`。
使用 conftest 的 `client` / `db_session` fixture（自动 override get_db 复用同一 db_session，
不逃逸事务）。所有写入经统一 db_session，测试结束外层事务 rollback。
禁止本地 self-host 运行（治理约束），经正式 panji-verify 入口执行。

Case 矩阵（仅 P0）：
- A: admin → 全市场/自选/K线/实时报价 全部 200
- B: market_data 用户 → 同上 200
- C: self_selection-only 用户 → scope=market 403；scope=watchlist 200；/bars 403；/quote 403
- D: 未登录（匿名）→ 401
- F: 直接 API bypass（匿名）→ /bars 401、/quote 401（匿名泄露闭合）
- H: 权限撤销（admin_revoke）→ 立即 403

注意：
- 不覆盖 C10（legacy plan→market_data 推导）、C2（quota fail-closed）——这两者属独立 change set，
  本文件不得混入。
- self_selection-only 用户通过「显式 UserCapability（仅 self_selection）」构造，
  而非 legacy plan fallback，确保 C10 回退后 scope=market 仍 403。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, get_password_hash
from app.models.instrument import Instrument
from app.models.user import Role, User, UserRole
from app.models.user_capability import UserCapability
from app.services.access_control_service import AccessContext
from app.services.subscription_service import generate_invite_codes, register_with_invite_code

pytestmark = pytest.mark.postgres

# 测试数据唯一前缀（结束必须无残留）
_TEST_EMAIL_PREFIX = "p0-matrix-"


async def _admin_user(db: AsyncSession) -> User:
    """创建 admin 用户（savepoint 内，结束 rollback）。"""
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


async def _register_with_capabilities(
    db: AsyncSession, email: str, caps: list[dict]
) -> User:
    """用显式 capabilities 生成邀请码并注册，返回 user（仅显式 capability 行）。"""
    created_by = await _admin_user(db)
    codes = await generate_invite_codes(
        db=db, count=1, note="p0-matrix", capabilities=caps, created_by=created_by.id
    )
    code = codes[0][1]
    user, _subscription = await register_with_invite_code(
        db, email=email, raw_invite_code=code, password="test-pass-123"
    )
    return user


async def _create_instrument(db: AsyncSession) -> Instrument:
    """创建一只测试股票。"""
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
# 纯逻辑测试（作用域感知守卫，不依赖数据库）
# ============================================================


def _make_ctx(is_admin: bool, caps: dict[str, dict]) -> AccessContext:
    """构造最小 AccessContext 用于守卫逻辑单测。"""
    return AccessContext(
        user_id=str(uuid.uuid4()),
        account_status="active",
        roles=["admin"] if is_admin else ["member"],
        is_admin=is_admin,
        is_member=not is_admin,
        subscription_active=True,
        capabilities=caps,
        default_route="/forbidden",
    )


@pytest.mark.asyncio
async def test_market_stocks_guard_scope_aware_pure():
    """C7：作用域感知守卫纯逻辑验证（绕过 FastAPI Depends 直接调用内部 _check）。"""
    from app.api.market import require_market_stocks_capability

    cap_self = {"active": True, "expires_at": None, "watchlist_limit": 20}
    cap_market = {"active": True, "expires_at": None, "watchlist_limit": None}

    # scope=market + 仅 self_selection → 403
    guard = require_market_stocks_capability(scope="market")
    with pytest.raises(Exception) as exc:
        await guard(ctx=_make_ctx(False, {"self_selection": dict(cap_self)}))
    assert exc.value.status_code == 403

    # scope=watchlist + 仅 self_selection → 放行
    guard = require_market_stocks_capability(scope="watchlist")
    assert await guard(ctx=_make_ctx(False, {"self_selection": dict(cap_self)})) is not None

    # scope=market + market_data → 放行
    guard = require_market_stocks_capability(scope="market")
    assert await guard(ctx=_make_ctx(False, {"market_data": dict(cap_market)})) is not None

    # admin → 任意 scope 放行
    guard = require_market_stocks_capability(scope="market")
    assert await guard(ctx=_make_ctx(True, {})) is not None


# ============================================================
# 端到端测试（PostgreSQL，经 panji-verify 执行）
# ============================================================


@pytest.mark.asyncio
async def test_case_a_admin_full_market_access(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case A：admin 可访问全市场/自选/K线/实时报价。"""
    admin = await _admin_user(db_session)
    inst = await _create_instrument(db_session)
    headers = _auth(admin)

    assert (await client.get("/v1/market/stocks?scope=market", headers=headers)).status_code == 200
    assert (await client.get("/v1/market/stocks?scope=watchlist", headers=headers)).status_code == 200
    assert (await client.get(f"/v1/instruments/{inst.id}/bars", headers=headers)).status_code == 200
    assert (await client.get(f"/v1/instruments/{inst.id}/quote", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_case_b_market_data_user_access(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case B：market_data 用户可访问全市场/K线/实时报价。"""
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}b-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    inst = await _create_instrument(db_session)
    headers = _auth(user)

    assert (await client.get("/v1/market/stocks?scope=market", headers=headers)).status_code == 200
    assert (await client.get(f"/v1/instruments/{inst.id}/bars", headers=headers)).status_code == 200
    assert (await client.get(f"/v1/instruments/{inst.id}/quote", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_case_c_self_selection_only_denied_market(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case C：self_selection-only 用户 → 全市场 403；自选 200；/bars 403；/quote 403。

    这是本次 P0 的核心回归：self_selection 不得通过任何路径取得 market_data。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}c-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    inst = await _create_instrument(db_session)
    headers = _auth(user)

    # 全市场行情：必须 403（核心泄露闭合）
    r_market = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_market.status_code == 403, r_market.text
    # 自选列表：self_selection 允许（200 或参数校验）
    r_watch = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_watch.status_code in (200, 422), r_watch.text
    # K线 / 实时报价：必须 403（market_data 端点）
    r_bars = await client.get(f"/v1/instruments/{inst.id}/bars", headers=headers)
    assert r_bars.status_code == 403, r_bars.text
    r_quote = await client.get(f"/v1/instruments/{inst.id}/quote", headers=headers)
    assert r_quote.status_code == 403, r_quote.text


@pytest.mark.asyncio
async def test_case_d_anonymous_denied(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case D：未登录（匿名）访问行情端点 → 401。"""
    inst = await _create_instrument(db_session)

    assert (await client.get("/v1/market/stocks?scope=market")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/bars")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/quote")).status_code == 401


@pytest.mark.asyncio
async def test_case_f_anonymous_bars_quote_bypass_denied(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case F：直接 API bypass（匿名）→ /bars、/quote 401（匿名泄露闭合）。

    修复前 /bars、/quote 完全无认证（匿名可读完整 K 线/实时报价）；
    修复后 require_capability("market_data") → 匿名 401。
    """
    inst = await _create_instrument(db_session)

    r_bars = await client.get(f"/v1/instruments/{inst.id}/bars")
    assert r_bars.status_code == 401, r_bars.text
    r_quote = await client.get(f"/v1/instruments/{inst.id}/quote")
    assert r_quote.status_code == 401, r_quote.text


@pytest.mark.asyncio
async def test_case_h_revoke_immediately_denied(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case H：权限撤销（admin_revoke）→ 立即 403。

    撤销后 resolve_effective_access 将 source=admin_revoke 解析为 active=False，
    新请求（服务端每次 resolve）立即失效，无需等服务端重启/JWT 过期。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}h-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    headers = _auth(user)

    # 撤销前：自选可用
    r_before = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_before.status_code in (200, 422), r_before.text

    # 撤销 self_selection
    stmt = select(UserCapability).where(UserCapability.user_id == user.id)
    cap_row = (await db_session.execute(stmt)).scalars().first()
    assert cap_row is not None
    cap_row.source = "admin_revoke"
    cap_row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    # 撤销后：立即 403
    r_after = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_after.status_code == 403, r_after.text
