"""F1A projection compute — parity / completeness / chunk / call-count / schema-shape / benchmark。

纯单元（PURE_UNIT_TEST，不连 DB）：全部通过 monkeypatch 注入合成数据。
覆盖合同：
A. market records 与 _aggregate_breadth_for_date oracle 逐字段一致（多股票/缺 bar/invalid/短上市）
B. scope records 与 _aggregate_breadth_for_date oracle 逐字段一致（industry/concept/overlap/empty/exact-T/invalid）
C. full matrix completeness：boards × dates 全部有行（空 board 也补显式零），key 唯一
D. chunk 结构：每次 dates <= SCOPE_PROJECTION_DATE_CHUNK，无 all_boards×250 单次调用，日期不重不漏
E. query/compute 调用次数：bars/boards/memberships/facts/build_membership 各 1
F. record keys 精确匹配 095 两张表写入列（无 ratio/index/delta/updated_at）
G. 合成规模 benchmark（1000×369 / 100 boards / overlap，250 日分块）
"""

from __future__ import annotations

import asyncio
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
from app.services import market_dashboard_projection_service as proj
from tests.test_market_dashboard_vectorize_core import _long_from_bars, _mk


# ---------------------------------------------------------------- helpers
def _board(bid, *, name="", scope_type="industry", level="L1", membership_version="mv-1"):
    return SimpleNamespace(
        id=bid,
        name=name,
        type=scope_type,
        hierarchyLevel=level,
        membershipVersion=membership_version,
    )


def _legacy_facts(bars):
    return {iid: svc._compute_stock_daily_facts(df) for iid, df in bars.items()}


def _context(*, bars, boards, memberships, display_dates):
    return proj.ProjectionContext(
        projection_trade_date=display_dates[-1],
        display_dates=list(display_dates),
        stock_facts=svc._compute_stock_facts_long(_long_from_bars(bars)),
        membership_long=svc._build_membership_long(memberships),
        board_ids=[b.id for b in boards],
        membership_versions={b.id: b.membershipVersion for b in boards},
    )


def _flat_scope_records(context, *, chunk_size=proj.SCOPE_PROJECTION_DATE_CHUNK):
    return [
        r for chunk in proj.iter_scope_record_chunks(context, chunk_size=chunk_size) for r in chunk
    ]


def _assert_record_matches(record, oracle):
    """projection record（dict）必须与 oracle BreadthResult 逐计数字段一致。"""
    assert record["member_count"] == oracle.member_count
    assert record["valid_return_count"] == oracle.valid_return_count
    if oracle.equal_weight_return is None:
        assert record["equal_weight_return"] is None
    else:
        assert record["equal_weight_return"] == pytest.approx(oracle.equal_weight_return)
    for k in WINDOWS:
        w = oracle.windows[k]
        assert record[f"ma{k}_valid_count"] == w.valid_count
        assert record[f"ma{k}_above_count"] == w.above_count


# ---------------------------------------------------------------
# A. market records parity
# ---------------------------------------------------------------
def test_market_records_parity_with_oracle():
    end = date(2026, 9, 18)
    n = 60
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i_full, i_missing, i_badclose, i_badfactor, i_short = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    bars = {
        i_full: _mk(dates, [float(i + 1) for i in range(n)]),
        i_missing: _mk(dates[:-3], [float(i + 1) for i in range(n - 3)]),  # 缺尾部 bar
        i_badclose: _mk(dates, [10.0, None] + [float(i + 3) for i in range(2, n)]),
        i_badfactor: _mk(dates, [float(i + 1) for i in range(n)], [1.0, None] + [1.0] * (n - 2)),
        i_short: _mk(dates[-5:], [float(i + 1) for i in range(5)]),  # 上市晚，窗口不足
    }
    ctx = _context(bars=bars, boards=[], memberships={}, display_dates=dates)
    records = proj.build_market_records(ctx)
    assert len(records) == len(dates)

    legacy = _legacy_facts(bars)
    for rec, d in zip(records, dates, strict=False):
        assert rec["trade_date"] == d
        _assert_record_matches(rec, svc._aggregate_breadth_for_date(d, legacy))


