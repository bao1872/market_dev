"""PHASE C2 — Review HTTP Runtime + Client Contract Closure.

C1 证明的是"数据库里该读谁"（直接调用 handler / service）。C2 证明整条 HTTP 链：

    real ASGI HTTP request
    -> app.main.app（真实 router / middleware / dependency）
    -> require_capability("research_replay") 真实检查
    -> 真实 DB（验证库 bz_stock_verify_<SHA>）
    -> Review formal owner
    -> Pydantic response_model 真实 JSON 序列化
    -> HTTP status / headers / JSON body

禁止以"直接 await endpoint(...)"作为 C2 主要证据；也禁止把
require_capability 整体 override 成"永远通过"（那是权限假绿）。
本文件只 override 身份来源 get_current_active_user；
require_capability / require_authenticated / get_access_context 全部保持生产实现。

DB identity（fail-closed，测试自身要求）：
- APP_ENV == "verification"
- current_database() 匹配 ^bz_stock_verify_[0-9a-f]{40}$ 且 != bz_stock
"""
# ruff: noqa: UP017  (验证容器 Python<3.11，datetime.UTC 不可用，使用 timezone.utc)

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text

from app.core.deps import _fetch_user_with_roles, get_current_active_user
from app.db import AsyncSessionLocal
from app.domain.review.versions import REVIEW_ALGORITHM_VERSION
from app.main import app
from app.models.factor_publication import FactorPublication
from app.models.instrument import Instrument
from app.models.market_review import (
    MarketReviewRun,
    ReviewScopeCompositionSnapshot,
    ReviewScopeObservationFact,
)
from app.models.stock_feature_snapshot_run import StockFeatureSnapshotRun
from app.models.user import Role, User, UserRole
from app.models.user_capability import UserCapability
from app.services.review_publication_service import (
    PUBLICATION_KIND_MARKET_REVIEW,
    SCOPE_KEY_REVIEW,
    SCOPE_TYPE_REVIEW,
    publish_review,
)

pytestmark = pytest.mark.postgres

_VERIFY_DB_RE = re.compile(r"^bz_stock_verify_[0-9a-f]{40}$")
BASE_URL = "http://test"

# ---------------------------------------------------------------------------
# 交易日：T 同时是最大 live-pointer date，保证 /latest 必定命中本文件的数据集。
# C1 文件使用同一 sentinel（2099-12-31 / 2099-12-30）并在首尾各自清理指针，
# 因此两个文件互不污染，且与 pytest 执行顺序无关。
# ---------------------------------------------------------------------------
T = date(2099, 12, 31)
T_EMPTY = date(1990, 1, 1)
T_BROKEN = date(2099, 12, 26)
SCOPE_TYPE = "industry_l1"
SCOPE_KEY = "all"

# 前端 ObservationGroups 固定 8 键（frontend/src/features/review/types.ts）
L2_GROUP_KEYS = {
    "price_capital",
    "trend_state",
    "trend_progress",
    "trend_volume_confirmation",
    "structure_break_turn",
    "structure_evolution_position",
    "momentum_squeeze_release",
    "volume_anomaly",
}

# 顶层 JSON 键集合（§9：与 frontend/types.ts 的 interface 字段逐一对齐）。
# 任何 alias 漂移 / 字段新增或丢失都会在这里被抓到。
LATEST_TOP_LEVEL_KEYS = {
    "review_run_id", "trade_date", "status", "algorithm_version", "filter_version",
}
OVERVIEW_TOP_LEVEL_KEYS = {
    "reviewRunId", "tradeDate", "status", "sourceCoreRunId", "sourceBoardRunId",
    "sourceChipRunId", "degradedReasons", "chipCoverage", "algorithmVersion",
    "filterVersion", "baselineWindow", "coverage", "coverageRatio",
    "expectedScopeCount", "succeededScopeCount", "failedScopeCount", "signalCount",
    "startedAt", "completedAt", "publishedAt",
}
SCOPE_LIST_TOP_LEVEL_KEYS = {"items", "total", "page", "page_size", "has_more"}
DETAIL_TOP_LEVEL_KEYS = {
    "reviewRunId", "tradeDate", "scopeType", "scopeKey", "scopeName",
    "algorithmVersion", "observation", "observationGroups", "composition",
    "memberDirectory", "history", "crossSection",
}

