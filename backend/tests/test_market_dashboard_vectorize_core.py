"""E1A 长表向量化 historical core — parity / structural / synthetic scale tests.

A. 长表向量化 market 聚合 == 旧 _aggregate_breadth_for_date（多日、多股票）
B. invalid 中间坐标：ret 不得跨缺口
C. 停牌：缺 T bar 的股票不进 market T 聚合
D. 复权：raw 20/10 factor 0.5/1 -> adj_close 10/10 -> ret 0
E. warmup：首 display date 能算 MA120
F. 结构：_compute_stock_facts_long 不得出现 df.apply(axis=1)
G. 结构：market history 不再调用 _aggregate_breadth_for_date（market-only 场景 0 次）
H. 合成规模 sanity benchmark（1000x369）：仅报告 facts_seconds / market_aggregate_seconds
"""

import ast
import asyncio
import inspect
import time
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

import app.services.market_dashboard_service as svc
from app.domain.market_dashboard.breadth import WINDOWS
from app.repositories import bar_repository
from app.services.market_dashboard_service import (
    build_dashboard_history,
    build_member_closes,
    compute_breadth,
)


def _mk(dates, closes, factors=None) -> pd.DataFrame:
    """构造单只股票 bars DataFrame（与 test_market_dashboard_history._mk 等价）。"""
    if factors is None:
        factors = [1.0] * len(closes)
    return pd.DataFrame(
        {"close": list(closes), "adj_factor": list(factors)},
        index=pd.to_datetime(dates).date,
    )


def _long_from_bars(bars) -> pd.DataFrame:
    """dict[uuid, DataFrame(index=date, close/adj_factor)] -> 生产窄读取同 schema 长表。"""
    frames = []
    for iid, df in bars.items():
        if df is None or len(df) == 0:
            continue
        g = df.reset_index()
        date_col = "trade_date" if "trade_date" in g.columns else g.columns[0]
        g = g.rename(columns={date_col: "trade_date"})
        g.insert(0, "instrument_id", iid)
        g["amount"] = 1.0  # 测试用占位成交额（生产为 bars_daily.amount）
        g = g[["instrument_id", "trade_date", "close", "adj_factor", "amount"]]
        frames.append(g)
    if not frames:
        return pd.DataFrame(
            columns=["instrument_id", "trade_date", "close", "adj_factor", "amount"]
        )
    return pd.concat(frames, ignore_index=True)


def _assert_breadth_close(a, b):
    assert a.member_count == b.member_count
    assert a.valid_return_count == b.valid_return_count
    if b.equal_weight_return is None:
        assert a.equal_weight_return is None
    else:
        assert a.equal_weight_return == pytest.approx(b.equal_weight_return)
    for k in WINDOWS:
        wa, wb = a.windows[k], b.windows[k]
        assert wa.valid_count == wb.valid_count
        assert wa.above_count == wb.above_count
        if wb.ratio is None:
            assert wa.ratio is None
        else:
            assert wa.ratio == pytest.approx(wb.ratio)


# ---------------------------------------------------------------
# A. 长表向量化 market 聚合 parity（多日、多股票）
# ---------------------------------------------------------------

