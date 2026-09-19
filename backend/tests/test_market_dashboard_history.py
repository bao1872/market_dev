"""Market Dashboard Checkpoint C 历史序列单元测试（PURE_UNIT_TEST，无真实 DB）。

覆盖合同：
A. historical parity（C 路径 vs A/B 单日 oracle）
B. warmup（首个 display date 能算 MA120）
C. market history 多日 valid/above/ratio
D. exact-date（T 无 bar 不进聚合）
E. 全市场等权指数（首点 100，后续 prev*(1+r)）
F. 板块 5 日变化用 display_dates[-1] vs [-6]
G. latest snapshot replay（不引用 PIT owner）
H. MA5/MA10 delta None 语义
I. watch board 归一化（首点 100）
J. bars loader 只调用一次
K. 股票历史事实每只只算一次（不随 board 重算）
"""
from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

from app.domain.market_dashboard.breadth import WINDOWS, compute_breadth
from app.repositories import bar_repository
from app.services import market_dashboard_service as svc


def _mk(dates, closes, factors=None) -> pd.DataFrame:
    """构造单只股票 bars DataFrame（index=date，含 close/adj_factor）。"""
    if factors is None:
        factors = [1.0] * len(closes)
    return pd.DataFrame(
        {"close": list(closes), "adj_factor": list(factors)},
        index=pd.to_datetime(dates).date,
    )


def _run_history(
    monkeypatch,
    *,
    end_date,
    instrument_ids,
    load_dates,
    bars,
    boards,
    memberships,
    watch_board_ids=(),
    selected_scope_ids=(),
):
    async def _insts(_s):
        return list(instrument_ids)

    async def _dates(_s, _end, _count):
        return list(load_dates)

    async def _boards(_s):
        return list(boards)

    async def _members(_s, bids):
        return {b: memberships.get(b, []) for b in bids}

    calls = []

    async def _loader(_s, _ids, _sd, _ed):
        calls.append((_sd, _ed))
        return bars

    monkeypatch.setattr(svc, "_query_market_instrument_ids", _insts)
    monkeypatch.setattr(svc, "_query_recent_trade_dates", _dates)
    monkeypatch.setattr(svc, "_query_active_boards", _boards)
    monkeypatch.setattr(svc, "_query_board_memberships", _members)
    monkeypatch.setattr(bar_repository, "get_daily_bars_batch", _loader)
    result = asyncio.run(
        svc.build_dashboard_history(
            SimpleNamespace(), end_date, watch_board_ids, selected_scope_ids
        )
    )
    return result, calls


# ---------------------------------------------------------------
# A. historical parity
# ---------------------------------------------------------------

def test_historical_parity_with_single_day_oracle(monkeypatch):
    end = date(2026, 9, 18)
    n = 130
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]  # 升序连续
    i1, i2 = uuid4(), uuid4()
    closes1 = [float(i + 1) for i in range(n)]          # 严格递增
    closes2 = [float((i % 5) + 1) for i in range(n)]    # 周期模式
    bars = {i1: _mk(dates, closes1), i2: _mk(dates, closes2)}

    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[], memberships={},
    )
    hist = snap.market_history[-1]  # 末日（=end）
    oracle = compute_breadth(
        [svc.build_member_closes(end, _mk(dates, c)) for c in (closes1, closes2)]
    )
    for k in WINDOWS:
        assert hist.breadth.windows[k].valid_count == oracle.windows[k].valid_count
        assert hist.breadth.windows[k].above_count == oracle.windows[k].above_count
        if oracle.windows[k].ratio is None:
            assert hist.breadth.windows[k].ratio is None
        else:
            assert hist.breadth.windows[k].ratio == pytest.approx(oracle.windows[k].ratio)
    assert hist.breadth.equal_weight_return == pytest.approx(oracle.equal_weight_return)


# ---------------------------------------------------------------
# C-FIX1. invalid 中间坐标不得被 pct_change 隐式补齐
# ---------------------------------------------------------------

