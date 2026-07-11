"""行情列表 API 测试（PRD §8.1 / §12.1）。

测试内容：
1. GET /market/stocks?scope=market: 全市场列表 + 分页
2. GET /market/stocks?scope=watchlist: 仅返回用户自选
3. 搜索关键词 query
4. is_watchlisted 标记
5. 响应结构（items + page + page_size + total + price_as_of + state_as_of + boards_as_of）
6. page_size 上限 100
7. 未认证返回 401
8. industry/concept/state 参数返回 422
9. sort 白名单校验（非法字段返回 422）
10. SQL 查询数量固定（5 条，不随 page_size 增长）
11. EXPLAIN 验证关键查询使用索引

测试策略：
- 使用 conftest 的 db_session / client fixtures（PostgreSQL 测试库）
- 通过 dependency_overrides 注入认证用户
- SQL 计数通过 SQLAlchemy event listener（before_cursor_execute）精确验证
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_active_user
from app.main import app
from app.models.instrument import Instrument
from app.models.user import User
from app.models.watchlist import UserWatchlistItem


@pytest_asyncio.fixture
async def market_stocks_client(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory,
    instrument_factory,
    subscription_factory,
) -> AsyncGenerator[tuple[AsyncClient, User, list[Instrument]], None]:
    """提供已认证 HTTP 客户端 + 测试用户 + 3 只测试标的 + 订阅。"""
    user = await user_factory(
        email="market_user@example.com",
        password_hash="fake-hash",
        timezone="Asia/Shanghai",
    )
    inst1 = await instrument_factory(
        symbol="600519", name="贵州茅台", market="SH", status="active",
    )
    inst2 = await instrument_factory(
        symbol="000001", name="平安银行", market="SZ", status="active",
    )
    inst3 = await instrument_factory(
        symbol="300750", name="宁德时代", market="SZ", status="active",
    )
    await subscription_factory(
        user_id=user.id,
        plan_code="observe_20",
        status="active",
        starts_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        source="invite",
    )

    # 直接创建一条自选记录（inst1）
    wl_item = UserWatchlistItem(
        user_id=user.id,
        instrument_id=inst1.id,
        source="manual",
        active=True,
    )
    db_session.add(wl_item)
    await db_session.flush()

    async def get_test_user() -> User:
        return user

    app.dependency_overrides[get_current_active_user] = get_test_user

    yield client, user, [inst1, inst2, inst3]

    app.dependency_overrides.pop(get_current_active_user, None)


@pytest.mark.asyncio
async def test_market_scope_list(market_stocks_client) -> None:
    """测试 market scope 列表查询：返回全部活跃 A 股。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    data = response.json()
    # 响应结构
    assert "items" in data
    assert "page" in data
    assert "page_size" in data
    assert "total" in data
    assert "price_as_of" in data
    assert "state_as_of" in data
    assert "boards_as_of" in data
    # as_of 字段不再存在
    assert "as_of" not in data
    # 至少包含测试标的
    assert data["total"] >= 3
    assert data["page"] == 1
    assert data["page_size"] == 50
    symbols = {item["symbol"] for item in data["items"]}
    assert "600519" in symbols
    assert "000001" in symbols


@pytest.mark.asyncio
async def test_market_scope_is_watchlisted(market_stocks_client) -> None:
    """测试 is_watchlisted 标记：inst1 在自选中，inst2 不在。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    items = response.json()["items"]
    by_symbol = {item["symbol"]: item for item in items}
    assert by_symbol["600519"]["is_watchlisted"] is True
    assert by_symbol["000001"]["is_watchlisted"] is False


@pytest.mark.asyncio
async def test_watchlist_scope(market_stocks_client) -> None:
    """测试 watchlist scope：仅返回用户自选。"""
    client, _, instruments = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "watchlist"})
    assert response.status_code == 200
    data = response.json()
    # 仅 inst1 在自选中
    assert data["total"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["symbol"] == "600519"
    assert data["items"][0]["is_watchlisted"] is True


@pytest.mark.asyncio
async def test_pagination(market_stocks_client) -> None:
    """测试分页查询。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "page": 1, "page_size": 2}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert len(data["items"]) <= 2