def test_market_records_full_dates_when_facts_empty():
    """P1-1 回归：stock_facts 为空时，market records 仍须按 display_dates 输出显式零。"""
    dates = [date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18)]
    ctx = proj.ProjectionContext(
        projection_trade_date=dates[-1],
        display_dates=dates,
        stock_facts=svc._compute_stock_facts_long(None),  # 空 facts
        membership_long=svc._build_membership_long({}),
        board_ids=[],
        membership_versions={},
    )
    records = proj.build_market_records(ctx)
    assert len(records) == 3
    for rec, d in zip(records, dates, strict=False):
        assert rec["trade_date"] == d
        assert rec["member_count"] == 0
        assert rec["valid_return_count"] == 0
        assert rec["equal_weight_return"] is None
        for k in WINDOWS:
            assert rec[f"ma{k}_valid_count"] == 0
            assert rec[f"ma{k}_above_count"] == 0


# ---------------------------------------------------------------
# B. scope records parity
# ---------------------------------------------------------------
def test_scope_records_parity_with_oracle():
    end = date(2026, 9, 18)
    n = 50
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2, i3, i4 = uuid4(), uuid4(), uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float((i % 7) + 1) for i in range(n)]),
        i3: _mk(dates[:-2], [float(i + 1) for i in range(n - 2)]),  # exact-T 尾部缺 bar
        i4: _mk(dates, [10.0, None, 12.0] + [float(i + 3) for i in range(3, n)]),  # invalid coord
    }
    industry = _board(
        uuid4(), name="电子", scope_type="industry", level="L1", membership_version="mv-ind"
    )
    concept = _board(
        uuid4(), name="AI", scope_type="concept", level="L1", membership_version="mv-con"
    )
    empty = _board(uuid4(), name="空板块", membership_version="mv-empty")
    memberships = {
        industry.id: [i1, i2, i4],
        concept.id: [i2, i3],  # 与 industry 在 i2 上 overlap
        empty.id: [],  # 空 membership
    }
    boards = [industry, concept, empty]
    ctx = _context(bars=bars, boards=boards, memberships=memberships, display_dates=dates)
    legacy = _legacy_facts(bars)

    records = _flat_scope_records(ctx)
    by_key = {(r["board_id"], r["trade_date"]): r for r in records}
    for b in boards:
        for d in dates:
            rec = by_key[(b.id, d)]
            assert rec["membership_version"] == b.membershipVersion
            _assert_record_matches(
                rec, svc._aggregate_breadth_for_date(d, legacy, memberships[b.id])
            )


# ---------------------------------------------------------------
# C. full matrix completeness
# ---------------------------------------------------------------
def test_scope_full_matrix_completeness():
    end = date(2026, 9, 18)
    n_dates = 20
    dates = [end - timedelta(days=n_dates - 1 - i) for i in range(n_dates)]
    i1 = uuid4()
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n_dates)])}
    b1 = _board(uuid4(), name="A")
    b2 = _board(uuid4(), name="B", scope_type="concept")
    b3 = _board(uuid4(), name="C-empty")
    boards = [b1, b2, b3]
    memberships = {b1.id: [i1], b2.id: [i1], b3.id: []}
    ctx = _context(bars=bars, boards=boards, memberships=memberships, display_dates=dates)

    records = _flat_scope_records(ctx)
    keys = [(r["board_id"], r["trade_date"]) for r in records]
    assert len(keys) == len(set(keys)) == len(boards) * n_dates == 60

    empty_rows = [r for r in records if r["board_id"] == b3.id]
    assert len(empty_rows) == n_dates
    for r in empty_rows:
        assert r["member_count"] == 0
        assert r["valid_return_count"] == 0
        assert r["equal_weight_return"] is None
        for k in WINDOWS:
            assert r[f"ma{k}_valid_count"] == 0
            assert r[f"ma{k}_above_count"] == 0


