"""E1B 长表向量化 scope 聚合 — parity / structural / synthetic scale tests。

A. 多 board overlap：新 long scope aggregate == 旧 _aggregate_breadth_for_date（逐 board/date/MA/计数/ewr）
B. exact-T：成员 T 无 bar → 该 scope T 不计入
C. invalid：成员 T 有 bar 但 MA/return invalid → denominator/return count 与旧 oracle 一致
D. empty board/date：缺失组合不出现在 agg；DTO 兜底 ratio=None 不是 0
E. UI 三路径 parity（全 build_dashboard_history 端到端）：
   scope_changes T/T-5 五档 + MA5/10 delta；watch 10 日 ewr + normalized_index；selected 多日五档
F. 结构：完整 history（board+watch+selected）monkeypatch _aggregate_breadth_for_date=raise 仍成功
G. stock facts 整批仅一次 + bars reader 仅一次（含 board+watch+selected）
H. 合成规模 sanity benchmark（1000x369 / 100 boards / overlap）：仅报告 scope 三段秒数
"""

import time
from datetime import date, timedelta
from uuid import UUID, uuid4

import numpy as np
import pandas as pd

import app.services.market_dashboard_service as svc
from app.domain.market_dashboard.breadth import WINDOWS
from tests.test_market_dashboard_history import _board, _run_history
from tests.test_market_dashboard_vectorize_core import (
    _assert_breadth_close,
    _long_from_bars,
    _mk,
)


def _membership_long(memberships: dict[UUID, list[UUID]]) -> pd.DataFrame:
    rows = [(bid, iid) for bid, mids in memberships.items() for iid in mids]
    return pd.DataFrame(rows, columns=["board_id", "instrument_id"])


def _legacy_facts(bars):
    return {iid: svc._compute_stock_daily_facts(df) for iid, df in bars.items()}


# ---------------------------------------------------------------
# A. 多 board overlap parity
# ---------------------------------------------------------------

def test_multi_board_overlap_scope_parity():
    end = date(2026, 9, 18)
    n = 60
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2, i3 = uuid4(), uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float((i % 7) + 1) for i in range(n)]),
        i3: _mk(dates, [float((i % 3) + 1) for i in range(n)]),
    }
    ba, bb, bc = uuid4(), uuid4(), uuid4()
    memberships = {ba: [i1, i2], bb: [i2, i3], bc: [i1, i2, i3]}  # 部分重叠
    mem_long = _membership_long(memberships)
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    legacy = _legacy_facts(bars)
    scope_agg = svc._aggregate_scope_breadth_long(long_facts, mem_long, [ba, bb, bc], dates)
    for bid, mids in memberships.items():
        for d in dates:
            got = scope_agg.get((bid, d))
            oracle = svc._aggregate_breadth_for_date(d, legacy, mids)
            if got is None:
                assert oracle.member_count == 0
            else:
                _assert_breadth_close(got, oracle)


# ---------------------------------------------------------------
# B. exact-T：成员 T 无 bar → 该 scope T 不计入
# ---------------------------------------------------------------

def test_scope_exact_t_member_missing_bar():
    end = date(2026, 9, 18)
    n = 30
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates[:-1], [float(i + 1) for i in range(n - 1)]),  # 缺最后一日 bar
    }
    b = uuid4()
    memberships = {b: [i1, i2]}
    mem_long = _membership_long(memberships)
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    legacy = _legacy_facts(bars)
    scope_agg = svc._aggregate_scope_breadth_long(long_facts, mem_long, [b], dates)
    for d in dates:
        got = scope_agg.get((b, d))
        oracle = svc._aggregate_breadth_for_date(d, legacy, [i1, i2])
        if got is None:
            assert oracle.member_count == 0
        else:
            _assert_breadth_close(got, oracle)
    assert scope_agg[(b, dates[-1])].member_count == 1  # 末日 i2 无 bar
    assert scope_agg[(b, dates[0])].member_count == 2   # 早期两只都在


# ---------------------------------------------------------------
# C. invalid：成员 T 有 bar 但 MA/return invalid → denominator 与 oracle 一致
# ---------------------------------------------------------------

