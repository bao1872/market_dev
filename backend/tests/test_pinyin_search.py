"""拼音搜索测试 - advice.md 第六节。

测试内容：
1. compute_pinyin_initials 单元测试（核心用例 + 边界）
2. GET /instruments 拼音首字母前缀搜索（dmgf -> 东睦股份）
3. 大小写不敏感（DMGF 与 dmgf 等价）
4. 名称包含搜索（东睦 -> 东睦股份）
5. 代码完全匹配（600114 -> 东睦股份）
6. 搜索优先级排序（代码完全匹配 > 代码前缀 > 拼音前缀 > 名称包含）
7. 响应包含 pinyin_initials 字段

测试策略：
- sqlite 内存数据库 + 异步 SQLAlchemy（参考 test_instruments.py）
- 自带 fixture，不依赖 conftest 的 APP_ENV=test 配置
- 可直接 `python tests/test_pinyin_search.py` 运行，也可 pytest 运行
"""
from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.main import app
from app.models.instrument import Instrument
from app.services.pinyin_util import compute_pinyin_initials


def _make_instrument(symbol: str, name: str, pinyin: str | None, market: str = "SH") -> Instrument:
    return Instrument(
        id=uuid.uuid4(),
        symbol=symbol,
        name=name,
        pinyin_initials=pinyin,
        market=market,
        status="active",
        listing_date=date(2001, 8, 27),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


@pytest_asyncio.fixture
async def pinyin_db() -> AsyncGenerator[AsyncSession, None]:
    """创建内存 SQLite 异步会话，注入含 pinyin_initials 的测试数据。"""
    try:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    except Exception:
        pytest.skip("aiosqlite 不可用，跳过 DB 测试")

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Instrument.__table__.create(sync_conn, checkfirst=True)
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        test_instruments = [
            _make_instrument("600114", "东睦股份", "dmgf", "SH"),
            _make_instrument("603730", "岱美股份", "dmgf", "SH"),
            _make_instrument("600519", "贵州茅台", "gzmt", "SH"),
            _make_instrument("000001", "平安银行", "payh", "SZ"),
            # 优先级测试专用：symbol 完全匹配 / 前缀 / pinyin 前缀 同时命中
            _make_instrument("dmgf", "优先级A", None, "SH"),
            _make_instrument("dmgf2", "优先级B", None, "SH"),
        ]
        for inst in test_instruments:
            session.add(inst)
        await session.commit()

        async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
            yield session

        app.dependency_overrides[__import__("app.core.deps", fromlist=["get_db"]).get_db] = get_test_db

        yield session

        app.dependency_overrides.clear()

    await engine.dispose()


# ===== 单元测试：compute_pinyin_initials =====


def test_compute_pinyin_initials_core() -> None:
    """核心用例：中文名 -> 小写拼音首字母。"""
    assert compute_pinyin_initials("东睦股份") == "dmgf"
    assert compute_pinyin_initials("贵州茅台") == "gzmt"
    assert compute_pinyin_initials("隆基绿能") == "ljln"
    assert compute_pinyin_initials("平安银行") == "payh"
    assert compute_pinyin_initials("宁德时代") == "ndsd"


def test_compute_pinyin_initials_special_chars() -> None:
    """边界：含符号/字母的名称，符号剔除、字母转小写。"""
    assert compute_pinyin_initials("*ST康美") == "stkm"
    assert compute_pinyin_initials("1000ETF") == "1000etf"


def test_compute_pinyin_initials_empty() -> None:
    """边界：空输入返回 None。"""
    assert compute_pinyin_initials("") is None
    assert compute_pinyin_initials(None) is None
    assert compute_pinyin_initials("   ") is None


# ===== API 测试：拼音搜索 =====


@pytest.mark.asyncio
async def test_search_by_pinyin_prefix(pinyin_db: AsyncSession) -> None:
    """拼音首字母前缀搜索：dmgf -> 东睦股份 + 岱美股份。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "dmgf"})
    assert response.status_code == 200
    data = response.json()
    # 命中：东睦股份 + 岱美股份(pinyin 前缀) + 优先级A(symbol 完全匹配) + 优先级B(symbol 前缀)
    assert data["total"] == 4
    symbols = [item["symbol"] for item in data["items"]]
    assert "600114" in symbols  # 东睦股份
    assert "603730" in symbols  # 岱美股份


@pytest.mark.asyncio
async def test_search_by_pinyin_uppercase(pinyin_db: AsyncSession) -> None:
    """大小写不敏感：DMGF 与 dmgf 等价（pinyin_initials 前缀匹配）。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "DMGF"})
    assert response.status_code == 200
    data = response.json()
    # DMGF 转小写后匹配 pinyin_initials LIKE 'dmgf%'，命中东睦+岱美；
    # symbol LIKE 'DMGF%'（sqlite 大小写不敏感）命中优先级A/B
    assert data["total"] == 4
    symbols = [item["symbol"] for item in data["items"]]
    assert "600114" in symbols


