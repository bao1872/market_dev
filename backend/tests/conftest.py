"""共享测试 fixtures - pytest 集成测试基础设施。

提供：
- 测试库连接校验（APP_ENV / TEST_DATABASE_URL）
- 测试专用 async_engine / AsyncSessionLocal
- async DB session fixture（每个测试独立事务，测试后回滚）
- 测试数据工厂 fixtures（用户、策略、运行、结果）

约束：
- 禁止在 APP_ENV != test 时连接测试库。
- TEST_DATABASE_URL 必须指向 *_test 数据库。
"""
import asyncio
import os
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


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

# 解析库名并校验必须包含 _test
from urllib.parse import urlparse

_parsed = urlparse(_TEST_DATABASE_URL)
_db_name = (_parsed.path or "").lstrip("/")
if "_test" not in _db_name:
    raise RuntimeError(
        f"TEST_DATABASE_URL 必须指向测试库（库名含 _test），当前库名={_db_name!r}"
    )

# 统一转换为 asyncpg 驱动格式
_TEST_ASYNC_URL = _TEST_DATABASE_URL.replace(
    "postgresql+psycopg://", "postgresql+asyncpg://"
).replace(
    "postgresql://", "postgresql+asyncpg://"
)

# 测试专用 engine / session factory
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
async def db_session():
    """提供独立的事务性 DB session，测试后自动回滚并释放连接。"""
    async with TestAsyncSessionLocal() as session:
        nested = await session.begin_nested()
        yield session
        await nested.rollback()
        await session.close()
    # 释放 asyncpg 连接，避免跨 event loop 复用导致 "attached to a different loop"
    await test_async_engine.dispose()


@pytest_asyncio.fixture
async def test_user(db_session):
    """创建测试用户。"""
    from app.models.user import User

    user = User(
        email=f"test_{uuid.uuid4().hex[:8]}@test.com",
        password_hash="$2b$12$dummyhash",
        status="active",
    )
    db_session.add(user)
    await db_session.flush()
    yield user


@pytest_asyncio.fixture
async def test_instrument(db_session):
    """创建测试标的（满足 FK 约束）。"""
    from app.models.instrument import Instrument

    instrument = Instrument(
        symbol=f"T{uuid.uuid4().hex[:5]}",
        name="测试标的",
        market="SZ",
        status="active",
    )
    db_session.add(instrument)
    await db_session.flush()
    yield instrument


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
        display_name="DSA选股策略",
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