# ---------------------------------------------------------------
# D. chunk structural
# ---------------------------------------------------------------
def test_scope_chunking_structural(monkeypatch):
    calls = []
    orig = svc._aggregate_scope_breadth_long

    def _spy(stock_facts, membership_long, board_ids, dates):
        calls.append((len(list(board_ids)), list(dates)))
        return orig(stock_facts, membership_long, board_ids, dates)

    monkeypatch.setattr(svc, "_aggregate_scope_breadth_long", _spy)

    end = date(2026, 9, 18)
    n_dates = 250
    dates = [end - timedelta(days=n_dates - 1 - i) for i in range(n_dates)]
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n_dates)]),
        i2: _mk(dates, [float((i % 4) + 1) for i in range(n_dates)]),
    }
    boards = [_board(uuid4(), name=f"B{k}") for k in range(5)]
    memberships = {b.id: [i1, i2] for b in boards}
    ctx = _context(bars=bars, boards=boards, memberships=memberships, display_dates=dates)

    records = _flat_scope_records(ctx)
    assert records  # 消费完整 chunk generator
    assert calls, "必须通过 _aggregate_scope_breadth_long 聚合"
    # 每次不超过 chunk 上限，且绝不存在 all_boards × 250 的单次调用
    assert all(
        date_count <= proj.SCOPE_PROJECTION_DATE_CHUNK
        for _bc, date_count in [(bc, len(ds)) for bc, ds in calls]
    )
    assert all(len(ds) < n_dates for _bc, ds in calls)
    assert all(bc == len(boards) for bc, _ds in calls)
    # 日期不重不漏
    flattened = [d for _bc, ds in calls for d in ds]
    assert flattened == dates
    # chunk 数 == ceil(250 / 10)
    assert (
        len(calls)
        == (n_dates + proj.SCOPE_PROJECTION_DATE_CHUNK - 1) // proj.SCOPE_PROJECTION_DATE_CHUNK
    )
    # 完整 record 数
    assert len(records) == len(boards) * n_dates


# ---------------------------------------------------------------
# D2. chunk_size guard（内存合同 fail closed）
# ---------------------------------------------------------------
@pytest.mark.parametrize("bad", [0, -1, 11, 250])
def test_scope_chunk_size_rejected(bad):
    dates = [date(2026, 9, 18)]
    ctx = proj.ProjectionContext(
        projection_trade_date=dates[-1],
        display_dates=dates,
        stock_facts=svc._compute_stock_facts_long(None),
        membership_long=svc._build_membership_long({}),
        board_ids=[],
        membership_versions={},
    )
    # eager 校验：调用即失败（不依赖迭代）
    with pytest.raises(ValueError):
        proj.iter_scope_record_chunks(ctx, chunk_size=bad)


@pytest.mark.parametrize("ok", [1, 5, proj.SCOPE_PROJECTION_DATE_CHUNK])
def test_scope_chunk_size_accepted(ok):
    end = date(2026, 9, 18)
    n = 12
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1 = uuid4()
    bars = {i1: _mk(dates, [float(i + 1) for i in range(n)])}
    b = _board(uuid4(), name="A")
    ctx = _context(bars=bars, boards=[b], memberships={b.id: [i1]}, display_dates=dates)
    chunks = list(proj.iter_scope_record_chunks(ctx, chunk_size=ok))
    assert chunks
    assert sum(len(c) for c in chunks) == n  # 1 board × n dates
    assert all(len(c) <= ok for c in chunks)


