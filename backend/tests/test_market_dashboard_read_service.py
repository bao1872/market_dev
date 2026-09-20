"""F2 Market Dashboard read model — 纯单元（无 DB）派生合同。

覆盖真正的业务语义，不连库：
- valid_count == 0 → ratio = None（不伪造 0%）
- EW index：首点 100；None 打断链，不 forward fill；前导 None 可恢复
- ranking：按 ma5_delta 排序；Top/Bottom；delta 用第 t 与第 t-5 个交易日行
- compare：各 scope 独立归一到 100
- 读路径只访问允许的三张表（不碰 bars_daily / instruments / MarketBoardMembership / F1A）
- market_data capability 门禁（无权限 → 403）
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

from app.models.user_capability import CAPABILITY_MARKET_DATA
from app.services.access_control_service import AccessContext, require_capability
from app.services.market_dashboard_read_service import (
    BoardMeta,
    MarketDailyRow,
    ScopeDailyRow,
    _ew_index,
    _ratio,
    build_compare,
    build_market_view,
    build_rankings,
)


# ---------------------------------------------------------------------------
# breadth ratio
# ---------------------------------------------------------------------------
def test_breadth_valid_zero_returns_none():
    assert _ratio(5, 0) is None
    assert _ratio(0, 0) is None
    assert _ratio(3, 5) == pytest.approx(0.6)
    assert _ratio(0, 5) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# EW index
# ---------------------------------------------------------------------------
def test_ew_index_first_valid_is_100():
    assert _ew_index([0.01, 0.02]) == pytest.approx([100.0, 102.0])


def test_ew_index_none_breaks_chain_no_forward_fill():
    # 链建立后遇到 None：该点为 None 且后续链被打断（不 forward fill）。
    assert _ew_index([0.01, None, 0.02]) == [pytest.approx(100.0), None, None]


def test_ew_index_leading_none_recovers():
    # 前导 None 只是无数据，不触发打断；首个有效点仍为 100。
    assert _ew_index([None, 0.01]) == [None, pytest.approx(100.0)]


# ---------------------------------------------------------------------------
# market view
# ---------------------------------------------------------------------------
def _mk_market(ew, *, v5=5, a5=3, v20=20, a20=12):
    return MarketDailyRow(
        trade_date=date(2026, 9, 1),
        ma5_above_count=a5,
        ma5_valid_count=v5,
        ma10_above_count=4,
        ma10_valid_count=5,
        ma20_above_count=a20,
        ma20_valid_count=v20,
        ma50_above_count=30,
        ma50_valid_count=50,
        ma120_above_count=80,
        ma120_valid_count=120,
        equal_weight_return=ew,
    )


def test_market_view_cards_and_series():
    rows = [_mk_market(0.01), _mk_market(0.02)]
    view = build_market_view(rows)
    assert view["projection_trade_date"] == "2026-09-01"
    assert view["cards"]["ma20"] == pytest.approx(12 / 20)
    assert view["cards"]["equal_weight_index"] == pytest.approx(100 * 1.02)
    assert len(view["series"]) == 2
    # 第 2 点 EW index = 100 * 1.02
    assert view["series"][1]["ew_index"] == pytest.approx(102.0)


def test_market_view_ratio_none_when_valid_zero():
    row = _mk_market(0.0, v20=0, a20=0)
    view = build_market_view([row])
    assert view["cards"]["ma20"] is None


def test_market_view_empty_returns_unavailable():
    view = build_market_view([])
    assert view["projection_trade_date"] is None
    assert view["cards"]["ma20"] is None
    assert view["series"] == []


# ---------------------------------------------------------------------------
# ranking
# ---------------------------------------------------------------------------
def _mk_scope(board_id, d, *, a5, v5, ew):
    return ScopeDailyRow(
        board_id=board_id,
        trade_date=d,
        membership_version="mv1",
        ma5_above_count=a5,
        ma5_valid_count=v5,
        ma10_above_count=a5,
        ma10_valid_count=v5,
        ma20_above_count=10,
        ma20_valid_count=10,
        ma50_above_count=10,
        ma50_valid_count=10,
        ma120_above_count=10,
        ma120_valid_count=10,
        equal_weight_return=ew,
    )


def _board(bid, *, type_, level):
    return BoardMeta(
        board_id=bid,
        name=f"b{level}",
        type=type_,
        hierarchy_level=level,
        membership_version="mv1",
        is_active=True,
    )


def test_rankings_delta_uses_t_and_t_minus_5():
    bid = __import__("uuid").uuid4()
    # 6 个交易日：ma5 ratio 线性变化 0.5 -> 0.6（t-5 到 t）
    rows = [
        _mk_scope(bid, date(2026, 9, d), a5=a, v5=10, ew=0.0)
        for d, a in [(1, 5), (2, 6), (3, 7), (4, 8), (5, 9), (6, 10)]
    ]
    boards = [_board(bid, type_="industry", level="L1")]
    out = build_rankings(boards, {bid: rows}, lookback=5)
    # current = 10/10 = 1.0；previous(t-5) = 5/10 = 0.5；delta = 0.5
    item = out["top"][0]
    assert item["current"]["ma5"] == pytest.approx(1.0)
    assert item["previous"]["ma5"] == pytest.approx(0.5)
    assert item["delta"]["ma5"] == pytest.approx(0.5)


def test_rankings_top_bottom_order_and_limit():
    b_up = __import__("uuid").uuid4()
    b_down = __import__("uuid").uuid4()
    b_flat = __import__("uuid").uuid4()
    boards = [
        _board(b_up, type_="industry", level="L1"),
        _board(b_down, type_="industry", level="L1"),
        _board(b_flat, type_="industry", level="L1"),
    ]
    scoped = {
        # ma5 ratio：up 升、down 降、flat 平
        b_up: [
            _mk_scope(b_up, date(2026, 9, d), a5=a, v5=10, ew=0.0)
            for d, a in [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5), (6, 9)]
        ],
        b_down: [
            _mk_scope(b_down, date(2026, 9, d), a5=a, v5=10, ew=0.0)
            for d, a in [(1, 9), (2, 9), (3, 9), (4, 9), (5, 9), (6, 5)]
        ],
        b_flat: [_mk_scope(b_flat, date(2026, 9, d), a5=5, v5=10, ew=0.0) for d in range(1, 7)],
    }
    out = build_rankings(boards, scoped, lookback=5, limit=2)
    assert out["top"][0]["board_id"] == str(b_up)
    assert out["bottom"][0]["board_id"] == str(b_down)
    assert len(out["top"]) == 2
    assert len(out["bottom"]) == 2


def test_rankings_excludes_boards_without_history():
    bid = __import__("uuid").uuid4()
    boards = [_board(bid, type_="industry", level="L1")]
    # 仅有 3 行（< lookback+1=6）→ 无法计算 delta，不参与排序
    scoped = {bid: [_mk_scope(bid, date(2026, 9, d), a5=5, v5=10, ew=0.0) for d in range(1, 4)]}
    out = build_rankings(boards, scoped, lookback=5)
    assert out["top"] == []
    assert out["bottom"] == []


# ---------------------------------------------------------------------------
# compare：各 scope 独立归一到 100
# ---------------------------------------------------------------------------
def test_compare_independent_rebasing():
    a = __import__("uuid").uuid4()
    c = __import__("uuid").uuid4()
    boards = [
        _board(a, type_="industry", level="L1"),
        _board(c, type_="concept", level="L1"),
    ]
    scoped = {
        a: [_mk_scope(a, date(2026, 9, d), a5=5, v5=10, ew=r) for d, r in [(1, 0.01), (2, 0.02)]],
        c: [_mk_scope(c, date(2026, 9, d), a5=5, v5=10, ew=r) for d, r in [(1, 0.03), (2, -0.01)]],
    }
    out = build_compare(boards, scoped)
    by_id = {b["board_id"]: b for b in out}
    assert by_id[str(a)]["points"][0]["ew_index"] == pytest.approx(100.0)
    assert by_id[str(a)]["points"][1]["ew_index"] == pytest.approx(100 * 1.02)
    assert by_id[str(c)]["points"][0]["ew_index"] == pytest.approx(100.0)
    assert by_id[str(c)]["points"][1]["ew_index"] == pytest.approx(100 * 0.99)


# ---------------------------------------------------------------------------
# 读路径表访问面（不碰 bars / instruments / membership / F1A）
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    def __init__(self):
        self.queries: list[str] = []

    async def execute(self, stmt):
        self.queries.append(str(stmt))
        return _FakeResult([])


@pytest.mark.asyncio
async def test_read_path_only_accesses_allowed_tables():
    import uuid

    from app.services.market_dashboard_read_service import (
        _fetch_boards,
        _fetch_market_rows,
        _fetch_scope_window,
        _fetch_single_board,
    )

    s = _FakeSession()
    bid = uuid.uuid4()
    # 直接驱动各 fetch helper：验证 read path 触达的表（不依赖返回结果）。
    await _fetch_market_rows(s, 250)
    await _fetch_boards(s, "industry", "L1")
    await _fetch_single_board(s, bid)
    await _fetch_scope_window(s, [bid], 10)

    joined = "\n".join(s.queries)
    # 允许的三张表
    assert "market_dashboard_market_daily" in joined
    assert "market_dashboard_scope_daily" in joined
    assert "market_boards" in joined
    # 禁止的表 / 服务
    assert "bars_daily" not in joined
    assert "instruments" not in joined
    assert "market_board_memberships" not in joined


# ---------------------------------------------------------------------------
# market_data capability 门禁
# ---------------------------------------------------------------------------
def _ctx(*, market_data_active: bool) -> AccessContext:
    return AccessContext(
        user_id="u1",
        account_status="active",
        roles=[],
        is_admin=False,
        is_member=False,
        subscription_active=False,
        default_route="/forbidden",
        capabilities={"market_data": {"active": market_data_active, "expires_at": None}},
    )


@pytest.mark.asyncio
async def test_market_data_capability_gate():
    # 无 market_data → 403
    with pytest.raises(HTTPException) as exc:
        await require_capability(CAPABILITY_MARKET_DATA)(
            ctx=_ctx(market_data_active=False)
        )
    assert exc.value.status_code == 403
    # 有 market_data → 通过（返回 ctx）
    ctx = await require_capability(CAPABILITY_MARKET_DATA)(
        ctx=_ctx(market_data_active=True)
    )
    assert ctx.capabilities["market_data"]["active"] is True
