"""共享测试 fixtures - pytest 集成测试基础设施。

提供：
- 测试库连接校验（APP_ENV / TEST_DATABASE_URL / CI）
- 测试专用 async_engine / TestAsyncSessionLocal
- async DB session fixture（savepoint 模式，被测代码调用 commit 也不污染数据库）
- 测试数据工厂 fixtures（用户、角色、订阅、邀请码、标的、策略、运行）
- HTTP 客户端 fixture（自动覆盖 get_db）

约束（CHANGE-20260728-007 永久测试库禁用）：
- 本地 Mac、开发服务器、腾讯云禁止创建或复用持久测试数据库（如 bz_stock_test）。
- 本地测试只能纯单元/mock，禁止连接正式库 bz_stock 或任何持久测试库。
- 数据库集成测试只在 CI 临时 Postgres 容器中运行（job 结束自动销毁，唯一例外）。
- 因此：非 CI 环境必须设置 PURE_UNIT_TEST=1；CI 环境需 APP_ENV=test + TEST_DATABASE_URL。
"""
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.instrument import Instrument
from app.models.invitation import InviteCode
from app.models.subscription import Subscription
from app.models.user import Role, User

# 异步工厂 fixture 返回类型：Callable[..., Coroutine[Any, Any, T]]
# conftest 中的 *_factory / make_user_eligible 等 fixture 返回的是 async 函数，
# 调用方需 `await factory(...)`，因此返回类型必须是 Coroutine 包装而非裸同步 Callable。
T = TypeVar("T")
AsyncFactory = Callable[..., Coroutine[Any, Any, T]]


def make_asgi_transport(app: FastAPI) -> httpx.ASGITransport:
    """构造 ASGITransport。

    httpx ASGITransport 存根用 dict[str, Any] 描述 ASGI scope/receive/send，
    而 Starlette/FastAPI __call__ 存根用 MutableMapping[str, Any]，结构子类型
    不兼容导致 mypy [arg-type]。这是第三方存根缺口，非测试错误；此处用单点
    cast 桥接（运行时 FastAPI 本就是合法 ASGI3 app）。
    """
    from typing import cast

    _asgi_app = Callable[
        [dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]],
        Coroutine[None, None, None],
    ]
    return httpx.ASGITransport(app=cast(_asgi_app, app))

# ---------------------------------------------------------------------------
# 测试库连接配置
# ---------------------------------------------------------------------------

_APP_ENV = os.environ.get("APP_ENV", "").lower()
_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
# 纯单元测试跳过 DB 初始化（不连接任何数据库）
_PURE_UNIT = os.environ.get("PURE_UNIT_TEST", "").lower() in ("1", "true", "yes")
# CI 环境标识：仅 GitHub Actions 设置 GITHUB_ACTIONS=true；CI=true 可能被本地 IDE 误设
# 显式 PANJI_CI_DB_TEST=1 用于其他 CI 系统的 opt-in（必须配合临时 Postgres 容器）
_CI_ENV = (
    os.environ.get("GITHUB_ACTIONS", "").lower() in ("1", "true", "yes")
    or os.environ.get("PANJI_CI_DB_TEST", "").lower() in ("1", "true", "yes")
)

if not _PURE_UNIT:
    # [CHANGE-20260728-007] 非 CI 环境禁止 DB 集成测试，避免本地 Mac 复用持久测试库
    if not _CI_ENV:
        raise RuntimeError(
            "数据库集成测试只在 CI 临时 Postgres 容器中运行（禁止本地 Mac / 开发服务器 / 腾讯云复用持久测试库）。\n"
            "本地运行测试请设置：PURE_UNIT_TEST=1 pytest tests/test_xxx.py\n"
            "如确实需要在 CI 之外运行集成测试，请使用一次性临时 Postgres 容器并设置 PANJI_CI_DB_TEST=1。"
        )

    if _APP_ENV != "test":
        raise RuntimeError(
            f"测试必须在 APP_ENV=test 下运行，当前 APP_ENV={_APP_ENV!r}。"
            "请使用：APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/"
            "；纯单元测试可设置 PURE_UNIT_TEST=1 跳过 DB 检查。"
        )

    if not _TEST_DATABASE_URL:
        raise RuntimeError(
            "TEST_DATABASE_URL 环境变量未设置。"
            "示例：TEST_DATABASE_URL=postgresql://user:pass@host:port/dbname_test"
        )

