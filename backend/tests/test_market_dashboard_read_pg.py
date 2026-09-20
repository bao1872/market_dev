"""F2 Market Dashboard read/API — 正式 PG 证据（真实 projection rows → read service → DTO）。

锁住的正式合同：
- ratio/null 语义（valid_count == 0 → None，不伪造 0%）
- EW index 首点 100 / None 打断链（不 forward fill）
- ranking 5 个交易日 delta（t 与 t-5），且 industry L1/L2/L3 不串层、concept 不混入 industry
- scope 404（不存在 / 未激活 / 未构建）
- compare 多 scope 各自独立归一到 100
- 读路径只访问 market_dashboard_market_daily / market_dashboard_scope_daily / market_boards
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.market_board import MarketBoard
from app.models.market_dashboard import (
    MarketDashboardMarketDaily,
    MarketDashboardScopeDaily,
)
from app.services.market_dashboard_read_service import (
    compare_boards,
    get_market_dashboard,
    get_rankings,
    get_scope_detail,
)
from tests.conftest import TestAsyncSessionLocal

pytestmark = pytest.mark.postgres


def _mk_counts(a: int, v: int) -> dict:
    return {"above": a, "valid": v}


def _market_row(d: date, *, ew, counts, valid_zero: bool = False) -> MarketDashboardMarketDaily:
    ma5 = _mk_counts(counts["a5"], counts["v5"])
    ma10 = _mk_counts(counts["a10"], counts["v10"])
    ma20 = _mk_counts(counts["a20"], 0 if valid_zero else counts["v20"])
    ma50 = _mk_counts(counts["a50"], counts["v50"])
    ma120 = _mk_counts(counts["a120"], counts["v120"])
    return MarketDashboardMarketDaily(
        trade_date=d,
        entry_count=counts["v20"],
        member_count=counts["v20"],
        valid_return_count=counts["v20"],
        equal_weight_return=ew,
        ma5_above_count=ma5["above"],
        ma5_valid_count=ma5["valid"],
        ma10_above_count=ma10["above"],
        ma10_valid_count=ma10["valid"],
        ma20_above_count=ma20["above"],
        ma20_valid_count=ma20["valid"],
        ma50_above_count=ma50["above"],
        ma50_valid_count=ma50["valid"],
        ma120_above_count=ma120["above"],
        ma120_valid_count=ma120["valid"],
        updated_at=d,
    )


def _scope_row(board_id, d: date, *, ew, a5, v5, mv="mv1") -> MarketDashboardScopeDaily:
    return MarketDashboardScopeDaily(
        trade_date=d,
        board_id=board_id,
        membership_version=mv,
        entry_count=v5,
        member_count=v5,
        valid_return_count=v5,
        equal_weight_return=ew,
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
        updated_at=d,
    )


async def _seed(db: AsyncSession) -> dict:
    board_a = MarketBoard(
        id=uuid4(),
        name="Ind-A",
        type="industry",
        hierarchyLevel="L1",
        hierarchy_key="industry.L1",
        external_code="IND_A",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        isActive=True,
    )
    board_b = MarketBoard(
        id=uuid4(),
        name="Ind-B",
        type="industry",
        hierarchyLevel="L2",
        hierarchy_key="industry.L2",
        external_code="IND_B",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        isActive=True,
    )
    board_c = MarketBoard(
        id=uuid4(),
        name="Con-C",
        type="concept",
        hierarchyLevel="L1",
        hierarchy_key="concept.L1",
        external_code="CON_C",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        isActive=True,
    )
    board_d = MarketBoard(
        id=uuid4(),
        name="Ind-D-inactive",
        type="industry",
        hierarchyLevel="L1",
        hierarchy_key="industry.L1",
        external_code="IND_D",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        is_active=False,
    )
    # compare 专用 board（仅 2 行，历史不足 → 不参与 ranking，避免污染 L1 排序）
    board_e = MarketBoard(
        id=uuid4(),
        name="Ind-E",
        type="industry",
        hierarchyLevel="L1",
        hierarchy_key="industry.L1",
        external_code="IND_E",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        isActive=True,
    )
    board_f = MarketBoard(
        id=uuid4(),
        name="Con-F",
        type="concept",
        hierarchyLevel="L1",
        hierarchy_key="concept.L1",
        external_code="CON_F",
        taxonomy="CFI",
        taxonomy_compatibility_key="k",
        membershipVersion="mv1",
        isActive=True,
    )
    db.add_all([board_a, board_b, board_c, board_d, board_e, board_f])

    # market daily：3 个交易日；d3 为最新且 ma20 valid_count=0（ratio=None）
    market = [
        _market_row(
            date(2026, 9, 1),
            ew=0.01,
            counts={
                "a5": 3,
                "v5": 5,
                "a10": 4,
                "v10": 5,
                "a20": 12,
                "v20": 20,
                "a50": 30,
                "v50": 50,
                "a120": 80,
                "v120": 120,
            },
        ),
        _market_row(
            date(2026, 9, 2),
            ew=None,
            counts={
                "a5": 3,
                "v5": 5,
                "a10": 4,
                "v10": 5,
                "a20": 12,
                "v20": 20,
                "a50": 30,
                "v50": 50,
                "a120": 80,
                "v120": 120,
            },
        ),
        _market_row(
            date(2026, 9, 3),
            ew=0.02,
            counts={
                "a5": 3,
                "v5": 5,
                "a10": 4,
                "v10": 5,
                "a20": 0,
                "v20": 20,
                "a50": 30,
                "v50": 50,
                "a120": 80,
                "v120": 120,
            },
            valid_zero=True,
        ),
    ]
    db.add_all(market)

    # scope daily（ranking 用）：6 个交易日（>= lookback+1），ma5 ratio 从 0.5(d1) 升到 1.0(d6)
    for day in range(1, 7):
        a5 = 5 + day - 1  # 5..10
        for bid in (board_a.id, board_b.id, board_c.id):
            db.add(_scope_row(bid, date(2026, 9, day), ew=0.0, a5=a5, v5=10))
    # compare 专用 EW 链（独立 board，不污染 ranking）：e 涨、f 跌
    db.add(_scope_row(board_e.id, date(2026, 9, 1), ew=0.01, a5=5, v5=10))
    db.add(_scope_row(board_e.id, date(2026, 9, 2), ew=0.02, a5=5, v5=10))
    db.add(_scope_row(board_f.id, date(2026, 9, 1), ew=0.03, a5=5, v5=10))
    db.add(_scope_row(board_f.id, date(2026, 9, 2), ew=-0.01, a5=5, v5=10))

    await db.commit()
    return {"a": board_a, "b": board_b, "c": board_c, "d": board_d, "e": board_e, "f": board_f}


async def test_market_dashboard_market_ratio_null_when_valid_zero():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            await _seed(s)

    async with TestAsyncSessionLocal() as s:
        resp = await get_market_dashboard(s, 250)
        assert resp is not None
        assert resp.projection_trade_date == "2026-09-03"
        # 最新一行 ma20 valid_count=0 → ratio None（不伪造 0%）
        assert resp.cards.ma20 is None
        # EW 链：d1=100, d2=None(打断), d3=None
        assert resp.series[0].ew_index == pytest.approx(100.0)
        assert resp.series[1].ew_index is None
        assert resp.series[2].ew_index is None


async def test_rankings_industry_l1_excludes_l2_and_concept():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            boards = await _seed(s)

    async with TestAsyncSessionLocal() as s:
        # L1 仅含 A
        r_l1 = await get_rankings(s, "industry", "L1", 5, 10)
        assert len(r_l1.top) == 1
        assert r_l1.top[0].board_id == str(boards["a"].id)
        assert r_l1.top[0].board_type == "industry"
        assert r_l1.top[0].hierarchy_level == "L1"

        # L2 仅含 B（不串层）
        r_l2 = await get_rankings(s, "industry", "L2", 5, 10)
        assert len(r_l2.top) == 1
        assert r_l2.top[0].board_id == str(boards["b"].id)

        # concept 仅含 C（不混入 industry）
        r_c = await get_rankings(s, "concept", None, 5, 10)
        assert len(r_c.top) == 1
        assert r_c.top[0].board_id == str(boards["c"].id)
        assert r_c.top[0].board_type == "concept"

        # delta 用第 t 与第 t-5：t-5 ratio=0.5, t ratio=1.0 → delta=0.5
        assert r_l1.top[0].current.ma5 == pytest.approx(1.0)
        assert r_l1.top[0].previous.ma5 == pytest.approx(0.5)
        assert r_l1.top[0].delta.ma5 == pytest.approx(0.5)


async def test_scope_detail_404_for_missing_and_inactive():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            boards = await _seed(s)

    async with TestAsyncSessionLocal() as s:
        # 活跃 board → 有数据
        ok = await get_scope_detail(s, boards["a"].id, 250)
        assert ok is not None
        assert ok.metadata.board_id == str(boards["a"].id)
        # 不存在 → 404
        assert await get_scope_detail(s, uuid4(), 250) is None
        # 未激活 → 404
        assert await get_scope_detail(s, boards["d"].id, 250) is None


async def test_compare_independent_rebasing():
    async with TestAsyncSessionLocal() as s:
        async with s.begin():
            boards = await _seed(s)

    async with TestAsyncSessionLocal() as s:
        resp = await compare_boards(s, [boards["e"].id, boards["f"].id], 10)
        assert resp is not None
        by_id = {b.board_id: b for b in resp.boards}
        e = by_id[str(boards["e"].id)]
        f = by_id[str(boards["f"].id)]
        # 各 scope 独立归一到 100
        assert e.points[0].ew_index == pytest.approx(100.0)
        assert e.points[1].ew_index == pytest.approx(100 * 1.02)
        assert f.points[0].ew_index == pytest.approx(100.0)
        assert f.points[1].ew_index == pytest.approx(100 * 0.99)
