"""[PANJI-MARKET-OVERVIEW] overview 聚合纯单元测试（不连 DB）。

覆盖 Reviewer 修正后的合同：
A. 涨跌家数来自 canonical adjusted return ``ret``（adj_close 坐标），
   不重新比较 raw close vs 前收：
   - 除权除息日（raw 20→10, factor 0.5→1.0 → adj_close 10→10 → ret=0）算 **平盘**，
     而非下跌（此用例在 db5ff200 的 raw-close 实现下会 FAIL）。
   - ret>0 涨 / ret<0 跌 / ret==0 平；ret=NaN（无前收坐标）不计入 valid、不算平。
B. 全市场成交额 = SUM(exact-T bars_daily.amount)：
   - 全部 amount 有效 → exact sum；
   - 任一 amount 缺失 → turnover_amount=None（fail-closed，不冒充全市场总额）；
   - 真实 amount=0 → 保留数值 0（若 canonical 源允许）。
C. 不复制整张 raw：ret 取自 stock_facts；turnover 直接 groupby raw amount（无整表副本）。
"""
from __future__ import annotations

from datetime import date
from uuid import uuid4

import pandas as pd

import app.services.market_dashboard_service as svc
from app.services import market_dashboard_projection_service as proj


def _raw(iid, dates, closes, factors, amounts):
    return pd.DataFrame(
        {
            "instrument_id": [iid] * len(dates),
            "trade_date": list(dates),
            "close": list(closes),
            "adj_factor": list(factors),
            "amount": list(amounts),
        }
    )


def _aggregate(raw: pd.DataFrame):
    sf = svc._compute_stock_facts_long(raw)
    dates = sorted(raw["trade_date"].unique())
    return proj._aggregate_market_overview_long(sf, raw, dates)


# ---------------------------------------------------------------
# A. 涨跌家数来自 canonical ret（含除权除息平盘）
# ---------------------------------------------------------------
def test_adjusted_return_flat_on_corporate_action():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    # raw close 20→10 看似下跌，但 factor 0.5→1.0 使 adj_close 10→10 → ret == 0 → 平盘
    raw = _raw(uuid4(), [d0, d1], [20.0, 10.0], [0.5, 1.0], [1.0, 1.0])
    adv, _ = _aggregate(raw)
    assert adv[d1] == (0, 0, 1, 1), "ret=0 必须算平盘，不得误判为下跌"
    assert adv[d0] == (0, 0, 0, 0), "首 bar 无前收坐标 → valid=0"


def test_advance_decline_uses_ret_sign():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    i1, i2 = uuid4(), uuid4()
    raw = pd.concat(
        [
            _raw(i1, [d0, d1], [10.0, 11.0], [1.0, 1.0], [1.0, 1.0]),  # 涨
            _raw(i2, [d0, d1], [10.0, 9.0], [1.0, 1.0], [1.0, 1.0]),  # 跌
        ],
        ignore_index=True,
    )
    adv, _ = _aggregate(raw)
    assert adv[d1] == (1, 1, 0, 2)


# ---------------------------------------------------------------
# B. 成交额完整性 fail-closed
# ---------------------------------------------------------------
def test_turnover_all_valid_sums_exactly():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    i1, i2 = uuid4(), uuid4()
    raw = pd.concat(
        [
            _raw(i1, [d0, d1], [10.0, 11.0], [1.0, 1.0], [100.0, 200.0]),
            _raw(i2, [d0, d1], [10.0, 11.0], [1.0, 1.0], [300.0, 400.0]),
        ],
        ignore_index=True,
    )
    _, to = _aggregate(raw)
    assert to[d0] == (100.0 + 300.0, 2)
    assert to[d1] == (200.0 + 400.0, 2)


def test_turnover_partial_missing_is_none():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    i1, i2 = uuid4(), uuid4()
    raw = pd.concat(
        [
            _raw(i1, [d0, d1], [10.0, 11.0], [1.0, 1.0], [100.0, 200.0]),
            _raw(i2, [d0, d1], [10.0, 11.0], [1.0, 1.0], [300.0, 400.0]),
        ],
        ignore_index=True,
    )
    raw2 = raw.copy()
    raw2.loc[raw2.index[0], "amount"] = float("nan")  # i1 d0 缺失
    _, to = _aggregate(raw2)
    assert to[d0] == (None, 1), "部分 amount 缺失 → 不得冒充全市场总额"
    assert to[d1] == (200.0 + 400.0, 2)


def test_turnover_real_zero_stays_numeric():
    d0, d1 = date(2026, 9, 17), date(2026, 9, 18)
    i1, i2 = uuid4(), uuid4()
    raw = pd.concat(
        [
            _raw(i1, [d0, d1], [10.0, 11.0], [1.0, 1.0], [100.0, 200.0]),
            _raw(i2, [d0, d1], [10.0, 11.0], [1.0, 1.0], [300.0, 400.0]),
        ],
        ignore_index=True,
    )
    raw3 = raw.copy()
    raw3.loc[raw3.index[0], "amount"] = 0.0  # 真实零成交额
    _, to = _aggregate(raw3)
    assert to[d0] == (0.0 + 300.0, 2)


# ---------------------------------------------------------------
# C. 空输入稳健
# ---------------------------------------------------------------
def test_empty_stock_facts_returns_empty():
    adv, to = proj._aggregate_market_overview_long(
        svc._compute_stock_facts_long(None), pd.DataFrame(columns=["trade_date", "amount"]), []
    )
    assert adv == {} and to == {}
