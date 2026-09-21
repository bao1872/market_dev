"""R2 Scope Explorer —— 正式 PG/API 证据（projection-only，全局 T / T-5）。

锁住的正式合同：
- industry / concept 隔离；industry L1/L2/L3 精确过滤；concept + hierarchy_level → 422
- q substring 搜索（转义通配符）、member_count / MA breadth / MA delta 范围过滤
- **全局 T / T-5**：T 与 PREV 取自全市场 projection 日历（最近 6 个投影交易日），
  所有 board 共用同一 T / PREV；board 缺中间日期**不得**滑到自身更老行；
  缺 PREV row → previous_*/delta = None（绝不 fallback）
- < 6 个 market dates → previous_trade_date=None，所有 delta=None；无 projection → 空 items
- valid_count == 0 → ratio None（禁止伪造 0%）
- 排序 NULLS LAST（asc/desc 皆然）+ board_id ASC 稳定二级排序
- 分页 + total（filter 后、pagination 前）；page_size 上限 100
- 认证边界 401；读路径只访问 3 张允许的表；查询数常数级（无 N+1）

隔离：全部使用 conftest 的 `db_session`（savepoint）fixture 做种子与读取。
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import event

from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)
from app.services.market_dashboard_scope_explorer_service import get_scope_explorer
from tests.test_market_dashboard_api import _auth_as, _create_admin_user

pytestmark = pytest.mark.postgres

_FORBIDDEN_TABLES = ("bars_daily", "instruments", "market_board_memberships")

# 全局 projection 日历（DESC）：T=09-10 …… T-5=09-05，更老=09-04
_T = date(2026, 9, 10)
_T5 = date(2026, 9, 5)
_D6 = date(2026, 9, 4)
_D1 = date(2026, 9, 9)

_SORT_TO_FIELD = {
    "name": "board_name",
    "member_count": "member_count",
    "ma5": "ma5",
    "ma10": "ma10",
    "ma20": "ma20",
    "ma50": "ma50",
    "ma120": "ma120",
    "ma5_delta": "ma5_delta",
    "ma10_delta": "ma10_delta",
}


# ---------------------------------------------------------------------------
# 种子
# ---------------------------------------------------------------------------
def _market_row(d: date) -> MarketDashboardMarketDaily:
    return MarketDashboardMarketDaily(
        trade_date=d,
        member_count=120,
        valid_return_count=100,
        equal_weight_return=0.01,
        ma5_above_count=50,
        ma5_valid_count=100,
        ma10_above_count=50,
        ma10_valid_count=100,
        ma20_above_count=50,
        ma20_valid_count=100,
        ma50_above_count=50,
        ma50_valid_count=100,
        ma120_above_count=50,
        ma120_valid_count=100,
        updated_at=datetime(d.year, d.month, d.day),
    )


def _scope(
    board_id,
    d: date,
    *,
    a5: int,
    v5: int,
    a10: int | None = None,
    v10: int | None = None,
    member: int | None = None,
) -> MarketDashboardScopeDaily:
    a10 = a5 if a10 is None else a10
    v10 = v5 if v10 is None else v10
    member = max(v5, v10, 1) if member is None else member
    assert a5 <= v5 <= member and a10 <= v10 <= member, "违反投影 count CHECK 不变量"
    return MarketDashboardScopeDaily(
        board_id=board_id,
        trade_date=d,
        membership_version="mv1",
        member_count=member,
        valid_return_count=member,
        equal_weight_return=0.0,
        ma5_above_count=a5,
        ma5_valid_count=v5,
        ma10_above_count=a10,
        ma10_valid_count=v10,
        ma20_above_count=member,
        ma20_valid_count=member,
        ma50_above_count=member,
        ma50_valid_count=member,
        ma120_above_count=member,
        ma120_valid_count=member,
        updated_at=datetime(d.year, d.month, d.day),
    )


def _board(name: str, btype: str, level: str, *, active: bool = True) -> MarketBoard:
    return MarketBoard(
        id=uuid4(),
        name=name,
        type=btype,
        hierarchyLevel=level,
        externalCode=f"EXT_{uuid4().hex[:10]}",
        taxonomy="CFI",
        taxonomyCompatibilityKey="k",
        membershipVersion="mv1",
        isActive=active,
    )


async def _seed(db) -> dict:
    """7 个 market dates（>= T_WINDOW=6）+ 8 个板块（含 anti-slide 反例）。"""
    a = _board("Ind-A", "industry", "L1")
    b = _board("Ind-B", "industry", "L2")
    c = _board("Ind-C", "industry", "L1")
    d = _board("Ind-D", "industry", "L1", active=False)
    e = _board("Con-E", "concept", "L1")
    f = _board("Ind-F", "industry", "L3")
    g = _board("Ind-G", "industry", "L2")
    h1 = _board("Ind-H1", "industry", "L3")
    h2 = _board("Ind-H2", "industry", "L3")
    db.add_all([a, b, c, d, e, f, g, h1, h2])
    db.add_all([_market_row(dd) for dd in (date(2026, 9, 10 - i) for i in range(7))])

    # A：全 7 日都有；T-5=0.5 / T-1=0.2（用于区分 T-5 与 T-1）/ D6=0.3
    for dd in (date(2026, 9, 10 - i) for i in range(7)):
        a5 = {_T: 8, _D1: 2, _T5: 5, _D6: 3}.get(dd, 4)
        db.add(_scope(a.id, dd, a5=a5, v5=10, a10=9, v10=10, member=10))

    # B：**缺 T-1（09-09）**，但有一个更老 D6（09-04）→ 必须用全局 PREV=T-5（0.6），
    #    绝不能滑到自身第 6 行 D6（0.1）
    for dd in (_T, date(2026, 9, 8), date(2026, 9, 7), date(2026, 9, 6), _T5, _D6):
        a5 = {_T: 8, _T5: 6, _D6: 1}.get(dd, 4)
        db.add(_scope(b.id, dd, a5=a5, v5=10, a10=3, v10=10, member=20))

    # C：**缺 T-5（09-05）**，但有更老 D6 → previous 必须 None（不 fallback 到 D6）
    for dd in (_T, _D1, date(2026, 9, 8), date(2026, 9, 7), date(2026, 9, 6), _D6):
        a5 = {_T: 7, _T5: 5}.get(dd, 4)
        db.add(_scope(c.id, dd, a5=a5, v5=10, member=30))

    # D：inactive（不得出现）
    db.add(_scope(d.id, _T, a5=5, v5=10, member=40))

    # E：concept；T=0.4 / T-5=0.2
    db.add(_scope(e.id, _T, a5=4, v5=10, member=40))
    db.add(_scope(e.id, _T5, a5=2, v5=10, member=40))

    # F：industry L3；T=0.6 / T-5=0.3
    db.add(_scope(f.id, _T, a5=6, v5=10, member=50))
    db.add(_scope(f.id, _T5, a5=3, v5=10, member=50))

    # G：T 行 ma5 valid_count=0 → ratio None（不伪造 0%）
    db.add(_scope(g.id, _T, a5=0, v5=0, a10=5, v10=10, member=60))
    db.add(_scope(g.id, _T5, a5=5, v5=10, member=60))

    # H1/H2：指标完全相同 → 只能靠 board_id ASC 稳定二级排序
    for board in (h1, h2):
        db.add(_scope(board.id, _T, a5=5, v5=10, member=70))
        db.add(_scope(board.id, _T5, a5=5, v5=10, member=70))

    await db.commit()
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f, "g": g, "h1": h1, "h2": h2}


async def _seed_short_calendar(db) -> dict:
    """只有 3 个 market dates（< T_WINDOW）→ previous_trade_date=None，delta 全 None。"""
    board = _board("Ind-Short", "industry", "L1")
    db.add(board)
    dates = [date(2026, 9, 10), date(2026, 9, 9), date(2026, 9, 8)]
    db.add_all([_market_row(dd) for dd in dates])
    for dd in dates:
        db.add(_scope(board.id, dd, a5=8, v5=10, member=10))
    await db.commit()
    return {"board": board}


async def _explore(db, **kw):
    params = {
        "scope_type": "industry",
        "hierarchy_level": None,
        "q": None,
        "page": 1,
        "page_size": 100,
        "sort": "ma5",
        "direction": "desc",
        "ranges": {},
    }
    params.update(kw)
    return await get_scope_explorer(db, **params)


def _ids(resp) -> list[str]:
    return [i.board_id for i in resp.items]


def _by_id(resp) -> dict[str, object]:
    return {i.board_id: i for i in resp.items}


def _expected_ids(items: list[dict], field: str, direction: str) -> list[str]:
    """文档化排序语义：NULLS LAST（asc/desc 皆然）+ board_id ASC 稳定二级排序。"""
    non_null = sorted([i for i in items if i[field] is not None], key=lambda i: i["board_id"])
    non_null.sort(key=lambda i: i[field], reverse=(direction == "desc"))
    nulls = sorted([i for i in items if i[field] is None], key=lambda i: i["board_id"])
    return [i["board_id"] for i in non_null] + [i["board_id"] for i in nulls]


# ===========================================================================
# A/B/C. isolation + hierarchy
# ===========================================================================
async def test_industry_isolation_excludes_concept(db_session):
    boards = await _seed(db_session)
    resp = await _explore(db_session, scope_type="industry")
    ids = _ids(resp)
    assert str(boards["a"].id) in ids
    assert str(boards["e"].id) not in ids, "concept 不得混入 industry"
    assert all(i.board_type == "industry" for i in resp.items)


async def test_concept_isolation_excludes_industry(db_session):
    boards = await _seed(db_session)
    resp = await _explore(db_session, scope_type="concept")
    assert _ids(resp) == [str(boards["e"].id)]
    assert resp.items[0].board_type == "concept"


async def test_industry_hierarchy_exact_filtering(db_session):
    boards = await _seed(db_session)
    l1 = await _explore(db_session, hierarchy_level="L1")
    # A/C 为 L1；D 为 L1 但 inactive → 排除；concept/L2/L3 不得混入
    assert set(_ids(l1)) == {str(boards["a"].id), str(boards["c"].id)}
    l2 = await _explore(db_session, hierarchy_level="L2")
    assert set(_ids(l2)) == {str(boards["b"].id), str(boards["g"].id)}
    l3 = await _explore(db_session, hierarchy_level="L3")
    assert set(_ids(l3)) == {str(boards["f"].id), str(boards["h1"].id), str(boards["h2"].id)}
    # hierarchy_level=None → 全部层级
    all_levels = await _explore(db_session)
    assert {i.hierarchy_level for i in all_levels.items} == {"L1", "L2", "L3"}


# ===========================================================================
# I/J. 全局 T / T-5（含 anti-slide）
# ===========================================================================
async def test_global_t_and_t5_dates(db_session):
    await _seed(db_session)
    resp = await _explore(db_session)
    assert resp.projection_trade_date == "2026-09-10"
    assert resp.previous_trade_date == "2026-09-05", "PREV 必须是全局 T-5，而不是 T-1 或更老"


async def test_previous_uses_t5_not_t1(db_session):
    boards = await _seed(db_session)
    resp = await _explore(db_session, hierarchy_level="L1")
    a = _by_id(resp)[str(boards["a"].id)]
    # T=0.8；T-5=0.5；T-1=0.2 → previous 必须是 0.5
    assert a.ma5 == pytest.approx(0.8)
    assert a.previous_ma5 == pytest.approx(0.5)
    assert a.ma5_delta == pytest.approx(0.3)


async def test_board_missing_middle_date_does_not_slide_to_older_row(db_session):
    """B 缺 09-09，但有一个更老 09-04（自身第 6 行）。

    错误实现（per-board row_number 滑窗）会取 09-04 → previous=0.1；
    正确实现使用全局 PREV=09-05 → previous=0.6。
    """
    boards = await _seed(db_session)
    resp = await _explore(db_session, hierarchy_level="L2")
    b = _by_id(resp)[str(boards["b"].id)]
    assert b.ma5 == pytest.approx(0.8)
    assert b.previous_ma5 == pytest.approx(0.6), "必须用全局 T-5（0.6），不得滑到更老 D6（0.1）"
    assert b.ma5_delta == pytest.approx(0.2)


async def test_board_missing_prev_row_yields_none_without_fallback(db_session):
    """C 缺 T-5（09-05）但有更老 D6 → previous/delta 必须 None（绝不 fallback）。"""
    boards = await _seed(db_session)
    resp = await _explore(db_session, hierarchy_level="L1")
    c = _by_id(resp)[str(boards["c"].id)]
    assert c.ma5 == pytest.approx(0.7)
    assert c.previous_ma5 is None
    assert c.previous_ma10 is None
    assert c.ma5_delta is None
    assert c.ma10_delta is None


async def test_below_window_previous_trade_date_is_none(db_session):
    boards = await _seed_short_calendar(db_session)
    resp = await _explore(db_session)
    assert resp.projection_trade_date == "2026-09-10"
    assert resp.previous_trade_date is None
    assert resp.total == 1
    item = resp.items[0]
    assert item.board_id == str(boards["board"].id)
    assert item.ma5 == pytest.approx(0.8)
    assert item.previous_ma5 is None and item.ma5_delta is None


async def test_no_market_projection_returns_empty_shape(db_session):
    """无任何 market projection → 200 空 items，不伪造日期。"""
    resp = await _explore(db_session)
    assert resp.projection_trade_date is None
    assert resp.previous_trade_date is None
    assert resp.total == 0
    assert resp.items == []


# ===========================================================================
# L. valid_count == 0 → ratio None
# ===========================================================================
async def test_valid_count_zero_yields_none_ratio(db_session):
    boards = await _seed(db_session)
    resp = await _explore(db_session, hierarchy_level="L2")
    g = _by_id(resp)[str(boards["g"].id)]
    assert g.ma5 is None, "valid_count == 0 必须 None，不得伪造 0%"
    assert g.ma5_delta is None


# ===========================================================================
# E/F/G/H. search + filters
# ===========================================================================
async def test_q_substring_search(db_session):
    await _seed(db_session)
    resp = await _explore(db_session, q="Ind-A")
    assert len(resp.items) == 1
    assert resp.items[0].board_name == "Ind-A"
    # 通配符必须转义（不得变成 match-all）
    wildcard = await _explore(db_session, q="%")
    assert wildcard.total == 0
    concept = await _explore(db_session, scope_type="concept", q="Con")
    assert concept.total == 1


async def test_member_count_range_filter(db_session):
    await _seed(db_session)
    resp = await _explore(db_session, ranges={"member_count_min": 50.0})
    assert resp.total == 4
    assert all(i.member_count >= 50 for i in resp.items)
    upper = await _explore(db_session, ranges={"member_count_max": 20.0})
    assert {i.member_count for i in upper.items} == {10, 20}


async def test_ma5_ma10_breadth_filters(db_session):
    boards = await _seed(db_session)
    ma5 = await _explore(db_session, ranges={"ma5_min": 0.75})
    assert set(_ids(ma5)) == {str(boards["a"].id), str(boards["b"].id)}
    ma10 = await _explore(db_session, ranges={"ma10_min": 0.5})
    assert set(_ids(ma10)) == {str(boards["a"].id)}


async def test_ma5_ma10_delta_filters(db_session):
    await _seed(db_session)
    resp = await _explore(db_session, ranges={"ma5_delta_min": 0.25})
    assert {_by_id(resp)[i].board_name for i in _ids(resp)} == {"Ind-A", "Ind-F"}


# ===========================================================================
# M/N/O/P. 排序
# ===========================================================================
@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("sort", sorted(_SORT_TO_FIELD))
async def test_sort_all_fields_nullslast_and_stable(db_session, sort: str, direction: str):
    await _seed(db_session)
    baseline = await _explore(db_session, page_size=100)
    all_items = [i.model_dump() for i in baseline.items]
    resp = await _explore(db_session, sort=sort, direction=direction, page_size=100)
    field = _SORT_TO_FIELD[sort]
    assert _ids(resp) == _expected_ids(all_items, field, direction)

    values = [getattr(i, field) for i in resp.items]
    nulls = [idx for idx, v in enumerate(values) if v is None]
    if nulls:
        assert min(nulls) > max(idx for idx, v in enumerate(values) if v is not None), (
            f"{sort}/{direction} 必须 NULLS LAST"
        )


@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_stable_board_id_secondary_order(db_session, direction: str):
    boards = await _seed(db_session)
    resp = await _explore(
        db_session, hierarchy_level="L3", q="Ind-H", sort="ma5", direction=direction
    )
    ids = _ids(resp)
    assert set(ids) == {str(boards["h1"].id), str(boards["h2"].id)}
    # 指标完全相同 → 只能按 board_id ASC（PG uuid 序 == canonical hex 字符串序）
    assert ids == sorted(ids)


# ===========================================================================
# Q. pagination + total
# ===========================================================================
async def test_pagination_and_total(db_session):
    await _seed(db_session)
    all_items = await _explore(db_session, page_size=100)
    total = all_items.total
    assert total == 8  # 8 个 active industry board（D inactive 排除）

    seen: list[str] = []
    for page in (1, 2, 3, 4):
        part = await _explore(db_session, page=page, page_size=2)
        assert part.total == total, "total 必须是 filter 后、pagination 前的总数"
        assert len(part.items) <= 2
        seen.extend(_ids(part))
    assert len(seen) == total
    assert len(set(seen)) == total, "分页不得重复/漂移"

    beyond = await _explore(db_session, page=99, page_size=2)
    assert beyond.items == []
    assert beyond.total == total, "越界页仍必须返回真实 total"


# ===========================================================================
# HTTP 层（client fixture）
# ===========================================================================
@pytest.fixture(autouse=True)
def _clear_overrides():
    from app.main import app

    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()


async def test_http_explorer_ok(client, db_session):
    await _seed(db_session)
    _auth_as(await _create_admin_user())
    r = await client.get(
        "/v1/market-dashboard/scopes?scope_type=industry&hierarchy_level=L1&sort=ma5_delta&direction=desc"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["projection_trade_date"] == "2026-09-10"
    assert body["previous_trade_date"] == "2026-09-05"
    assert body["total"] == 2
    assert {i["board_name"] for i in body["items"]} == {"Ind-A", "Ind-C"}


async def test_http_concept_hierarchy_level_422(client, db_session):
    await _seed(db_session)
    _auth_as(await _create_admin_user())
    r = await client.get("/v1/market-dashboard/scopes?scope_type=concept&hierarchy_level=L1")
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "qs",
    [
        "scope_type=board",
        "hierarchy_level=L4",
        "sort=bogus",
        "direction=sideways",
        "ma5_min=0.9&ma5_max=0.1",
        "member_count_min=9&member_count_max=1",
        "page_size=101",
    ],
)
async def test_http_invalid_params_422(client, db_session, qs: str):
    await _seed(db_session)
    _auth_as(await _create_admin_user())
    r = await client.get(f"/v1/market-dashboard/scopes?{qs}")
    assert r.status_code == 422, r.text


async def test_http_unauthenticated_401(client, db_session):
    await _seed(db_session)
    r = await client.get("/v1/market-dashboard/scopes")
    assert r.status_code == 401, r.text


# ===========================================================================
# T/U. 只读允许表 + 查询数常数级（无 N+1）
# ===========================================================================
async def test_bounded_query_count_and_allowed_tables_only(db_session):
    await _seed(db_session)

    statements: list[str] = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _on_execute)
    try:
        await _explore(db_session, page_size=5)
    finally:
        event.remove(engine, "before_cursor_execute", _on_execute)

    assert len(statements) <= 2, f"explorer 正常请求必须 <= 2 次查询，实际 {len(statements)}"
    joined = " ".join(statements).lower()
    for bad in _FORBIDDEN_TABLES:
        assert bad not in joined, f"读路径不得触碰 {bad}"

    # 查询数与 page_size 无关（无 N+1）
    counts: list[int] = []
    for size in (1, 25, 100):
        n = 0

        def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            nonlocal n
            if statement.lstrip().upper().startswith(("SELECT", "WITH")):
                n += 1

        event.listen(engine, "before_cursor_execute", _count)
        try:
            await _explore(db_session, page_size=size)
        finally:
            event.remove(engine, "before_cursor_execute", _count)
        counts.append(n)
    assert max(counts) <= 2, counts
