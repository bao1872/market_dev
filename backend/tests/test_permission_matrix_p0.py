"""P0 权限矩阵测试 — capability boundary + Commit A resource-scope 纠偏。

覆盖修复（Commit A 权限模型纠偏 + P0 Capability Boundary，不含 C10 Plan→Authorization / C2 Quota cleanup）：
- C7：/market/stocks、/market/export 作用域感知授权
  - scope=market → market_data（self_selection-only 严禁隐式获得全市场）
  - scope=watchlist → self_selection（Commit A：universe 由 market_stocks_service 以
    current user + active UserWatchlistItem INNER JOIN 强制，self-only 用户可看自己自选集合的
    完整行情；不再要求 market_data）
  - export 授权 scope 与执行 scope 同源（body.scope），禁止 query scope 授权 body scope（P0-4 SSOT）
- C11：/bars、/quote、chart-snapshot、indicators、structural-factors、temporal-features、
       /instruments/{id}/events 接统一 instrument resource guard（Commit A A3/A4）：
  - 匿名 → 401；无 self_selection 也无 market_data（research_replay-only / 无权限）→ 403
  - market_data → 任意 instrument（404/400 业务证据）
  - self_selection-only → 仅本人 active watchlist 内 instrument（resource-scope；未自选 → 403）

运行模式：PANJI_REMOTE_VERIFY_DB_TEST=1，仅连接 `bz_stock_verify_<sha>`。
使用 conftest 的 `client` / `db_session` fixture（自动 override get_db 复用同一 db_session，
不逃逸事务）。所有写入经统一 db_session，测试结束外层事务 rollback。
禁止本地 self-host 运行（治理约束），经正式 panji-verify 入口执行。

=== 正式 evidence 边界（AGENTS.md §9：禁止 mock / fallback / stale data 作为 formal evidence）===

本文件是 panji-verify 的 required formal evidence，因此：
- 禁止 monkeypatch 行情 provider（_is_quote_realtime_session / pytdx / Redis 缓存）。
- 禁止人工构造 BarDaily 强制 /quote 走 DB 日线 fallback。
- 禁止声称「/bars 空数据稳定 200」——/bars 默认 include_realtime=True，日线路径在
  DB miss 时触发 fetch_daily_bars（外部 pytdx），verification_replay 下 fail-closed，
  结果依赖外部网络/交易时段，不可作为确定性 evidence。

权限测试真正要证明的是「capability → authorization」，而非「行情 provider → 成功返回行情」。
因此：
- /quote 正向授权用「确定不存在的 instrument UUID」→ 404（guard 通过后业务逻辑执行 →
  instrument lookup → 404），精确证明 authorization PASS 且不依赖行情数据。
- /bars 及详情端点正向授权用「非法 timeframe / primary_timeframe」→ 400（guard 通过后
  endpoint 本地参数校验抛 400），证明 guard 已通过、业务校验已触达，不碰行情 provider。
- self_selection-only 的 resource-scope 正向证据：先把 instrument 加入本人 active watchlist
  （POST /v1/watchlist），再以非法参数访问详情端点 → 400（guard 通过 + watchlist membership）；
  未加入自选的 instrument → 403。确定性成立、不依赖行情数据。
- 不硬要求 /bars 的 positive 200（同上原因）。
- /market/stocks、/export 的空数据 200 是纯 DB 路径（不碰外部行情 provider），确定性成立。

Case 矩阵：
- 纯逻辑：_authorize_market_scope 授权矩阵（admin 豁免 / market / watchlist / neither）
- A: admin → /market/stocks market/watchlist 200；/quote 不存在 UUID 404（admin 豁免后业务 404）
- B: market_data → market 200；scope=watchlist 403（无 self_selection）；/quote 不存在 UUID 404
- C: self_selection-only → market 403；scope=watchlist 200（own rows）；详情端点：
      未自选 instrument → 403；本人 watchlist 内 instrument → 非法参数 400（resource guard PASS）
- D: 匿名 → market/stocks(market+watchlist) 401、bars 401、quote 401、详情端点 401、export 401
- F: 匿名直接 API bypass → /bars /quote 401（匿名泄露闭合）
- H: 权限撤销（admin_revoke market_data）→ 全市场立即 403
- I: export 授权 SSOT（body.scope 授权；query scope 不改变结论；精确 200 断言，无弱断言）
- J: both（self_selection + market_data）→ market/watchlist 200（真实 HTTP 双权限成功路径）
- K: research_replay-only → 全 market 端点 403（三类 capability 严格独立；详情端点因
      无 self_selection/market_data → resource guard 403）
- L: market_data-only → 非法 timeframe/primary_timeframe → 400（正向 guard evidence，
     证明 guard 通过后业务校验已触达，防 deny-all false-green）
- M: symbol-scope（stocks/{symbol}/context + first-pyramid）+ send-feishu resource guard：
     context 无 run 时 DB-only 200（正向）；first-pyramid 负向 403（不碰 provider）；
     send-feishu 已自选→404 CHANNEL_NOT_FOUND（guard 通过停在纯 DB channel lookup）、
     market_data-only→任意 instrument 404 CHANNEL_NOT_FOUND（防 future false-green）、
     未自选→403、research_replay-only→403、匿名→401

注意：
- 不覆盖 C10（legacy plan→market_data 推导）、C2（quota fail-closed）——这两者属独立 change set，
  本文件不得混入。
- self_selection-only / research_replay-only / market_data-only 用户均通过「显式 UserCapability」
  构造，而非 legacy plan fallback，确保 C10 回退后 scope=market 仍 403。
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

# 一个确定不存在的 instrument UUID（用于 /quote 正向授权的 404 证据）
_NONEXISTENT_INSTRUMENT_ID = uuid.UUID("00000000-0000-0000-0000-00000000dead")


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
    """创建一只测试股票（不造行情数据；仅用于负向 403/401 与 /quote 的 404 证据）。"""
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
# 纯逻辑测试（作用域感知授权，不依赖数据库）
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


def test_authorize_market_scope_pure():
    """P0-1/P0-2/P0-3 + Commit A：_authorize_market_scope 纯授权函数矩阵（scope 感知 + 纠偏）。

    注意：这是纯逻辑验证，不替代真实 HTTP integration test（见下方端到端 case）。
    仅用于快速锁定授权矩阵正确性。
    """
    from app.api.market import _authorize_market_scope

    cap_self = {"active": True, "expires_at": None, "watchlist_limit": 20}
    cap_market = {"active": True, "expires_at": None, "watchlist_limit": None}

    def ctx(admin=False, caps=None):
        return _make_ctx(admin, caps or {})

    # admin → 任意 scope 放行
    assert _authorize_market_scope(ctx(admin=True), "market") is not None
    assert _authorize_market_scope(ctx(admin=True), "watchlist") is not None

    # scope=market：仅 market_data
    assert _authorize_market_scope(ctx(caps={"market_data": dict(cap_market)}), "market") is not None
    with pytest.raises(Exception) as exc:
        _authorize_market_scope(ctx(caps={"self_selection": dict(cap_self)}), "market")
    assert exc.value.status_code == 403

    # scope=watchlist（Commit A 纠偏）：仅 self_selection。
    # universe 由 market_stocks_service 以 current user + active UserWatchlistItem 强制，
    # self-only 用户对其自选集合拥有完整使用权；market_data-only 仍 403（无自选权）。
    assert (
        _authorize_market_scope(ctx(caps={"self_selection": dict(cap_self)}), "watchlist")
        is not None
    )
    with pytest.raises(Exception) as exc:
        _authorize_market_scope(ctx(caps={"market_data": dict(cap_market)}), "watchlist")
    assert exc.value.status_code == 403
    assert (
        _authorize_market_scope(
            ctx(caps={"self_selection": dict(cap_self), "market_data": dict(cap_market)}),
            "watchlist",
        )
        is not None
    )

    # neither → 全拒
    with pytest.raises(Exception) as exc:
        _authorize_market_scope(ctx(), "market")
    assert exc.value.status_code == 403
    with pytest.raises(Exception) as exc:
        _authorize_market_scope(ctx(), "watchlist")
    assert exc.value.status_code == 403


# ============================================================
# 端到端测试（PostgreSQL，经 panji-verify 执行）
# ============================================================


@pytest.mark.asyncio
async def test_case_a_admin_full_market_access(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case A：admin 可访问全市场/自选；/quote 不存在 UUID → 404（admin 豁免后业务 404）。

    /market/stocks 空数据返回 200（纯 DB 查询，不碰外部行情 provider），确定性成立。
    /quote 用确定不存在的 UUID → 404，证明 require_capability 通过后业务逻辑执行，
    完全不依赖行情数据（不 monkeypatch、不造 BarDaily）。
    """
    admin = await _admin_user(db_session)
    headers = _auth(admin)

    assert (await client.get("/v1/market/stocks?scope=market", headers=headers)).status_code == 200
    assert (await client.get("/v1/market/stocks?scope=watchlist", headers=headers)).status_code == 200
    # admin 豁免 market_data 守卫 → 业务层 instrument lookup → 404（标的不存在）
    r_quote = await client.get(
        f"/v1/instruments/{_NONEXISTENT_INSTRUMENT_ID}/quote", headers=headers
    )
    assert r_quote.status_code == 404, r_quote.text


