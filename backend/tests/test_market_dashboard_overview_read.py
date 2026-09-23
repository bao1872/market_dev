"""[PANJI-MARKET-OVERVIEW] read model 快照/轨迹字段纯单元测试（不连 DB）。

覆盖：
A. 三大指数 rebasing（首有效显示点=100；分别归一；read-time 派生，不持久化）。
B. 最新卡片 change_pct = last vs prev 指数收盘（除权等非 100 起点无关）。
C. additive 字段（advance/decline/flat/turnover/limit up-down）进入 cards 与 series。
D. 全 None 行 → cards/series 字段为 None（不伪造 0）。
E. 前导 None 指数 → rebased 链首点起算，前导点 None。
"""
from __future__ import annotations

from datetime import date

import pytest

from app.services.market_dashboard_read_service import (
    MarketDailyRow,
    build_market_view,
)


def _row(*, sse, szse, chinext, adv, dec, flat, turn, lu, ld, ew, d):
    return MarketDailyRow(
        trade_date=d,
        ma5_above_count=3,
        ma5_valid_count=5,
        ma10_above_count=4,
        ma10_valid_count=5,
        ma20_above_count=12,
        ma20_valid_count=20,
        ma50_above_count=30,
        ma50_valid_count=50,
        ma120_above_count=80,
        ma120_valid_count=120,
        equal_weight_return=ew,
        advance_count=adv,
        decline_count=dec,
        flat_count=flat,
        change_valid_count=(adv or 0) + (dec or 0) + (flat or 0),
        turnover_amount=turn,
        turnover_valid_count=2,
        limit_up_count=lu,
        limit_down_count=ld,
        sse_close=sse,
        szse_close=szse,
        chinext_close=chinext,
    )


# ---------------------------------------------------------------
# A + B + C. rebasing / change_pct / additive 字段
# ---------------------------------------------------------------
def test_rebasing_first_valid_100_and_change_pct():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    rows = [
        _row(sse=3000.0, szse=12000.0, chinext=2500.0, adv=100, dec=50, flat=10,
             turn=1e12, lu=10, ld=3, ew=0.01, d=d0),
        _row(sse=3300.0, szse=12600.0, chinext=2625.0, adv=120, dec=40, flat=5,
             turn=1.1e12, lu=12, ld=2, ew=0.02, d=d1),
    ]
    view = build_market_view(rows)
    sse_rebased = [p["sse_rebased"] for p in view["series"]]
    assert sse_rebased[0] == pytest.approx(100.0)
    assert sse_rebased[1] == pytest.approx(110.0)

    cards = view["cards"]
    assert cards["sse_change_pct"] == pytest.approx(round(3300.0 / 3000.0 - 1.0, 4))
    assert cards["turnover_amount"] == pytest.approx(1.1e12)
    assert cards["limit_up_count"] == 12
    assert cards["advance_count"] == 120
    assert view["series"][1]["turnover_amount"] == pytest.approx(1.1e12)
    assert view["series"][1]["limit_up_count"] == 12


# ---------------------------------------------------------------
# D. 全 None → 不伪造 0
# ---------------------------------------------------------------
def test_null_fields_render_none():
    d0 = date(2026, 9, 17)
    rows = [
        _row(sse=None, szse=None, chinext=None, adv=None, dec=None, flat=None,
             turn=None, lu=None, ld=None, ew=0.0, d=d0),
    ]
    view = build_market_view(rows)
    cards = view["cards"]
    assert cards["sse_close"] is None
    assert cards["turnover_amount"] is None
    assert cards["advance_count"] is None
    assert cards["limit_up_count"] is None
    p = view["series"][0]
    assert p["sse_close"] is None
    assert p["sse_rebased"] is None


# ---------------------------------------------------------------
# E. 前导 None 指数 → rebased 自首个有效点起算
# ---------------------------------------------------------------
def test_rebasing_skips_leading_none():
    d0, d1, d2 = date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)
    rows = [
        _row(sse=None, szse=None, chinext=None, adv=1, dec=1, flat=1,
             turn=1.0, lu=1, ld=1, ew=0.0, d=d0),
        _row(sse=3000.0, szse=12000.0, chinext=2500.0, adv=1, dec=1, flat=1,
             turn=1.0, lu=1, ld=1, ew=0.01, d=d1),
        _row(sse=3300.0, szse=12600.0, chinext=2625.0, adv=1, dec=1, flat=1,
             turn=1.0, lu=1, ld=1, ew=0.02, d=d2),
    ]
    view = build_market_view(rows)
    rebased = [p["sse_rebased"] for p in view["series"]]
    assert rebased[0] is None
    assert rebased[1] == pytest.approx(100.0)
    assert rebased[2] == pytest.approx(110.0)