def test_long_market_aggregation_parity_multidate():
    end = date(2026, 9, 18)
    n = 130
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    c1 = [float(i + 1) for i in range(n)]
    c2 = [float((i % 5) + 1) for i in range(n)]
    bars = {i1: _mk(dates, c1), i2: _mk(dates, c2)}

    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    new_hist = {d: br for d, br, _ in svc._aggregate_market_history_long(long_facts, dates)}

    # 旧 per-instrument compute -> 旧 _aggregate_breadth_for_date
    # （A/B oracle compute_breadth == 旧 _aggregate_breadth_for_date 已由现有测试验证）
    legacy = {iid: svc._compute_stock_daily_facts(df) for iid, df in bars.items()}

    # 1) facts 逐 stock/date 逐字段完全相等
    fact_cols = ["adj_close", "ret"] + [f"ma{k}" for k in WINDOWS] + [f"above{k}" for k in WINDOWS]
    for iid in bars:
        lf = long_facts[long_facts["instrument_id"] == iid].set_index("trade_date").sort_index()
        old = legacy[iid]
        for col in fact_cols:
            for d in old.index:
                nv = lf.loc[d, col] if d in lf.index else None
                ov = old.loc[d, col]
                if pd.isna(ov):
                    assert pd.isna(nv), (iid, col, d, nv)
                else:
                    assert nv == pytest.approx(ov), (iid, col, d, nv, ov)

    # 2) 多日聚合相等（新向量化 == 旧逐日期聚合）
    for d in dates:
        _assert_breadth_close(new_hist[d], svc._aggregate_breadth_for_date(d, legacy))

    # 3) 末日再 tie 到 A/B oracle compute_breadth
    oracle = compute_breadth([build_member_closes(end, _mk(dates, c)) for c in (c1, c2)])
    _assert_breadth_close(new_hist[end], oracle)


# ---------------------------------------------------------------
# B. invalid 中间坐标：ret 不得跨缺口
# ---------------------------------------------------------------

def test_invalid_mid_return_no_cross_gap():
    dates = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
    iid = uuid4()
    bars = {iid: _mk(dates, [10.0, None, 20.0])}  # 中间 close invalid -> adj_close NaN
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    ret = long_facts.set_index("trade_date").sort_index()["ret"]
    # 中间坐标 invalid -> 其 ret 必须 NaN；且不得跨缺口（前一有效 coordinate 不 fallback）
    assert list(ret.isna()) == [True, True, True]
    # market 聚合该股票 ret 全 NaN -> ewr None
    for _d, br, ewr in svc._aggregate_market_history_long(long_facts, dates):
        assert ewr is None
        assert br.valid_return_count == 0


# ---------------------------------------------------------------
# C. 停牌：缺 T bar 的股票不进 market T 聚合（exact-T）
# ---------------------------------------------------------------

def test_suspension_excluded_from_market_t():
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates[:-1], [float(i + 1) for i in range(n - 1)]),  # 缺最后一日 bar
    }
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    hist = {d: br for d, br, _ in svc._aggregate_market_history_long(long_facts, dates)}
    legacy = {iid: svc._compute_stock_daily_facts(df) for iid, df in bars.items()}
    # 末日：i2 无 bar -> 仅 i1 exact-T
    _assert_breadth_close(hist[end], svc._aggregate_breadth_for_date(end, legacy))
    assert hist[end].member_count == 1
    # 早期：两只都有 -> member_count 2
    assert hist[dates[0]].member_count == 2


# ---------------------------------------------------------------
# D. 复权：raw 20/10 factor 0.5/1 -> adj_close 10/10 -> ret 0
# ---------------------------------------------------------------

def test_adjustment_factor_returns_zero():
    dates = [date(2026, 9, 17), date(2026, 9, 18)]
    iid = uuid4()
    bars = {iid: _mk(dates, [20.0, 10.0], factors=[0.5, 1.0])}
    lf = svc._compute_stock_facts_long(_long_from_bars(bars)).set_index("trade_date").sort_index()
    assert list(lf["adj_close"]) == pytest.approx([10.0, 10.0])
    assert lf.loc[dates[1], "ret"] == pytest.approx(0.0)


# ---------------------------------------------------------------
# E. warmup：首 display date 能正确 MA120
# ---------------------------------------------------------------

def test_warmup_first_display_date_ma120():
    end = date(2026, 9, 18)
    n = 369
    load_dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    display_dates = load_dates[-250:]
    iid = uuid4()
    # 单调上涨，保证 MA120 一旦满窗口即有效
    bars = {iid: _mk(load_dates, [float(i + 1) for i in range(n)])}
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    hist = {d: br for d, br, _ in svc._aggregate_market_history_long(long_facts, display_dates)}
    first = display_dates[0]
    # 首 display date 之前恰有 120 个实际 bar（indices 0..119）-> MA120 可算
    assert hist[first].windows[120].valid_count >= 1