def test_missing_adj_factor_does_not_implicitly_fill_return(monkeypatch):
    """adj_close = [10, None, 20]（中间 adj_factor 缺失）→ 收益必须两处皆 None。

    冻结合同：invalid 坐标不 fallback，也不跨无效前一 bar 计算收益。
    pandas 2.x 的 pct_change() 默认 fill_method 会前向填充，故生产必须显式
    pct_change(fill_method=None)。此测试同时校验 C 路径与 A/B oracle 一致。
    """
    end = date(2026, 9, 18)
    dates = [end - timedelta(days=2), end - timedelta(days=1), end]
    closes = [10.0, 10.0, 20.0]
    factors = [1.0, None, 1.0]  # 中间 adj_factor 缺失 → 该日坐标 unavailable
    iid = uuid4()
    df = _mk(dates, closes, factors)

    # C 路径：stock-day facts
    facts = svc._compute_stock_daily_facts(df)
    assert pd.isna(facts.loc[dates[1], "adj_close"])  # 坐标 10, None, 20
    assert pd.isna(facts.loc[dates[1], "ret"])
    assert pd.isna(facts.loc[dates[2], "ret"])  # 不得跨无效前一 bar 计算收益

    # A/B oracle：build_member_closes + compute_breadth
    mc = svc.build_member_closes(end, df)
    assert list(mc.closes) == [10.0, None, 20.0]
    oracle = compute_breadth([mc])
    assert oracle.equal_weight_return is None

    # C 历史聚合：与 oracle 一致
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[iid],
        load_dates=dates, bars={iid: df}, boards=[], memberships={},
    )
    assert snap.market_history[-1].breadth.equal_weight_return is None


# ---------------------------------------------------------------
# B. warmup
# ---------------------------------------------------------------

def test_warmup_first_display_date_can_compute_ma120(monkeypatch):
    end = date(2026, 9, 18)
    n = 369
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    iid = uuid4()
    closes = [float(i + 1) for i in range(n)]  # 严格递增 → 全窗口 above
    bars = {iid: _mk(dates, closes)}

    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[iid],
        load_dates=dates, bars=bars, boards=[], memberships={},
    )
    assert len(snap.market_history) == 250  # 仅 display 窗口
    first = snap.market_history[0]  # load_dates[119]，已有 120 根
    assert first.breadth.windows[120].valid_count == 1
    assert first.breadth.windows[120].above_count == 1
    assert first.breadth.windows[120].ratio == 1.0


# ---------------------------------------------------------------
# D. exact-date
# ---------------------------------------------------------------

def test_exact_date_excludes_stale_bar_from_aggregation(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates[:-1], [float(i + 1) for i in range(n - 1)]),  # 缺末日 bar
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[], memberships={},
    )
    assert snap.market_history[-1].breadth.member_count == 1  # 仅 i1
    assert snap.market_history[-2].breadth.member_count == 2  # 倒数第二日两者均在


# ---------------------------------------------------------------
# E. 全市场等权指数
# ---------------------------------------------------------------

def test_market_equal_weight_index_rebase(monkeypatch):
    end = date(2026, 9, 18)
    # 额外 1 根 return warmup，保证首个 display date 已有 >=2 根 bar → 收益有效、index 可 seed 100
    n = 30
    all_n = n + 1
    all_dates = [end - timedelta(days=all_n - 1 - i) for i in range(all_n)]
    load_dates = all_dates[1:]  # 末 30 日；首 load 日有 1 根前置 warmup bar
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(all_dates, [float(i + 1) for i in range(all_n)]),
        i2: _mk(all_dates, [float(i + 1) for i in range(all_n)]),
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=load_dates, bars=bars, boards=[], memberships={},
    )
    pts = snap.market_history
    # 首点：equal_weight_return 有效 → 100（spec §8）
    assert pts[0].equal_weight_index == 100.0
    for idx, cur in enumerate(pts[1:], start=1):
        prev = pts[idx - 1]
        # 严格链式 prev*(1+r)，链路中断（prev 或当日收益为 None）后续恒 None
        if cur.breadth.equal_weight_return is None or prev.equal_weight_index is None:
            assert cur.equal_weight_index is None
        else:
            expected = prev.equal_weight_index * (1 + cur.breadth.equal_weight_return)
            assert cur.equal_weight_index == pytest.approx(expected)