REVIEW_USER_PREFIX = "/v1/review"
EXPECTED_USER_ROUTES = {
    "/v1/review/dates",
    "/v1/review/latest",
    "/v1/review/{trade_date}/overview",
    "/v1/review/{trade_date}/scopes",
    "/v1/review/{trade_date}/scopes/{scope_type}/{scope_key}",
}


# ===========================================================================
# helpers
# ===========================================================================


async def _assert_verify_db(db) -> str:
    env = (os.environ.get("APP_ENV") or "").lower()
    assert env == "verification", f"APP_ENV 必须 verification, got {env!r}"
    name = (await db.execute(text("select current_database()"))).scalar_one()
    assert _VERIFY_DB_RE.match(name), f"非法验证数据库: {name!r}"
    assert name != "bz_stock"
    return name


def _client() -> httpx.AsyncClient:
    """真实 ASGI HTTP client：经过 app.main.app 的 middleware / router / DI。"""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL,
    )


def _collect_registered_paths() -> set[str]:
    """从真实 app 递归收集全部已注册路径。

    注意（C2 实测）：FastAPI 0.141 的 ``include_router`` 在 ``app.routes`` 中放置的是
    ``fastapi.routing._IncludedRouter`` **延迟包装对象**，它本身没有 ``path``；
    真实路由在其 ``original_router.routes`` 内。只遍历顶层 ``app.routes`` 会得到
    空结果（假阴性），必须下钻。
    """
    out: set[str] = set()

    def _walk(routes) -> None:
        for route in routes or []:
            if hasattr(route, "path"):
                out.add(route.path)
            for attr in ("routes", "router", "original_router"):
                inner = getattr(route, attr, None)
                if inner is not None:
                    _walk(getattr(inner, "routes", None) or inner)

    _walk(app.routes)
    return out


@pytest.fixture(autouse=True)
def _clean_overrides():
    """每个用例后清空 dependency_overrides，避免跨用例污染。"""
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