@pytest.mark.asyncio
async def test_case_b_market_data_user_access(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case B：market_data 用户可访问全市场；scope=watchlist 403；/quote 不存在 UUID 404。

    权限独立性（P0-2 + Commit A）：market_data-only 无 self_selection，scope=watchlist
    （需 self_selection）→ 403。market_data 在 resource guard 下仍可访问任意 instrument 详情。
    /quote 正向授权用不存在 UUID → 404 证明 market_data 授权通过（业务逻辑执行后标的不存在）。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}b-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    headers = _auth(user)

    assert (await client.get("/v1/market/stocks?scope=market", headers=headers)).status_code == 200

    # 正向授权证据：market_data 通过守卫 → 业务层 instrument lookup → 404（非 403/401）
    r_quote = await client.get(
        f"/v1/instruments/{_NONEXISTENT_INSTRUMENT_ID}/quote", headers=headers
    )
    assert r_quote.status_code == 404, r_quote.text

    # 权限独立性：market_data-only 无 self_selection，scope=watchlist 403
    r_watch = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_watch.status_code == 403, r_watch.text


@pytest.mark.asyncio
async def test_case_c_self_selection_only_watchlist_access(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case C：self_selection-only 用户 → 全市场 403；scope=watchlist 200（own rows）。

    Commit A 权限模型纠偏：self_selection 用户对其「自己 active 自选集合」拥有完整使用权。
    - /market/stocks?scope=market → 403（严禁隐式获得全市场）
    - /market/stocks?scope=watchlist → 200（universe 由服务端 current user + active watchlist 强制）
    - instrument resource guard：未加入自选的 instrument 详情 → 403；本人 watchlist 内 → 放行
      （非法参数 400 作正向 evidence，不碰行情 provider）
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}c-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    inst = await _create_instrument(db_session)
    other_inst = await _create_instrument(db_session)
    headers = _auth(user)

    # 全市场行情：必须 403（核心泄露闭合）
    r_market = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_market.status_code == 403, r_market.text
    # watchlist scope（Commit A）：仅 self_selection → 200（当前无自选也 200，空 own universe）
    r_watch = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_watch.status_code == 200, r_watch.text

    # 先把 inst 加入本人自选（POST /v1/watchlist 仅要求 self_selection → 201）
    r_add = await client.post(
        "/v1/watchlist",
        json={"instrument_id": str(inst.id), "source": "manual"},
        headers=headers,
    )
    assert r_add.status_code == 201, r_add.text

    # resource guard：已自选 inst 详情 → 放行（非法参数 → 400，正向 evidence）
    r_bars_ok = await client.get(
        f"/v1/instruments/{inst.id}/bars", params={"timeframe": "INVALID"}, headers=headers
    )
    assert r_bars_ok.status_code == 400, r_bars_ok.text
    r_chart_ok = await client.get(
        f"/v1/instruments/{inst.id}/chart-snapshot",
        params={"timeframe": "INVALID"},
        headers=headers,
    )
    assert r_chart_ok.status_code == 400, r_chart_ok.text
    r_indicators_ok = await client.get(
        f"/v1/instruments/{inst.id}/indicators",
        params={"timeframe": "INVALID"},
        headers=headers,
    )
    assert r_indicators_ok.status_code == 400, r_indicators_ok.text
    r_struct_ok = await client.get(
        f"/v1/instruments/{inst.id}/structural-factors",
        params={"primary_timeframe": "INVALID"},
        headers=headers,
    )
    assert r_struct_ok.status_code == 400, r_struct_ok.text
    r_temporal_ok = await client.get(
        f"/v1/instruments/{inst.id}/temporal-features",
        params={"primary_timeframe": "INVALID"},
        headers=headers,
    )
    assert r_temporal_ok.status_code == 400, r_temporal_ok.text

    # resource guard：未自选 other_inst 详情 → 403（self_selection-only 不得访问非自选股票详情）
    r_bars = await client.get(f"/v1/instruments/{other_inst.id}/bars", headers=headers)
    assert r_bars.status_code == 403, r_bars.text
    r_quote = await client.get(f"/v1/instruments/{other_inst.id}/quote", headers=headers)
    assert r_quote.status_code == 403, r_quote.text
    r_chart = await client.get(
        f"/v1/instruments/{other_inst.id}/chart-snapshot", headers=headers
    )
    assert r_chart.status_code == 403, r_chart.text
    r_indicators = await client.get(
        f"/v1/instruments/{other_inst.id}/indicators", headers=headers
    )
    assert r_indicators.status_code == 403, r_indicators.text
    r_struct = await client.get(
        f"/v1/instruments/{other_inst.id}/structural-factors", headers=headers
    )
    assert r_struct.status_code == 403, r_struct.text
    r_temporal = await client.get(
        f"/v1/instruments/{other_inst.id}/temporal-features", headers=headers
    )
    assert r_temporal.status_code == 403, r_temporal.text
    # events（A4 同 guard）：已自选 inst → 200（DB 空结果，确定性）；未自选 other_inst → 403
    r_events_ok = await client.get(f"/v1/instruments/{inst.id}/events", headers=headers)
    assert r_events_ok.status_code == 200, r_events_ok.text
    r_events = await client.get(f"/v1/instruments/{other_inst.id}/events", headers=headers)
    assert r_events.status_code == 403, r_events.text


