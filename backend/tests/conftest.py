"""共享测试 fixtures - pytest 集成测试基础设施。

提供：
- async DB session fixture（每个测试独立事务，测试后回滚）
- 测试数据工厂 fixtures（用户、策略、运行、结果）
"""
import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.db import AsyncSessionLocal, async_engine
from app.models.base import Base


@pytest_asyncio.fixture
async def db_session():
    """提供独立的事务性 DB session，测试后自动回滚并释放连接。"""
    async with AsyncSessionLocal() as session:
        nested = await session.begin_nested()
        yield session
        await nested.rollback()
        await session.close()
    # 释放 asyncpg 连接，避免跨 event loop 复用导致 "attached to a different loop"
    await async_engine.dispose()


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