# ---------------------------------------------------------------
# F. 结构：_compute_stock_facts_long 禁止 df.apply(axis=1)
# ---------------------------------------------------------------

def test_no_apply_axis1_in_long_facts():
    src = inspect.getsource(svc._compute_stock_facts_long)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "apply", "历史 facts 禁止 df.apply(axis=1)"
    # 确认确实走了向量化路径（而非逐股票 Python loop）
    assert "groupby" in src and "rolling" in src and "shift" in src


# ---------------------------------------------------------------
# G. 结构：market history 不再调用 _aggregate_breadth_for_date（market-only 0 次）
# ---------------------------------------------------------------

def test_market_history_does_not_call_legacy_aggregate(monkeypatch):
    calls = []
    orig = svc._aggregate_breadth_for_date

    def _spy(target_date, stock_facts, member_ids=None):
        calls.append(target_date)
        return orig(target_date, stock_facts, member_ids)

    end = date(2026, 9, 18)
    n = 60
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}

    async def _insts(_s):
        return [i1]

    async def _dates(_s, _end, _count):
        return list(dates)

    async def _boards(_s):
        return []

    async def _members(_s, bids):
        return {}

    async def _loader(_s, _ids, _sd, _ed):
        g = bars[i1].reset_index()
        date_col = "trade_date" if "trade_date" in g.columns else g.columns[0]
        g = g.rename(columns={date_col: "trade_date"})
        g.insert(0, "instrument_id", i1)
        return g[["instrument_id", "trade_date", "close", "adj_factor"]]

    monkeypatch.setattr(svc, "_query_market_instrument_ids", _insts)
    monkeypatch.setattr(svc, "_query_recent_trade_dates", _dates)
    monkeypatch.setattr(svc, "_query_active_boards", _boards)
    monkeypatch.setattr(svc, "_query_board_memberships", _members)
    monkeypatch.setattr(bar_repository, "get_dashboard_daily_facts_source", _loader)
    monkeypatch.setattr(svc, "_aggregate_breadth_for_date", _spy)

    snap = asyncio.run(build_dashboard_history(SimpleNamespace(), end))
    # market-only 场景：board/watch/selected 均空 -> 不应调用旧 _aggregate_breadth_for_date
    assert len(calls) == 0
    assert len(snap.market_history) == len(dates)


# ---------------------------------------------------------------
# H. 合成规模 sanity benchmark（1000 x 369）
# ---------------------------------------------------------------

def test_synthetic_scale_sanity_benchmark():
    n_inst = 1000
    n_days = 369
    end = date(2026, 9, 18)
    dates = [end - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    rng = np.random.default_rng(0)
    frames = []
    for _ in range(n_inst):
        iid = uuid4()
        closes = np.arange(n_days, dtype="float64") * 0.1 + rng.random(n_days) + 1.0
        factors = np.ones(n_days)
        frames.append(
            pd.DataFrame(
                {
                    "instrument_id": iid,
                    "trade_date": dates,
                    "close": closes,
                    "adj_factor": factors,
                }
            )
        )
    raw = pd.concat(frames, ignore_index=True)

    t0 = time.perf_counter()
    facts = svc._compute_stock_facts_long(raw)
    facts_sec = time.perf_counter() - t0

    t0 = time.perf_counter()
    _hist = svc._aggregate_market_history_long(facts, dates)
    agg_sec = time.perf_counter() - t0

    print(
        f"[E1A benchmark] instruments={n_inst} days={n_days} rows={len(raw)} "
        f"facts_seconds={facts_sec:.3f} market_aggregate_seconds={agg_sec:.3f}"
    )
    # 合成规模 sanity（非严格 gate）；若本地仍需数十秒 -> STOP 并报告
    assert facts_sec < 30.0
    assert agg_sec < 15.0