# ---------------------------------------------------------------
# F + H. 板块 5 日变化 / delta None 语义
# ---------------------------------------------------------------

def test_board_5day_change_uses_display_t_and_t5(monkeypatch):
    end = date(2026, 9, 18)
    n = 10
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    board = SimpleNamespace(id=uuid4(), name="AI", type="concept", hierarchyLevel="L1")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates[-5:], [float(i + 1) for i in range(5)]),  # T5 无 bar → delta None
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[board],
        memberships={board.id: [i2]},  # 仅 i2：T5 无成员 → ma5_previous None → delta None
    )
    assert len(snap.scope_changes) == 1
    ch = snap.scope_changes[0]
    assert ch.current_date == dates[-1]
    assert ch.previous_date == dates[-6]
    assert ch.ma5_delta is None  # i2 在 T5 无 bar → ma5_previous None → delta None


def test_membership_basis_is_latest_snapshot_replay(monkeypatch):
    end = date(2026, 9, 18)
    n = 10
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    board = SimpleNamespace(id=uuid4(), name="L1", type="industry", hierarchyLevel="L1")
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[board], memberships={board.id: [i1]},
    )
    assert snap.membership_basis == "latest_snapshot_replay"


# ---------------------------------------------------------------
# I. watch board 归一化
# ---------------------------------------------------------------

def test_watch_board_rebased_index_first_is_100(monkeypatch):
    end = date(2026, 9, 18)
    n = 30
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    watch = SimpleNamespace(id=uuid4(), name="关注", type="concept", hierarchyLevel="L1")
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[watch],
        memberships={watch.id: [i1]}, watch_board_ids=[watch.id],
    )
    assert len(snap.watch_series) == 1
    series = snap.watch_series[0]
    assert series.points[0].normalized_index == 100.0
    for prev, cur in zip(series.points, series.points[1:], strict=False):
        if cur.equal_weight_return is None:
            assert cur.normalized_index is None
        elif prev.normalized_index is not None:
            assert cur.normalized_index == pytest.approx(
                prev.normalized_index * (1 + cur.equal_weight_return)
            )


# ---------------------------------------------------------------
# J. bars loader 一次
# ---------------------------------------------------------------

def test_bars_loader_called_once_for_full_history(monkeypatch):
    end = date(2026, 9, 18)
    n = 250
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    b1 = SimpleNamespace(id=uuid4(), name="A", type="industry", hierarchyLevel="L1")
    b2 = SimpleNamespace(id=uuid4(), name="B", type="concept", hierarchyLevel="L1")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, calls = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[b1, b2],
        memberships={b1.id: [i1], b2.id: [i2]},
        watch_board_ids=[b1.id, b2.id],
    )
    assert len(calls) == 1
    assert snap.membership_basis == "latest_snapshot_replay"


# ---------------------------------------------------------------
# K. 股票历史事实每只只计算一次
# ---------------------------------------------------------------

def test_stock_facts_computed_once_per_instrument(monkeypatch):
    end = date(2026, 9, 18)
    n = 250
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    b1 = SimpleNamespace(id=uuid4(), name="A", type="industry", hierarchyLevel="L1")
    b2 = SimpleNamespace(id=uuid4(), name="B", type="concept", hierarchyLevel="L1")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    seen = set()
    orig = svc._compute_stock_daily_facts

    def _spy(df):
        seen.add(id(df))
        return orig(df)

    monkeypatch.setattr(svc, "_compute_stock_daily_facts", _spy)
    _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[b1, b2],
        memberships={b1.id: [i1], b2.id: [i2]}, watch_board_ids=[b1.id, b2.id],

    )
    # 每只股票只算一次（board 聚合不重算股票 MA）
    assert len(seen) == 2


# ===============================================================
# Checkpoint D — UI 反推数据补齐（行业/概念详情五档 + 历史）
# ===============================================================

def _board(bid, *, name="", scope_type="industry", level="L1"):
    return SimpleNamespace(id=bid, name=name, type=scope_type, hierarchyLevel=level)