# [测试配置] - 描述: 校验数据库 URL scheme 与测试库命名
if not _PURE_UNIT:
    _parsed = urlparse(_TEST_DATABASE_URL)
    _ALLOWED_SCHEMES = {"postgresql", "postgresql+psycopg", "postgresql+asyncpg"}
    if _parsed.scheme not in _ALLOWED_SCHEMES:
        raise RuntimeError(
            f"TEST_DATABASE_URL scheme 必须是 postgresql / postgresql+psycopg / postgresql+asyncpg，"
            f"当前={_parsed.scheme!r}"
        )

    _db_name = (_parsed.path or "").lstrip("/")
    if "_test" not in _db_name:
        raise RuntimeError(
            f"TEST_DATABASE_URL 必须指向测试库（库名含 _test），当前库名={_db_name!r}"
        )

    # [测试配置] - 描述: 同步 DATABASE_URL 与 TEST_DATABASE_URL，确保 app.db 与测试引擎连接同一库
    os.environ["DATABASE_URL"] = _TEST_DATABASE_URL

    # 统一转换为 asyncpg 驱动格式
    _TEST_ASYNC_URL = _TEST_DATABASE_URL.replace(
        "postgresql+psycopg://", "postgresql+asyncpg://"
    ).replace(
        "postgresql://", "postgresql+asyncpg://"
    )

    # 测试专用 engine / session factory
    # [测试] - 描述: test_async_engine 与 TestAsyncSessionLocal 保留供需要独立 session 的测试导入使用
    test_async_engine = create_async_engine(
        _TEST_ASYNC_URL,
        echo=False,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )
    TestAsyncSessionLocal = async_sessionmaker(
        bind=test_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
else:
    # [CI 分层] PURE_UNIT_TEST=1 下不建立任何数据库连接，但仍需保证「收集期不报错」。
    #
    # 背景：部分模块在 import 期就 `from tests.conftest import TestAsyncSessionLocal`
    # 或读取 os.environ["TEST_DATABASE_URL"]，若这些名字在纯单元模式下完全缺失，
    # pytest 会在 collection 阶段直接 ImportError/KeyError 而中断整个 session
    # （Interrupted: errors during collection），导致纯单元 job 一条测试都跑不了。
    #
    # 处理方式：提供占位符，使模块可被导入并完成收集；这些占位符一旦被真正调用
    # 就会抛出明确错误，避免「本该连库的测试静默跑成假通过」。
    test_async_engine = None  # type: ignore[assignment]

    def _pure_unit_db_guard(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "当前处于 PURE_UNIT_TEST=1 纯单元测试模式，禁止建立数据库会话。\n"
            "该测试依赖真实 PostgreSQL，应带 @pytest.mark.postgres 并在 CI 临时容器中运行。"
        )

    TestAsyncSessionLocal = _pure_unit_db_guard  # type: ignore[assignment]

    # 供模块级读取 TEST_DATABASE_URL 的测试在收集期不至于 KeyError；
    # 真正连接时 asyncpg 仍会失败，但此时测试已被 postgres marker 过滤掉。
    os.environ.setdefault(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://pure-unit-placeholder@127.0.0.1:1/pure_unit_test",
    )


# ---------------------------------------------------------------------------
# postgres marker 自动标注
# ---------------------------------------------------------------------------

# 依赖真实数据库的 fixture 名称。任何测试（或其 fixture 链）用到这些，
# 就必然需要真实 PostgreSQL。
_DB_FIXTURE_NAMES = frozenset(
    {
        "db_session",
        "pg_connection",
        "_db_connection",
        "client",
        "role_factory",
        "user_factory",
        "instrument_factory",
        "subscription_factory",
        "invite_code_factory",
        "test_user",
        "test_instrument",
    }
)

# 源码级标志：模块自建真实 PG 连接。
#
# 用源码扫描而非 getattr(module, ...)，因为这些引用常写在函数体内
# （如 `from tests.conftest import TestAsyncSessionLocal` 位于测试函数内部），
# 模块对象上并不存在对应属性，运行期反射无法识别。
_DB_SOURCE_MARKERS = (
    "TestAsyncSessionLocal",  # 复用 conftest 的 session 工厂
    "test_async_engine",      # 复用 conftest 的引擎
    "create_async_engine",    # 模块自建独立引擎（如 phase8a 的 _sep_engine）
)


def pytest_collection_modifyitems(config, items) -> None:  # type: ignore[no-untyped-def]
    """自动为依赖真实数据库的测试打 `postgres` marker，并在纯单元模式下跳过它们。

    [CI 分层] 目的：让「纯单元测试」与「真实 PG 集成测试」有一条机器可判定的边界，
    而不是靠人工维护清单或靠目录约定。

    判定规则（满足任一即视为需要真实 PG）：
    1. 测试用例的 fixture 闭包中出现 _DB_FIXTURE_NAMES 中的任一 fixture；
    2. 测试所在模块直接导入了 TestAsyncSessionLocal / test_async_engine
       （这些模块自建 DB fixture，绕过 conftest.db_session，无法靠 fixture 名识别）；
    3. 测试已被作者显式标注 @pytest.mark.postgres。

    行为：
    - 命中的用例统一补上 `postgres` marker，供 CI 用 `-m postgres` / `-m "not postgres"` 精确切分；
    - PURE_UNIT_TEST=1 时给这些用例追加 skip 标记，避免它们在无数据库环境下报错失败。

    注意：这里只做「分类」，不删除任何测试。真实锁/事务/JSONB/UUID/唯一约束等测试
    仍会在 Release Gate 与 Nightly 的完整 PG job 中执行。
    """
    import pytest

    skip_pg = pytest.mark.skip(
        reason="需要真实 PostgreSQL；当前为 PURE_UNIT_TEST=1 纯单元测试模式"
    )

    # 文件级判定缓存：源码中是否出现自建真实 PG 连接的标志。
    file_needs_pg: dict[str, bool] = {}

    def _file_uses_real_db(item) -> bool:  # type: ignore[no-untyped-def]
        path = str(getattr(item, "fspath", "") or "")
        if not path:
            return False
        if path not in file_needs_pg:
            try:
                with open(path, encoding="utf-8") as fh:
                    source = fh.read()
            except OSError:
                file_needs_pg[path] = False
            else:
                file_needs_pg[path] = any(m in source for m in _DB_SOURCE_MARKERS)
        return file_needs_pg[path]

    for item in items:
        fixtures = set(getattr(item, "fixturenames", ()))
        needs_pg = (
            bool(fixtures & _DB_FIXTURE_NAMES)
            or item.get_closest_marker("postgres") is not None
            or _file_uses_real_db(item)
        )
        if not needs_pg:
            continue
        item.add_marker(pytest.mark.postgres)
        if _PURE_UNIT:
            item.add_marker(skip_pg)


def _run_alembic_upgrade():
    """同步执行 Alembic 升级到测试库。"""
    import subprocess

    # Alembic env.py 使用 psycopg3 同步驱动
    alembic_url = _TEST_DATABASE_URL.replace(
        "postgresql+asyncpg://", "postgresql+psycopg://"
    ).replace(
        "postgresql://", "postgresql+psycopg://"
    )
    env = os.environ.copy()
    env["DATABASE_URL"] = alembic_url
    # [测试] - 描述: alembic 子进程必须继承 test 环境，否则 app.config 会拒绝连测试库
    env["APP_ENV"] = "test"
    subprocess.run(
        ["alembic", "upgrade", "head"],
        cwd=os.path.dirname(os.path.dirname(__file__)),
        env=env,
        check=True,
    )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def init_test_db():
    """在测试 session 开始前对测试库应用 Alembic 迁移。

    [Phase 5A] 设置 SKIP_ALEMBIC_UPGRADE=1 可跳过迁移，用于运行纯 mock 测试
    （不连接数据库/Redis）。满足"不运行 Migration"约束下执行 readiness/config 测试。
    [Round 2026-07-28] PURE_UNIT_TEST=1 时完全跳过 DB 初始化（纯单元测试不连接数据库）。
    """
    if _PURE_UNIT:
        yield
        return
    if os.environ.get("SKIP_ALEMBIC_UPGRADE", "") == "1":
        yield
        await test_async_engine.dispose()
        return
    await asyncio.to_thread(_run_alembic_upgrade)
    yield
    await test_async_engine.dispose()


@pytest_asyncio.fixture
async def _db_connection() -> AsyncGenerator[AsyncConnection, None]:
    """[内部] function 级数据库连接，每个测试独立事务，通过 savepoint 隔离。

    设计说明：
    - 每个测试获得独立连接与事务，fixture 退出时 rollback，确保测试间无数据交叉
    - 被测代码调用 session.commit() 仅提交 savepoint，不污染其他测试
    """
    async with test_async_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest_asyncio.fixture
async def pg_connection(_db_connection: AsyncConnection) -> AsyncConnection:
    """function 级数据库连接，直接返回底层连接。"""
    return _db_connection


@pytest_asyncio.fixture
async def db_session(
    _db_connection: AsyncConnection,
) -> AsyncGenerator[AsyncSession, None]:
    """提供独立 savepoint 的 DB session，测试代码调用 commit 也不会污染数据库。

    机制：
    - 每个测试获得独立连接与事务，db_session 在该事务上创建 savepoint
    - 被测代码调用 session.commit() 仅提交 savepoint，不持久化到数据库
    - fixture 退出时外层事务 rollback，所有 savepoint 变更被丢弃
    """
    async with AsyncSession(
        _db_connection,
        join_transaction_mode="create_savepoint",
        expire_on_commit=False,
    ) as session:
        yield session
        # [测试] - 描述: AsyncSession 上下文退出时自动回滚 savepoint


# ---------------------------------------------------------------------------
# 数据工厂 fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def role_factory(db_session: AsyncSession) -> AsyncFactory[Role]:
    """创建或复用指定名称的角色。"""
    async def _create_role(name: str = "member", description: str | None = None) -> Role:
        from sqlalchemy import select

        result = await db_session.execute(select(Role).where(Role.name == name))
        role = result.scalar_one_or_none()
        if role is None:
            role = Role(id=uuid.uuid4(), name=name, description=description or name)
            db_session.add(role)
            await db_session.flush()
        return role

    return _create_role


@pytest_asyncio.fixture
async def user_factory(
    db_session: AsyncSession,
    role_factory: AsyncFactory[Role],
) -> AsyncFactory[User]:
    """创建测试用户，可选分配角色。"""
    async def _create_user(
        email: str | None = None,
        password_hash: str = "$2b$12$dummyhash",
        status: str = "active",
        roles: list[str] | None = None,
        **kwargs,
    ) -> User:
        from app.models.user import UserRole

        email = email or f"test_{uuid.uuid4().hex[:8]}@test.com"
        user = User(
            email=email,
            password_hash=password_hash,
            status=status,
            **kwargs,
        )
        db_session.add(user)
        await db_session.flush()

        role_names = roles or []
        for role_name in role_names:
            role = await role_factory(name=role_name)
            db_session.add(UserRole(user_id=user.id, role_id=role.id))
        if role_names:
            await db_session.flush()

        # [测试] - 描述: 模拟 deps._fetch_user_with_roles 挂载的 _roles 属性
        object.__setattr__(user, "_roles", role_names)
        return user

    return _create_user


@pytest_asyncio.fixture
async def instrument_factory(db_session: AsyncSession) -> AsyncFactory[Instrument]:
    """创建测试标的。"""
    async def _create_instrument(
        symbol: str | None = None,
        name: str = "测试标的",
        market: str = "SZ",
        status: str = "active",
        **kwargs,
    ) -> Instrument:
        symbol = symbol or f"T{uuid.uuid4().hex[:5]}"
        instrument = Instrument(
            symbol=symbol,
            name=name,
            market=market,
            status=status,
            **kwargs,
        )
        db_session.add(instrument)
        await db_session.flush()
        return instrument

    return _create_instrument


@pytest_asyncio.fixture
async def subscription_factory(db_session: AsyncSession) -> AsyncFactory[Subscription]:
    """创建测试订阅记录，entitlement_snapshot 从 plans 表查询构造。"""
    async def _create_subscription(
        user_id: uuid.UUID,
        plan_code: str = "observe_20",
        status: str = "active",
        starts_at: datetime | None = None,
        expires_at: datetime | None = None,
        source: str = "invite",
        **kwargs,
    ) -> Subscription:
        from app.services.plan_service import get_plan

        plan = await get_plan(db_session, plan_code)
        entitlement_snapshot = {
            "monitor_limit": int(plan.monitor_limit),
            "notification_channel_limit": int(plan.notification_channel_limit),
            "message_retention_days": int(plan.message_retention_days),
            "features": list(plan.features) if plan.features else [],
        }

        now = datetime.now(UTC)
        starts_at = starts_at or now - timedelta(days=1)
        expires_at = expires_at or now + timedelta(days=30)

        subscription = Subscription(
            user_id=user_id,
            plan_code=plan_code,
            status=status,
            starts_at=starts_at,
            expires_at=expires_at,
            entitlement_snapshot=entitlement_snapshot,
            source=source,
            **kwargs,
        )
        db_session.add(subscription)
        await db_session.flush()
        return subscription

    return _create_subscription


@pytest_asyncio.fixture
async def make_user_eligible(
    db_session: AsyncSession,
    role_factory: AsyncFactory[Role],
    subscription_factory: AsyncFactory[Subscription],
) -> AsyncFactory[User]:
    """为用户添加 member 角色 + active subscription，使其有资格进入监控 universe。

    [eligible_user_service] - 资格条件：active member + 有效 subscription
    用于需要通过 Worker 资格检查的测试场景（outbox_relay / delivery_worker /
    event_recipient_service / monitor_batch_service）。
    """
    async def _make_eligible(
        user: User,
        plan_code: str = "observe_20",
    ) -> User:
        from app.models.user import UserRole

        role = await role_factory(name="member")
        db_session.add(UserRole(user_id=user.id, role_id=role.id))
        await db_session.flush()
        await subscription_factory(user_id=user.id, plan_code=plan_code)
        return user

    return _make_eligible


@pytest_asyncio.fixture
async def invite_code_factory(
    db_session: AsyncSession,
) -> AsyncFactory[tuple[InviteCode, str]]:
    """创建测试邀请码，code_hash 使用 subscription_service.hash_invite_code 生成。"""
    async def _create_invite_code(
        created_by: uuid.UUID,
        raw_code: str | None = None,
        plan_code: str = "observe_20",
        grant_months: int = 1,
        status: str = "unused",
        **kwargs,
    ) -> tuple[InviteCode, str]:
        from app.services.plan_service import get_plan
        from app.services.subscription_service import hash_invite_code

        raw_code = raw_code or f"TEST-{uuid.uuid4().hex[:16].upper()}"
        code_hash = hash_invite_code(raw_code)

        plan = await get_plan(db_session, plan_code)
        invite = InviteCode(
            code_hash=code_hash,
            status=status,
            grant_days=30,
            plan_code=plan_code,
            monitor_limit=int(plan.monitor_limit),
            grant_months=grant_months,
            created_by=created_by,
            **kwargs,
        )
        db_session.add(invite)
        await db_session.flush()
        return invite, raw_code

    return _create_invite_code


# ---------------------------------------------------------------------------
# 通用应用/客户端 fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_db_override(
    db_session: AsyncSession,
) -> Callable[[], AsyncGenerator[AsyncSession, None]]:
    """返回覆盖 get_db 的依赖函数，yield 当前测试 session。"""
    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    return _get_db


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    """提供 httpx.AsyncClient，自动覆盖 get_db 为当前测试 session。"""
    from app.core.deps import get_db as deps_get_db
    from app.db import get_db as db_get_db
    from app.main import app

    async def _get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[deps_get_db] = _get_db
    app.dependency_overrides[db_get_db] = _get_db

    transport = make_asgi_transport(app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# 保留的传统 fixtures（底层复用 factories）
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_user(user_factory) -> User:
    """创建测试用户（无角色，与历史行为一致）。"""
    return await user_factory()


@pytest_asyncio.fixture
async def test_instrument(instrument_factory) -> Instrument:
    """创建测试标的（满足 FK 约束）。"""
    return await instrument_factory()


@pytest_asyncio.fixture
async def test_selector_strategy(db_session):
    """创建测试选股策略定义+版本。"""
    from app.models.strategy import StrategyDefinition, StrategyVersion

    definition = StrategyDefinition(
        strategy_key=f"test_selector_{uuid.uuid4().hex[:8]}",
        kind="selector",
        display_name="测试选股策略",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()

    yield {"definition": definition, "version": version}


@pytest_asyncio.fixture
async def dsa_selector_strategy(db_session):
    """创建 strategy_key='dsa_selector' 的选股策略定义+ released 版本。

    用于测试 system_overview_service 中限定 dsa_selector 的盘后流水线逻辑。
    """
    from app.models.strategy import StrategyDefinition, StrategyVersion

    definition = StrategyDefinition(
        strategy_key="dsa_selector",
        kind="selector",
        display_name="趋势选股",
    )
    db_session.add(definition)
    await db_session.flush()

    version = StrategyVersion(
        strategy_definition_id=definition.id,
        version="1.0.0",
        status="released",
        manifest={
            "outputs": [
                {"key": "dsa_dir_bars", "type": "numeric", "filterable": True, "sortable": True},
                {"key": "offset_mean", "type": "numeric", "filterable": True, "sortable": True},
            ],
        },
        build_hash=f"test_hash_{uuid.uuid4().hex[:16]}",
        released_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()

    yield {"definition": definition, "version": version}


@pytest_asyncio.fixture
async def test_published_run(db_session, test_selector_strategy):
    """创建已发布的测试运行+结果。"""
    from app.models.strategy_run import StrategyRun

    version = test_selector_strategy["version"]
    trade_date = date(2026, 6, 23)
    now = datetime.now(UTC)

    run = StrategyRun(
        strategy_version_id=version.id,
        run_type="scheduled",
        trade_date=trade_date,
        status="published",
        input_overrides={},
        started_at=now,
        finished_at=now,
        idempotency_key=f"test:{version.id}:scheduled:{trade_date}",
        published_at=now,
    )
    db_session.add(run)
    await db_session.flush()

    yield run