# ---------------------------------------------------------------
# E. query / compute call counts
# ---------------------------------------------------------------
def test_query_and_compute_call_counts(monkeypatch):
    end = date(2026, 9, 18)
    n = 30
    dates = [end - timedelta(days=n - 1 - i) for i in range(n)]
    i1, i2 = uuid4(), uuid4()
    bars = {
        i1: _mk(dates, [float(i + 1) for i in range(n)]),
        i2: _mk(dates, [float((i % 3) + 1) for i in range(n)]),
    }
    b1 = _board(uuid4(), name="A")
    b2 = _board(uuid4(), name="B", scope_type="concept")
    boards = [b1, b2]
    memberships = {b1.id: [i1], b2.id: [i2]}

    counts = {
        "instruments": 0,
        "trade_dates": 0,
        "reader": 0,
        "facts": 0,
        "boards": 0,
        "members": 0,
        "memlong": 0,
    }

    async def _insts(_s):
        counts["instruments"] += 1
        return [i1, i2]

    async def _dates(_s, _end, _count):
        counts["trade_dates"] += 1
        return list(dates)

    async def _boards(_s):
        counts["boards"] += 1
        return list(boards)

    async def _members(_s, bids):
        counts["members"] += 1
        return {b: memberships.get(b, []) for b in bids}

    async def _loader(_s, _ids, _sd, _ed):
        counts["reader"] += 1
        return _long_from_bars(bars)

    orig_facts = svc._compute_stock_facts_long

    def _spy_facts(df):
        counts["facts"] += 1
        return orig_facts(df)

    orig_memlong = svc._build_membership_long

    def _spy_memlong(m):
        counts["memlong"] += 1
        return orig_memlong(m)

    monkeypatch.setattr(svc, "_query_market_instrument_ids", _insts)
    monkeypatch.setattr(svc, "_query_recent_trade_dates", _dates)
    monkeypatch.setattr(svc, "_query_active_boards", _boards)
    monkeypatch.setattr(svc, "_query_board_memberships", _members)
    monkeypatch.setattr(bar_repository, "get_dashboard_daily_facts_source", _loader)
    monkeypatch.setattr(svc, "_compute_stock_facts_long", _spy_facts)
    monkeypatch.setattr(svc, "_build_membership_long", _spy_memlong)

    ctx = asyncio.run(proj.prepare_projection_context(SimpleNamespace(), end))
    market_records = proj.build_market_records(ctx)
    scope_records = _flat_scope_records(ctx)

    assert len(market_records) == len(dates)
    assert len(scope_records) == len(boards) * len(dates)
    assert counts == {
        "instruments": 1,
        "trade_dates": 1,
        "reader": 1,
        "facts": 1,
        "boards": 1,
        "members": 1,
        "memlong": 1,
    }


# ---------------------------------------------------------------
# E2. no trading dates fail closed
# ---------------------------------------------------------------
def test_prepare_no_trading_dates_fails_closed(monkeypatch):
    counts = {"reader": 0, "boards": 0, "members": 0}

    async def _insts(_s):
        return [uuid4()]

    async def _dates(_s, _end, _count):
        return []  # 无任何真实交易日

    async def _boards(_s):
        counts["boards"] += 1
        return []

    async def _members(_s, bids):
        counts["members"] += 1
        return {}

    async def _loader(_s, _ids, _sd, _ed):
        counts["reader"] += 1
        return None

    monkeypatch.setattr(svc, "_query_market_instrument_ids", _insts)
    monkeypatch.setattr(svc, "_query_recent_trade_dates", _dates)
    monkeypatch.setattr(svc, "_query_active_boards", _boards)
    monkeypatch.setattr(svc, "_query_board_memberships", _members)
    monkeypatch.setattr(bar_repository, "get_dashboard_daily_facts_source", _loader)

    with pytest.raises(ValueError):
        asyncio.run(proj.prepare_projection_context(SimpleNamespace(), date(2026, 9, 18)))
    # fail closed：不得继续 bars / boards / memberships 查询
    assert counts == {"reader": 0, "boards": 0, "members": 0}