# A. 五档 current 宽度齐全且来自 current date 聚合
def test_scope_current_five_breadths(monkeypatch):
    end = date(2026, 9, 18)
    n = 130
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    board = _board(uuid4(), name="电子", level="L1")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[board], memberships={board.id: [i1, i2]},
    )
    ch = snap.scope_changes[0]
    t_now = dates[-1]
    b_now = svc._aggregate_breadth_for_date(t_now, _stock_facts(bars), [i1, i2])
    for k in (5, 10, 20, 50, 120):
        assert getattr(ch, f"ma{k}_current") == b_now.windows[k].ratio
    assert ch.ma5_current is not None and ch.ma10_current is not None
    assert ch.ma20_current is not None and ch.ma50_current is not None
    assert ch.ma120_current is not None


def _stock_facts(bars):
    return {iid: svc._compute_stock_daily_facts(df) for iid, df in bars.items()}


# B. delta 仍只 MA5/MA10，不新增其它 delta
def test_only_ma5_ma10_delta(monkeypatch):
    end = date(2026, 9, 18)
    n = 30
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    board = _board(uuid4())
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[board], memberships={board.id: [i1]},
    )
    ch = snap.scope_changes[0]
    assert ch.ma5_delta is not None
    assert ch.ma10_delta is not None
    assert not hasattr(ch, "ma20_delta")
    assert not hasattr(ch, "ma50_delta")
    assert not hasattr(ch, "ma120_delta")


# C. 选中 industry 历史（含完整 display dates，五档与聚合一致）
def test_selected_industry_history_full_and_consistent(monkeypatch):
    end = date(2026, 9, 18)
    n = 30
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    board = _board(uuid4(), name="电子", level="L1")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[board],
        memberships={board.id: [i1, i2]}, selected_scope_ids=[board.id],
    )
    series_list = snap.selected_scope_history
    assert len(series_list) == 1
    series = series_list[0]
    assert series.scope_key == str(board.id)
    assert len(series.points) == len(dates)  # 完整 display dates
    facts = _stock_facts(bars)
    for pt, d in zip(series.points, dates, strict=False):
        assert pt.trade_date == d
        expected = svc._aggregate_breadth_for_date(d, facts, [i1, i2])
        for k in (5, 10, 20, 50, 120):
            assert pt.breadth.windows[k].ratio == expected.windows[k].ratio
            assert pt.breadth.windows[k].valid_count == expected.windows[k].valid_count


# D. hierarchy 原样保留（L1/L2/L3）
def test_hierarchy_levels_preserved(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    b1 = _board(uuid4(), name="行业L1", level="L1")
    b2 = _board(uuid4(), name="行业L2", level="L2")
    b3 = _board(uuid4(), name="行业L3", level="L3")
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[b1, b2, b3],
        memberships={b1.id: [i1], b2.id: [i1], b3.id: [i1]},
        selected_scope_ids=[b1.id, b2.id, b3.id],
    )
    got = {s.scope_key: s for s in snap.selected_scope_history}
    assert got[str(b1.id)].hierarchy_level == "L1"
    assert got[str(b2.id)].hierarchy_level == "L2"
    assert got[str(b3.id)].hierarchy_level == "L3"


# E. concept 与 industry 共用同一 DTO/逻辑
def test_concept_history_same_shape(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    c1 = _board(uuid4(), name="AI算力", scope_type="concept", level="L1")
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[c1],
        memberships={c1.id: [i1]}, selected_scope_ids=[c1.id],
    )
    assert len(snap.selected_scope_history) == 1
    s = snap.selected_scope_history[0]
    assert s.scope_type == "concept"
    assert len(s.points) == len(dates)
    assert isinstance(s.points[0], svc.ScopeHistoryPoint)


# F. selected id 不在当前 active boards → 忽略
def test_unknown_selected_scope_ignored(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    board = _board(uuid4())
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[board], memberships={board.id: [i1]},
        selected_scope_ids=[uuid4()],  # 不存在
    )
    assert snap.selected_scope_history == []