async def _ensure_role(db, name: str) -> Role:
    role = (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if role is None:
        role = Role(id=uuid.uuid4(), name=name, description=name)
        db.add(role)
        await db.flush()
    return role


async def _make_user(db, *, roles: list[str], research_replay: str | None) -> uuid.UUID:
    """创建用户；research_replay ∈ {"active", "expired", None}。返回 user_id。

    None = 完全没有 user_capabilities 行（capability 缺失）。
    """
    now = datetime.now(timezone.utc)
    user = User(
        id=uuid.uuid4(),
        email=f"c2_{uuid.uuid4().hex[:12]}@test.local",
        password_hash="not-a-real-hash",
        status="active",
        timezone="Asia/Shanghai",
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    await db.flush()
    for name in roles:
        role = await _ensure_role(db, name)
        db.add(UserRole(user_id=user.id, role_id=role.id))
    await db.flush()
    if research_replay is not None:
        db.add(
            UserCapability(
                id=uuid.uuid4(),
                user_id=user.id,
                capability="research_replay",
                watchlist_limit=None,
                granted_at=now - timedelta(days=1),
                expires_at=(
                    now + timedelta(days=30)
                    if research_replay == "active"
                    else now - timedelta(days=1)
                ),
                source="admin_grant",
            )
        )
        await db.flush()
    return user.id


def _auth_as(user_id: uuid.UUID) -> None:
    """只替换身份来源；require_capability 保持真实实现。

    用生产同一个 _fetch_user_with_roles 重新加载用户（真实 roles 来自 DB），
    再 expunge 使其脱离 session 后仍可读，等价 deps.get_current_user 的产物。
    """
    async def _override_current_user() -> User:
        async with AsyncSessionLocal() as s:
            user = await _fetch_user_with_roles(s, user_id)
            s.expunge(user)
            return user

    app.dependency_overrides[get_current_active_user] = _override_current_user


# ---------------------------------------------------------------------------
# Review 正式成功数据集（§7：优先走生产 publish_review，不手写正式 SQL）
# ---------------------------------------------------------------------------


def _make_core_run(td: date) -> StockFeatureSnapshotRun:
    return StockFeatureSnapshotRun(
        trade_date=td,
        run_type="after_close",
        status="succeeded",
        started_at=datetime.now(timezone.utc),
        finished_at=datetime.now(timezone.utc),
    )


def _make_publishable_review_run(core_id: uuid.UUID, td: date) -> MarketReviewRun:
    return MarketReviewRun(
        trade_date=td,
        source_core_run_id=core_id,
        source_board_run_id=None,
        source_chip_run_id=None,
        degraded_reasons=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        filter_version="filters-1.0.0",
        expected_scope_count=5,
        succeeded_scope_count=5,
        failed_scope_count=0,
        signal_count=0,
        coverage_ratio=Decimal("1.0"),
        status="signals_ready",
        metadata_json={"canonical_composition_readiness": {SCOPE_KEY: "ready"}},
    )


_OBSERVATION_PAYLOAD = {
    "price": {
        "equal_weight_return": 0.0123,
        "amount_weighted_return": 0.0145,
        "capital_tilt": 0.0022,
        "price_normalized_hhi": 0.05,
        "amount_normalized_hhi": 0.06,
    },
    "trend": {
        "state": "up",
        "breadth": 0.62,
        "position": 0.55,
        "velocity": 0.01,
        "acceleration": 0.0,
    },
    "freshness": {"today_count": 0, "decay_weighted_density": 0.0},
}


def _make_fact(run_id, td, changed_member_ids=()) -> ReviewScopeObservationFact:
    """最小合法 Fact；``changed_member_ids`` 注入 observation.trend.transition.changed_members，
    用于验证 memberDirectory = Composition refs UNION Observation changed-member refs。"""
    payload = dict(_OBSERVATION_PAYLOAD)
    if changed_member_ids:
        payload = dict(payload)
        payload["trend"] = dict(payload["trend"])
        payload["trend"]["transition"] = {
            "denominator": len(changed_member_ids),
            "changed_members": [
                {
                    "member_id": str(mid),
                    "previous_state": "Neutral",
                    "current_state": "Up",
                }
                for mid in changed_member_ids
            ],
        }
    return ReviewScopeObservationFact(
        review_run_id=run_id,
        trade_date=td,
        scope_type=SCOPE_TYPE,
        scope_key=SCOPE_KEY,
        pit_member_count=100,
        pit_member_count_t1=100,
        provided_member_count=80,
        t1_membership_available=True,
        pit_status_t="ready",
        pit_status_t1="ready",
        readiness="ready",
        observation_payload=payload,
        diagnostics=[],
        algorithm_version=REVIEW_ALGORITHM_VERSION,
    )


def _make_composition(
    run_id: uuid.UUID, td: date, member_id: uuid.UUID,
) -> ReviewScopeCompositionSnapshot:
    """最小合法 composition：leadership 引用一个真实 Instrument，
    用于验证 memberDirectory 真的按批量查询解析出 symbol/name。"""
    return ReviewScopeCompositionSnapshot(
        review_run_id=run_id,
        scope_type=SCOPE_TYPE,
        scope_key=SCOPE_KEY,
        trade_date=td,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        composition_payload={
            "scope": {"scope_type": SCOPE_TYPE, "scope_key": SCOPE_KEY},
            "trade_date": td.isoformat(),
            "capability": {},
            "scope_observation": None,
            "historical_dynamics": None,
            "internal_structure_facts": None,
            "leadership": {
                "status": "ready",
                "reason": None,
                "coverage": 1.0,
                "current_leader_ids": [str(member_id)],
                "previous_leader_ids": [str(member_id)],
                "entrant_ids": [],
                "exit_ids": [],
            },
            "member_attribution": None,
            "composition_readiness": "ready",
        },
    )


def _make_broken_pointer(run_id: uuid.UUID, td: date) -> FactorPublication:
    """构造一个指向未正式发布 run 的 live pointer（人工制造异常 DB 状态）。"""
    return FactorPublication(
        scope_type=SCOPE_TYPE_REVIEW,
        scope_key=SCOPE_KEY_REVIEW,
        trade_date=td,
        publication_kind=PUBLICATION_KIND_MARKET_REVIEW,
        algorithm_version=REVIEW_ALGORITHM_VERSION,
        data_run_id=run_id,
        coverage_ratio=1.0,
        published_at=None,
        metadata_json="{}",
        superseded_by=None,
    )


async def _clean_pointers(db, td: date) -> None:
    await db.execute(
        text(
            "DELETE FROM factor_publications "
            "WHERE scope_type=:st AND scope_key=:sk AND trade_date=:d AND publication_kind=:pk"
        ),
        {
            "st": SCOPE_TYPE_REVIEW,
            "sk": SCOPE_KEY_REVIEW,
            "d": td,
            "pk": PUBLICATION_KIND_MARKET_REVIEW,
        },
    )


@pytest.fixture
async def published_review():
    """构造并发布一个最小但完整的正式 Review（Core X -> Review Y）。"""
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        await _clean_pointers(s, T)
        await s.commit()

        core = _make_core_run(T)
        s.add(core)
        await s.flush()
        run = _make_publishable_review_run(core.id, T)
        s.add(run)
        await s.flush()
        # symbol 全局唯一（instruments_symbol_key）：fixture 每次调用生成新符号，
        # 否则同一 pytest session 内第二次使用本 fixture 会撞唯一约束。
        # A：仅被 Composition leadership 引用；B：仅被 observation.trend.transition.changed_members 引用。
        inst_a = Instrument(
            id=uuid.uuid4(),
            symbol=f"C2A{uuid.uuid4().hex[:8].upper()}",
            name="C2 领导标的",
            market="SH",
            status="active",
        )
        s.add(inst_a)
        await s.flush()
        inst_b = Instrument(
            id=uuid.uuid4(),
            symbol=f"C2B{uuid.uuid4().hex[:8].upper()}",
            name="C2 仅变化成员标的",
            market="SH",
            status="active",
        )
        s.add(inst_b)
        await s.flush()
        # composition 只引用 A（不引用 B）
        s.add(_make_composition(run.id, T, inst_a.id))
        await s.flush()
        # observation.changed_members 只引用 B（验证 UNION 不依赖 composition）
        s.add(_make_fact(run.id, T, changed_member_ids=[inst_b.id]))
        await s.flush()
        await publish_review(s, run)  # 生产发布路径
        await s.commit()
        data = {
            "trade_date": T,
            "core_id": core.id,
            "run_id": run.id,
            "instrument_a_id": inst_a.id,
            "symbol_a": inst_a.symbol,
            "instrument_b_id": inst_b.id,
            "symbol_b": inst_b.symbol,
        }
    yield data
    async with AsyncSessionLocal() as s:
        await _clean_pointers(s, T)
        await s.commit()


# ===========================================================================
# §4 路由注册合同（真实 app route table / OpenAPI，不是源码字符串）
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_route_registration_on_real_app():
    """§4：5 个用户 Review 路由真实注册在 app.main.app 上；admin 仍在 /v1/admin/review/..."""
    paths = _collect_registered_paths()
    missing = EXPECTED_USER_ROUTES - paths
    assert not missing, f"app.main.app 缺少 Review 用户路由: {sorted(missing)}"
    # backend router 本身必须是 /v1/review/...，不得把 gateway 的 /api 前缀写进后端路由
    assert not any(p.startswith("/api/") for p in paths if "review" in p), (
        "backend 路由不得包含 gateway 前缀 /api"
    )

    # 真实 OpenAPI 也必须与 route table 一致（证明 response_model 已挂上）
    spec = app.openapi()
    for route in EXPECTED_USER_ROUTES:
        assert route in spec["paths"], f"OpenAPI 缺少 {route}"
        assert "get" in spec["paths"][route], f"OpenAPI {route} 缺少 GET"

    admin_paths = {p for p in paths if p.startswith("/v1/admin/review")}
    assert admin_paths, "admin Review 路由必须仍注册在 /v1/admin/review/..."


# ===========================================================================
# §5 Authentication / Capability Contract
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_auth_matrix():
    """AUTH-1 401 / AUTH-2 403 / AUTH-3 PASS / AUTH-4 admin bypass。

    只 override 身份来源 get_current_active_user；
    require_capability("research_replay") 保持生产实现。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        member_active = await _make_user(s, roles=["member"], research_replay="active")
        member_expired = await _make_user(s, roles=["member"], research_replay="expired")
        member_none = await _make_user(s, roles=["member"], research_replay=None)
        admin_id = await _make_user(s, roles=["admin"], research_replay=None)
        await s.commit()

    url = f"{REVIEW_USER_PREFIX}/dates"

    # AUTH-1 未认证 -> 401（依赖链完全不 override，走真实 HTTPBearer）
    async with _client() as c:
        resp = await c.get(url)
    assert resp.status_code == 401, f"AUTH-1 期望 401, got {resp.status_code}"
    assert isinstance(resp.json().get("detail"), str)
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")

    # AUTH-2a 已认证但 research_replay 已过期 -> 403
    _auth_as(member_expired)
    async with _client() as c:
        resp = await c.get(url)
    assert resp.status_code == 403, f"AUTH-2(expired) 期望 403, got {resp.status_code}"

    # AUTH-2b 已认证但无任何 research_replay capability 行 -> 403
    app.dependency_overrides.clear()
    _auth_as(member_none)
    async with _client() as c:
        resp = await c.get(url)
    assert resp.status_code == 403, f"AUTH-2(missing) 期望 403, got {resp.status_code}"
    assert "research_replay" in resp.json().get("detail", "")

    # AUTH-3 research_replay active -> 可访问
    app.dependency_overrides.clear()
    _auth_as(member_active)
    async with _client() as c:
        resp = await c.get(url)
    assert resp.status_code == 200, f"AUTH-3 期望 200, got {resp.status_code}"

    # AUTH-4 admin（无 capability 行）-> bypass 生效
    app.dependency_overrides.clear()
    _auth_as(admin_id)
    async with _client() as c:
        resp = await c.get(url)
    assert resp.status_code == 200, f"AUTH-4 admin bypass 期望 200, got {resp.status_code}"


# ===========================================================================
# §6 include_partial 权限
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_include_partial_permission(published_review):
    """普通 research_replay 用户 ?include_partial=true -> 403；admin -> 200。"""
    async with AsyncSessionLocal() as s:
        member = await _make_user(s, roles=["member"], research_replay="active")
        admin_id = await _make_user(s, roles=["admin"], research_replay=None)
        await s.commit()

    td = published_review["trade_date"].isoformat()
    url = f"{REVIEW_USER_PREFIX}/{td}/overview"

    _auth_as(member)
    async with _client() as c:
        resp = await c.get(url, params={"include_partial": True})
    assert resp.status_code == 403, f"INCLUDE_PARTIAL_MEMBER 期望 403, got {resp.status_code}"

    app.dependency_overrides.clear()
    _auth_as(admin_id)
    async with _client() as c:
        resp = await c.get(url, params={"include_partial": True})
    assert resp.status_code == 200, f"admin include_partial 期望 200, got {resp.status_code}"


# ===========================================================================
# §8 HTTP Success Matrix + §9 真实 JSON 序列化
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_http_success_matrix(published_review):
    """§8/§9：5 个用户 endpoint 的真实 HTTP 200 + 真实 JSON 字段与 null/0/[] 语义。"""
    async with AsyncSessionLocal() as s:
        member = await _make_user(s, roles=["member"], research_replay="active")
        await s.commit()
    _auth_as(member)

    td = published_review["trade_date"].isoformat()
    run_id = str(published_review["run_id"])
    core_id = str(published_review["core_id"])

    async with _client() as c:
        # ---------- /dates ----------
        r = await c.get(f"{REVIEW_USER_PREFIX}/dates")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert isinstance(body["trade_dates"], list)
        assert td in body["trade_dates"]
        assert body["latest_trade_date"] == td, "/dates 的最新正式日必须是本数据集 T"

        # ---------- /latest ----------
        r = await c.get(f"{REVIEW_USER_PREFIX}/latest")
        assert r.status_code == 200
        latest = r.json()
        assert set(latest.keys()) == LATEST_TOP_LEVEL_KEYS, (
            f"/latest 顶层键漂移: {sorted(set(latest.keys()) ^ LATEST_TOP_LEVEL_KEYS)}"
        )
        assert latest["review_run_id"] == run_id
        assert latest["trade_date"] == td
        assert latest["status"] == "published"

        # ---------- /{T}/overview ----------
        r = await c.get(f"{REVIEW_USER_PREFIX}/{td}/overview")
        assert r.status_code == 200
        ov = r.json()
        assert set(ov.keys()) == OVERVIEW_TOP_LEVEL_KEYS, (
            f"/overview 顶层键漂移: {sorted(set(ov.keys()) ^ OVERVIEW_TOP_LEVEL_KEYS)}"
        )

        # HTTP_LINEAGE：Review Y -> Core X
        assert ov["reviewRunId"] == run_id, "overview 必须返回正式 Review Y"
        assert ov["sourceCoreRunId"] == core_id, "overview 必须暴露显式 Core X lineage"
        assert ov["tradeDate"] == td
        assert ov["status"] == "published"
        # null vs 0 vs [] 语义（§9）
        assert ov["sourceBoardRunId"] is None, "core-only run 的 board lineage 必须是 null"
        assert ov["sourceChipRunId"] is None, "core-only run 的 chip lineage 必须是 null"
        assert ov["degradedReasons"] == [], "无降级必须是空数组，不是 null"
        assert abs(ov["coverage"]["industryL1"] - 0.8) < 1e-9
        assert ov["coverage"]["market"] is None, "未激活家族覆盖率必须是 null，不是 0"
        assert ov["publishedAt"] is not None

        # ---------- /{T}/scopes ----------
        r = await c.get(f"{REVIEW_USER_PREFIX}/{td}/scopes")
        assert r.status_code == 200
        scopes = r.json()
        assert set(scopes.keys()) == SCOPE_LIST_TOP_LEVEL_KEYS, (
            f"/scopes 顶层键漂移: "
            f"{sorted(set(scopes.keys()) ^ SCOPE_LIST_TOP_LEVEL_KEYS)}"
        )
        assert scopes["total"] == 1 and len(scopes["items"]) == 1
        item = scopes["items"][0]
        for key in (
            "scopeType", "scopeKey", "scopeName", "readiness", "status",
            "eligibleCount", "providedCount", "coverageRatio",
            "summary", "observationSummary",
        ):
            assert key in item, f"/scopes item JSON 缺字段 {key}"
        assert item["scopeType"] == SCOPE_TYPE and item["scopeKey"] == SCOPE_KEY
        assert item["eligibleCount"] == 100 and item["providedCount"] == 80
        assert abs(item["coverageRatio"] - 0.8) < 1e-9
        assert item["summary"] is not None, "存在 Composition 时 summary 不得为 null"
        assert item["observationSummary"] is not None

        # ---------- /{T}/scopes/{type}/{key} ----------
        r = await c.get(f"{REVIEW_USER_PREFIX}/{td}/scopes/{SCOPE_TYPE}/{SCOPE_KEY}")
        assert r.status_code == 200
        detail = r.json()
        assert set(detail.keys()) == DETAIL_TOP_LEVEL_KEYS, (
            f"scope detail 顶层键漂移: "
            f"{sorted(set(detail.keys()) ^ DETAIL_TOP_LEVEL_KEYS)}"
        )
        assert detail["reviewRunId"] == run_id
        assert detail["tradeDate"] == td
        assert isinstance(detail["observation"], dict)
        assert set(detail["observationGroups"].keys()) == L2_GROUP_KEYS, (
            "observationGroups 必须是前端 types.ts 声明的固定 8 键"
        )
        assert isinstance(detail["composition"], dict)
        assert detail["composition"]["composition_readiness"] == "ready"

        # memberDirectory = Composition leadership refs UNION Observation changed-member refs
        # -> ONE bulk Instrument query 响应。A 仅被 composition 引用；B 仅被 observation
        # transition.changed_members 引用（composition 不引用 B）。两者都必须出现，证明 UNION。
        md = detail["memberDirectory"]
        a_id = str(published_review["instrument_a_id"])
        b_id = str(published_review["instrument_b_id"])
        assert a_id in md, "memberDirectory 必须包含 composition leadership 引用的成员 A"
        assert b_id in md, "memberDirectory 必须包含 Observation changed_members 引用的成员 B（UNION 不依赖 composition）"
        assert md[a_id] == {
            "symbol": published_review["symbol_a"],
            "name": "C2 领导标的",
        }, "A 必须按批量查询解析出 symbol/name"
        assert md[b_id] == {
            "symbol": published_review["symbol_b"],
            "name": "C2 仅变化成员标的",
        }, "B 必须按批量查询解析出 symbol/name"
        assert len(md) == 2, f"memberDirectory 必须是 A∪B 两项，无额外泄漏: {sorted(md)}"


# ===========================================================================
# §10 Empty / Invalid / Broken HTTP Contract
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_empty_invalid_broken_http_contract(published_review):
    """EMPTY_404 / INVALID_DATE_422 / 缺 Fact 404 / broken formal pointer 500。"""
    async with AsyncSessionLocal() as s:
        member = await _make_user(s, roles=["member"], research_replay="active")
        await s.commit()
    _auth_as(member)

    async with _client() as c:
        # 无正式 Review 的 T -> 404
        for sub in ("overview", "scopes", f"scopes/{SCOPE_TYPE}/{SCOPE_KEY}"):
            r = await c.get(f"{REVIEW_USER_PREFIX}/{T_EMPTY.isoformat()}/{sub}")
            assert r.status_code == 404, f"{sub} 无正式 Review 期望 404, got {r.status_code}"
            assert isinstance(r.json().get("detail"), str)

        # invalid date -> 422
        r = await c.get(f"{REVIEW_USER_PREFIX}/not-a-date/overview")
        assert r.status_code == 422, f"非法日期期望 422, got {r.status_code}"
        assert isinstance(r.json().get("detail"), str)

        # 正式 Review 存在但该 scope 无 Fact -> 404
        td = published_review["trade_date"].isoformat()
        r = await c.get(f"{REVIEW_USER_PREFIX}/{td}/scopes/{SCOPE_TYPE}/does-not-exist")
        assert r.status_code == 404, f"缺 Fact 的 scope detail 期望 404, got {r.status_code}"

    # broken formal pointer（异常 DB 状态，允许 fixture 直接插表）-> HTTP 500
    async with AsyncSessionLocal() as s:
        await _clean_pointers(s, T_BROKEN)
        core = _make_core_run(T_BROKEN)
        s.add(core)
        await s.flush()
        broken = _make_publishable_review_run(core.id, T_BROKEN)
        broken.status = "signals_ready"  # 未正式发布，published_at 保持 NULL
        s.add(broken)
        await s.flush()
        s.add(_make_broken_pointer(broken.id, T_BROKEN))
        await s.commit()

    async with _client() as c:
        r = await c.get(f"{REVIEW_USER_PREFIX}/{T_BROKEN.isoformat()}/overview")
    assert r.status_code == 500, f"BROKEN_POINTER_HTTP 期望 500, got {r.status_code}"
    assert isinstance(r.json().get("detail"), str)

    async with AsyncSessionLocal() as s:
        await _clean_pointers(s, T_BROKEN)
        await s.commit()


# ===========================================================================
# §11 Error Contract + §19 request-id 观察
# ===========================================================================


@pytest.mark.asyncio
async def test_c2_error_contract_and_request_id():
    """§11：401/403/404/422 的真实 body 可被 frontend extractReviewError 解析。

    §19：只观察 x-request-id 是否由 app 提供 —— Review 不拥有该问题，
    不得为了 C2 在 Review endpoint 内局部实现 request-id。
    """
    async with AsyncSessionLocal() as s:
        await _assert_verify_db(s)
        member_none = await _make_user(s, roles=["member"], research_replay=None)
        member_active = await _make_user(s, roles=["member"], research_replay="active")
        await s.commit()

    observed: dict[int, httpx.Response] = {}

    # 401（无 override -> 真实 HTTPBearer）
    async with _client() as c:
        observed[401] = await c.get(f"{REVIEW_USER_PREFIX}/dates")

    # 403（无 research_replay）
    app.dependency_overrides.clear()
    _auth_as(member_none)
    async with _client() as c:
        observed[403] = await c.get(f"{REVIEW_USER_PREFIX}/dates")

    # 404 / 422
    app.dependency_overrides.clear()
    _auth_as(member_active)
    async with _client() as c:
        observed[404] = await c.get(f"{REVIEW_USER_PREFIX}/{T_EMPTY.isoformat()}/overview")
        observed[422] = await c.get(f"{REVIEW_USER_PREFIX}/not-a-date/overview")

    for status, resp in observed.items():
        assert resp.status_code == status, f"期望 {status}, got {resp.status_code}"
        body = resp.json()
        # frontend extractReviewError 读 response.data.detail
        assert isinstance(body.get("detail"), str) and body["detail"], (
            f"HTTP {status} 的 body.detail 必须是非空字符串（frontend 依赖它）"
        )
        assert resp.headers["content-type"].startswith("application/json")

    # §19 request-id：真实 HTTP 证据（本地与验证环境均已实测）。
    # app.main.app **不**产出 x-request-id —— 它只在 app/api/auth.py 等处**读取**
    # 上游 header，从不写入。因此该 header 属 gateway/middleware owner，
    # 不在 Review API 内实现（frontend extractReviewError 已容忍 requestId=null）。
    for status, resp in observed.items():
        assert "x-request-id" not in resp.headers, (
            f"HTTP {status} 出现了 app 自产的 x-request-id；若已新增 request-id "
            "middleware，请更新本合同并确认 owner 不是 Review endpoint"
        )