def test_scope_invalid_coordinate_denominator():
    end = date(2026, 9, 18)
    n = 40
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    # i1 中间 close invalid → 该日 adj_close NaN → ret NaN；ma 分母也随之变化
    bars = {
        i1: _mk(dates, [10.0, None, 12.0, 14.0] + [float(j + 1) for j in range(4, n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    b = uuid4()
    memberships = {b: [i1, i2]}
    mem_long = _membership_long(memberships)
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    legacy = _legacy_facts(bars)
    scope_agg = svc._aggregate_scope_breadth_long(long_facts, mem_long, [b], dates)
    for d in dates:
        got = scope_agg.get((b, d))
        oracle = svc._aggregate_breadth_for_date(d, legacy, [i1, i2])
        if got is None:
            assert oracle.member_count == 0
        else:
            _assert_breadth_close(got, oracle)


# ---------------------------------------------------------------
# D. empty board/date：缺失组合不出现在 agg；DTO 兜底 ratio=None 不是 0
# ---------------------------------------------------------------

def test_scope_empty_board_date_ratio_none():
    end = date(2026, 9, 18)
    n = 10
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    b = uuid4()
    memberships = {b: [i1]}
    mem_long = _membership_long(memberships)
    long_facts = svc._compute_stock_facts_long(_long_from_bars(bars))
    d_out = date(2000, 1, 1)  # 完全无 member bar 的日期
    scope_agg = svc._aggregate_scope_breadth_long(long_facts, mem_long, [b], [d_out])
    assert (b, d_out) not in scope_agg  # 缺失组合不出现（不造 fake market row）
    got = scope_agg.get((b, d_out), svc._empty_market_breadth())
    assert got.member_count == 0
    for k in WINDOWS:
        assert got.windows[k].ratio is None
        assert got.windows[k].valid_count == 0
        assert got.windows[k].above_count == 0


# ---------------------------------------------------------------
# E. UI 三路径 parity（端到端 build_dashboard_history vs 旧 oracle）
# ---------------------------------------------------------------

def test_ui_scope_paths_parity_with_oracle(monkeypatch):
    end = date(2026, 9, 18)
    n = 60
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2, i3 = uuid4(), uuid4(), uuid4()
    ba = _board(uuid4(), name="A")
    bb = _board(uuid4(), name="B", scope_type="concept")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
        i3: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    memberships = {ba.id: [i1, i2], bb.id: [i2, i3]}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2, i3],
        load_dates=dates, bars=bars, boards=[ba, bb],
        memberships=memberships, watch_board_ids=[ba.id], selected_scope_ids=[bb.id],
    )
    legacy = _legacy_facts(bars)

    # A. scope_changes：T/T-5 五档 current + MA5/10 delta
    t_now = dates[-1]
    t_prev = dates[-(5 + 1)]
    for ch in snap.scope_changes:
        bid = UUID(ch.scope_key)
        b_now = svc._aggregate_breadth_for_date(t_now, legacy, memberships[bid])
        b_prev = svc._aggregate_breadth_for_date(t_prev, legacy, memberships[bid])
        assert ch.ma5_current == b_now.windows[5].ratio
        assert ch.ma10_current == b_now.windows[10].ratio
        assert ch.ma20_current == b_now.windows[20].ratio
        assert ch.ma50_current == b_now.windows[50].ratio
        assert ch.ma120_current == b_now.windows[120].ratio
        assert ch.ma5_previous == b_prev.windows[5].ratio
        assert ch.ma5_delta == svc._delta(b_now.windows[5].ratio, b_prev.windows[5].ratio)
        assert ch.ma10_previous == b_prev.windows[10].ratio
        assert ch.ma10_delta == svc._delta(b_now.windows[10].ratio, b_prev.windows[10].ratio)

    # B. watch：10 日 equal_weight_return + normalized_index 完全一致
    watch_dates = dates[-10:]
    ws = snap.watch_series[0]
    assert ws.scope_key == str(ba.id)
    ewrs = [
        (svc._aggregate_breadth_for_date(d, legacy, memberships[ba.id]).equal_weight_return,)
        for d in watch_dates
    ]
    oracle_idx = svc._rebase_index(ewrs)
    assert [p.normalized_index for p in ws.points] == oracle_idx
    for pt, d in zip(ws.points, watch_dates, strict=False):
        oracle = svc._aggregate_breadth_for_date(d, legacy, memberships[ba.id])
        assert pt.equal_weight_return == oracle.equal_weight_return

    # C. selected：多 sample dates 五档 breadth 一致
    ss = snap.selected_scope_history[0]
    assert ss.scope_key == str(bb.id)
    for pt, d in zip(ss.points, dates, strict=False):
        oracle = svc._aggregate_breadth_for_date(d, legacy, memberships[bb.id])
        _assert_breadth_close(pt.breadth, oracle)


# ---------------------------------------------------------------
# F. 结构：完整 history（board+watch+selected）monkeypatch legacy=raise 仍成功
# ---------------------------------------------------------------

def test_history_no_legacy_scope_aggregate(monkeypatch):
    def _raise(*_a, **_k):
        raise AssertionError("production 仍调用了 legacy _aggregate_breadth_for_date")

    monkeypatch.setattr(svc, "_aggregate_breadth_for_date", _raise)
    end = date(2026, 9, 18)
    n = 60
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    ba = _board(uuid4(), name="A")
    bb = _board(uuid4(), name="B", scope_type="concept")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[ba, bb],
        memberships={ba.id: [i1], bb.id: [i2]},
        watch_board_ids=[ba.id], selected_scope_ids=[bb.id],
    )
    # 成功产出（证明生产已脱离旧逐 scope/member 聚合）
    assert len(snap.scope_changes) == 2
    assert len(snap.watch_series) == 1
    assert len(snap.selected_scope_history) == 1


# ---------------------------------------------------------------
# G. stock facts 整批仅一次 + bars reader 仅一次（含 board+watch+selected）
# ---------------------------------------------------------------

def test_stock_facts_and_bars_once_with_full_scopes(monkeypatch):
    seen_facts = set()
    orig = svc._compute_stock_facts_long

    def _spy_facts(df):
        seen_facts.add(id(df))
        return orig(df)

    monkeypatch.setattr(svc, "_compute_stock_facts_long", _spy_facts)
    end = date(2026, 9, 18)
    n = 250
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    ba = _board(uuid4(), name="A")
    bb = _board(uuid4(), name="B", scope_type="concept")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, calls = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[ba, bb],
        memberships={ba.id: [i1], bb.id: [i2]},
        watch_board_ids=[ba.id, bb.id], selected_scope_ids=[ba.id, bb.id],
    )
    assert len(calls) == 1          # bars reader 仅一次
    assert len(seen_facts) == 1     # stock facts 整批仅一次
    assert len(snap.selected_scope_history) == 2