@pytest.mark.asyncio
async def test_page_size_max_100(market_stocks_client) -> None:
    """测试 page_size 上限 100：超过 100 应返回 422。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "page_size": 200}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_search_query(market_stocks_client) -> None:
    """测试搜索关键词（名称包含）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "query": "茅台"}
    )
    assert response.status_code == 200
    data = response.json()
    symbols = {item["symbol"] for item in data["items"]}
    assert "600519" in symbols
    # 不应包含不匹配的
    assert "000001" not in symbols


@pytest.mark.asyncio
async def test_row_fields(market_stocks_client) -> None:
    """测试每行返回页面所需全部字段。"""
    client, _, _ = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) > 0
    row = items[0]
    # 必需字段
    assert "instrument_id" in row
    assert "symbol" in row
    assert "name" in row
    assert "latest_price" in row
    assert "change_pct" in row
    assert "industry" in row
    assert "concepts" in row
    assert "dsa_state" in row
    assert "structure_state" in row
    assert "latest_event_title" in row
    assert "latest_event_time" in row
    assert "is_watchlisted" in row
    # industry/concepts 在 Phase 6 前为 null/空
    assert row["industry"] is None
    assert row["concepts"] == []


@pytest.mark.asyncio
async def test_unauthenticated_returns_401(client: AsyncClient) -> None:
    """测试未认证返回 401。"""
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 401


# ===== P0: industry/concept/state 返回 422 =====


@pytest.mark.asyncio
async def test_industry_param_returns_422(market_stocks_client) -> None:
    """industry 参数非空时返回 422（未实现）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "industry": "银行"}
    )
    assert response.status_code == 422
    assert "not yet implemented" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_concept_param_returns_422(market_stocks_client) -> None:
    """concept 参数非空时返回 422（未实现）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "concept": "新能源"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_state_param_returns_422(market_stocks_client) -> None:
    """state 参数非空时返回 422（未实现）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "state": "上行"}
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_empty_filter_params_ok(market_stocks_client) -> None:
    """industry/concept/state 为空字符串时正常返回（不触发 422）。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks",
        params={"scope": "market", "industry": "", "concept": "", "state": ""},
    )
    assert response.status_code == 200


# ===== P1: sort 白名单校验 =====


@pytest.mark.asyncio
async def test_sort_invalid_field_returns_422(market_stocks_client) -> None:
    """非法排序字段返回 422。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "sort": "invalid_field:asc"}
    )
    assert response.status_code == 422
    assert "Invalid sort field" in response.json()["detail"]


@pytest.mark.asyncio
async def test_sort_invalid_direction_returns_422(market_stocks_client) -> None:
    """非法排序方向返回 422。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "sort": "symbol:up"}
    )
    assert response.status_code == 422
    assert "Invalid sort direction" in response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort_param",
    [
        "name:asc",
        "name:desc",
        "symbol:asc",
        "symbol:desc",
        "change_pct:asc",
        "change_pct:desc",
        "dsa_state:asc",
        "dsa_state:desc",
        "latest_event_time:asc",
        "latest_event_time:desc",
    ],
)
async def test_sort_whitelist_accepted(market_stocks_client, sort_param: str) -> None:
    """白名单内排序字段+方向均返回 200。"""
    client, _, _ = market_stocks_client
    response = await client.get(
        "/market/stocks", params={"scope": "market", "sort": sort_param}
    )
    assert response.status_code == 200


# ===== P1: as_of 时间戳字段 =====


@pytest.mark.asyncio
async def test_as_of_fields_null_when_no_data(market_stocks_client) -> None:
    """无行情/快照数据时 price_as_of/state_as_of 为 null，boards_as_of 始终为 null。"""
    client, _, _ = market_stocks_client
    response = await client.get("/market/stocks", params={"scope": "market"})
    assert response.status_code == 200
    data = response.json()
    # boards_as_of 始终为 null（qstock 未同步）
    assert data["boards_as_of"] is None
    # 无 bar 数据时 price_as_of 为 null
    assert data["price_as_of"] is None


