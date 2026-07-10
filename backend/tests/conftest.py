"""共享测试 fixtures - pytest 集成测试基础设施。

提供：
- 测试库连接校验（APP_ENV / TEST_DATABASE_URL）
- 测试专用 async_engine / TestAsyncSessionLocal
- async DB session fixture（savepoint 模式，被测代码调用 commit 也不污染数据库）
- 测试数据工厂 fixtures（用户、角色、订阅、邀请码、标的、策略、运行）
- HTTP 客户端 fixture（自动覆盖 get_db）

约束：
- 禁止在 APP_ENV != test 时连接测试库。
- TEST_DATABASE_URL 必须指向 *_test 数据库。
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

if _APP_ENV != "test":
    raise RuntimeError(
        f"测试必须在 APP_ENV=test 下运行，当前 APP_ENV={_APP_ENV!r}。"
        "请使用：APP_ENV=test TEST_DATABASE_URL=postgresql://... pytest tests/"
    )

if not _TEST_DATABASE_URL:
    raise RuntimeError(
        "TEST_DATABASE_URL 环境变量未设置。"
        "示例：TEST_DATABASE_URL=postgresql://user:pass@host:port/dbname_test"
    )

# [测试配置] - 描述: 校验数据库 URL scheme 与测试库命名
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
    """在测试 session 开始前对测试库应用 Alembic 迁移。"""
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
