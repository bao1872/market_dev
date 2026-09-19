"""Market Dashboard Checkpoint B 单元测试（PURE_UNIT_TEST，无真实 DB）。

通过 monkeypatch 数据加载 helper 与 bars 批量读取，验证真实数据接入的合同：
- exact-T 硬合同（A）
- qfq 坐标等价（B）
- adj_factor 缺失不 fallback（C）
- 全市场候选不依赖 status=active（D）
- 板块复用同一份 bars，loader 只调用一次（E）
- 板块 type / hierarchy 保留（F）
- 测试/服务代码不引用 PIT 历史成员 API（G）
- 空/部分板块 ratio=None 而非 0（H）
"""
from __future__ import annotations

import asyncio
import inspect
import pathlib
from datetime import date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
import pytest

from app.repositories import bar_repository
from app.services import market_dashboard_service as svc


def _df(dates, closes, factors) -> pd.DataFrame:
    """构造 bars_daily 批量返回形态的 DataFrame（index=datetime，含 close/adj_factor）。"""
    return pd.DataFrame(
        {"close": closes, "adj_factor": factors},
        index=pd.to_datetime(dates),
    )


def _run_snapshot(monkeypatch, *, trade_date, instrument_ids, start_date, bars, boards, memberships, bars_loader=None):
    """用 monkeypatch 替换全部数据加载，运行单日横截面（无真实 DB）。"""

    async def _insts(_session):
        return list(instrument_ids)

    async def _start(_session, requested_trade_date):
        # 锁定 production wiring：snapshot 必须把目标 T 传给 recent-trade-date resolver
        assert requested_trade_date == trade_date
        return start_date

    async def _boards(_session):
        return list(boards)

    async def _members(_session, board_ids):
        return {b: memberships.get(b, []) for b in board_ids}

    async def _default_loader(_s, _ids, _sd, _ed):
        return bars

    monkeypatch.setattr(svc, "_query_market_instrument_ids", _insts)
    monkeypatch.setattr(svc, "_query_recent_start_date", _start)
    monkeypatch.setattr(svc, "_query_active_boards", _boards)
    monkeypatch.setattr(svc, "_query_board_memberships", _members)
    monkeypatch.setattr(bar_repository, "get_daily_bars_batch", bars_loader or _default_loader)
    return asyncio.run(svc.build_daily_dashboard_snapshot(SimpleNamespace(), trade_date))


# ---------------------------------------------------------------
# 纯 helper 测试
# ---------------------------------------------------------------

def test_adjusted_close_coordinate_basic() -> None:
    assert svc.adjusted_close_coordinate(20.0, 0.5) == 10.0
    assert svc.adjusted_close_coordinate(10.0, 1.0) == 10.0


def test_adjusted_close_coordinate_factor_missing_is_none():  # 合同 C
    # 严禁 adj_factor 缺失时 fallback 1.0
    assert svc.adjusted_close_coordinate(10.0, None) is None
    assert svc.adjusted_close_coordinate(None, 1.0) is None
    assert svc.adjusted_close_coordinate(10.0, 0.0) is None  # 非正
    assert svc.adjusted_close_coordinate(10.0, float("nan")) is None
    assert svc.adjusted_close_coordinate(10.0, float("inf")) is None


def test_qfq_coordinate_equivalence():  # 合同 B
    """raw 20->10, factor 0.5->1.0 → 坐标 10->10 → 收益为 0 而非 -50%。"""
    t = date(2026, 9, 18)
    df = _df(["2026-09-17", "2026-09-18"], [20.0, 10.0], [0.5, 1.0])
    mc = svc.build_member_closes(t, df)
    assert mc is not None
    assert mc.closes == [10.0, 10.0]
    from app.domain.market_dashboard.breadth import compute_breadth

    result = compute_breadth([mc])
    assert result.equal_weight_return == pytest.approx(0.0)


def test_build_member_closes_exact_t_enforced():  # 合同 A（纯）
    t = date(2026, 9, 18)
    dates = [datetime.fromordinal(t.toordinal() - (120 - i)) for i in range(120)]
    df = _df(dates, [float(i + 1) for i in range(120)], [1.0] * 120)
    assert svc.build_member_closes(t, df) is None  # 最后一根在 T-1
    dates[-1] = datetime(t.year, t.month, t.day)
    df2 = _df(dates, [float(i + 1) for i in range(120)], [1.0] * 120)
    assert svc.build_member_closes(t, df2) is not None


# ---------------------------------------------------------------
# 市场候选集合（合同 D）
# ---------------------------------------------------------------

def test_market_universe_no_status_filter() -> None:
    """候选查询应只筛 market，不引用 status=active。"""

    class _Capture:
        def __init__(self) -> None:
            self.stmt = None

        async def execute(self, stmt):
            self.stmt = stmt
            return self

        def all(self):
            return []

    fake = _Capture()
    asyncio.run(svc._query_market_instrument_ids(fake))  # type: ignore[arg-type]
    sql = str(fake.stmt).lower()
    assert "market" in sql
    assert "status" not in sql


# ---------------------------------------------------------------
# 端到端横截面
# ---------------------------------------------------------------

def test_exact_t_excludes_stale_bar_from_market(monkeypatch):  # 合同 A
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime.fromordinal(t.toordinal() - (20 - i)) for i in range(20)]  # 最后一根 T-1
    bars = {iid: _df(dts, [float(i + 1) for i in range(20)], [1.0] * 20)}
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid],
        start_date=dts[0].date(), bars=bars, boards=[], memberships={},
    )
    assert snap.market.breadth.member_count == 0
    assert snap.market.breadth.windows[5].valid_count == 0
    assert snap.market.breadth.equal_weight_return is None