# ===== P1: SQL 查询数量固定（5 条，不随 page_size 增长） =====


@pytest.mark.asyncio
async def test_sql_query_count_fixed(
    market_stocks_client,
    db_session: AsyncSession,
) -> None:
    """验证 SQL 查询数量固定为 5 条，不随 page_size 变化。

    使用 SQLAlchemy before_cursor_execute 事件精确计数 SELECT 语句。
    """
    from app.services.market_stocks_service import get_market_stocks

    client, user, _ = market_stocks_client

    query_counts: dict[int, int] = {}

    for ps in (10, 50, 100):
        counter = {"select_count": 0}

        def _on_execute(
            conn, cursor, statement, parameters, context, executemany,
            _counter=counter,
        ):
            stmt_lower = statement.strip().lower()
            if stmt_lower.startswith("select"):
                _counter["select_count"] += 1

        # 注册事件监听器（在 db_session 的 engine 上）
        engine = db_session.get_bind()
        event.listen(engine, "before_cursor_execute", _on_execute)
        try:
            await get_market_stocks(
                db=db_session,
                user_id=user.id,
                scope="market",
                query=None,
                page=1,
                page_size=ps,
                sort=None,
            )
        finally:
            event.remove(engine, "before_cursor_execute", _on_execute)

        query_counts[ps] = counter["select_count"]

    # 所有 page_size 的查询数量必须一致
    assert len(set(query_counts.values())) == 1, (
        f"查询数量不一致: {query_counts}"
    )
    # 查询数量应为 5（instruments + count + bars + snapshots + events）
    expected_count = 5
    actual_count = list(query_counts.values())[0]
    assert actual_count == expected_count, (
        f"期望 {expected_count} 条 SQL，实际 {actual_count} 条。"
        f"各 page_size 计数: {query_counts}"
    )


# ===== P1: EXPLAIN 验证关键查询使用索引 =====


@pytest.mark.asyncio
async def test_explain_uses_index(
    market_stocks_client,
    db_session: AsyncSession,
) -> None:
    """EXPLAIN 主查询（instruments + watchlist EXISTS）验证索引可用。

    测试库数据量小（3 行），PostgreSQL 默认选择 Seq Scan。
    通过 SET enable_seqscan = off 强制使用索引，验证索引存在且可用。
    """
    from uuid import uuid4

    from sqlalchemy import select

    from app.models.instrument import Instrument
    from app.models.watchlist import UserWatchlistItem
    from app.services.market_stocks_service import _build_search_conditions

    conditions, _ = _build_search_conditions(None)

    test_user_id = uuid4()
    watched_exists = (
        select(1)
        .where(
            UserWatchlistItem.instrument_id == Instrument.id,
            UserWatchlistItem.user_id == test_user_id,
            UserWatchlistItem.active.is_(True),
        )
        .exists()
    )
    base_stmt = (
        select(
            Instrument.id,
            Instrument.symbol,
            Instrument.name,
            Instrument.market,
            watched_exists.label("is_watchlisted"),
        )
        .where(*conditions)
        .order_by(Instrument.symbol)
        .limit(50)
    )

    # 编译并运行 EXPLAIN（强制关闭 seqscan 以验证索引可用性）
    compiled = base_stmt.compile(
        bind=db_session.get_bind(), compile_kwargs={"literal_binds": True}
    )
    # 先关闭 seqscan，再运行 EXPLAIN
    await db_session.execute(text("SET enable_seqscan = off"))
    explain_sql = f"EXPLAIN {compiled}"
    result = await db_session.execute(text(explain_sql))
    plan_lines = [row[0] for row in result.fetchall()]
    plan_text = "\n".join(plan_lines)

    # 验证使用索引扫描（强制关闭 seqscan 后应使用 Index Scan）
    assert "Index Scan" in plan_text, (
        f"EXPLAIN 结果未使用索引扫描（即使强制关闭 seqscan）:\n{plan_text}"
    )