# G. 无选择 → []
def test_no_selected_scope_empty(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    board = _board(uuid4())
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1],
        load_dates=dates, bars=bars, boards=[board], memberships={board.id: [i1]},
    )
    assert snap.selected_scope_history == []


# H. latest snapshot replay：当前成员回放整个历史；不引用 PIT
def test_selected_scope_uses_latest_membership_across_history(monkeypatch):
    end = date(2026, 9, 18)
    n = 20
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    board = _board(uuid4(), name="电子")
    # i2 是“当前”成员，但其 bar 实际从中间才开始；latest replay 下仍参与早期历史聚合
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates[5:], [float(i + 1) for i in range(n - 5)]),
    }
    snap, _ = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[board],
        memberships={board.id: [i1, i2]}, selected_scope_ids=[board.id],
    )
    series = snap.selected_scope_history[0]
    assert snap.membership_basis == "latest_snapshot_replay"
    # 早期 display date，i2 尚无 bar → 仅 i1 计入；但成员集合是“当前”的（i2 在列）
    early = series.points[0]
    late = series.points[-1]
    assert early.breadth.member_count == 1  # 仅 i1 exact-T
    assert late.breadth.member_count == 2   # i1 + i2 均 exact-T
    # 所有点都用同一当前成员集合（未引用 PIT 历史成员）
    assert all(pt.breadth.member_count >= 1 for pt in series.points)


# I. 即使 watch + selected 都多个，bars loader 仍只一次
def test_one_bars_load_with_watch_and_selected(monkeypatch):
    end = date(2026, 9, 18)
    n = 250
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    b1 = _board(uuid4(), name="A")
    b2 = _board(uuid4(), name="B", scope_type="concept")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    snap, calls = _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[b1, b2],
        memberships={b1.id: [i1], b2.id: [i2]},
        watch_board_ids=[b1.id, b2.id], selected_scope_ids=[b1.id, b2.id],
    )
    assert len(calls) == 1
    assert len(snap.selected_scope_history) == 2
    assert snap.membership_basis == "latest_snapshot_replay"


# J. stock facts 一次/stock（market + board + watch + selected 共用）
def test_stock_facts_reused_across_market_board_watch_selected(monkeypatch):
    end = date(2026, 9, 18)
    n = 250
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    b1 = _board(uuid4(), name="A")
    b2 = _board(uuid4(), name="B", scope_type="concept")
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float(i + 1) for i in range(n)]),
    }
    seen = set()
    orig = svc._compute_stock_daily_facts

    def _spy(df):
        seen.add(id(df))
        return orig(df)

    monkeypatch.setattr(svc, "_compute_stock_daily_facts", _spy)
    _run_history(
        monkeypatch, end_date=end, instrument_ids=[i1, i2],
        load_dates=dates, bars=bars, boards=[b1, b2],
        memberships={b1.id: [i1], b2.id: [i2]},
        watch_board_ids=[b1.id, b2.id], selected_scope_ids=[b1.id, b2.id],
    )
    assert len(seen) == 2


# E0. 窄 regression：唯一 date index 直接查找，exact-T 不破坏（无 T-1 仍 member_count==0）
def test_indexed_lookup_exact_date_no_ffill():
    t = date(2026, 9, 18)
    t_minus_1 = date(2026, 9, 17)
    t_minus_2 = date(2026, 9, 16)
    iid = uuid4()
    # 单只股票：有 T-2 与 T 的 bar，缺 T-1（模拟停牌/缺失日），index 唯一且不连续
    df = _mk([t_minus_2, t], [10.0, 12.0])
    facts = {iid: svc._compute_stock_daily_facts(df)}
    miss = svc._aggregate_breadth_for_date(t_minus_1, facts)
    hit = svc._aggregate_breadth_for_date(t, facts)
    assert miss.member_count == 0  # T-1 不存在 → 不 fallback、不 nearest、不 iloc last
    assert hit.member_count == 1   # T 存在 → 精确命中
    # 行为矩阵：T-2 同样精确命中，T-1 缺失不污染相邻日
    assert svc._aggregate_breadth_for_date(t_minus_2, facts).member_count == 1