# ---------------------------------------------------------------
# H. 合成规模 sanity benchmark
# ---------------------------------------------------------------

def test_synthetic_scope_sanity_benchmark():
    n_inst = 1000
    n_days = 369
    end = date(2026, 9, 18)
    dates = [end - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    rng = np.random.default_rng(1)
    frames = []
    for _ in range(n_inst):
        iid = uuid4()
        closes = np.arange(n_days, dtype="float64") * 0.1 + rng.random(n_days) + 1.0
        frames.append(
            pd.DataFrame(
                {"instrument_id": iid, "trade_date": dates, "close": closes, "adj_factor": 1.0}
            )
        )
    raw = pd.concat(frames, ignore_index=True)
    long_facts = svc._compute_stock_facts_long(raw)

    n_boards = 100
    board_ids = [uuid4() for _ in range(n_boards)]
    all_iids = raw["instrument_id"].unique().tolist()
    membership_rows = []
    for bid in board_ids:
        mids = rng.choice(all_iids, size=100, replace=False).tolist()
        for m in mids:
            membership_rows.append((bid, m))
    membership_long = pd.DataFrame(membership_rows, columns=["board_id", "instrument_id"])

    t_now = dates[-1]
    t_prev = dates[-6]
    scope_dates = [t_now, t_prev]
    watch_dates = dates[-10:]
    sel_dates = dates[-250:]

    t0 = time.perf_counter()
    sc = svc._aggregate_scope_breadth_long(long_facts, membership_long, board_ids, scope_dates)
    scope_sec = time.perf_counter() - t0

    t0 = time.perf_counter()
    _wd = svc._aggregate_scope_breadth_long(long_facts, membership_long, board_ids[:10], watch_dates)
    watch_sec = time.perf_counter() - t0

    t0 = time.perf_counter()
    _sd = svc._aggregate_scope_breadth_long(long_facts, membership_long, board_ids[:1], sel_dates)
    sel_sec = time.perf_counter() - t0

    total = scope_sec + watch_sec + sel_sec
    print(
        f"[E1B benchmark] boards={n_boards} inst={n_inst} days={n_days} "
        f"membership_rows={len(membership_rows)} scope_sec={scope_sec:.3f} "
        f"watch_sec={watch_sec:.3f} selected_sec={sel_sec:.3f} total_sec={total:.3f}"
    )
    assert len(sc) > 0
    assert total < 30.0