@pytest.mark.asyncio
async def test_case_d_anonymous_denied(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case D：未登录（匿名）访问行情端点 → 401（含详情四端点 + watchlist + export，补全匿名完整矩阵）。"""
    inst = await _create_instrument(db_session)

    assert (await client.get("/v1/market/stocks?scope=market")).status_code == 401
    # 匿名 watchlist 也 401（claim「Anonymous access is 401」覆盖 watchlist scope）
    assert (await client.get("/v1/market/stocks?scope=watchlist")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/bars")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/quote")).status_code == 401
    # 详情四端点匿名也 401（claim「Anonymous access is 401」需覆盖 stock-detail endpoints）
    assert (await client.get(f"/v1/instruments/{inst.id}/chart-snapshot")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/indicators")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/structural-factors")).status_code == 401
    assert (await client.get(f"/v1/instruments/{inst.id}/temporal-features")).status_code == 401
    # events（A4 同 guard）：匿名 401
    assert (await client.get(f"/v1/instruments/{inst.id}/events")).status_code == 401

    # 匿名 export（body.scope=market / watchlist）→ 401（合法完整 body，无 DB/provider 依赖）
    def _body(scope: str) -> dict:
        return {
            "scope": scope,
            "visible_columns": [{"key": "symbol", "title": "代码", "data_type": "text"}],
        }

    assert (
        await client.post("/v1/market/export", json=_body("market"))
    ).status_code == 401
    assert (
        await client.post("/v1/market/export", json=_body("watchlist"))
    ).status_code == 401


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

    用 both 用户（self_selection + market_data），撤销 market_data 后 scope=market 立即 403。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}h-{uuid.uuid4().hex[:8]}@test.local",
        [
            {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
            {"capability": "market_data", "months": 1},
        ],
    )
    headers = _auth(user)

    # 撤销前：全市场可用
    r_before = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_before.status_code == 200, r_before.text

    # 撤销 market_data
    stmt = select(UserCapability).where(
        UserCapability.user_id == user.id,
        UserCapability.capability == "market_data",
    )
    cap_row = (await db_session.execute(stmt)).scalars().first()
    assert cap_row is not None
    cap_row.source = "admin_revoke"
    cap_row.expires_at = datetime.now(UTC) - timedelta(days=1)
    await db_session.flush()

    # 撤销后：全市场立即 403
    r_after = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_after.status_code == 403, r_after.text


@pytest.mark.asyncio
async def test_case_i_export_scope_ssot(db_session: AsyncSession, client: AsyncClient) -> None:
    """Case I：/market/export 授权 scope 与执行 scope 单一事实源（body.scope）。

    P0-4：授权必须用 request.scope（body），禁止 query scope 授权 body scope。
    攻击场景：query ?scope=watchlist + body.scope=market 不得改变 body 的授权结论。

    成功路径精确断言 200（授权通过后空数据导出空 .xlsx，确定性），
    禁止用 `!= 403` 这类弱断言（401/422/500/503 都不得被当作 success）。
    """
    self_only = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}i1-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    market_only = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}i2-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    both = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}i3-{uuid.uuid4().hex[:8]}@test.local",
        [
            {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
            {"capability": "market_data", "months": 1},
        ],
    )

    def _body(scope: str) -> dict:
        return {
            "scope": scope,
            "visible_columns": [{"key": "symbol", "title": "代码", "data_type": "text"}],
        }

    # self_selection-only + body.scope=market → 403
    r1 = await client.post("/v1/market/export", json=_body("market"), headers=_auth(self_only))
    assert r1.status_code == 403, r1.text

    # 攻击场景：query ?scope=watchlist 不影响 body.scope=market 授权 → 仍 403
    r2 = await client.post(
        "/v1/market/export?scope=watchlist", json=_body("market"), headers=_auth(self_only)
    )
    assert r2.status_code == 403, r2.text

    # self_selection-only + body.scope=watchlist → 200（Commit A：export watchlist 仅要求
    # self_selection，universe 强制为该用户 own active watchlist；空自选导出空 xlsx，确定性）
    r3 = await client.post("/v1/market/export", json=_body("watchlist"), headers=_auth(self_only))
    assert r3.status_code == 200, r3.text

    # market_data-only + body.scope=market → 200（精确断言，非弱断言）
    r4 = await client.post("/v1/market/export", json=_body("market"), headers=_auth(market_only))
    assert r4.status_code == 200, r4.text

    # market_data-only + body.scope=watchlist → 403（无 self_selection，权限独立性）
    r5 = await client.post("/v1/market/export", json=_body("watchlist"), headers=_auth(market_only))
    assert r5.status_code == 403, r5.text

    # both + body.scope=market → 200
    r6 = await client.post("/v1/market/export", json=_body("market"), headers=_auth(both))
    assert r6.status_code == 200, r6.text

    # both + body.scope=watchlist → 200（AND 分支真实 HTTP 成功路径）
    r7 = await client.post("/v1/market/export", json=_body("watchlist"), headers=_auth(both))
    assert r7.status_code == 200, r7.text


@pytest.mark.asyncio
async def test_case_j_both_capabilities_full_access(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case J：both（self_selection + market_data）→ market/watchlist 200。

    真实 HTTP 双权限成功路径，验证整条链路：
    FastAPI DI + resolve_effective_access + 两个显式 capability + AND 判断 + get_market_stocks。
    这是审核要求的「both 用户 watchlist 成功」的真实 integration 证明（非纯 helper）。

    /market/stocks 空数据返回 200（纯 DB），不依赖行情 provider，确定性成立。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}j-{uuid.uuid4().hex[:8]}@test.local",
        [
            {"capability": "self_selection", "months": 1, "watchlist_limit": 20},
            {"capability": "market_data", "months": 1},
        ],
    )
    headers = _auth(user)

    # market scope：market_data 放行
    r_market = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_market.status_code == 200, r_market.text
    # watchlist scope：self_selection AND market_data 放行
    r_watch = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_watch.status_code == 200, r_watch.text


@pytest.mark.asyncio
async def test_case_k_research_replay_only_denied_market(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case K：research_replay-only 用户 → 所有 market_data 端点 403。

    Manifest claim 明确「三类 capability 严格独立」，故必须有 research_replay 独立性的 evidence：
    research_replay 不隐式获得 market_data（不能碰 /market/stocks、/bars、/quote、
    详情四端点、export）。全部 guard-first，deterministic（不依赖行情数据）。
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}k-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "research_replay", "months": 1}],
    )
    inst = await _create_instrument(db_session)
    headers = _auth(user)

    # market scope 403
    r_market = await client.get("/v1/market/stocks?scope=market", headers=headers)
    assert r_market.status_code == 403, r_market.text
    # full watchlist 403
    r_watch = await client.get("/v1/market/stocks?scope=watchlist", headers=headers)
    assert r_watch.status_code == 403, r_watch.text
    # bars / quote 403
    r_bars = await client.get(f"/v1/instruments/{inst.id}/bars", headers=headers)
    assert r_bars.status_code == 403, r_bars.text
    r_quote = await client.get(f"/v1/instruments/{inst.id}/quote", headers=headers)
    assert r_quote.status_code == 403, r_quote.text
    # 详情四端点 403
    assert (await client.get(f"/v1/instruments/{inst.id}/chart-snapshot", headers=headers)).status_code == 403
    assert (await client.get(f"/v1/instruments/{inst.id}/indicators", headers=headers)).status_code == 403
    assert (await client.get(f"/v1/instruments/{inst.id}/structural-factors", headers=headers)).status_code == 403
    assert (await client.get(f"/v1/instruments/{inst.id}/temporal-features", headers=headers)).status_code == 403
    # events（A4 同 guard）：research_replay-only → 403
    assert (await client.get(f"/v1/instruments/{inst.id}/events", headers=headers)).status_code == 403

    # export body.scope=market / watchlist 均 403
    def _body(scope: str) -> dict:
        return {
            "scope": scope,
            "visible_columns": [{"key": "symbol", "title": "代码", "data_type": "text"}],
        }

    r_exp_market = await client.post("/v1/market/export", json=_body("market"), headers=headers)
    assert r_exp_market.status_code == 403, r_exp_market.text
    r_exp_watch = await client.post("/v1/market/export", json=_body("watchlist"), headers=headers)
    assert r_exp_watch.status_code == 403, r_exp_watch.text


@pytest.mark.asyncio
async def test_case_l_market_data_positive_guard_evidence(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case L：market_data-only 用户正向 guard evidence —— 非法参数 → 400，证明授权已通过。

    这是「deny-all false-green」的防护：若某个端点被误改成「所有人一律 403」，
    这些正向 case 会失败。400 来自 endpoint 在 require_capability("market_data") 之后的
    本地参数校验（timeframe / primary_timeframe 非法），不碰 Pytdx / Redis / Bars DB /
    fallback / 当前交易时间，完全 deterministic。

    对照表（审核给定）：
      /quote           → nonexistent UUID → 404（见 Case B）
      /bars            → ?timeframe=INVALID → 400
      /chart-snapshot  → ?timeframe=INVALID → 400
      /indicators      → ?timeframe=INVALID → 400
      /structural-factors → ?primary_timeframe=INVALID → 400
      /temporal-features  → ?primary_timeframe=INVALID → 400
    """
    user = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}l-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    inst = await _create_instrument(db_session)
    headers = _auth(user)

    # /bars 非法 timeframe → 400（guard 通过后业务校验）
    r_bars = await client.get(
        f"/v1/instruments/{inst.id}/bars", params={"timeframe": "INVALID"}, headers=headers
    )
    assert r_bars.status_code == 400, r_bars.text

    # /chart-snapshot 非法 timeframe → 400
    r_chart = await client.get(
        f"/v1/instruments/{inst.id}/chart-snapshot",
        params={"timeframe": "INVALID"},
        headers=headers,
    )
    assert r_chart.status_code == 400, r_chart.text

    # /indicators 非法 timeframe → 400
    r_indicators = await client.get(
        f"/v1/instruments/{inst.id}/indicators",
        params={"timeframe": "INVALID"},
        headers=headers,
    )
    assert r_indicators.status_code == 400, r_indicators.text

    # /structural-factors 非法 primary_timeframe → 400
    r_struct = await client.get(
        f"/v1/instruments/{inst.id}/structural-factors",
        params={"primary_timeframe": "INVALID"},
        headers=headers,
    )
    assert r_struct.status_code == 400, r_struct.text

    # /temporal-features 非法 primary_timeframe → 400
    r_temporal = await client.get(
        f"/v1/instruments/{inst.id}/temporal-features",
        params={"primary_timeframe": "INVALID"},
        headers=headers,
    )
    assert r_temporal.status_code == 400, r_temporal.text