# ---------------------------------------------------------------
# F. schema-shape contract
# ---------------------------------------------------------------
def test_record_keys_match_095_write_columns():
    from app.models.market_dashboard import (
        MarketDashboardMarketDaily,
        MarketDashboardScopeDaily,
    )

    end = date(2026, 9, 18)
    dates = [end - timedelta(days=4 - i) for i in range(5)]
    i1 = uuid4()
    bars = {i1: _mk(dates, [float(i + 1) for i in range(5)])}
    b = _board(uuid4(), name="A")
    ctx = _context(bars=bars, boards=[b], memberships={b.id: [i1]}, display_dates=dates)

    market_records = proj.build_market_records(ctx)
    scope_records = _flat_scope_records(ctx)
    assert market_records and scope_records

    def _write_cols(model):
        return {c.name for c in model.__table__.columns if c.name != "updated_at"}

    assert set(market_records[0].keys()) == _write_cols(MarketDashboardMarketDaily)
    assert set(scope_records[0].keys()) == _write_cols(MarketDashboardScopeDaily)

    forbidden = {
        "ratio",
        "normalized_index",
        "scope_name",
        "scope_type",
        "hierarchy_level",
        "updated_at",
    }
    for rec in (market_records[0], scope_records[0]):
        assert not (set(rec.keys()) & forbidden)
        assert not any(key.endswith("_delta") for key in rec)


# ---------------------------------------------------------------
# G. synthetic projection benchmark
# ---------------------------------------------------------------
def test_synthetic_projection_benchmark():
    n_inst = 1000
    n_days = 369
    end = date(2026, 9, 18)
    dates = [end - timedelta(days=n_days - 1 - i) for i in range(n_days)]
    rng = np.random.default_rng(7)
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

    t0 = time.perf_counter()
    facts = svc._compute_stock_facts_long(raw)
    facts_sec = time.perf_counter() - t0

    n_boards = 100
    board_ids = [uuid4() for _ in range(n_boards)]
    all_iids = raw["instrument_id"].unique().tolist()
    membership_rows = []
    for bid in board_ids:
        for m in rng.choice(all_iids, size=100, replace=False).tolist():
            membership_rows.append((bid, m))
    membership_long = pd.DataFrame(membership_rows, columns=["board_id", "instrument_id"])

    display_dates = dates[-250:]
    ctx = proj.ProjectionContext(
        projection_trade_date=display_dates[-1],
        display_dates=display_dates,
        stock_facts=facts,
        membership_long=membership_long,
        board_ids=board_ids,
        membership_versions=dict.fromkeys(board_ids, "mv-bench"),
    )

    t0 = time.perf_counter()
    market_records = proj.build_market_records(ctx)
    market_sec = time.perf_counter() - t0

    t0 = time.perf_counter()
    scope_record_count = 0
    scope_chunk_count = 0
    max_scope_chunk_dates = 0
    for chunk in proj.iter_scope_record_chunks(ctx):
        scope_chunk_count += 1
        scope_record_count += len(chunk)
        max_scope_chunk_dates = max(max_scope_chunk_dates, len(chunk) // n_boards)
    scope_sec = time.perf_counter() - t0

    print(
        f"[F1A benchmark] inst={n_inst} days={n_days} boards={n_boards} "
        f"facts_seconds={facts_sec:.3f} market_seconds={market_sec:.3f} "
        f"scope_250d_seconds={scope_sec:.3f} scope_chunk_count={scope_chunk_count} "
        f"max_scope_chunk_dates={max_scope_chunk_dates} "
        f"market_record_count={len(market_records)} scope_record_count={scope_record_count}"
    )

    assert len(market_records) == len(display_dates)
    assert scope_record_count == n_boards * len(display_dates)
    assert (
        scope_chunk_count
        == (len(display_dates) + proj.SCOPE_PROJECTION_DATE_CHUNK - 1)
        // proj.SCOPE_PROJECTION_DATE_CHUNK
    )
    assert max_scope_chunk_dates <= proj.SCOPE_PROJECTION_DATE_CHUNK
    # 宽松 sanity（非 SLA）；超过则 STOP，不自行引入 cache/multiprocessing/Polars/Dask/SQL rewrite
    assert scope_sec < 30.0


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v", "--tb=short"])