def test_daily_snapshot_market_and_board_basic(monkeypatch):
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime(t.year, t.month, t.day - 4 + i) for i in range(5)]  # 5 根递增 → MA5 above
    bars = {iid: _df(dts, [float(i + 1) for i in range(5)], [1.0] * 5)}
    board = SimpleNamespace(id=uuid4(), name="银行", type="industry", hierarchyLevel="L1")
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=[board], memberships={board.id: [iid]},
    )
    assert snap.membership_basis == "latest_snapshot_replay"
    assert snap.market.scope_type == "market"
    assert snap.market.breadth.windows[5].valid_count == 1
    assert snap.market.breadth.windows[5].above_count == 1
    assert len(snap.scopes) == 1
    assert snap.scopes[0].scope_type == "industry"
    assert snap.scopes[0].hierarchy_level == "L1"
    assert snap.scopes[0].breadth.windows[5].valid_count == 1


def test_factor_missing_excludes_day_from_denominator(monkeypatch):  # 合同 C（端到端）
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime(t.year, t.month, t.day - 4 + i) for i in range(5)]
    # 最后一根 adj_factor 缺失 → 该日坐标不可用 → MA5 尾部无效 → 分母 0
    factors = [1.0, 1.0, 1.0, 1.0, None]
    bars = {iid: _df(dts, [10.0, 10.0, 10.0, 10.0, 10.0], factors)}
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=[], memberships={},
    )
    assert snap.market.breadth.windows[5].valid_count == 0
    assert snap.market.breadth.windows[5].ratio is None


def test_board_reuse_single_bars_loader_call(monkeypatch):  # 合同 E
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime(t.year, t.month, t.day - 4 + i) for i in range(5)]
    bars = {iid: _df(dts, [float(i + 1) for i in range(5)], [1.0] * 5)}
    b1 = SimpleNamespace(id=uuid4(), name="银行", type="industry", hierarchyLevel="L1")
    b2 = SimpleNamespace(id=uuid4(), name="AI", type="concept", hierarchyLevel="L1")
    calls = []

    async def _loader(_s, _ids, _sd, _ed):
        calls.append(1)
        return bars

    monkeypatch.setattr(bar_repository, "get_daily_bars_batch", _loader)
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=[b1, b2], memberships={b1.id: [iid], b2.id: [iid]},
        bars_loader=_loader,
    )
    assert len(calls) == 1, "两个 board 共用股票时 bars loader 只应调用一次"
    assert len(snap.scopes) == 2


def test_hierarchy_preserved_for_l1_l2_l3_concept(monkeypatch):  # 合同 F
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime(t.year, t.month, t.day - 4 + i) for i in range(5)]
    bars = {iid: _df(dts, [float(i + 1) for i in range(5)], [1.0] * 5)}
    boards = [
        SimpleNamespace(id=uuid4(), name="L1", type="industry", hierarchyLevel="L1"),
        SimpleNamespace(id=uuid4(), name="L2", type="industry", hierarchyLevel="L2"),
        SimpleNamespace(id=uuid4(), name="L3", type="industry", hierarchyLevel="L3"),
        SimpleNamespace(id=uuid4(), name="C1", type="concept", hierarchyLevel="L1"),
    ]
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=boards, memberships={b.id: [iid] for b in boards},
    )
    meta = {(s.scope_type, s.hierarchy_level) for s in snap.scopes}
    assert ("industry", "L1") in meta
    assert ("industry", "L2") in meta
    assert ("industry", "L3") in meta
    assert ("concept", "L1") in meta


def test_board_with_no_exact_t_member_has_none_ratio(monkeypatch):  # 合同 H
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime.fromordinal(t.toordinal() - 5 + i) for i in range(5)]  # 最后一根 T-1
    bars = {iid: _df(dts, [float(i + 1) for i in range(5)], [1.0] * 5)}
    board = SimpleNamespace(id=uuid4(), name="空板", type="industry", hierarchyLevel="L1")
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=[board], memberships={board.id: [iid]},
    )
    assert len(snap.scopes) == 1
    sc = snap.scopes[0]
    for k in (5, 10, 20, 50, 120):
        assert sc.breadth.windows[k].valid_count == 0
        assert sc.breadth.windows[k].ratio is None  # 不是 0


def test_no_pit_membership_api_referenced():  # 合同 G
    """测试代码与服务代码均不得 import/call PIT 历史成员 API。

    注意：测试自身用字符串字面量提及这些名字做元检查，故只校验
    不出现 import 形式与函数调用形式的真实引用（避免影响生产/测试命名）。
    """
    svc_src = pathlib.Path(inspect.getfile(svc)).read_text()
    test_src = pathlib.Path(__file__).read_text()
    pit_import = "import" + " BoardMembershipHistory"
    pit_call = "resolve_board_membership_at" + "("
    assert pit_import not in svc_src
    assert pit_import not in test_src
    assert pit_call not in svc_src
    assert pit_call not in test_src


def test_snapshot_passes_trade_date_to_history_resolver(monkeypatch):  # FIX1 窄回归
    """build_daily_dashboard_snapshot(session, T) 必须把 T 传给 recent-start-date resolver。

    锁定 production wiring：mock 的 _start 已断言第二参数 == T；
    若 production 调用再次漏传 trade_date，asyncio.run 会因
    TypeError(missing argument) 直接失败，测试不再绿。
    """
    t = date(2026, 9, 18)
    iid = uuid4()
    dts = [datetime(t.year, t.month, t.day - 4 + i) for i in range(5)]
    bars = {iid: _df(dts, [float(i + 1) for i in range(5)], [1.0] * 5)}
    snap = _run_snapshot(
        monkeypatch, trade_date=t, instrument_ids=[iid], start_date=dts[0].date(),
        bars=bars, boards=[], memberships={},
    )
    assert snap.trade_date == t