@pytest.mark.asyncio
async def test_case_m_symbol_scope_and_feishu_resource_guard(
    db_session: AsyncSession, client: AsyncClient
) -> None:
    """Case M：symbol-scope 端点（context / first-pyramid）+ send-feishu 的 resource-scope 授权。

    Commit A corrective（审核 blocker）：
    - context / first-pyramid 改接 require_stock_symbol_market_access（symbol → Instrument →
      authorize_instrument_market_access），此前 formal matrix 未覆盖，属 false-green 缺口。
    - send-feishu 是「个股详情完整数据的主动导出路径」，此前仅 get_current_active_user，
      可对任意 instrument 创建详情分享，绕开 PA-13；现接 require_instrument_market_access。

    确定性 evidence（禁止 mock/fallback/provider）：
    - /context 无 published full run → _empty_atomic_response（reason_code=no_published_full_run）
      纯 DB 200。self-selection-only + 已自选 → 200（正向 resource-scope 证据）。
    - /first-pyramid 不造正向 200（无 published pyramid 会 fallback 到 MarketDataAggregationService
      即时计算，可能碰行情 provider）→ 仅负向 403/401 作 route-wiring 证据，正向 resource owner
      已由 /context 的 200 路径经同一 require_stock_symbol_market_access 证明。
    - /send-feishu：guard 通过后停在纯 DB channel lookup（用户无 active Feishu channel →
      ChannelNotFoundError → 404），未碰 capture worker / snapshot provider。故：
        self-selection-only + 已自选 → 404 CHANNEL_NOT_FOUND（guard 通过）
        market_data-only + 任意 instrument → 404 CHANNEL_NOT_FOUND（guard 通过，防 future false-green）
        self-selection-only + 未自选 → 403（guard 拒绝）
        research_replay-only → 403；匿名 → 401
    """
    # self_selection-only 用户
    self_only = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}m1-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "self_selection", "months": 1, "watchlist_limit": 20}],
    )
    inst = await _create_instrument(db_session)
    other_inst = await _create_instrument(db_session)
    self_headers = _auth(self_only)

    # 把 inst 加入本人自选（POST /v1/watchlist 仅要求 self_selection）
    r_add = await client.post(
        "/v1/watchlist",
        json={"instrument_id": str(inst.id), "source": "manual"},
        headers=self_headers,
    )
    assert r_add.status_code == 201, r_add.text

    # /context：已自选 → 200（无 published full run → DB-only empty atomic response）
    r_ctx_ok = await client.get(
        f"/v1/stocks/{inst.symbol}/context", headers=self_headers
    )
    assert r_ctx_ok.status_code == 200, r_ctx_ok.text

    # /context：未自选 other_inst → 403
    r_ctx_denied = await client.get(
        f"/v1/stocks/{other_inst.symbol}/context", headers=self_headers
    )
    assert r_ctx_denied.status_code == 403, r_ctx_denied.text

    # /first-pyramid：已自选 → 不硬要求 200（可能碰 provider）；只断言未自选 → 403
    r_fp_denied = await client.get(
        f"/v1/stocks/{other_inst.symbol}/first-pyramid", headers=self_headers
    )
    assert r_fp_denied.status_code == 403, r_fp_denied.text

    # /send-feishu：已自选 inst → guard 通过 → 无 Feishu channel → 404 CHANNEL_NOT_FOUND
    r_feishu_ok = await client.post(
        f"/v1/instruments/{inst.id}/send-feishu", json={}, headers=self_headers
    )
    assert r_feishu_ok.status_code == 404, r_feishu_ok.text
    assert (
        r_feishu_ok.json()["detail"]["error_code"] == "CHANNEL_NOT_FOUND"
    ), r_feishu_ok.text

    # /send-feishu：未自选 other_inst → 403（resource guard 拒绝）
    r_feishu_denied = await client.post(
        f"/v1/instruments/{other_inst.id}/send-feishu", json={}, headers=self_headers
    )
    assert r_feishu_denied.status_code == 403, r_feishu_denied.text

    # market_data-only → 任意真实 instrument 的 resource guard 应通过；
    # 测试用户无 Feishu channel，因此停在纯 DB channel lookup → 404 CHANNEL_NOT_FOUND
    market_only = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}m-market-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "market_data", "months": 1}],
    )
    market_headers = _auth(market_only)
    r_feishu_market = await client.post(
        f"/v1/instruments/{other_inst.id}/send-feishu",
        json={},
        headers=market_headers,
    )
    assert r_feishu_market.status_code == 404, r_feishu_market.text
    assert (
        r_feishu_market.json()["detail"]["error_code"] == "CHANNEL_NOT_FOUND"
    ), r_feishu_market.text

    # research_replay-only → symbol 端点 + send-feishu 均 403
    replay_only = await _register_with_capabilities(
        db_session,
        f"{_TEST_EMAIL_PREFIX}m2-{uuid.uuid4().hex[:8]}@test.local",
        [{"capability": "research_replay", "months": 1}],
    )
    replay_headers = _auth(replay_only)
    assert (
        await client.get(f"/v1/stocks/{inst.symbol}/context", headers=replay_headers)
    ).status_code == 403
    assert (
        await client.get(
            f"/v1/stocks/{inst.symbol}/first-pyramid", headers=replay_headers
        )
    ).status_code == 403
    assert (
        await client.post(
            f"/v1/instruments/{inst.id}/send-feishu", json={}, headers=replay_headers
        )
    ).status_code == 403

    # 匿名 → symbol 端点 + send-feishu 均 401
    assert (
        await client.get(f"/v1/stocks/{inst.symbol}/context")
    ).status_code == 401
    assert (
        await client.get(f"/v1/stocks/{inst.symbol}/first-pyramid")
    ).status_code == 401
    assert (
        await client.post(f"/v1/instruments/{inst.id}/send-feishu", json={})
    ).status_code == 401