@pytest.mark.asyncio
async def test_search_by_name_contains(pinyin_db: AsyncSession) -> None:
    """名称包含搜索：东睦 -> 东睦股份。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "东睦"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600114"
    assert data["items"][0]["name"] == "东睦股份"


@pytest.mark.asyncio
async def test_search_by_symbol_exact(pinyin_db: AsyncSession) -> None:
    """代码完全匹配：600114 -> 东睦股份。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "600114"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["symbol"] == "600114"
    assert data["items"][0]["name"] == "东睦股份"


@pytest.mark.asyncio
async def test_search_priority_ordering(pinyin_db: AsyncSession) -> None:
    """搜索优先级：代码完全匹配(0) < 代码前缀(1) < 拼音前缀(2) < 名称包含(3)。

    keyword=dmgf 命中：
    - 优先级A: symbol='dmgf' 完全匹配 -> rank 0
    - 优先级B: symbol='dmgf2' 前缀匹配 -> rank 1
    - 东睦股份/岱美股份: pinyin_initials='dmgf' 前缀 -> rank 2
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "dmgf", "page_size": 10})
    assert response.status_code == 200
    data = response.json()
    symbols = [item["symbol"] for item in data["items"]]
    # rank 0 (dmgf) 排第一
    assert symbols[0] == "dmgf"
    # rank 1 (dmgf2) 排第二
    assert symbols[1] == "dmgf2"
    # rank 2 (东睦股份 600114 / 岱美股份 603730) 排后，按 symbol 排序 600114 < 603730
    assert symbols[2] == "600114"
    assert symbols[3] == "603730"


@pytest.mark.asyncio
async def test_response_has_pinyin_initials(pinyin_db: AsyncSession) -> None:
    """响应包含 pinyin_initials 字段。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "600114"})
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert "pinyin_initials" in item
    assert item["pinyin_initials"] == "dmgf"


@pytest.mark.asyncio
async def test_search_no_match(pinyin_db: AsyncSession) -> None:
    """边界：无匹配 keyword 返回空。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/instruments", params={"keyword": "zzzznotexist"})
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert data["items"] == []


# ===== 独立运行入口（不依赖 pytest/conftest，可在容器内直接 python 运行）=====


if __name__ == "__main__":
    # 自测入口：直接运行验证（不写库表/不改生产数据）
    # 注意：fixture 是 pytest 专用，独立运行时手动驱动 sqlite 会话
    async def _main():
        print("=== test_pinyin_search 独立运行 ===")
        # 单元测试
        test_compute_pinyin_initials_core()
        print("OK test_compute_pinyin_initials_core")
        test_compute_pinyin_initials_special_chars()
        print("OK test_compute_pinyin_initials_special_chars")
        test_compute_pinyin_initials_empty()
        print("OK test_compute_pinyin_initials_empty")

        # 手动构建 sqlite 会话驱动 API 测试
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Instrument.__table__.create(sync_conn, checkfirst=True)
            )
        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            for inst in [
                _make_instrument("600114", "东睦股份", "dmgf", "SH"),
                _make_instrument("603730", "岱美股份", "dmgf", "SH"),
                _make_instrument("600519", "贵州茅台", "gzmt", "SH"),
                _make_instrument("000001", "平安银行", "payh", "SZ"),
                _make_instrument("dmgf", "优先级A", None, "SH"),
                _make_instrument("dmgf2", "优先级B", None, "SH"),
            ]:
                session.add(inst)
            await session.commit()

            async def get_test_db() -> AsyncGenerator[AsyncSession, None]:
                yield session

            app.dependency_overrides[__import__("app.core.deps", fromlist=["get_db"]).get_db] = get_test_db

            await test_search_by_pinyin_prefix(session)
            print("OK test_search_by_pinyin_prefix")
            await test_search_by_pinyin_uppercase(session)
            print("OK test_search_by_pinyin_uppercase")
            await test_search_by_name_contains(session)
            print("OK test_search_by_name_contains")
            await test_search_by_symbol_exact(session)
            print("OK test_search_by_symbol_exact")
            await test_search_priority_ordering(session)
            print("OK test_search_priority_ordering")
            await test_response_has_pinyin_initials(session)
            print("OK test_response_has_pinyin_initials")
            await test_search_no_match(session)
            print("OK test_search_no_match")

            app.dependency_overrides.clear()
        await engine.dispose()
        print("=== 全部通过 ===")

    asyncio.run(_main())
